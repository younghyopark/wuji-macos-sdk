"""Python API and ownership rules corresponding to the pinned upstream stubs.

All hardware work is delegated to the native backend. Tests inject their own
backend explicitly; there is no simulated-device fallback in this module.
"""
from __future__ import annotations

import asyncio
from functools import wraps
import math
import operator
import threading
import weakref
from typing import Generic, TypeVar

from . import _types as types
from ._backend import StreamLaggedError

T = TypeVar("T")


def _cache_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._devices_lock:
            return method(self, *args, **kwargs)
    return wrapped


def _integer(value, low, high, name):
    value = operator.index(value)
    if value < low or value > high:
        raise OverflowError(f"{name} must be in [{low}, {high}]")
    return value


def _finite(value, name, *, nonnegative=False):
    value = float(value)
    if not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return value


def _mask(joints):
    if joints is None:
        return None
    if len(joints) != 20:
        raise ValueError("Joint mask must contain exactly 20 entries")
    return [int(_integer(v, 0, 255, "joint mask entry") != 0) for v in joints]


def _convert(dtype, value):
    if value is None or not dtype:
        return value
    if isinstance(dtype, str):
        dtype = dtype.removeprefix("wuji.")
        if dtype not in types.__all__:
            return value  # Schema-validated primitive/array, no modeled class.
    if dtype == "JointDiagnosticsFrame":
        value = {**value, "joints": [
            {**joint, "status_word": _decode_status(joint["status_word"])}
            for joint in value["joints"]
        ]}
    elif dtype == "StatusWord":
        value = _decode_status(value)
    return types._from_native(dtype, value)


def _decode_status(value):
    if not isinstance(value, int):
        return value
    value = _integer(value, 0, 0xFFFFFFFF, "status word")
    return {"ext_state": value & 3,
            "position_limit_active": bool(value & (1 << 4)),
            "velocity_limit_active": bool(value & (1 << 5)),
            "current_limit_active": bool(value & (1 << 6))}


class Subscription(Generic[T]):
    def __init__(self, backend, handle, dtype, stream_id, owner=None):
        self._backend, self._handle, self._owner = backend, handle, owner
        self.dtype, self.stream_id = dtype, stream_id
        self._closed = False
        self._lock = threading.RLock()

    def _call(self, operation, **args):
        with self._lock:
            if self._closed:
                raise types.WujiException("StreamClosed: subscription is closed")
            return self._backend.call("subscription", self._handle, operation, **args)

    def recv(self):
        return _convert(self.dtype, self._call("recv"))

    @property
    def lagged_frames(self):
        """Frames skipped because this receiver fell behind (macOS port extension).

        recv() skips a backlog and continues from the oldest retained frame,
        as the upstream examples expect; this counts what was skipped.
        """
        return self._call("lagged_frames")

    async def recv_async(self):
        while True:
            frame = self.recv()
            if frame is not None:
                return frame
            await asyncio.sleep(0.001)

    def set_rate(self, frequency_hz):
        return self._call("set_rate", frequency_hz=_integer(frequency_hz, 0, 65535, "frequency_hz"))

    def close(self):
        with self._lock:
            if not self._closed:
                try:
                    self._backend.call("subscription", self._handle, "close")
                finally:
                    self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class CallbackSubscription:
    def __init__(self, subscription, callback, on_error=None):
        if not callable(callback):
            raise TypeError("callback must be callable")
        if on_error is not None and not callable(on_error):
            raise TypeError("on_error must be callable or None")
        self._subscription = subscription
        self._callback, self._on_error = callback, on_error
        self.dtype = subscription.dtype
        self.received_count = self.error_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run,
            args=(weakref.ref(self), subscription, self._stop), name="wuji-sdk-callback", daemon=True)
        self._thread.start()

    @property
    def is_active(self):
        return not self._stop.is_set() and self._thread.is_alive()

    def _error(self, error):
        self.error_count += 1
        if self._on_error:
            try:
                self._on_error(str(error))
            except Exception:
                self.error_count += 1

    @staticmethod
    def _run(reference, subscription, stop):
        try:
            while not stop.is_set():
                owner = reference()
                if owner is None:
                    break
                try:
                    frame = subscription.recv()
                except StreamLaggedError as error:
                    # The receiver already skipped to the oldest retained
                    # frame; report the gap and keep delivering.
                    if not stop.is_set():
                        owner._error(error)
                    del owner
                    continue
                except Exception as error:
                    if not stop.is_set():
                        owner._error(error)
                    break
                if frame is None:
                    # The worker must not keep the public handle alive while
                    # idle; releasing the handle also releases its stream.
                    del owner
                    stop.wait(0.001)
                    continue
                owner.received_count += 1
                try:
                    owner._callback(frame)
                except Exception as error:
                    owner._error(error)
                del owner
        finally:
            stop.set()
            try:
                subscription.close()
            except Exception as error:
                owner = reference()
                if owner is not None:
                    owner._error(error)

    def close(self):
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join()
        self._subscription.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Publisher:
    def __init__(self, backend, handle, dtype, owner=None):
        self._backend, self._handle, self._owner = backend, handle, owner
        self.dtype = dtype
        self._closed = False
        self._lock = threading.RLock()

    def send(self, data):
        with self._lock:
            if self._closed:
                raise types.WujiException("StreamClosed: publisher is closed")
            return self._backend.call("publisher", self._handle, "send", data=types._to_native(data))

    async def send_async(self, data):
        await asyncio.to_thread(self.send, data)

    def close(self):
        with self._lock:
            if not self._closed:
                try:
                    self._backend.call("publisher", self._handle, "close")
                finally:
                    self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class JointCommandPublisher(Publisher):
    def send(self, joints):
        if len(joints) != 20:
            raise ValueError("Joint commands must contain exactly 20 JointCommand entries")
        for joint in joints:
            if not isinstance(joint, types.JointCommand):
                raise TypeError("Every entry must be a JointCommand")
            for name in ("position", "velocity", "effort"):
                _finite(getattr(joint, name), name)
        super().send(joints)


class _SemanticResource:
    def __init__(self, owner, name, *, dtype=None, index=None):
        self._owner, self._name, self._dtype, self._index = owner, name, dtype, index

    def _call(self, action, **arguments):
        return self._owner._call("resource", resource=self._name, action=action,
                                 index=self._index, **arguments)

    def __repr__(self):
        return f"{type(self).__name__}({self._owner.device_name!r}, {self._name!r})"


class _Get:
    def get(self):
        return _convert(self._dtype, self._call("get"))


class _Set:
    def set(self, value):
        self._call("set", value=types._to_native(value))


class _Subscribe:
    def subscribe(self):
        result = self._call("subscribe")
        return Subscription(self._owner._backend, result["handle"], result["dtype"],
                            result["stream_id"], self._owner)

    def subscribe_with_callback(self, callback, on_error=None):
        return CallbackSubscription(self.subscribe(), callback, on_error)


def _resource_class(name, operations):
    bases = tuple({"get": _Get, "set": _Set, "sub": _Subscribe}[op] for op in operations)
    cls = type(name, bases + (_SemanticResource,), {"__module__": __name__})
    globals()[name] = cls
    return cls


class JointCommandResource(_SemanticResource):
    def publish(self):
        result = self._call("publish")
        return JointCommandPublisher(self._owner._backend, result["handle"], result["dtype"], self._owner)


class DeviceEffortLimitResource(_SemanticResource):
    def set(self, value):
        if isinstance(value, (list, tuple)):
            if len(value) != 20:
                raise ValueError("Per-joint effort limits require exactly 20 values")
            value = [_finite(v, "effort limit", nonnegative=True) for v in value]
        else:
            value = _finite(value, "effort limit", nonnegative=True)
        self._call("set", value=value)

    def get(self):
        return self._call("get")


class DeviceMitParamsResource(_SemanticResource):
    @staticmethod
    def _one(value):
        if isinstance(value, types.MitParam):
            value = (value.kp, value.kd)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise TypeError("MIT parameters require MitParam or a (kp, kd) pair")
        return {"kp": _finite(value[0], "kp", nonnegative=True),
                "kd": _finite(value[1], "kd", nonnegative=True)}

    def set(self, value):
        if isinstance(value, list) and len(value) == 20:
            value = [self._one(v) for v in value]
        else:
            value = self._one(value)
        self._call("set", value=value)

    def get(self):
        return [None if v is None else _convert("MitParam", v) for v in self._call("get")]


class JointEffortLimitResource(_Get, _SemanticResource):
    def set(self, limit):
        self._call("set", value=_finite(limit, "effort limit", nonnegative=True))


_resource_class("JointErrorCodeResource", ("get",))
_resource_class("JointStatusWordResource", ("get",))
_resource_class("OnlineJointsCountResource", ("get",))
_resource_class("HandednessResource", ("get",))


def _feed_forward_mode(mode):
    return int(types.FeedForwardMode(_integer(mode, 0, 3, "feed-forward mode")))


class JointConstantResource:
    """Single-joint constant feed-forward mode (hand.joint(k).constant())."""
    def __init__(self, joint):
        self._joint = joint

    def set(self, mode):
        self._joint._action("set_constant", _feed_forward_mode(mode))


class JointPositionTableResource:
    """Single-joint position-indexed lookup-table feed-forward switch."""
    def __init__(self, joint):
        self._joint = joint

    def set(self, on):
        self._joint._action("set_position_table", bool(on))


class JointHandle:
    def __init__(self, hand, index):
        self._hand, self._index = hand, _integer(index, 0, 19, "joint index")

    @property
    def index(self):
        return self._index

    @property
    def label(self):
        return f"{('thumb', 'index', 'middle', 'ring', 'pinky')[self.index // 4]}_S{self.index % 4 + 1}"

    def effort_limit(self):
        return JointEffortLimitResource(self._hand, "joint_effort_limit", index=self.index)

    def error_code(self):
        return JointErrorCodeResource(self._hand, "joint_error_code", index=self.index)

    def status_word(self):
        return JointStatusWordResource(self._hand, "joint_status_word", dtype="StatusWord", index=self.index)

    def _action(self, operation, *args):
        mask = [0] * 20
        mask[self.index] = 1
        getattr(self._hand, operation)(*args, joints=mask)

    def enable(self): self._action("enable")
    def disable(self): self._action("disable")
    def clear_fault(self): self._action("clear_fault")
    def set_origin(self): self._action("set_origin")
    def clear_origin(self): self._action("clear_origin")
    def constant(self): return JointConstantResource(self)
    def position_table(self): return JointPositionTableResource(self)


class FingerHandle:
    def __init__(self, hand, index):
        self._hand, self._index = hand, index

    def j0(self): return self._hand.joint(self._index * 4)
    def j1(self): return self._hand.joint(self._index * 4 + 1)
    def j2(self): return self._hand.joint(self._index * 4 + 2)
    def j3(self): return self._hand.joint(self._index * 4 + 3)
    def joints(self): return [self._hand.joint(self._index * 4 + j) for j in range(4)]


class WujiDevice:
    def __init__(self, backend, handle, metadata, manager=None):
        self._backend, self._handle, self._manager = backend, handle, manager
        self.device_name = metadata["device_name"]
        self.serial_number = metadata["serial_number"]
        self._info = metadata.get("info")
        release = getattr(backend, "release_device", None)
        self._lease = weakref.finalize(self, release, handle) if release is not None else None

    def _call(self, operation, **arguments):
        return self._backend.call("device", self._handle, operation, **arguments)

    @property
    def info(self):
        return _convert("DeviceInfo", self._call("info"))

    @property
    def is_connected(self):
        return self._call("is_connected")

    def resources(self): return [_convert("Resource", v) for v in self._call("resources")]
    def params(self): return [_convert("Param", v) for v in self._call("params")]
    def topics(self): return [_convert("Topic", v) for v in self._call("topics")]
    def nodes(self): return [_convert("NodeInfo", v) for v in self._call("nodes")]
    def get(self, path): return self._call("get", path=path)
    def set(self, path, value): self._call("set", path=path, value=types._to_native(value))
    def execute(self, path): self._call("execute", path=path)
    def save_params(self): self._call("save_params")
    def get_device_log_dir(self): return self._call("get_device_log_dir")
    def set_device_log_elf(self, elf_path): self._call("set_device_log_elf", elf_path=elf_path)

    def subscribe(self, path):
        result = self._call("subscribe", path=path)
        return Subscription(self._backend, result["handle"], result["dtype"], result["stream_id"], self)

    def publish(self, path):
        result = self._call("publish", path=path)
        return Publisher(self._backend, result["handle"], result["dtype"], self)

    def disconnect(self):
        if self._manager is None:
            self._call("disconnect")
            return
        with self._manager._devices_lock:
            self._call("disconnect")
            current = self._manager._devices.get(self.device_name)
            if current is not None and current._handle == self._handle:
                self._manager._devices.pop(self.device_name, None)

    def __repr__(self):
        return f"{type(self).__name__}(serial_number={self.serial_number!r}, device_name={self.device_name!r})"


class _SemanticDevice(WujiDevice):
    def __init__(self, device):
        if not isinstance(device, WujiDevice):
            raise TypeError("device must be a WujiDevice")
        self.__dict__.update(device.__dict__)
        self._raw_device = device

    @property
    def device(self):
        return self._raw_device


class WujiHand2(_SemanticDevice):
    def handedness(self): return HandednessResource(self, "handedness")
    def effort_limit(self): return DeviceEffortLimitResource(self, "effort_limit")
    def mit_params(self): return DeviceMitParamsResource(self, "mit_params")
    def online_joints_count(self): return OnlineJointsCountResource(self, "online_joints_count")
    def joint_command(self): return JointCommandResource(self, "joint_command")
    def joint(self, joint_index): return JointHandle(self, joint_index)
    def joints(self): return [self.joint(i) for i in range(20)]
    def fingers(self): return [FingerHandle(self, i) for i in range(5)]

    def enable(self, joints=None): self._call("enable", joints=_mask(joints))
    def disable(self, joints=None): self._call("disable", joints=_mask(joints))
    def clear_fault(self, joints=None): self._call("clear_fault", joints=_mask(joints))
    def set_origin(self, joints=None): self._call("set_origin", joints=_mask(joints))
    def clear_origin(self, joints=None): self._call("clear_origin", joints=_mask(joints))

    def set_constant(self, mode, joints=None):
        """Set the feed-forward mode (FeedForwardMode or 0..3); RAM only."""
        self._call("set_constant", value=_feed_forward_mode(mode), joints=_mask(joints))

    def set_position_table(self, on, joints=None):
        """Enable/disable position-indexed lookup-table feed-forward; RAM only."""
        self._call("set_position_table", value=int(bool(on)), joints=_mask(joints))

    def emergency_stop(self): self._call("emergency_stop")
    def tactile_calibrate(self): self._call("tactile_calibrate")
    def reboot(self): self._call("reboot")

    def tactile_status(self, finger):
        if finger not in ("thumb", "index", "middle", "ring", "pinky"):
            raise ValueError("Unknown finger name")
        return _convert("TactileStatus", self._call("tactile_status", finger=finger))

    def get_fingertip_info(self, finger):
        return _convert("FingertipSensorInfo", self._call("get_fingertip_info", finger=_integer(finger, 0, 4, "finger")))

    def export_flash_logs(self, out_dir=None):
        return self._call("export_flash_logs", out_dir=out_dir)

    @staticmethod
    def joint_id_from_bus_node(bus_id, node_id):
        bus_id = _integer(bus_id, 0, 255, "bus_id")
        node_id = _integer(node_id, 0, 255, "node_id")
        if node_id == 0:
            raise ValueError("node_id must be at least 1")
        # Pinned C SDK documents sparse firmware IDs, not dense joint indices.
        return (bus_id * 5 + node_id - 1) & 255

    @staticmethod
    def describe_error(code):
        from ._error_catalog import CATALOG
        result = CATALOG.get(_integer(code, 0, 65535, "code"))
        return None if result is None else dict(result)


_HAND2_RESOURCES = {
    "ip": ("IpResource", ("get", "set"), None),
    "hw_version": ("HwVersionResource", ("get",), "HwVersion"),
    "comm_diag": ("CommDiagResource", ("get",), "HandCommunicationDiagnostics"),
    "imu": ("ImuResource", ("sub",), "ImuData"),
    "joint_states": ("JointStatesResource", ("sub",), "JointStateFrame"),
    "joint_diagnostics": ("JointDiagnosticsResource", ("sub",), "JointDiagnosticsFrame"),
    **{f"fingertip_{f}_data": (f"Fingertip{f.title()}DataResource", ("sub",), "FingertipSensorData")
       for f in ("thumb", "index", "middle", "ring", "pinky")},
}


def _accessor(name, cls, dtype):
    def method(self):
        return cls(self, name, dtype=dtype)
    method.__name__ = name
    return method


for _name, (_class_name, _operations, _dtype) in _HAND2_RESOURCES.items():
    _class = _resource_class(_class_name, _operations)
    setattr(WujiHand2, _name, _accessor(_name, _class, _dtype))


class SdkManager:
    _singleton = None
    _singleton_lock = threading.Lock()

    def __init__(self, backend):
        self._backend = backend
        self._handle = backend.call("manager", None, "open")
        self._devices = {}
        self._devices_lock = threading.RLock()

    @staticmethod
    def instance():
        with SdkManager._singleton_lock:
            if SdkManager._singleton is None:
                from ._backend import default_backend
                SdkManager._singleton = SdkManager(default_backend())
            return SdkManager._singleton

    def _call(self, operation, **arguments):
        return self._backend.call("manager", self._handle, operation, **arguments)

    def scan(self):
        return [_convert("DiscoveredDevice", item) for item in self._call("scan")]

    @staticmethod
    def _name(value):
        if not isinstance(value, str):
            raise TypeError("device_name must be a string")
        if not value or "/" in value or "." in value:
            raise types.WujiException("device_name must be nonempty and contain neither '/' nor '.'")
        return value

    @_cache_locked
    def connect(self, *, sn=None, address=None, handedness=None, device_name, options=None):
        device_name = self._name(device_name)
        if sum(v is not None for v in (sn, address, handedness)) != 1:
            raise ValueError("Specify exactly one of sn, address, or handedness")
        if options is None:
            options = types.ConnectOptions()
        elif not isinstance(options, types.ConnectOptions):
            raise TypeError("options must be ConnectOptions or None")
        if handedness is not None and not isinstance(handedness, types.Handedness):
            raise TypeError("handedness must be Handedness.Left or Handedness.Right")
        result = self._call("connect", sn=sn, address=address,
                            handedness=handedness.name if handedness is not None else None, device_name=device_name,
                            options=types._to_native(options))
        return self._register(result)

    @_cache_locked
    def _register(self, result):
        metadata = result["metadata"]
        device = WujiDevice(self._backend, result["handle"], metadata, self)
        kind = metadata["device_type"]
        if kind in ("WujiHand2", types.DeviceType.WujiHand2):
            device = WujiHand2(device)
        self._devices[device.device_name] = device
        return device

    @_cache_locked
    def auto_connect(self, device_name):
        self._name(device_name)
        # Device selection comes from the native SDK, never the local alias.
        return self._register(self._call("auto_connect", device_name=device_name))

    @_cache_locked
    def get_device(self, device_name):
        return self._devices.get(device_name)

    @_cache_locked
    def get_connected_devices(self):
        return list(self._devices.items())

    @_cache_locked
    def disconnect(self, device_name):
        self._call("disconnect", device_name=device_name)
        self._devices.pop(device_name, None)

    @_cache_locked
    def disconnect_all(self):
        self._call("disconnect_all")
        self._devices.clear()

    def get(self, path): return self._call("get", path=path)
    def set(self, path, value): self._call("set", path=path, value=types._to_native(value))
    def execute(self, path): self._call("execute", path=path)
    def global_topics(self): return [_convert("Topic", v) for v in self._call("global_topics")]
    def get_all_topics(self):
        with self._devices_lock:
            devices = list(self._devices.values())
        return [(d.device_name, t) for d in devices for t in d.topics()]

    def subscribe(self, path):
        result = self._call("subscribe", path=path)
        return Subscription(self._backend, result["handle"], result["dtype"], result["stream_id"], self)

    def publish(self, path):
        result = self._call("publish", path=path)
        return Publisher(self._backend, result["handle"], result["dtype"], self)

    def current_user(self): return self._call("current_user")
    def list_users(self): return self._call("list_users")
    def switch_user(self, user_id): return self._call("switch_user", user_id=user_id)
    def switch_to_default_user(self): return self.switch_user("")

    def create_user(self, display_name, *, description=None, external_id=None):
        return self._call("create_user", display_name=display_name,
                          description=description, external_id=external_id)

    def update_user(self, user_id, *, display_name=None, description=None, external_id=None,
                    clear_external_id=False, clear_description=False):
        if not isinstance(clear_external_id, bool) or not isinstance(clear_description, bool):
            raise TypeError("clear_external_id and clear_description must be bool")
        return self._call("update_user", user_id=user_id, display_name=display_name,
                          description=description, external_id=external_id,
                          clear_external_id=clear_external_id, clear_description=clear_description)

    def delete_user(self, user_id): self._call("delete_user", user_id=user_id)
    def tactile_dir(self, user_id, device_sn):
        return self._call("tactile_dir", user_id=user_id, device_sn=device_sn)
    def tactile_run_dir(self, user_id, device_sn):
        return self._call("tactile_run_dir", user_id=user_id, device_sn=device_sn)
    def tactile_model_paths(self, user_id, device_sn):
        return self._call("tactile_model_paths", user_id=user_id, device_sn=device_sn)


def set_log_level(level):
    if not isinstance(level, str):
        raise TypeError("level must be a string")
    if level.lower() not in ("trace", "debug", "info", "warn", "warning", "error", "off"):
        raise ValueError(f"Invalid log level: {level}")
    from ._backend import default_backend
    default_backend().call("static", None, "set_log_level", level=level)


__all__ = [name for name, value in globals().items()
           if not name.startswith("_") and isinstance(value, type)
           and value.__module__ == __name__] + ["set_log_level"]
