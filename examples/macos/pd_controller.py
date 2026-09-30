#!/usr/bin/env python3
"""Minimal Wuji Hand 2 joint PD controller on the lowest layer of this port.

The PD law runs in the hand's firmware (MIT mode, the device default):

    effort[i] = kp[i] * (q_desired[i] - q[i]) + kd[i] * (qd_desired[i] - qd[i]) + effort_ff[i]

in amperes (the SDK uses Kt = 1). The host sets kp/kd/effort limit once, then a
1 kHz thread streams (q_desired, qd_desired, effort_ff) for all 20 joints.
With kp = kd = 0 the same stream becomes direct current commands for a
host-side controller.

The hot path skips the SDK's Python value objects: commands go to the native
publisher as one float32[60], and joint_states arrive as raw Postcard bytes
decoded with struct (about 1 us instead of about 70 us per frame).

The API follows aiofranka / aioflexiv:

    controller = WujiController("WH2KA...")
    await controller.start()
    try:
        await controller.move(target)          # smooth, velocity feedforward
        controller.set_freq(50)
        await controller.set("q_desired", q)   # paced step targets
    finally:
        await controller.stop()

Safety. start() refuses unless all 20 joints are online, Ready, fault-free and
inside their documented range. The loop disables the hand when joint_states go
stale, a joint drops out, a joint faults or leaves Enabled, or the tracking error
exceeds max_error. Reported joint velocity is deliberately not used for safety:
on this hardware it can read 0.6 rad/s while the joint is stationary. stop()
disables, confirms from fresh telemetry, falls back to emergency stop, and only
then restores the previous gains. If the process dies without stop(), the
firmware keeps holding the last target with the configured gains.

As a script it is a dry run by default: connect and run the loop without
changing gains, enabling, or sending commands. Motion requires
--confirm-motion with the same serial as the positional argument.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes as C
import math
import struct
import sys
import threading
import time

from wuji_sdk import ConnectOptions, DeviceType, SdkManager, WujiException, WujiHand2, _codec

JOINTS = 20
# Documented joint ranges: https://docs.wuji.tech/docs/en/wuji-hand/latest/control-guide/
_THUMB_DEG = ((-68, 74), (-85, 40), (-60, 90), (-60, 90))
_FINGER_DEG = ((-60, 90), (-40, 40), (-60, 120), (-60, 90))
JOINT_LIMITS = tuple((math.radians(low), math.radians(high))
                     for low, high in _THUMB_DEG + _FINGER_DEG * 4)
# Guards against typos, not tuning advice. effort_limit bounds the actual current.
MAX_KP, MAX_KD, MAX_EFFORT_LIMIT = 20.0, 1.0, 1.5
_ENABLED, _READY, _STOPPED = 2, 1, 3
_TARGETS = ("q_desired", "qd_desired", "effort_ff")


def dense_index(nid):
    """Sparse telemetry NID (five slots per finger bus, joints in slots 1-4) -> 0..19."""
    bus, slot = divmod(nid, 5)
    if bus >= 5 or not 1 <= slot <= 4:
        raise ValueError(f"unexpected joint NID {nid}")
    return bus * 4 + slot - 1


_ENTRY = struct.Struct("<Bfff")


def _varint(buf, i):
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode_joint_states(payload):
    """Decode a Postcard JointStateFrame (schema 330) into dense per-joint lists.

    Wire order, fixed by the native serializer fixture: header {seq varint,
    timestamp_us varint, frame_id string}, num_joints u8, then a Vec of
    {nid u8, position f32, velocity f32, effort f32}. Joints absent from the
    frame are None. Returns (seq, timestamp_us, q, qd, effort).
    """
    try:
        seq, i = _varint(payload, 0)
        stamp, i = _varint(payload, i)
        length, i = _varint(payload, i)
        count, i = _varint(payload, i + length + 1)  # skip frame_id and num_joints
    except IndexError:
        raise ValueError("truncated joint_states payload") from None
    if i + _ENTRY.size * count != len(payload):
        raise ValueError("malformed joint_states payload")
    q, qd, effort = [None] * JOINTS, [None] * JOINTS, [None] * JOINTS
    for nid, position, velocity, current in _ENTRY.iter_unpack(payload[i:]):
        j = dense_index(nid)
        q[j], qd[j], effort[j] = position, velocity, current
    return seq, stamp, q, qd, effort


def diagnostic_faults(frame, *, require_enabled):
    """Problems in a decoded JointDiagnosticsFrame (schema 332) mapping."""
    faults = []
    for joint in frame["joints"]:
        status, code = joint["status_word"], joint["error_code_current"]
        problems = []
        if code:
            info = WujiHand2.describe_error(code)
            problems.append(f"error 0x{code:04x}({info['name'] if info else 'Unknown'})")
        if status & 0x70:
            problems.append("limit active")
        if status & 3 == _STOPPED or (require_enabled and status & 3 != _ENABLED):
            problems.append(f"ext_state {status & 3}")
        if problems:
            faults.append(f"NID {joint['nid']}: {', '.join(problems)}")
    return faults


def cosine_segment(q_from, q_to, duration_s, t):
    """Raised-cosine move: zero velocity at both ends. Returns (q, qd, done)."""
    if t >= duration_s:
        return list(q_to), [0.0] * len(q_to), True
    phase = math.pi * t / duration_s
    shape = 0.5 * (1.0 - math.cos(phase))
    rate = 0.5 * math.pi * math.sin(phase) / duration_s
    q = [a + (b - a) * shape for a, b in zip(q_from, q_to)]
    qd = [(b - a) * rate for a, b in zip(q_from, q_to)]
    return q, qd, False


def _vector(value, name, low=-math.inf, high=math.inf):
    values = [float(value)] * JOINTS if isinstance(value, (int, float)) else [float(v) for v in value]
    if len(values) != JOINTS:
        raise ValueError(f"{name} needs {JOINTS} values, got {len(values)}")
    if not all(math.isfinite(v) and low <= v <= high for v in values):
        raise ValueError(f"{name} must be finite and within [{low}, {high}]")
    return values


def _check_within_limits(q, name):
    outside = [j for j, (v, (low, high)) in enumerate(zip(q, JOINT_LIMITS)) if not low <= v <= high]
    if outside:
        raise ValueError(f"{name} outside documented joint range at joints {outside}")


class _RawStream:
    """Drain a native subscription, keeping only the newest payload's bytes."""

    def __init__(self, subscription, schema_id):
        backend = subscription._backend
        descriptor = backend._subscriptions[subscription._handle]
        if (descriptor["schema_id"], descriptor["serde_format"]) != (schema_id, 0):
            raise WujiException(f"expected Postcard schema {schema_id}, got {descriptor}")
        self.subscription = subscription
        self._handle = subscription._handle
        self._recv = backend.device_lib.wuji_subscription_try_recv
        self._free = backend.device_lib.wuji_device_buffer_free
        self._out, self._length, self._lag = C.c_void_p(), C.c_size_t(), C.c_uint64()
        self._error = C.create_string_buffer(256)
        self.lagged_frames = 0

    def drain(self):
        latest = None
        while True:
            code = self._recv(self._handle, C.byref(self._out), C.byref(self._length),
                              C.byref(self._lag), self._error, len(self._error))
            if code == 0:
                try:
                    latest = C.string_at(self._out, self._length.value) if self._out else b""
                finally:
                    self._free(self._out)
            elif code == 1:
                return latest
            elif code == 4:
                self.lagged_frames += self._lag.value
            else:
                raise WujiException(f"native receive failed ({code}): {self._error.value.decode()}")


class _RawCommandSender:
    """Send one float32[60] (position, velocity, effort per joint) per call."""

    def __init__(self, publisher):
        backend = publisher._backend
        entry = backend._publishers[publisher._handle]
        if not entry["typed"] or entry["descriptor"]["schema_id"] != 315:
            raise WujiException("expected the typed joint_command publisher (schema 315)")
        self.publisher = publisher
        self._handle = publisher._handle
        self._send = backend.device_lib.wuji_publisher_send_joint_commands
        self._error = C.create_string_buffer(256)
        self.buffer = (C.c_float * (3 * JOINTS))()

    def send(self, q, qd, effort):
        self.buffer[0::3], self.buffer[1::3], self.buffer[2::3] = q, qd, effort
        code = self._send(self._handle, self.buffer, len(self.buffer), self._error, len(self._error))
        if code:
            raise WujiException(f"joint command send failed ({code}): {self._error.value.decode()}")


class WujiController:
    """Stream firmware-PD targets to a Wuji Hand 2 from a 1 kHz background thread."""

    def __init__(self, sn=None, *, kp=3.0, kd=0.05, effort_limit=0.5, rate_hz=1000.0,
                 max_error=math.radians(10.0), stale_ms=20.0, dry_run=False,
                 device_name="wuji_controller"):
        self.sn = sn
        self.kp = _vector(kp, "kp", 0.0, MAX_KP)
        self.kd = _vector(kd, "kd", 0.0, MAX_KD)
        if not 0.0 <= effort_limit <= MAX_EFFORT_LIMIT:
            raise ValueError(f"effort_limit must be within [0, {MAX_EFFORT_LIMIT}] A")
        if not 100.0 <= rate_hz <= 1000.0:
            raise ValueError("rate_hz must be within [100, 1000]")
        self.effort_limit, self.rate_hz = float(effort_limit), float(rate_hz)
        self.max_error, self.stale_ms = float(max_error), float(stale_ms)
        self.dry_run, self.device_name = dry_run, device_name

        self.hand = None
        self.state = None
        self.initial_qpos = None
        self.q_desired = self.qd_desired = self.effort_ff = None
        self.error = None
        self.stats = {"ticks": 0, "sends": 0, "overruns": 0, "max_interval_ms": 0.0,
                      "max_tracking_error_rad": 0.0, "lagged_frames": 0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._manager = self._states = self._diagnostics = self._sender = None
        self._diagnostic = None  # (host_ns, decoded frame)
        self._motion = None  # (start_ns, duration_s, q_from, q_to, done_event)
        self._enable_requested = self._enabled = self._shut_down = False
        self._saved_gains = None
        self._update_freq = 50.0
        self._last_update_time = {}

    @property
    def dof(self):
        return JOINTS

    # ---- lifecycle ----------------------------------------------------------

    async def start(self):
        try:
            await asyncio.to_thread(self._prepare)
            self._thread = threading.Thread(target=self._loop, name="wuji-controller", daemon=True)
            self._thread.start()
            if not self.dry_run:
                await asyncio.to_thread(self._enable)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        await asyncio.to_thread(self._shutdown)

    def _prepare(self):
        self._manager = SdkManager.instance()
        if self.sn is None:
            hands = [d for d in self._manager.scan() if d.device_type == DeviceType.WujiHand2]
            if len(hands) != 1:
                raise WujiException(f"found {len(hands)} Wuji Hand 2 devices; pass the serial")
            self.sn = hands[0].sn
        self.hand = self._manager.connect(sn=self.sn, device_name=self.device_name,
                                          options=ConnectOptions(enable_bridge=False))
        online = self.hand.online_joints_count().get()
        if online != JOINTS:
            raise WujiException(f"expected {JOINTS} online joints, got {online}")
        self._states = _RawStream(self.hand.joint_states().subscribe(), 330)
        self._diagnostics = _RawStream(self.hand.joint_diagnostics().subscribe(), 332)

        deadline, state, diagnostic = time.monotonic() + 2.0, None, None
        while (state is None or diagnostic is None) and time.monotonic() < deadline:
            state = self._states.drain() or state
            diagnostic = self._diagnostics.drain() or diagnostic
            time.sleep(0.002)
        if state is None or diagnostic is None:
            raise WujiException("no joint_states/joint_diagnostics within 2 s")
        seq, stamp, q, qd, effort = decode_joint_states(state)
        if None in q:
            raise WujiException("joint_states is missing joints")
        frame = _codec.decode(332, diagnostic)
        not_ready = [j["nid"] for j in frame["joints"] if j["status_word"] & 3 != _READY]
        faults = diagnostic_faults(frame, require_enabled=False)
        if not_ready or faults or len(frame["joints"]) != JOINTS:
            raise WujiException(f"refusing to start: not Ready {not_ready}, faults {faults}")
        _check_within_limits(q, "measured position")
        now = time.perf_counter_ns()
        self._publish_state(now, seq, stamp, q, qd, effort)
        self._diagnostic = (now, frame)
        self.initial_qpos = list(q)
        self.q_desired, self.qd_desired, self.effort_ff = list(q), [0.0] * JOINTS, [0.0] * JOINTS
        if self.dry_run:
            return

        self._saved_gains = (self.hand.effort_limit().get(), self.hand.mit_params().get())
        if None in self._saved_gains[0] or None in self._saved_gains[1]:
            raise WujiException("cannot save the current gains of every joint")
        self.hand.effort_limit().set(self.effort_limit)
        self.hand.mit_params().set([(p, d) for p, d in zip(self.kp, self.kd)])
        limits, gains = self.hand.effort_limit().get(), self.hand.mit_params().get()
        close = lambda a, b: math.isclose(a, b, rel_tol=0.0, abs_tol=1e-5)
        if not (all(close(v, self.effort_limit) for v in limits) and
                all(close(g.kp, p) and close(g.kd, d) for g, p, d in zip(gains, self.kp, self.kd))):
            raise WujiException("gain readback does not match the requested values")
        self._sender = _RawCommandSender(self.hand.joint_command().publish())

    def _enable(self):
        # The loop is already streaming the measured pose, so the firmware's
        # target equals the current position the moment motors engage.
        time.sleep(0.02)
        requested = time.perf_counter_ns()
        self._enable_requested = True
        self.hand.enable()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.error is None:
            received, frame = self._diagnostic
            if received > requested and all(j["status_word"] & 3 == _ENABLED for j in frame["joints"]):
                self._enabled = True
                return
            time.sleep(0.005)
        raise WujiException(self.error or "not every joint reached Enabled within 2 s")

    def _shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        problems = []
        disabled = not self._enable_requested
        if self._enable_requested:
            disabled = self._disable_and_confirm(self.hand.disable)
            if not disabled:
                problems.append("disable not confirmed; sent emergency stop")
                disabled = self._disable_and_confirm(self.hand.emergency_stop)
        for resource in (self._sender and self._sender.publisher,
                         self._states and self._states.subscription,
                         self._diagnostics and self._diagnostics.subscription):
            try:
                if resource is not None:
                    resource.close()
            except Exception as error:
                problems.append(f"close: {error}")
        if self._saved_gains is not None:
            if disabled:
                try:
                    self.hand.effort_limit().set(self._saved_gains[0])
                    self.hand.mit_params().set([(g.kp, g.kd) for g in self._saved_gains[1]])
                except Exception as error:
                    problems.append(f"restore gains: {error}")
            else:
                problems.append("gains not restored because disable was not confirmed")
        if self.hand is not None:
            try:
                self._manager.disconnect(self.device_name)
            except Exception as error:
                problems.append(f"disconnect: {error}")
        if not disabled:
            raise WujiException("; ".join(problems))
        if problems and self.error is None:
            self.error = "; ".join(problems)

    def _disable_and_confirm(self, action, timeout_s=1.0):
        """Run disable/emergency stop, then require a fresh frame with no joint Enabled."""
        try:
            action()
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                payload = self._diagnostics.drain()
                if payload is not None:
                    frame = _codec.decode(332, payload)
                    if not any(j["status_word"] & 3 == _ENABLED for j in frame["joints"]):
                        return True
                time.sleep(0.005)
        except Exception:
            pass
        return False

    # ---- target API (aiofranka / aioflexiv style) ---------------------------

    def set_freq(self, freq):
        if freq <= 0:
            raise ValueError("freq must be positive")
        self._update_freq = float(freq)

    def set_now(self, attr, value):
        self._raise_if_stopped()
        if attr not in _TARGETS:
            raise ValueError(f"attr must be one of {_TARGETS}")
        if attr == "effort_ff":
            values = _vector(value, attr, -self.effort_limit, self.effort_limit)
        else:
            values = _vector(value, attr)
        if attr == "q_desired":
            _check_within_limits(values, attr)
        with self._lock:
            if self._motion is not None:
                self._motion[4].set()  # an explicit target supersedes a move in progress
                self._motion = None
            setattr(self, attr, values)

    async def set(self, attr, value):
        """Set a target, then sleep so repeated calls run at set_freq() Hz."""
        now = time.perf_counter()
        target_time = self._last_update_time.get(attr, now) + 1.0 / self._update_freq
        self.set_now(attr, value)
        self._last_update_time[attr] = max(target_time, now)
        if target_time > now:
            await asyncio.sleep(target_time - now)

    async def move(self, qpos, *, max_velocity=0.5, min_duration=0.5):
        """Raised-cosine move from the current target with velocity feedforward."""
        self._raise_if_stopped()
        goal = _vector(qpos, "qpos")
        _check_within_limits(goal, "qpos")
        with self._lock:
            start = list(self.q_desired)
        distance = max(abs(b - a) for a, b in zip(start, goal))
        duration = max(min_duration, 0.5 * math.pi * distance / max_velocity)
        done = threading.Event()
        with self._lock:
            self._motion = (time.perf_counter_ns(), duration, start, goal, done)
        while not done.is_set():
            self._raise_if_stopped()
            await asyncio.sleep(0.01)

    async def wait(self, seconds):
        """Sleep while the loop holds the target; raise if the controller stops."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._raise_if_stopped()
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        self._raise_if_stopped()

    def _raise_if_stopped(self):
        if self.error is not None:
            raise WujiException(f"controller stopped: {self.error}")
        if self._shut_down:
            raise WujiException("controller is stopped")

    # ---- 1 kHz loop ---------------------------------------------------------

    def _publish_state(self, now, seq, stamp, q, qd, effort):
        self.state = {"qpos": q, "qvel": qd, "effort": effort, "seq": seq,
                      "timestamp_us": stamp, "host_ns": now}

    def _fail(self, reason):
        if self.error is None:
            self.error = reason
        if self._enable_requested:
            try:
                self.hand.disable()
            except Exception:
                pass  # stop() confirms and escalates to emergency stop.

    def _loop(self):
        period = round(1e9 / self.rate_hz)
        stale_ns = self.stale_ms * 1e6
        deadline = previous = time.perf_counter_ns()
        last_frame = self.state["host_ns"]
        tick = 0
        try:
            while not self._stop.is_set():
                now = time.perf_counter_ns()
                if deadline > now:
                    time.sleep((deadline - now) / 1e9)
                    now = time.perf_counter_ns()
                if now - deadline >= period:
                    self.stats["overruns"] += 1
                    deadline = now  # shift the schedule; never send a catch-up burst
                deadline += period
                self.stats["max_interval_ms"] = max(self.stats["max_interval_ms"], (now - previous) / 1e6)
                previous = now

                payload = self._states.drain()
                if payload is not None:
                    self._publish_state(now, *decode_joint_states(payload))
                    last_frame = now
                if tick % 10 == 0:
                    diagnostic = self._diagnostics.drain()
                    if diagnostic is not None:
                        self._diagnostic = (now, _codec.decode(332, diagnostic))
                    faults = diagnostic_faults(self._diagnostic[1], require_enabled=self._enabled)
                    if faults:
                        return self._fail(f"joint fault: {faults}")
                self.stats["lagged_frames"] = self._states.lagged_frames + self._diagnostics.lagged_frames

                with self._lock:
                    motion = self._motion
                    if motion is not None:
                        started, duration, q_from, q_to, done = motion
                        q_des, qd_des, finished = cosine_segment(q_from, q_to, duration, (now - started) / 1e9)
                        self.q_desired, self.qd_desired = q_des, qd_des
                        if finished:
                            self._motion = None
                            done.set()
                    q_des, qd_des, effort_ff = self.q_desired, self.qd_desired, self.effort_ff

                q = self.state["qpos"]
                if now - last_frame > stale_ns:
                    return self._fail(f"no joint_states for {(now - last_frame) / 1e6:.1f} ms")
                if None in q:
                    return self._fail("a joint is missing from joint_states")
                error = max(abs(a - b) for a, b in zip(q_des, q))
                if self._enabled:
                    self.stats["max_tracking_error_rad"] = max(self.stats["max_tracking_error_rad"], error)
                    if error > self.max_error:
                        return self._fail(f"tracking error {error:.3f} rad exceeds {self.max_error:.3f}")
                if self._sender is not None:
                    self._sender.send(q_des, qd_des, effort_ff)
                    self.stats["sends"] += 1
                tick += 1
                self.stats["ticks"] = tick
        except Exception as error:
            self._fail(f"control loop error: {error}")


async def _run(args):
    motion = args.confirm_motion is not None
    if motion and (args.sn is None or args.sn != args.confirm_motion):
        raise SystemExit("--confirm-motion must repeat the serial passed as the first argument")
    if args.joint is not None and not 0 <= args.joint < JOINTS:
        raise SystemExit(f"--joint must be within [0, {JOINTS - 1}]")
    controller = WujiController(args.sn, kp=args.kp, kd=args.kd, effort_limit=args.effort_limit,
                                dry_run=not motion)
    print(("MOTION" if motion else "DRY RUN (no enable, no commands)") +
          f": kp={args.kp} kd={args.kd} effort_limit={args.effort_limit} A")
    try:
        await controller.start()
        print(f"Connected {controller.sn}; initial qpos "
              f"{[round(v, 3) for v in controller.initial_qpos]}")
        if args.joint is not None:
            target = list(controller.initial_qpos)
            target[args.joint] += math.radians(args.delta_deg)
            await controller.move(target)
            await controller.wait(1.0)
            await controller.move(controller.initial_qpos)
        await controller.wait(args.hold)
    finally:
        await controller.stop()
        print(f"Loop stats: {controller.stats}")
    return controller


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sn", nargs="?", help="Hand 2 serial; required for motion")
    parser.add_argument("--confirm-motion", metavar="SERIAL",
                        help="enable motors and stream commands (repeat the serial)")
    parser.add_argument("--kp", type=float, default=3.0, help="A/rad (default 3.0)")
    parser.add_argument("--kd", type=float, default=0.05, help="A*s/rad (default 0.05)")
    parser.add_argument("--effort-limit", type=float, default=0.5, help="A (default 0.5)")
    parser.add_argument("--joint", type=int, help="move this joint (0-19) by --delta-deg and back")
    parser.add_argument("--delta-deg", type=float, default=5.0)
    parser.add_argument("--hold", type=float, default=3.0, help="seconds to hold at the end")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (WujiException, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
