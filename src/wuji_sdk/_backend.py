"""Checked C ABI access to the pinned native macOS SDK.

Importing wuji_sdk does not load the native image or initialize hardware.
Unimplemented operations raise explicitly; there is no simulated transport.
"""
from __future__ import annotations

import ctypes as C
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import threading
import time

from wuji_macos._native import LIB_DIR, _bridge
from ._types import WujiException

_INITIALIZATION_LOCK = threading.RLock()


class NativeCapabilityError(WujiException, NotImplementedError):
    """This operation has not yet been connected to the native SDK."""


class StreamLaggedError(WujiException):
    """A receiver skipped ``lagged_frames`` old frames.

    The native backend's recv() now skips the backlog itself and counts it in
    ``Subscription.lagged_frames``; callback subscriptions still report this
    error to ``on_error`` and keep delivering if a backend raises it.
    """

    def __init__(self, lagged_frames):
        super().__init__(f"Stream lagged by {lagged_frames} frame(s)")
        self.lagged_frames = lagged_frames


_FINGERS = ("thumb", "index", "middle", "ring", "pinky")


def _joint_prefix(index):
    # Pinned Linux C SDK 0x270144 formats the finger through its enum Display
    # (0x29dd20, literals at 0xc73990), not the bus number. The live Hand 2
    # advertises thumb/S1 ... pinky/S4; numeric 0/S1 paths do not exist.
    return f"{_FINGERS[index // 4]}/S{index % 4 + 1}"


def _text(value):
    if not isinstance(value, str):
        raise TypeError("Expected a string")
    if "\0" in value:
        raise ValueError("Strings passed to the native SDK cannot contain NUL")
    return value.encode("utf-8")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _state_root():
    override = os.environ.get("WUJI_MACOS_STATE_DIR")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        # A checkout-local default keeps SDK registry/calibration/log files with
        # this project even when the wheel is installed in its verification env.
        for candidate in (Path.cwd(), *Path.cwd().parents):
            if (candidate / "artifacts.lock.json").is_file() and (candidate / "scripts/build_macos.py").is_file():
                root = candidate / ".state"
                break
        else:
            raise RuntimeError("Set WUJI_MACOS_STATE_DIR to an absolute SDK state directory, or run from the wuji-macos checkout")
    root.mkdir(parents=True, exist_ok=True)
    return root


class NativeBackend:
    def __init__(self):
        _bridge()  # Platform and every bundled native SHA-256 are checked first.
        self.library_path = _text(str(LIB_DIR / "libwuji_cli_reuse.dylib"))
        self.manager_lib = C.CDLL(str(LIB_DIR / "libwuji_manager_bridge.dylib"))
        self.device_lib = C.CDLL(str(LIB_DIR / "libwuji_device_bridge.dylib"))
        self._lock = threading.RLock()
        self._manager_lock = threading.RLock()
        self._manager = None
        self._devices = {}
        self._device_locks = {}
        self._subscriptions = {}
        self._subscription_locks = {}
        self._publishers = {}
        self._publisher_locks = {}
        self._signatures()

    def _signatures(self):
        p, n, s, pp = C.c_void_p, C.c_size_t, C.c_char_p, C.POINTER(C.c_void_p)
        signatures = {
            "wuji_set_log_level": [s, s, p, n],
            "wuji_manager_open": [s, s, pp, p, n],
            "wuji_manager_scan_json": [p, pp, p, n],
            "wuji_manager_connect": [p, s, s, s, s, pp, pp, p, n],
            "wuji_manager_disconnect": [p, s, p, n],
            "wuji_manager_disconnect_all": [p, p, n],
            "wuji_manager_current_user_json": [p, pp, p, n],
            "wuji_manager_list_users_json": [p, pp, p, n],
            "wuji_manager_switch_user_json": [p, s, pp, p, n],
            "wuji_manager_create_user_json": [p, s, s, s, pp, p, n],
            "wuji_manager_update_user_json": [p, s, s, s, s, C.c_uint, pp, p, n],
            "wuji_manager_delete_user": [p, s, p, n],
            "wuji_manager_tactile_paths_json": [p, s, s, s, pp, p, n],
        }
        for name, signature in signatures.items():
            fn = getattr(self.manager_lib, name)
            fn.argtypes, fn.restype = signature, C.c_int
        for name in ("wuji_manager_free_string", "wuji_manager_release_device"):
            fn = getattr(self.manager_lib, name)
            fn.argtypes, fn.restype = [p], None
        fn = self.device_lib.wuji_device_request_raw
        fn.argtypes = [s, p, C.c_int, s, p, n, pp, C.POINTER(n), p, n]
        fn.restype = C.c_int
        self.device_lib.wuji_device_buffer_free.argtypes = [p]
        self.device_lib.wuji_device_buffer_free.restype = None
        fn = self.device_lib.wuji_device_resources_json
        fn.argtypes, fn.restype = [s, p, C.c_int, p, n, C.POINTER(n), p, n], C.c_int
        fn = self.device_lib.wuji_device_is_connected
        fn.argtypes, fn.restype = [s, p, C.POINTER(C.c_int), p, n], C.c_int
        for name in ("info", "nodes"):
            fn = getattr(self.device_lib, "wuji_device_" + name + "_json")
            fn.argtypes, fn.restype = [s, p, p, n, C.POINTER(n), p, n], C.c_int
        fn = self.device_lib.wuji_hand2_handedness
        fn.argtypes, fn.restype = [s, p, C.POINTER(C.c_int), p, n], C.c_int
        fn = self.device_lib.wuji_hand2_fingertip_info_json
        fn.argtypes, fn.restype = [s, p, C.c_uint8, pp, p, n], C.c_int
        fn = self.device_lib.wuji_device_subscribe
        fn.argtypes, fn.restype = [s, p, s, pp, p, n], C.c_int
        fn = self.device_lib.wuji_subscription_try_recv
        fn.argtypes, fn.restype = [p, pp, C.POINTER(n), C.POINTER(C.c_uint64), p, n], C.c_int
        self.device_lib.wuji_subscription_close.argtypes = [p]
        self.device_lib.wuji_subscription_close.restype = None
        fn = self.device_lib.wuji_subscription_stream_id
        fn.argtypes, fn.restype = [p, C.POINTER(C.c_uint16)], C.c_int
        fn = self.device_lib.wuji_subscription_set_rate
        fn.argtypes, fn.restype = [p, C.c_uint16, C.POINTER(C.c_uint16), p, n], C.c_int
        fn = self.device_lib.wuji_device_publisher_open
        fn.argtypes, fn.restype = [s, p, s, pp, p, n], C.c_int
        fn = self.device_lib.wuji_publisher_send
        fn.argtypes, fn.restype = [p, p, n, p, n], C.c_int
        fn = self.device_lib.wuji_publisher_send_joint_commands
        fn.argtypes, fn.restype = [p, C.POINTER(C.c_float), n, p, n], C.c_int
        fn = self.device_lib.wuji_publisher_stream_id
        fn.argtypes, fn.restype = [p, C.POINTER(C.c_uint16)], C.c_int
        fn = self.device_lib.wuji_publisher_close
        fn.argtypes, fn.restype = [p, p, n], C.c_int

    @staticmethod
    def _check(code, error, *, operation=None):
        if code:
            message = error.value.decode("utf-8", errors="replace") or f"Native bridge returned {code}"
            if operation:
                message = f"{operation}: {message}"
            raise WujiException(message)

    def _take_json(self, pointer):
        try:
            if not pointer.value:
                raise RuntimeError("Native bridge returned a null JSON result")
            return json.loads(C.string_at(pointer))
        finally:
            self.manager_lib.wuji_manager_free_string(pointer)

    def call(self, scope, handle, operation, **arguments):
        if scope == "static" and operation == "set_log_level":
            error = C.create_string_buffer(2048)
            self._check(self.manager_lib.wuji_set_log_level(self.library_path, _text(arguments["level"]), error, len(error)), error)
            return None
        if scope == "manager":
            if operation == "open":
                with _INITIALIZATION_LOCK, self._lock:
                    return self._manager_call(handle, operation, arguments)
            # Connection mutations are ordered, but path routing never holds
            # this lock while acquiring a device lock. The inverse is needed
            # by WujiDevice.disconnect, so a global-to-device order would
            # deadlock against it.
            if operation in ("connect", "auto_connect", "disconnect", "disconnect_all", "switch_user",
                             "create_user", "update_user", "delete_user"):
                with self._manager_lock:
                    return self._manager_call(handle, operation, arguments)
            return self._manager_call(handle, operation, arguments)
        if scope in ("device", "subscription", "publisher"):
            with self._lock:
                locks = getattr(self, "_" + scope + "_locks")
                lock = locks.get(handle)
            if lock is None:
                if scope != "device" and operation == "close":
                    return None
                raise WujiException("Invalid or released device handle" if scope == "device"
                                    else f"StreamClosed: {scope} is closed")
            # Keep each native clone alive through its own operation. No map
            # lock is held during native I/O or polling sleeps; other devices
            # and streams can progress independently.
            with lock:
                # A native pointer can be reused after close while this call
                # waits. Its original lock must still own the mapped handle.
                with self._lock:
                    if locks.get(handle) is not lock:
                        if scope != "device" and operation == "close":
                            return None
                        raise WujiException("Invalid or released device handle" if scope == "device"
                                            else f"StreamClosed: {scope} is closed")
                if scope == "device":
                    return self._device_call(handle, operation, arguments)
                if scope == "publisher":
                    return self._publisher_call(handle, operation, arguments)
                return self._subscription_call(handle, operation, arguments)
        raise NativeCapabilityError(f"Native {scope}.{operation} is not implemented in this build")

    def _manager_call(self, handle, operation, args):
        error = C.create_string_buffer(2048)
        if operation == "open":
            if self._manager is None:
                pointer = C.c_void_p()
                self._check(self.manager_lib.wuji_manager_open(self.library_path, _text(str(_state_root())), C.byref(pointer), error, len(error)), error)
                self._manager = pointer.value
            return self._manager
        if handle != self._manager or handle is None:
            raise WujiException("Invalid manager handle")
        if operation == "scan":
            output = C.c_void_p()
            self._check(self.manager_lib.wuji_manager_scan_json(handle, C.byref(output), error, len(error)), error)
            return self._take_json(output)
        if operation in ("current_user", "list_users", "switch_user", "create_user", "update_user"):
            output = C.c_void_p()
            fn = getattr(self.manager_lib, "wuji_manager_" + operation + "_json")
            arguments = [handle]
            if operation in ("switch_user", "update_user"):
                arguments.append(_text(args["user_id"]))
            if operation in ("create_user", "update_user"):
                arguments.extend(_text(args[k]) if args.get(k) is not None else None
                                 for k in ("display_name", "description", "external_id"))
            if operation == "update_user":
                arguments.append(int(args["clear_description"]) | (int(args["clear_external_id"]) << 1))
            self._check(fn(*arguments, C.byref(output), error, len(error)), error)
            value = self._take_json(output)
            records = value if operation == "list_users" else [value]
            for record in records:
                # Native JSON omits absent Option<String>; the public Python
                # TypedDict includes these fields as None.
                record.setdefault("description", None)
                record.setdefault("external_id", None)
            return value
        if operation == "delete_user":
            self._check(self.manager_lib.wuji_manager_delete_user(
                handle, _text(args["user_id"]), error, len(error)), error)
            return None
        if operation in ("tactile_dir", "tactile_run_dir", "tactile_model_paths"):
            output = C.c_void_p()
            self._check(self.manager_lib.wuji_manager_tactile_paths_json(
                handle, _text(operation), _text(args["user_id"]), _text(args["device_sn"]),
                C.byref(output), error, len(error)), error)
            return self._take_json(output)
        if operation == "auto_connect":
            devices = self._manager_call(handle, "scan", {})
            if not devices:
                raise WujiException("Device not found: no device discovered")
            if len(devices) != 1:
                raise WujiException(f"Multiple devices discovered ({len(devices)}); use connect(sn=...) to select one")
            return self._manager_call(handle, "connect", dict(sn=devices[0]["sn"], device_name=args["device_name"], options=None))
        if operation == "connect":
            kind, target = next((k, args[k]) for k in ("sn", "address", "handedness") if args.get(k) is not None)
            pointer, output = C.c_void_p(), C.c_void_p()
            self._check(self.manager_lib.wuji_manager_connect(handle, _text(kind), _text(target), _text(args["device_name"]), _json(args["options"]) if args.get("options") is not None else None, C.byref(pointer), C.byref(output), error, len(error)), error)
            try:
                metadata = self._take_json(output)
                metadata["serial_number"] = metadata.pop("sn")
            except BaseException:
                cleanup_error = C.create_string_buffer(2048)
                self.manager_lib.wuji_manager_disconnect(handle, _text(args["device_name"]), cleanup_error, len(cleanup_error))
                self.manager_lib.wuji_manager_release_device(pointer)
                raise
            with self._lock:
                self._devices[pointer.value] = metadata
                self._device_locks[pointer.value] = threading.RLock()
            return {"handle": pointer.value, "metadata": metadata}
        if operation in ("disconnect", "disconnect_all"):
            if operation == "disconnect" and args.get("expected_handle") is not None:
                with self._lock:
                    expected = self._devices.get(args["expected_handle"])
                    if expected is None or expected.get("disconnected"):
                        return None
            fn = getattr(self.manager_lib, "wuji_manager_" + operation)
            arguments = [handle] + ([_text(args["device_name"])] if operation == "disconnect" else [])
            self._check(fn(*arguments, error, len(error)), error)
            # Keep owned native records alive until all Python references go
            # away; a disconnected resource must never become a dangling ptr.
            with self._lock:
                for metadata in self._devices.values():
                    if operation == "disconnect_all" or metadata["device_name"] == args["device_name"]:
                        metadata["disconnected"] = True
            return None
        if operation in ("get", "set", "execute", "publish", "subscribe"):
            path = args["path"]
            with self._lock:
                devices = list(self._devices.items())
            for pointer, metadata in devices:
                if metadata.get("disconnected"):
                    continue
                prefix = metadata["device_name"]
                if path.startswith(prefix + ".") or path.startswith(prefix + "/"):
                    return self.call("device", pointer, operation, **{**args, "path": path[len(prefix) + 1:]})
            raise WujiException(f"Device resource path cannot be resolved: {path}")
        raise NativeCapabilityError(f"Native manager.{operation} is not implemented in this build")

    def request_raw(self, handle, operation, path, payload=b""):
        """Single native GET/SET/ACTION; payload is already schema-encoded."""
        error, output, length = C.create_string_buffer(2048), C.c_void_p(), C.c_size_t()
        source = C.create_string_buffer(payload)
        self._check(self.device_lib.wuji_device_request_raw(self.library_path, handle, operation, _text(path), source, len(payload), C.byref(output), C.byref(length), error, len(error)), error)
        try:
            return C.string_at(output, length.value) if output else b""
        finally:
            self.device_lib.wuji_device_buffer_free(output)

    def release_device(self, handle):
        """Release the owned Rust clone once no Python device/resource uses it."""
        with self._lock:
            lock = self._device_locks.get(handle)
        if lock is None:
            return
        with lock:
            with self._lock:
                metadata = self._devices.pop(handle, None)
                self._device_locks.pop(handle, None)
            if metadata is not None:
                self.manager_lib.wuji_manager_release_device(handle)

    def _resources(self, handle, include_hidden=False):
        error = C.create_string_buffer(2048)
        capacity = 65536
        while capacity <= 16 * 1024 * 1024:
            output, needed = C.create_string_buffer(capacity), C.c_size_t()
            code = self.device_lib.wuji_device_resources_json(self.library_path, handle, include_hidden,
                                                            output, capacity, C.byref(needed), error, len(error))
            if code == 1:
                capacity = max(capacity * 2, needed.value)
                continue
            self._check(code, error)
            return json.loads(output.value)
        raise WujiException("Native resource metadata exceeds 16 MiB")

    def _device_json(self, handle, name):
        error = C.create_string_buffer(2048)
        capacity = 4096
        fn = getattr(self.device_lib, "wuji_device_" + name + "_json")
        while capacity <= 16 * 1024 * 1024:
            output, needed = C.create_string_buffer(capacity), C.c_size_t()
            code = fn(self.library_path, handle, output, capacity, C.byref(needed), error, len(error))
            if code == 1:
                capacity = max(capacity * 2, needed.value)
                continue
            self._check(code, error)
            return json.loads(output.value)
        raise WujiException("Native device metadata exceeds 16 MiB")

    def _resource(self, handle, path):
        canonical = path.replace(".", "/")
        for descriptor in self._resources(handle, include_hidden=True):
            if descriptor["path"] == canonical:
                return descriptor
        raise WujiException(f"Resource not found: {path}")

    @staticmethod
    def _decode(descriptor, payload):
        from . import _codec
        if descriptor["serde_format"] == 1:
            return json.loads(payload)
        if descriptor["serde_format"] == 0:
            return _codec.decode(descriptor["schema_id"], payload)
        if descriptor["serde_format"] == 2:
            return _codec.decode_le(descriptor["schema_id"], payload)
        raise NativeCapabilityError(f"Unsupported native serialization format: {descriptor['serde_format']}")

    @staticmethod
    def _encode(descriptor, value):
        from . import _codec
        if descriptor["serde_format"] == 1:
            return _json(value)
        if descriptor["serde_format"] == 0:
            # Sending a guessed struct order can be an unsafe control command.
            # Only primitive/spec-defined or native-fixture-verified structures
            # can be written through this experimental adapter.
            return _codec.encode(descriptor["schema_id"], value, require_verified=True)
        if descriptor["serde_format"] == 2:
            return _codec.encode_le(descriptor["schema_id"], value)
        raise NativeCapabilityError(f"Unsupported native serialization format: {descriptor['serde_format']}")

    def _device_call(self, handle, operation, args):
        metadata = self._devices.get(handle)
        if metadata is None:
            raise WujiException("Invalid or released device handle")
        if operation == "disconnect":
            if metadata.get("disconnected"):
                return None
            return self.call("manager", self._manager, "disconnect", device_name=metadata["device_name"],
                             expected_handle=handle)
        if operation == "info":
            return self._device_json(handle, "info")
        if metadata.get("disconnected"):
            if operation == "is_connected":
                return False
            raise WujiException("DeviceDisconnected: device was disconnected")
        if operation == "is_connected":
            value, error = C.c_int(), C.create_string_buffer(2048)
            self._check(self.device_lib.wuji_device_is_connected(self.library_path, handle, C.byref(value), error, len(error)), error)
            return bool(value.value)
        if operation == "resources":
            keys = ("path", "can_get", "can_set", "can_sub", "can_pub", "can_exec")
            return [{k: d[k] for k in keys} for d in self._resources(handle)]
        if operation == "nodes":
            return self._device_json(handle, "nodes")
        if operation == "get_fingertip_info":
            output, error = C.c_void_p(), C.create_string_buffer(2048)
            try:
                self._check(self.device_lib.wuji_hand2_fingertip_info_json(
                    self.library_path, handle, args["finger"], C.byref(output), error, len(error)), error)
                if not output.value:
                    raise WujiException("Native fingertip metadata returned a null JSON result")
                return json.loads(C.string_at(output))
            finally:
                self.device_lib.wuji_device_buffer_free(output)
        if operation in ("params", "topics"):
            descriptors = self._resources(handle)
            keys = ("can_get", "can_set") if operation == "params" else ("can_sub", "can_pub")
            return [{k: d[k] for k in ("path", *keys)} for d in descriptors if any(d[k] for k in keys)]
        if operation in ("get", "set"):
            descriptor = self._resource(handle, args["path"])
            if not descriptor["can_" + operation]:
                raise WujiException(f"Resource does not support {operation}: {args['path']}")
            if operation == "get":
                return self._decode(descriptor, self.request_raw(handle, 0, args["path"]))
            self.request_raw(handle, 1, args["path"], self._encode(descriptor, args["value"]))
            return None
        if operation == "subscribe":
            from ._codec import schema_info
            descriptor = self._resource(handle, args["path"])
            if not descriptor["can_sub"]:
                raise WujiException(f"Resource does not support subscribe: {args['path']}")
            schema = schema_info(descriptor["schema_id"])
            if descriptor["serde_format"] == 0 and not schema["supported"]:
                raise NativeCapabilityError(f"Cannot decode subscription schema: {schema}")
            pointer, stream_id, error = C.c_void_p(), C.c_uint16(), C.create_string_buffer(2048)
            self._check(self.device_lib.wuji_device_subscribe(self.library_path, handle, _text(args["path"]), C.byref(pointer), error, len(error)), error)
            try:
                self._check(self.device_lib.wuji_subscription_stream_id(pointer, C.byref(stream_id)), error)
                with self._lock:
                    self._subscriptions[pointer.value] = descriptor
                    self._subscription_locks[pointer.value] = threading.RLock()
            except BaseException:
                self.device_lib.wuji_subscription_close(pointer)
                raise
            return {"handle": pointer.value, "stream_id": stream_id.value, "dtype": schema["name"] or str(descriptor["schema_id"])}
        if operation == "publish":
            from ._codec import schema_info
            descriptor = self._resource(handle, args["path"])
            if not descriptor["can_pub"]:
                raise WujiException(f"Resource does not support publish: {args['path']}")
            schema = schema_info(descriptor["schema_id"])
            typed = args.get("typed_joint_commands", False)
            if typed and (descriptor["schema_id"] != 315 or descriptor["serde_format"] not in (0, 1)):
                raise NativeCapabilityError("Typed Hand 2 commands require native Postcard or JSON JointCommandFrame schema 315")
            pointer, error = C.c_void_p(), C.create_string_buffer(2048)
            self._check(self.device_lib.wuji_device_publisher_open(
                self.library_path, handle, _text(args["path"]), C.byref(pointer), error, len(error)), error,
                operation=f"opening publisher {args['path']!r}")
            try:
                with self._lock:
                    self._publishers[pointer.value] = {"descriptor": descriptor, "typed": typed}
                    self._publisher_locks[pointer.value] = threading.RLock()
            except BaseException:
                self.device_lib.wuji_publisher_close(pointer, error, len(error))
                raise
            return {"handle": pointer.value, "dtype": schema["name"] or str(descriptor["schema_id"])}
        if operation == "resource" and args.get("index") is None:
            name, action = args["resource"], args["action"]
            if name == "joint_command" and action == "publish":
                return self._device_call(handle, "publish", {"path": "joint_command", "typed_joint_commands": True})
            if name == "handedness" and action == "get":
                value, error = C.c_int(), C.create_string_buffer(2048)
                self._check(self.device_lib.wuji_hand2_handedness(self.library_path, handle, C.byref(value), error, len(error)), error)
                return ("left", "right")[value.value]
            if name in ("effort_limit", "mit_params"):
                if action == "set":
                    value = args["value"]
                    joints = value if isinstance(value, list) else [value] * 20
                    return self._device_call(handle, "set", {"path": name, "value": {"joints": joints}})
                if action == "get":
                    joints = self._device_call(handle, "get", {"path": name})["joints"]
                    if len(joints) != 20:
                        raise WujiException(f"{name} returned {len(joints)} joints; expected 20")
                    if name == "effort_limit":
                        return [None if math.isnan(v) else v for v in joints]
                    return [None if math.isnan(v["kp"]) or math.isnan(v["kd"]) else v for v in joints]
            if name == "online_joints_count" and action == "get":
                fingers = self._device_call(handle, "get", {"path": "comm_diag/hand"})["fingers"]
                if len(fingers) != 5 or any(len(f["nodes"]) != 5 for f in fingers):
                    raise WujiException("Invalid hand communication diagnostics shape")
                return sum(node["online"] for finger in fingers for node in finger["nodes"][:4])
            if name in ("ip", "hw_version", "comm_diag") and action in ("get", "set"):
                path = {"ip": "ip_address", "comm_diag": "comm_diag/hand"}.get(name, name)
                return self._device_call(handle, action, {"path": path, **({"value": args["value"]} if action == "set" else {})})
            if action == "subscribe" and name in ("imu", "joint_states", "joint_diagnostics", "fingertip_thumb_data", "fingertip_index_data", "fingertip_middle_data", "fingertip_ring_data", "fingertip_pinky_data"):
                # Python generated accessor names flatten the actual path.
                path = name.replace("_", "/") if name.startswith("fingertip_") else name
                return self._device_call(handle, "subscribe", {"path": path})
        if operation == "resource" and args.get("index") is not None:
            # Per-joint scalars advertised by the device: effort_limit float
            # GET/SET, error_code u16 GET, status_word u32 GET.
            suffix = {"joint_effort_limit": "effort_limit", "joint_error_code": "error_code",
                      "joint_status_word": "status_word"}.get(args["resource"])
            if suffix is not None and args["action"] in ("get", "set"):
                request = {"path": f"{_joint_prefix(args['index'])}/{suffix}"}
                if args["action"] == "set":
                    request["value"] = args["value"]
                return self._device_call(handle, args["action"], request)
        if operation == "execute":
            self.request_raw(handle, 2, args["path"])
            return None
        if operation == "reboot":
            # Pinned C SDK documents wuji_hand_2_reboot as execute("reboot").
            self.request_raw(handle, 2, "reboot")
            return None
        if operation == "tactile_calibrate":
            for finger in _FINGERS:
                self.request_raw(handle, 2, f"{finger}/tactile/tactile_calibrate")
            return None
        if operation == "tactile_status":
            if args["finger"] not in _FINGERS:
                raise ValueError("Unknown finger name")
            prefix = f"{args['finger']}/tactile/"
            self.request_raw(handle, 2, prefix + "tactile_query_status")
            for _ in range(50):
                state = self._device_call(handle, "get", {"path": prefix + "tactile_state"})
                if state == 255:
                    time.sleep(0.02)
                    continue
                if state not in (0, 1):
                    raise WujiException(f"Invalid tactile state: {state}")
                model = self._device_call(handle, "get", {"path": prefix + "tactile_type"})
                if model not in (0, 1):
                    raise WujiException(f"Invalid tactile model: {model}")
                return {"model": model, "state": state}
            raise WujiException("Timed out waiting for tactile status")
        if operation in ("set_constant", "set_position_table"):
            # SDK 2026.9.22 feed-forward: each joint gets a raw SET of a u32 LE
            # (mode 0..3, or 0/1) on its od/action_* resource, in index order,
            # stopping at the first failure. Firmware applies it in RAM only.
            resource = {"set_constant": "od/action_set_constant",
                        "set_position_table": "od/action_set_position_table"}[operation]
            payload = int(args["value"]).to_bytes(4, "little")
            mask = args.get("joints") or [1] * 20
            for index, selected in enumerate(mask):
                if selected:
                    self.request_raw(handle, 1, f"{_joint_prefix(index)}/{resource}", payload)
            return None
        actions = {"enable": "motor_enable", "disable": "motor_disable",
                   "clear_fault": "clear_fault", "set_origin": "set_origin",
                   "clear_origin": "clear_origin", "emergency_stop": "emergency_stop"}
        if operation in actions:
            # Hand2 convenience methods use raw SET with an empty payload.
            # Unlike WujiDevice.execute, they do not first check can_exec.
            mask = args.get("joints")
            if mask is None:
                self.request_raw(handle, 1, actions[operation])
            else:
                for index, selected in enumerate(mask):
                    if not selected:
                        continue
                    prefix = _joint_prefix(index)
                    if operation in ("set_origin", "clear_origin"):
                        control = 1 if operation == "set_origin" else 2
                        self.request_raw(handle, 1, prefix + "/od/aux/set_origin_ctrl", control.to_bytes(4, "little"))
                    else:
                        self.request_raw(handle, 1, prefix + "/" + actions[operation])
            return None
        raise NativeCapabilityError(f"Native device.{operation} is not implemented in this build")

    def _subscription_call(self, handle, operation, args):
        descriptor = self._subscriptions.get(handle)
        if operation == "close":
            with self._lock:
                descriptor = self._subscriptions.pop(handle, None)
                self._subscription_locks.pop(handle, None)
            if descriptor is not None:
                self.device_lib.wuji_subscription_close(handle)
            return None
        if descriptor is None:
            raise WujiException("StreamClosed: subscription is closed")
        if operation == "set_rate":
            actual, error = C.c_uint16(), C.create_string_buffer(2048)
            self._check(self.device_lib.wuji_subscription_set_rate(handle, args["frequency_hz"], C.byref(actual), error, len(error)), error)
            return actual.value
        if operation == "lagged_frames":
            return descriptor.get("lagged_frames", 0)
        if operation == "recv":
            output, length, lagged = C.c_void_p(), C.c_size_t(), C.c_uint64()
            error = C.create_string_buffer(2048)
            while True:
                code = self.device_lib.wuji_subscription_try_recv(handle, C.byref(output), C.byref(length), C.byref(lagged), error, len(error))
                if code != 4:
                    break
                # The receiver has already skipped to the oldest retained frame.
                # Keep receiving, as the upstream examples expect (they poll
                # joint_diagnostics every 0.2 s at 1 kHz); count the gap instead.
                descriptor["lagged_frames"] = descriptor.get("lagged_frames", 0) + lagged.value
            if code == 1:
                return None
            if code == 2:
                raise WujiException("StreamClosed: native subscription disconnected")
            self._check(code, error)
            try:
                return self._decode(descriptor, C.string_at(output, length.value) if output else b"")
            finally:
                self.device_lib.wuji_device_buffer_free(output)
        raise NativeCapabilityError(f"Native subscription.{operation} is not implemented in this build")

    def _publisher_call(self, handle, operation, args):
        publisher = self._publishers.get(handle)
        error = C.create_string_buffer(2048)
        if operation == "close":
            with self._lock:
                publisher = self._publishers.pop(handle, None)
                self._publisher_locks.pop(handle, None)
            if publisher is not None:
                # Native close releases all ownership even if cancellation fails.
                self._check(self.device_lib.wuji_publisher_close(handle, error, len(error)), error,
                            operation="closing publisher")
            return None
        if publisher is None:
            raise WujiException("StreamClosed: publisher is closed")
        if operation == "send":
            if publisher["typed"]:
                joints = args["data"]
                if len(joints) != 20:
                    raise ValueError("Joint commands must contain exactly 20 entries")
                values = [float(joint[field]) for joint in joints for field in ("position", "velocity", "effort")]
                native_values = (C.c_float * 60)(*values)
                if not all(math.isfinite(v) for v in native_values):
                    raise ValueError("Joint commands must be finite and representable as float32")
                self._check(self.device_lib.wuji_publisher_send_joint_commands(
                    handle, native_values, len(native_values), error, len(error)), error,
                    operation="sending joint commands")
            else:
                payload = self._encode(publisher["descriptor"], args["data"])
                source = C.create_string_buffer(payload)
                self._check(self.device_lib.wuji_publisher_send(
                    handle, source, len(payload), error, len(error)), error, operation="sending publisher data")
            return None
        raise NativeCapabilityError(f"Native publisher.{operation} is not implemented in this build")


@lru_cache(maxsize=1)
def _cached_backend():
    return NativeBackend()


def default_backend():
    # functools.lru_cache can invoke a cache miss more than once concurrently.
    # Keep image/bridge setup and the shared backend singleton single-threaded.
    with _INITIALIZATION_LOCK:
        return _cached_backend()
