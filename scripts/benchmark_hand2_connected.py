#!/usr/bin/env python3
"""Benchmark a connected WUJI Hand 2 without conflating clock domains.

The default ``observe`` mode is read-only: it samples the hand communication
diagnostics and records the joint-diagnostics stream without enabling motors,
opening a command publisher, or changing the device stream rate.

The ``opposition`` mode runs the same recorded trajectory and scheduling policy
as the normal opposition example.  The ``sine`` mode centers a small, smoothly
ramped sine wave on the freshly measured pose.  Both active modes are
intentionally gated by an exact serial-number confirmation because they enable
and move the physical hand.

Results are buffered in memory during the measured phase and written after the
hand is disabled.  Host timestamps describe Python scheduling and SDK return
times.  Device timestamps and counters describe telemetry.  Neither is a
firmware acknowledgement that a particular command was applied.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from enum import Enum
import gc
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import signal
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
UINT32_HALF = 1 << 31
FINGER_NAMES = ("thumb", "index", "middle", "ring", "pinky")
ACTIVE_MODES = frozenset(("opposition", "sine"))
DIAGNOSTIC_FAILURE_COUNTERS = (
    "e2e_lost", "e2e_reordered", "e2e_duplicates", "sdk_dropped",
    "rpc_retries", "rpc_timeouts", "comm_get_failures",
)
SINE_SAFE_KP = 3.0
SINE_SAFE_KD = 0.05
SINE_SAFE_EFFORT_LIMIT_A = 0.5
SINE_STATIONARY_MAX_VELOCITY_RAD_S = 0.1
SINE_MAX_VELOCITY_RAD_S = 0.55
SINE_MAX_EXCURSION_RAD = math.radians(7.0)
SINE_JOINT_LIMIT_GUARD_RAD = math.radians(5.0)
SINE_MIN_SEND_INTERVAL_RATIO = 0.8
# Hand 2 uses five NID slots per finger bus, with joints in slots 0..3.  The
# documented dense command index is ``bus * 4 + slot``.  Keep the sparse NID
# check explicit because telemetry frames can omit offline joints.
EXPECTED_MOTOR_NIDS = tuple(
    bus * 5 + slot + 1 for bus in range(5) for slot in range(4)
)
NID_TO_DENSE_INDEX = {
    nid: dense_index for dense_index, nid in enumerate(EXPECTED_MOTOR_NIDS)
}
NID_MAPPING_SOURCE = "https://docs.wuji.tech/docs/en/wuji-hand/latest/quick-start/"
JOINT_LIMITS_SOURCE = "https://docs.wuji.tech/docs/en/wuji-hand/latest/control-guide/"
_THUMB_LIMITS_DEG = ((-68, 74), (-85, 40), (-60, 90), (-60, 90))
_FINGER_LIMITS_DEG = ((-60, 90), (-40, 40), (-60, 120), (-60, 90))
JOINT_LIMITS_RAD = tuple(
    (math.radians(low), math.radians(high))
    for low, high in (_THUMB_LIMITS_DEG + _FINGER_LIMITS_DEG * 4)
)
LAG_PATTERN = re.compile(r"lagged by (\d+) frame", re.IGNORECASE)
STOP_REQUESTED = threading.Event()


def _percentile(sorted_values, quantile):
    position = (len(sorted_values) - 1) * quantile
    index = int(position)
    fraction = position - index
    upper = min(index + 1, len(sorted_values) - 1)
    return sorted_values[index] * (1.0 - fraction) + sorted_values[upper] * fraction


def distribution_ns(values):
    """Return a millisecond distribution for integer nanosecond samples."""
    values = sorted(int(value) for value in values)
    if not values:
        return None
    result = {
        "count": len(values),
        "mean_ms": sum(values) / len(values) / 1e6,
        "min_ms": values[0] / 1e6,
        "p1_ms": _percentile(values, 0.01) / 1e6,
        "p50_ms": _percentile(values, 0.50) / 1e6,
        "p90_ms": _percentile(values, 0.90) / 1e6,
        "p95_ms": _percentile(values, 0.95) / 1e6,
        "p99_ms": _percentile(values, 0.99) / 1e6,
        "p999_ms": _percentile(values, 0.999) / 1e6,
        "max_ms": values[-1] / 1e6,
    }
    result.update({
        "under_0_5_ms": sum(value < 500_000 for value in values),
        "under_0_8_ms": sum(value < 800_000 for value in values),
        "over_1_2_ms": sum(value > 1_200_000 for value in values),
        "over_1_5_ms": sum(value > 1_500_000 for value in values),
        "over_2_ms": sum(value > 2_000_000 for value in values),
        "over_5_ms": sum(value > 5_000_000 for value in values),
    })
    return result


def signed_distribution_ns(values):
    """Return signed millisecond quantiles plus absolute residual statistics."""
    values = sorted(int(value) for value in values)
    if not values:
        return None
    return {
        "count": len(values),
        "mean_ms": sum(values) / len(values) / 1e6,
        "min_ms": values[0] / 1e6,
        "p1_ms": _percentile(values, 0.01) / 1e6,
        "p50_ms": _percentile(values, 0.50) / 1e6,
        "p90_ms": _percentile(values, 0.90) / 1e6,
        "p95_ms": _percentile(values, 0.95) / 1e6,
        "p99_ms": _percentile(values, 0.99) / 1e6,
        "max_ms": values[-1] / 1e6,
        "absolute": distribution_ns(abs(value) for value in values),
    }


def cadence_ns(timestamps):
    """Summarize a series of monotonic nanosecond timestamps."""
    timestamps = [int(value) for value in timestamps]
    intervals = [after - before for before, after in zip(timestamps, timestamps[1:])]
    span = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
    return {
        "samples": len(timestamps),
        "span_s": span / 1e9,
        "rate_hz": (len(timestamps) - 1) * 1e9 / span if span > 0 else None,
        "intervals": distribution_ns(intervals),
    }


def _sequence_analysis(sequences):
    """Classify modulo-u32 transitions and retain per-sample classifications."""
    seen = set()
    missing = set()
    high = None
    classes = []
    counts = {
        "samples": 0,
        "unique_sequence_values": 0,
        "contiguous_transitions": 0,
        "forward_gap_events": 0,
        "missing_sequence_values": 0,
        "recovered_reordered_values": 0,
        "duplicate_transitions": 0,
        "reordered_transitions": 0,
        "backward_or_restart_events": 0,
        "u32_wrap_transitions": 0,
        "largest_forward_gap": 0,
    }
    for raw in sequences:
        sequence = int(raw) & 0xFFFFFFFF
        counts["samples"] += 1
        if high is None:
            high = sequence
            seen.add(sequence)
            counts["unique_sequence_values"] += 1
            classes.append({"class": "first", "delta": None, "estimated_missing": 0})
            continue
        if sequence in seen:
            counts["duplicate_transitions"] += 1
            classes.append({"class": "duplicate", "delta": 0, "estimated_missing": 0})
            continue
        delta = (sequence - high) & 0xFFFFFFFF
        if 0 < delta < UINT32_HALF:
            wrapped = sequence < high
            if wrapped:
                counts["u32_wrap_transitions"] += 1
            if delta == 1:
                counts["contiguous_transitions"] += 1
                label = "contiguous_wrap" if wrapped else "contiguous"
                estimated_missing = 0
            else:
                counts["forward_gap_events"] += 1
                counts["largest_forward_gap"] = max(counts["largest_forward_gap"], delta - 1)
                estimated_missing = delta - 1
                label = "forward_gap_wrap" if wrapped else "forward_gap"
                # Real benchmark gaps are expected to be small.  Bound set
                # expansion so corrupt/restarted sequence values cannot consume
                # unbounded memory; the aggregate count remains exact below.
                if estimated_missing <= 1_000_000:
                    for offset in range(1, delta):
                        missing.add((high + offset) & 0xFFFFFFFF)
            high = sequence
            seen.add(sequence)
            counts["unique_sequence_values"] += 1
            classes.append({"class": label, "delta": delta,
                            "estimated_missing": estimated_missing})
            continue
        counts["reordered_transitions"] += 1
        counts["backward_or_restart_events"] += 1
        recovered = sequence in missing
        if recovered:
            missing.remove(sequence)
            counts["recovered_reordered_values"] += 1
        seen.add(sequence)
        counts["unique_sequence_values"] += 1
        classes.append({"class": "reordered_recovery" if recovered else "backward_or_restart",
                        "delta": delta, "estimated_missing": 0})
    # If a gap was too large to materialize, reconstruct the unresolved count
    # from forward gaps and known recoveries.
    total_forward_missing = 0
    if sequences:
        previous = int(sequences[0]) & 0xFFFFFFFF
        observed = {previous}
        for raw in sequences[1:]:
            sequence = int(raw) & 0xFFFFFFFF
            if sequence in observed:
                continue
            delta = (sequence - previous) & 0xFFFFFFFF
            if 0 < delta < UINT32_HALF:
                total_forward_missing += max(0, delta - 1)
                previous = sequence
            observed.add(sequence)
    counts["missing_sequence_values"] = max(
        0, total_forward_missing - counts["recovered_reordered_values"]
    )
    return counts, classes


def sequence_summary(sequences):
    """Return aggregate modulo-u32 sequence continuity statistics."""
    return _sequence_analysis(list(sequences))[0]


def counter_change(before, after):
    """Return a counter delta, or None when reset/wrap is ambiguous."""
    if before is None or after is None:
        return None
    before, after = int(before), int(after)
    return after - before if after >= before else None


def to_plain(value):
    """Convert SDK value objects into JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    slots = getattr(type(value), "__slots__", ())
    if slots:
        return {name: to_plain(getattr(value, name)) for name in slots}
    return str(value)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


class Phase:
    def __init__(self):
        self.value = "setup"


class Receiver:
    """Drain one native subscription on its own thread."""

    def __init__(self, name, subscription, phase, abort, poll_us):
        self.name = name
        self.subscription = subscription
        self.phase = phase
        self.abort = abort
        self.poll_s = poll_us / 1e6
        self.records = []
        self.lag_events = []
        self.errors = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"benchmark-{name}", daemon=True)

    def start(self):
        self._thread.start()

    def _lag_since(self, previous):
        # recv() skips a backlog and counts it rather than raising.
        current = getattr(self.subscription, "lagged_frames", 0)
        if current > previous:
            self.lag_events.append({"phase": self.phase.value, "host_recv_ns": time.perf_counter_ns(),
                                    "lagged_frames": current - previous,
                                    "error": f"Stream lagged by {current - previous} frame(s)"})
        return current

    def _run(self):
        lagged = getattr(self.subscription, "lagged_frames", 0)
        while not self._stop.is_set() and not self.abort.is_set():
            entered = time.perf_counter_ns()
            try:
                frame = self.subscription.recv()
                lagged = self._lag_since(lagged)
            except Exception as error:
                finished = time.perf_counter_ns()
                text = str(error)
                match = LAG_PATTERN.search(text)
                if match:
                    self.lag_events.append({
                        "phase": self.phase.value,
                        "host_recv_ns": finished,
                        "lagged_frames": int(match.group(1)),
                        "error": text,
                    })
                    continue
                self.errors.append({"phase": self.phase.value, "host_recv_ns": finished,
                                    "error": text})
                self.abort.set()
                break
            finished = time.perf_counter_ns()
            if frame is None:
                self._stop.wait(self.poll_s)
                continue
            self.records.append((self.phase.value, finished, finished - entered, frame))

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        return not self._thread.is_alive()

    def latest(self):
        return self.records[-1][3] if self.records else None

    def latest_record(self):
        return self.records[-1] if self.records else None


class DiagnosticSampler:
    """Collect low-rate comm_diag GET snapshots away from the sender thread."""

    def __init__(self, hand, phase, abort, interval_s):
        self.hand = hand
        self.phase = phase
        self.abort = abort
        self.interval_s = interval_s
        self.records = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="benchmark-comm-diag", daemon=True)

    def sample(self, phase=None):
        entered = time.perf_counter_ns()
        try:
            value = to_plain(self.hand.comm_diag().get())
            error = None
        except Exception as exception:
            value, error = None, str(exception)
        finished = time.perf_counter_ns()
        record = {"phase": phase or self.phase.value, "host_start_ns": entered,
                  "host_return_ns": finished, "get_duration_ns": finished - entered,
                  "value": value, "error": error}
        self.records.append(record)
        return record

    def _run(self):
        while not self._stop.wait(self.interval_s):
            if self.abort.is_set():
                return
            self.sample()

    def start(self):
        if self.interval_s > 0:
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(5.0, self.interval_s + 1.0))
        return not self._thread.is_alive()


def _wait(seconds, abort):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if STOP_REQUESTED.is_set():
            raise InterruptedError("operator stop requested")
        if abort.is_set():
            raise RuntimeError("telemetry receiver stopped unexpectedly")
        abort.wait(min(0.05, max(0.0, deadline - time.monotonic())))


def _load_opposition_module():
    path = ROOT / "examples/python/wuji_hand_2/6_opposition/opposition.py"
    spec = importlib.util.spec_from_file_location("connected_benchmark_opposition", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def _inspect_replay(module, side, frame_limit, rate_hz, max_step_rad, *, retain_bytes=False):
    stream, total_frames = module._open_replay(side)
    replay_bytes = None
    if retain_bytes:
        try:
            stream.seek(0)
            replay_bytes = stream.read()
        finally:
            stream.close()
        expected_size = module.HEADER.size + total_frames * module.FRAME.size
        if len(replay_bytes) != expected_size:
            raise RuntimeError(
                f"captured replay is {len(replay_bytes)} bytes; expected {expected_size}")
        stream = io.BytesIO(replay_bytes)
        stream.seek(module.HEADER.size)
    count = total_frames if frame_limit is None else min(frame_limit, total_frames)
    first = previous = None
    max_step = 0.0
    minima = [math.inf] * 20
    maxima = [-math.inf] * 20
    try:
        for frame_index in range(count):
            values = tuple(float(value) for value in module._read_qpos(stream))
            if len(values) != 20 or not all(math.isfinite(value) for value in values):
                raise RuntimeError(
                    f"replay frame {frame_index} must contain exactly 20 finite positions")
            for dense_index, (value, (low, high)) in enumerate(zip(values, JOINT_LIMITS_RAD)):
                if not low <= value <= high:
                    raise RuntimeError(
                        f"replay frame {frame_index} joint {dense_index} (NID "
                        f"{EXPECTED_MOTOR_NIDS[dense_index]}) is {value:.6f} rad, outside "
                        f"documented [{low:.6f}, {high:.6f}] rad")
                minima[dense_index] = min(minima[dense_index], value)
                maxima[dense_index] = max(maxima[dense_index], value)
            if first is None:
                first = values
            if previous is not None:
                frame_step = max(abs(a - b) for a, b in zip(values, previous))
                if frame_step > max_step_rad:
                    raise RuntimeError(
                        f"replay frame {frame_index} step is {frame_step:.6f} rad; "
                        f"limit is {max_step_rad:.6f} rad")
                max_step = max(max_step, frame_step)
            previous = values
    finally:
        stream.close()
    result = {
        "total_frames": total_frames,
        "selected_frames": count,
        "first_positions": list(first or ()),
        "last_positions": list(previous or ()),
        "max_adjacent_step_rad": max_step,
        "max_implied_target_speed_rad_s": max_step * rate_hz,
        "position_minima_rad": minima,
        "position_maxima_rad": maxima,
        "joint_limits_rad": [list(limits) for limits in JOINT_LIMITS_RAD],
        "joint_limits_source": JOINT_LIMITS_SOURCE,
        "max_allowed_step_rad": max_step_rad,
    }
    if replay_bytes is not None:
        result["sha256"] = hashlib.sha256(replay_bytes).hexdigest()
        result["execution_source"] = "validated immutable in-memory replay snapshot"
        return result, replay_bytes
    return result


def _sine_envelope(time_s, duration_s, ramp_s):
    """Raised-cosine amplitude envelope with zero motion at both endpoints."""
    if ramp_s <= 0:
        return 1.0
    if time_s <= 0 or time_s >= duration_s:
        return 0.0
    if time_s < ramp_s:
        return 0.5 * (1.0 - math.cos(math.pi * time_s / ramp_s))
    if time_s > duration_s - ramp_s:
        remaining_s = duration_s - time_s
        return 0.5 * (1.0 - math.cos(math.pi * remaining_s / ramp_s))
    return 1.0


def _sine_positions(center_positions, frame_index, rate_hz, duration_s,
                    amplitude_rad, frequency_hz, ramp_s):
    time_s = frame_index / rate_hz
    envelope = _sine_envelope(time_s, duration_s, ramp_s)
    offset = amplitude_rad * envelope * math.sin(2.0 * math.pi * frequency_hz * time_s)
    return tuple(center + offset for center in center_positions)


class _SineStream:
    def __init__(self, trajectory):
        self.trajectory = trajectory
        self.index = 0
        self.closed = False

    def read_positions(self):
        if self.closed:
            raise RuntimeError("sine trajectory stream is closed")
        if self.index >= self.trajectory.total_frames:
            raise EOFError("sine trajectory is exhausted")
        positions = _sine_positions(
            self.trajectory.center_positions, self.index,
            self.trajectory.rate_hz, self.trajectory.duration_s,
            self.trajectory.amplitude_rad, self.trajectory.frequency_hz,
            self.trajectory.ramp_s)
        self.index += 1
        return positions

    def close(self):
        self.closed = True


class _SineTrajectory:
    """Replay-compatible deterministic in-memory sine trajectory."""

    def __init__(self, center_positions, rate_hz, duration_s, amplitude_rad,
                 frequency_hz, ramp_s, total_frames):
        self.center_positions = tuple(float(value) for value in center_positions)
        self.rate_hz = float(rate_hz)
        self.duration_s = float(duration_s)
        self.amplitude_rad = float(amplitude_rad)
        self.frequency_hz = float(frequency_hz)
        self.ramp_s = float(ramp_s)
        self.total_frames = int(total_frames)

    def _open_replay(self, _side):
        return _SineStream(self), self.total_frames

    @staticmethod
    def _read_qpos(stream):
        return stream.read_positions()


def _build_sine_trajectory(center_positions, rate_hz, duration_s, amplitude_deg,
                           frequency_hz, ramp_s, max_step_rad,
                           joint_limit_guard_rad=0.0):
    center_positions = tuple(float(value) for value in center_positions)
    if len(center_positions) != 20 or not all(math.isfinite(value)
                                               for value in center_positions):
        raise RuntimeError("sine center must contain exactly 20 finite joint positions")
    amplitude_rad = math.radians(amplitude_deg)
    for dense_index, (center, (low, high)) in enumerate(
            zip(center_positions, JOINT_LIMITS_RAD)):
        target_low = center - amplitude_rad - joint_limit_guard_rad
        target_high = center + amplitude_rad + joint_limit_guard_rad
        if target_low < low or target_high > high:
            raise RuntimeError(
                f"sine envelope plus {joint_limit_guard_rad:.6f}-rad guard for joint "
                f"{dense_index} (NID "
                f"{EXPECTED_MOTOR_NIDS[dense_index]}) would be "
                f"[{target_low:.6f}, {target_high:.6f}] rad, outside documented "
                f"[{low:.6f}, {high:.6f}] rad")
    interval_count_float = duration_s * rate_hz
    interval_count = round(interval_count_float)
    if not math.isclose(interval_count_float, interval_count, rel_tol=0.0, abs_tol=1e-7):
        raise RuntimeError(
            "sine duration multiplied by command rate must be an integer number of intervals")
    total_frames = interval_count + 1
    trajectory = _SineTrajectory(
        center_positions, rate_hz, duration_s, amplitude_rad,
        frequency_hz, ramp_s, total_frames)
    minima = [math.inf] * 20
    maxima = [-math.inf] * 20
    peak_offsets = [0.0] * 20
    first = previous = last = None
    max_step = 0.0
    for frame_index in range(total_frames):
        values = _sine_positions(
            center_positions, frame_index, rate_hz, duration_s,
            amplitude_rad, frequency_hz, ramp_s)
        for dense_index, (value, center, (low, high)) in enumerate(
                zip(values, center_positions, JOINT_LIMITS_RAD)):
            if not math.isfinite(value) or not low <= value <= high:
                raise RuntimeError(
                    f"sine frame {frame_index} joint {dense_index} (NID "
                    f"{EXPECTED_MOTOR_NIDS[dense_index]}) is outside its documented range")
            minima[dense_index] = min(minima[dense_index], value)
            maxima[dense_index] = max(maxima[dense_index], value)
            peak_offsets[dense_index] = max(peak_offsets[dense_index], abs(value - center))
        if first is None:
            first = values
        if previous is not None:
            frame_step = max(abs(a - b) for a, b in zip(values, previous))
            if frame_step > max_step_rad:
                raise RuntimeError(
                    f"sine frame {frame_index} step is {frame_step:.6f} rad; "
                    f"limit is {max_step_rad:.6f} rad")
            max_step = max(max_step, frame_step)
        previous = last = values
    if max(abs(value - center) for value, center in zip(first, center_positions)) > 1e-12:
        raise RuntimeError("sine trajectory does not start at the measured center pose")
    if max(abs(value - center) for value, center in zip(last, center_positions)) > 1e-12:
        raise RuntimeError("sine trajectory does not return to the measured center pose")
    max_nominal_speed = max_step * rate_hz
    max_bounded_catchup_speed = max_nominal_speed / SINE_MIN_SEND_INTERVAL_RATIO
    if max_bounded_catchup_speed > SINE_MAX_VELOCITY_RAD_S:
        raise RuntimeError(
            f"sine target speed can reach {max_bounded_catchup_speed:.6f} rad/s at "
            f"the minimum allowed send interval; safety limit is "
            f"{SINE_MAX_VELOCITY_RAD_S:.6f} rad/s")
    info = {
        "type": "sine",
        "selected_frames": total_frames,
        "total_frames": total_frames,
        "interval_count": interval_count,
        "duration_s": duration_s,
        "rate_hz": rate_hz,
        "amplitude_deg_peak": amplitude_deg,
        "amplitude_rad_peak": amplitude_rad,
        "peak_to_peak_deg": 2.0 * amplitude_deg,
        "frequency_hz": frequency_hz,
        "ramp_s": ramp_s,
        "phase": "all joints in phase",
        "center_source": "immutable snapshot of fresh post-countdown joint_states",
        "center_positions_rad": list(center_positions),
        "first_positions": list(first),
        "last_positions": list(last),
        "position_minima_rad": minima,
        "position_maxima_rad": maxima,
        "achieved_target_peak_offsets_rad": peak_offsets,
        "max_adjacent_step_rad": max_step,
        "max_implied_target_speed_at_nominal_rate_rad_s": max_nominal_speed,
        "max_implied_target_speed_at_minimum_send_interval_rad_s": (
            max_bounded_catchup_speed),
        "max_allowed_target_speed_rad_s": SINE_MAX_VELOCITY_RAD_S,
        "joint_limits_rad": [list(limits) for limits in JOINT_LIMITS_RAD],
        "joint_limits_source": JOINT_LIMITS_SOURCE,
        "joint_limit_guard_rad": joint_limit_guard_rad,
        "max_allowed_step_rad": max_step_rad,
        "inclusive_endpoint_note": (
            "frames include t=0 and t=duration; interval_count is frames minus one"),
    }
    return trajectory, info


def _joints_by_nid(frame):
    joints = {int(joint.nid): joint for joint in frame.joints}
    observed = set(joints)
    expected = set(EXPECTED_MOTOR_NIDS)
    if int(frame.num_joints) != 20 or len(joints) != 20 or observed != expected:
        raise RuntimeError(
            "telemetry must contain the exact 20 motor NIDs; "
            f"expected {list(EXPECTED_MOTOR_NIDS)}, observed {sorted(observed)}")
    return joints


def _joints_in_dense_command_order(frame):
    joints = _joints_by_nid(frame)
    ordered = [None] * 20
    for nid, joint in joints.items():
        ordered[NID_TO_DENSE_INDEX[nid]] = joint
    return ordered


def _positions_in_dense_command_order(frame):
    return [float(joint.position) for joint in _joints_in_dense_command_order(frame)]


def _validated_state_positions(frame, joint_limit_margin_rad=0.05):
    positions = _positions_in_dense_command_order(frame)
    for dense_index, (position, (low, high)) in enumerate(zip(positions, JOINT_LIMITS_RAD)):
        if not math.isfinite(position):
            raise RuntimeError(
                f"joint state {dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]}) "
                "is not finite")
        if not low - joint_limit_margin_rad <= position <= high + joint_limit_margin_rad:
            raise RuntimeError(
                f"joint state {dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]}) is "
                f"{position:.6f} rad, outside the documented range plus "
                f"{joint_limit_margin_rad:.3f} rad measurement margin")
    return positions


def _validated_stationary_state(frame, max_velocity_rad_s=0.1):
    positions = _validated_state_positions(frame)
    velocities = [float(joint.velocity) for joint in _joints_in_dense_command_order(frame)]
    for dense_index, velocity in enumerate(velocities):
        if not math.isfinite(velocity):
            raise RuntimeError(
                f"joint velocity {dense_index} (NID "
                f"{EXPECTED_MOTOR_NIDS[dense_index]}) is not finite")
        if abs(velocity) > max_velocity_rad_s:
            raise RuntimeError(
                f"joint {dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]}) is moving at "
                f"{velocity:.6f} rad/s; sine preflight limit is "
                f"{max_velocity_rad_s:.6f} rad/s")
    return positions, velocities


def _wait_for_fresh_record(receiver, previous_seq, abort, *, timeout_s=1.0, max_age_ms=100.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if STOP_REQUESTED.is_set():
            raise InterruptedError("operator stop requested")
        if abort.is_set():
            raise RuntimeError(f"{receiver.name} receiver stopped unexpectedly")
        record = receiver.latest_record()
        if record is not None:
            age_ms = (time.perf_counter_ns() - record[1]) / 1e6
            if int(record[3].header.seq) != int(previous_seq) and age_ms <= max_age_ms:
                return record, age_ms
        time.sleep(0.005)
    raise RuntimeError(
        f"timed out waiting for fresh {receiver.name} telemetry no older than {max_age_ms:g} ms")


def _validate_active_frames(diagnostic_frame, state_frame, first_positions, max_initial_error_rad):
    diagnostic_joints = _joints_by_nid(diagnostic_frame)
    state_positions = _validated_state_positions(state_frame)
    faults = _diagnostic_faults(diagnostic_frame)
    if faults:
        raise RuntimeError(f"active benchmark refused due to joint faults/limits: {faults}")
    if len(first_positions) != 20 or not all(math.isfinite(float(value))
                                              for value in first_positions):
        raise RuntimeError("active trajectory must contain exactly 20 finite starting positions")
    errors = [abs(float(target) - current)
              for target, current in zip(first_positions, state_positions)]
    max_error = max(errors)
    max_error_index = errors.index(max_error)
    if max_error > max_initial_error_rad:
        raise RuntimeError(
            f"first-command error is {max_error:.3f} rad at dense joint "
            f"{max_error_index} (NID {EXPECTED_MOTOR_NIDS[max_error_index]}); "
            f"limit is {max_initial_error_rad:.3f} rad")
    return {
        "diagnostic_nids": sorted(diagnostic_joints),
        "state_positions_dense_order": state_positions,
        "dense_command_nids": list(EXPECTED_MOTOR_NIDS),
        "dense_command_to_nid_mapping_verified": True,
        "dense_command_to_nid_mapping_source": NID_MAPPING_SOURCE,
        "max_initial_error_rad": max_error,
        "max_initial_error_dense_index": max_error_index,
        "max_initial_error_nid": EXPECTED_MOTOR_NIDS[max_error_index],
        "diagnostic_seq": int(diagnostic_frame.header.seq),
        "state_seq": int(state_frame.header.seq),
    }


def _watchdog_cursors_after(diagnostic_receiver, diagnostic_record,
                            state_receiver, state_record):
    def cursor_after(receiver, validated_record):
        for record_index in range(len(receiver.records) - 1, -1, -1):
            if receiver.records[record_index] is validated_record:
                return record_index + 1
        return len(receiver.records)

    return {
        "diagnostic": cursor_after(diagnostic_receiver, diagnostic_record),
        "state": cursor_after(state_receiver, state_record),
        "diagnostic_lag": len(getattr(diagnostic_receiver, "lag_events", [])),
        "state_lag": len(getattr(state_receiver, "lag_events", [])),
        "diagnostic_last_seq": int(diagnostic_record[3].header.seq),
        "diagnostic_last_timestamp_us": int(diagnostic_record[3].header.timestamp_us),
        "diagnostic_last_progress_ns": diagnostic_record[1],
        "state_last_seq": int(state_record[3].header.seq),
        "state_last_timestamp_us": int(state_record[3].header.timestamp_us),
        "state_last_progress_ns": state_record[1],
        "diagnostic_timeout_totals": {
            int(joint.nid): int(getattr(joint, "comm_timeout_total", 0))
            for joint in diagnostic_record[3].joints
        },
        "diagnostic_comm_counters": {
            name: int(getattr(getattr(diagnostic_record[3], "comm", None), name, 0))
            for name in DIAGNOSTIC_FAILURE_COUNTERS
        },
    }


def _motion_watchdog(diagnostic_receiver, state_receiver, max_age_ms, cursors=None,
                     expected_positions=None, max_tracking_error_rad=None,
                     require_enabled=True, require_ready=False,
                     min_response_rate_pct=80,
                     center_positions=None, max_excursion_rad=None,
                     max_velocity_rad_s=None):
    """Validate all unseen feedback while commands are being published."""
    if cursors is None:
        cursors = {"diagnostic": 0, "state": 0}
    diagnostic_record = diagnostic_receiver.latest_record()
    state_record = state_receiver.latest_record()
    if diagnostic_record is None or state_record is None:
        raise RuntimeError("motion watchdog has no telemetry record")
    for receiver, key in ((diagnostic_receiver, "diagnostic"),
                          (state_receiver, "state")):
        lag_events = getattr(receiver, "lag_events", [])
        lag_cursor_key = key + "_lag"
        lag_start = min(cursors.get(lag_cursor_key, 0), len(lag_events))
        if lag_events[lag_start:]:
            raise RuntimeError(
                f"motion watchdog observed native {key} receiver lag: "
                f"{lag_events[lag_start:]}")
    diagnostic_records = getattr(diagnostic_receiver, "records", None)
    state_records = getattr(state_receiver, "records", None)
    if diagnostic_records is None:
        unseen_diagnostics = [diagnostic_record]
        next_diagnostic = cursors.get("diagnostic", 0)
    else:
        next_diagnostic = len(diagnostic_records)
        start = min(cursors.get("diagnostic", 0), next_diagnostic)
        unseen_diagnostics = list(diagnostic_records[start:next_diagnostic])
    if state_records is None:
        unseen_states = [state_record]
        next_state = cursors.get("state", 0)
    else:
        next_state = len(state_records)
        start = min(cursors.get("state", 0), next_state)
        unseen_states = list(state_records[start:next_state])
    if unseen_diagnostics:
        diagnostic_record = unseen_diagnostics[-1]
    if unseen_states:
        state_record = unseen_states[-1]
    now = time.perf_counter_ns()

    def check_progress(key, records, latest_record):
        last_seq = cursors.get(key + "_last_seq")
        last_timestamp_us = cursors.get(key + "_last_timestamp_us")
        last_progress_ns = cursors.get(key + "_last_progress_ns")
        if last_seq is None:
            source_records = getattr(
                diagnostic_receiver if key == "diagnostic" else state_receiver,
                "records", None)
            start_index = cursors.get(key, 0)
            if source_records is not None and start_index > 0:
                baseline = source_records[start_index - 1]
                last_seq = int(baseline[3].header.seq)
                last_timestamp_us = int(baseline[3].header.timestamp_us)
                last_progress_ns = baseline[1]
        for record in records:
            sequence = int(record[3].header.seq)
            timestamp_us = int(record[3].header.timestamp_us)
            if last_seq is not None:
                delta = (sequence - last_seq) & 0xFFFFFFFF
                if delta != 1:
                    raise RuntimeError(
                        f"motion watchdog observed {key} sequence discontinuity: "
                        f"{last_seq} -> {sequence} (mod-u32 delta {delta})")
                if timestamp_us <= last_timestamp_us:
                    raise RuntimeError(
                        f"motion watchdog observed non-forward {key} device timestamp: "
                        f"{last_timestamp_us} -> {timestamp_us}")
            last_seq = sequence
            last_timestamp_us = timestamp_us
            last_progress_ns = record[1]
        if last_progress_ns is None:
            last_progress_ns = latest_record[1]
            last_seq = int(latest_record[3].header.seq)
            last_timestamp_us = int(latest_record[3].header.timestamp_us)
        progress_age_ms = (now - last_progress_ns) / 1e6
        if progress_age_ms > max_age_ms:
            raise RuntimeError(
                f"motion watchdog {key} stream is stale: no forward progress for "
                f"{progress_age_ms:.3f} ms; limit={max_age_ms:.3f} ms")
        return last_seq, last_timestamp_us, last_progress_ns, progress_age_ms

    diagnostic_progress = check_progress(
        "diagnostic", unseen_diagnostics, diagnostic_record)
    state_progress = check_progress("state", unseen_states, state_record)

    timeout_totals = dict(cursors.get("diagnostic_timeout_totals", {}))
    comm_counters = dict(cursors.get("diagnostic_comm_counters", {}))
    for record in unseen_diagnostics or [diagnostic_record]:
        frame = record[3]
        diagnostic_joints = _joints_by_nid(frame)
        enabled_nids = set(_enabled_nids(frame))
        if require_ready:
            not_ready = sorted(
                nid for nid, joint in diagnostic_joints.items()
                if int(joint.status_word.ext_state) != 1)
            if not_ready:
                raise RuntimeError(
                    "motion watchdog observed joints outside Ready state before active "
                    f"setup; NIDs: {not_ready}")
        elif require_enabled and enabled_nids != set(EXPECTED_MOTOR_NIDS):
            raise RuntimeError(
                "motion watchdog observed joints leave Enabled state; "
                f"enabled NIDs: {sorted(enabled_nids)}")
        faults = _diagnostic_faults(frame)
        if faults:
            raise RuntimeError(f"motion watchdog observed joint faults/limits: {faults}")
        for joint in frame.joints:
            nid = int(joint.nid)
            response_rate = int(getattr(joint, "comm_response_rate_pct", 100))
            if response_rate < min_response_rate_pct:
                raise RuntimeError(
                    f"motion watchdog observed NID {nid} response rate {response_rate}%; "
                    f"minimum is {min_response_rate_pct}%")
            timeout_total = int(getattr(joint, "comm_timeout_total", 0))
            previous_timeout_total = timeout_totals.get(nid, timeout_total)
            if timeout_total != previous_timeout_total:
                raise RuntimeError(
                    f"motion watchdog observed NID {nid} comm_timeout_total change: "
                    f"{previous_timeout_total} -> {timeout_total}")
            timeout_totals[nid] = timeout_total
        comm = getattr(frame, "comm", None)
        for name in DIAGNOSTIC_FAILURE_COUNTERS:
            value = int(getattr(comm, name, 0))
            previous = comm_counters.get(name, value)
            if value != previous:
                raise RuntimeError(
                    f"motion watchdog observed {name} counter change: "
                    f"{previous} -> {value}")
            comm_counters[name] = value
    max_measured_velocity = 0.0
    max_center_excursion = 0.0
    tracking_error = None
    tracking_error_index = None
    if expected_positions is not None and max_tracking_error_rad is not None:
        if (len(expected_positions) != 20
                or not all(math.isfinite(float(value)) for value in expected_positions)):
            raise RuntimeError(
                "motion watchdog expected target must contain 20 finite positions")
    for record in unseen_states or [state_record]:
        positions = _validated_state_positions(record[3])
        if max_velocity_rad_s is not None:
            velocities = [float(joint.velocity)
                          for joint in _joints_in_dense_command_order(record[3])]
            for dense_index, velocity in enumerate(velocities):
                if not math.isfinite(velocity):
                    raise RuntimeError(
                        f"motion watchdog observed non-finite velocity at dense joint "
                        f"{dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]})")
                max_measured_velocity = max(max_measured_velocity, abs(velocity))
                if abs(velocity) > max_velocity_rad_s:
                    raise RuntimeError(
                        f"motion watchdog velocity is {velocity:.3f} rad/s at dense joint "
                        f"{dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]}); "
                        f"limit is {max_velocity_rad_s:.3f} rad/s")
        if center_positions is not None and max_excursion_rad is not None:
            excursions = [abs(position - float(center))
                          for position, center in zip(positions, center_positions)]
            frame_excursion = max(excursions)
            max_center_excursion = max(max_center_excursion, frame_excursion)
            if frame_excursion > max_excursion_rad:
                dense_index = excursions.index(frame_excursion)
                raise RuntimeError(
                    f"motion watchdog center excursion is {frame_excursion:.3f} rad at "
                    f"dense joint {dense_index} (NID "
                    f"{EXPECTED_MOTOR_NIDS[dense_index]}); limit is "
                    f"{max_excursion_rad:.3f} rad")
        if expected_positions is not None and max_tracking_error_rad is not None:
            errors = [abs(float(target) - actual)
                      for target, actual in zip(expected_positions, positions)]
            frame_tracking_error = max(errors)
            frame_tracking_index = errors.index(frame_tracking_error)
            if tracking_error is None or frame_tracking_error > tracking_error:
                tracking_error = frame_tracking_error
                tracking_error_index = frame_tracking_index
            if frame_tracking_error > max_tracking_error_rad:
                raise RuntimeError(
                    f"motion watchdog tracking error is {frame_tracking_error:.3f} rad at "
                    f"dense joint {frame_tracking_index} (NID "
                    f"{EXPECTED_MOTOR_NIDS[frame_tracking_index]}); limit is "
                    f"{max_tracking_error_rad:.3f} rad")

    diagnostic_frame = diagnostic_record[3]
    state_frame = state_record[3]
    cursors.update({
        "diagnostic": next_diagnostic,
        "state": next_state,
        "diagnostic_lag": len(getattr(diagnostic_receiver, "lag_events", [])),
        "state_lag": len(getattr(state_receiver, "lag_events", [])),
        "diagnostic_last_seq": diagnostic_progress[0],
        "diagnostic_last_timestamp_us": diagnostic_progress[1],
        "diagnostic_last_progress_ns": diagnostic_progress[2],
        "state_last_seq": state_progress[0],
        "state_last_timestamp_us": state_progress[1],
        "state_last_progress_ns": state_progress[2],
        "diagnostic_timeout_totals": timeout_totals,
        "diagnostic_comm_counters": comm_counters,
    })
    return {
        "diagnostic_seq": int(diagnostic_frame.header.seq),
        "state_seq": int(state_frame.header.seq),
        "diagnostic_age_ms": diagnostic_progress[3],
        "state_age_ms": state_progress[3],
        "diagnostic_frames_checked": len(unseen_diagnostics),
        "state_frames_checked": len(unseen_states),
        "tracking_error_rad": tracking_error,
        "tracking_error_dense_index": tracking_error_index,
        "max_measured_velocity_rad_s": max_measured_velocity,
        "max_center_excursion_rad": max_center_excursion,
    }


def _disable_and_confirm(hand, diagnostic_receiver, cleanup, operation, timeout_s=2.0,
                         poll_s=0.005, max_age_ms=100.0):
    """Request disable, then require a fresh full diagnostic frame confirming it."""
    started = time.perf_counter_ns()
    before = diagnostic_receiver.latest_record() if diagnostic_receiver is not None else None
    before_seq = int(before[3].header.seq) if before is not None else None
    before_count = (len(diagnostic_receiver.records)
                    if diagnostic_receiver is not None
                    and getattr(diagnostic_receiver, "records", None) is not None else None)
    request = {"operation": operation + "_request", "ok": False,
               "requested_at_ns": started}
    try:
        hand.disable()
        disable_return_ns = time.perf_counter_ns()
        request.update({"ok": True, "disable_call_duration_ns": disable_return_ns - started})
    except Exception as error:
        disable_return_ns = time.perf_counter_ns()
        request.update({"error": f"disable call failed: {error}",
                        "disable_call_duration_ns": disable_return_ns - started})
    cleanup.append(request)
    confirmation = {"operation": operation + "_telemetry_confirmation", "ok": False,
                    "confirmed_disabled": False}
    if diagnostic_receiver is None:
        confirmation["error"] = "no diagnostic receiver exists to confirm disable"
        cleanup.append(confirmation)
        return False

    deadline = time.monotonic() + timeout_s
    last_enabled_nids = None
    last_validation_error = None
    while time.monotonic() < deadline:
        records = getattr(diagnostic_receiver, "records", None)
        if records is not None and before_count is not None:
            candidates = list(records[before_count:])
        else:
            latest = diagnostic_receiver.latest_record()
            candidates = [latest] if latest is not None else []
        # Confirm the newest post-request sample, not an earlier transient
        # disabled state that may already have returned to Enabled.
        for record in candidates[-1:]:
            frame = record[3]
            sequence = int(frame.header.seq)
            age_ms = (time.perf_counter_ns() - record[1]) / 1e6
            if ((before_seq is None or sequence != before_seq)
                    and record[1] >= started
                    and age_ms <= max_age_ms):
                try:
                    _joints_by_nid(frame)
                    last_validation_error = None
                except RuntimeError as error:
                    last_validation_error = str(error)
                else:
                    last_enabled_nids = sorted(_enabled_nids(frame))
                    if not last_enabled_nids:
                        finished = time.perf_counter_ns()
                        confirmation.update({
                            "ok": True,
                            "confirmed_disabled": True,
                            "confirmation_seq": sequence,
                            "confirmation_age_ms": age_ms,
                            "total_duration_ns": finished - started,
                        })
                        cleanup.append(confirmation)
                        return True
        time.sleep(poll_s)
    confirmation.update({
        "last_enabled_nids": last_enabled_nids,
        "last_validation_error": last_validation_error,
        "total_duration_ns": time.perf_counter_ns() - started,
        "error": "fresh telemetry did not confirm every motor disabled",
    })
    cleanup.append(confirmation)
    return False


def _diagnostic_faults(frame):
    faults = []
    for joint in frame.joints:
        status = joint.status_word
        if (joint.error_code_current or status.ext_state == 3 or status.position_limit_active
                or status.velocity_limit_active or status.current_limit_active):
            faults.append({
                "nid": int(joint.nid),
                "error_code_current": int(joint.error_code_current),
                "ext_state": int(status.ext_state),
                "ext_state_name": status.ext_state_name,
                "position_limit_active": bool(status.position_limit_active),
                "velocity_limit_active": bool(status.velocity_limit_active),
                "current_limit_active": bool(status.current_limit_active),
            })
    return faults


def _enabled_nids(frame):
    return [int(joint.nid) for joint in frame.joints if joint.status_word.ext_state == 2]


def _require_all_ready(frame, context):
    """Refuse active setup unless every expected motor is explicitly Ready."""
    joints = _joints_by_nid(frame)
    not_ready = [
        {
            "nid": nid,
            "ext_state": int(joint.status_word.ext_state),
            "ext_state_name": str(joint.status_word.ext_state_name),
        }
        for nid, joint in sorted(joints.items())
        if int(joint.status_word.ext_state) != 1
    ]
    if not_ready:
        raise RuntimeError(
            f"{context} requires every motor in Ready state before active setup; "
            f"not ready: {not_ready}")
    return {
        "ready_nids": sorted(joints),
        "all_20_ready": True,
        "diagnostic_seq": int(frame.header.seq),
    }


def _capture_controller_settings(hand):
    effort = hand.effort_limit().get()
    mit = hand.mit_params().get()
    if (len(effort) != 20
            or any(value is None or not math.isfinite(float(value))
                   or float(value) < 0.0 for value in effort)):
        raise RuntimeError(
            "cannot safely restore sine controller: effort_limit GET must return "
            "20 finite nonnegative values")
    if (len(mit) != 20
            or any(value is None
                   or not math.isfinite(float(value.kp))
                   or not math.isfinite(float(value.kd))
                   or float(value.kp) < 0.0
                   or float(value.kd) < 0.0 for value in mit)):
        raise RuntimeError(
            "cannot safely restore sine controller: mit_params GET must return "
            "20 finite nonnegative kp/kd pairs")
    return {
        "effort_limit": [float(value) for value in effort],
        "mit_params": [(float(value.kp), float(value.kd)) for value in mit],
    }


def _apply_sine_controller_settings(hand):
    hand.effort_limit().set(SINE_SAFE_EFFORT_LIMIT_A)
    hand.mit_params().set((SINE_SAFE_KP, SINE_SAFE_KD))
    effort = hand.effort_limit().get()
    mit = hand.mit_params().get()
    effort_ok = (len(effort) == 20 and all(
        value is not None
        and math.isclose(float(value), SINE_SAFE_EFFORT_LIMIT_A,
                         rel_tol=0.0, abs_tol=1e-5)
        for value in effort))
    mit_ok = (len(mit) == 20 and all(
        value is not None
        and math.isclose(float(value.kp), SINE_SAFE_KP, rel_tol=0.0, abs_tol=1e-5)
        and math.isclose(float(value.kd), SINE_SAFE_KD, rel_tol=0.0, abs_tol=1e-5)
        for value in mit))
    if not effort_ok or not mit_ok:
        raise RuntimeError(
            "sine controller safety write did not verify on all 20 joints "
            f"(effort_ok={effort_ok}, mit_ok={mit_ok})")
    return {
        "effort_limit_a": SINE_SAFE_EFFORT_LIMIT_A,
        "kp": SINE_SAFE_KP,
        "kd": SINE_SAFE_KD,
        "source": JOINT_LIMITS_SOURCE,
        "readback_verified_all_20": True,
    }


def _restore_controller_settings(hand, settings):
    hand.mit_params().set(settings["mit_params"])
    hand.effort_limit().set(settings["effort_limit"])
    restored_mit = hand.mit_params().get()
    restored_effort = hand.effort_limit().get()
    mit_ok = (len(restored_mit) == 20 and all(
        value is not None
        and math.isclose(float(value.kp), expected[0], rel_tol=0.0, abs_tol=1e-5)
        and math.isclose(float(value.kd), expected[1], rel_tol=0.0, abs_tol=1e-5)
        for value, expected in zip(restored_mit, settings["mit_params"])))
    effort_ok = (len(restored_effort) == 20 and all(
        value is not None
        and math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-5)
        for value, expected in zip(restored_effort, settings["effort_limit"])))
    if not mit_ok or not effort_ok:
        raise RuntimeError(
            f"controller restore readback mismatch (mit_ok={mit_ok}, effort_ok={effort_ok})")
    return {"readback_verified_all_20": True}


def _run_opposition(args, sdk, hand, module, side, abort, phase,
                    diagnostic_receiver, state_receiver, records, cleanup,
                    active_metadata, first_positions, replay_bytes=None,
                    watchdog_cursors=None):
    period_ns = round(1e9 / args.rate_hz)
    if replay_bytes is None:
        stream, total_frames = module._open_replay(side)
    else:
        stream = io.BytesIO(replay_bytes)
        header = module.HEADER.unpack(replay_bytes[:module.HEADER.size])
        total_frames = int(header[4])
        stream.seek(module.HEADER.size)
    count = total_frames if args.frames is None else min(args.frames, total_frames)
    max_command_lateness_ms = getattr(args, "max_command_lateness_ms", None)
    max_send_call_ms = getattr(args, "max_send_call_ms", None)
    publisher = None
    enable_attempted = False
    try:
        diagnostic_before_record = diagnostic_receiver.latest_record()
        state_before_record = state_receiver.latest_record()
        if watchdog_cursors is None:
            watchdog_cursors = _watchdog_cursors_after(
                diagnostic_receiver, diagnostic_before_record,
                state_receiver, state_before_record)
        else:
            # Continue the cursor established before the countdown.  Re-seeding
            # it from a newer latest frame would leave a race in which an
            # intervening fault or cumulative timeout increment is never
            # validated.
            active_metadata["pre_enable_handoff_telemetry_check"] = _motion_watchdog(
                diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                watchdog_cursors, require_enabled=False,
                require_ready=True,
                min_response_rate_pct=args.min_joint_response_rate_pct,
                center_positions=(first_positions
                                  if getattr(args, "mode", "opposition") == "sine" else None),
                max_excursion_rad=(args.max_initial_error_rad
                                   if getattr(args, "mode", "opposition") == "sine" else None),
                max_velocity_rad_s=(SINE_STATIONARY_MAX_VELOCITY_RAD_S
                                    if getattr(args, "mode", "opposition") == "sine" else None))
            diagnostic_before_record = diagnostic_receiver.latest_record()
            state_before_record = state_receiver.latest_record()
        diagnostic_before = diagnostic_before_record[3]
        state_before = state_before_record[3]
        active_metadata["immediately_pre_enable_ready_check"] = _require_all_ready(
            diagnostic_before, "active benchmark")
        diagnostic_seq_before = int(diagnostic_before.header.seq)
        state_seq_before = int(state_before.header.seq)
        enable_transition_cursors = watchdog_cursors
        if STOP_REQUESTED.is_set():
            raise InterruptedError("operator stop requested before enable")
        if abort.is_set():
            raise RuntimeError("telemetry receiver stopped unexpectedly before enable")
        enable_start = time.perf_counter_ns()
        enable_attempted = True
        hand.enable()
        active_metadata["enable_call_duration_ns"] = time.perf_counter_ns() - enable_start
        enabled_deadline = time.monotonic() + 5.0
        enable_wait_watchdog_checks = 0
        while time.monotonic() < enabled_deadline:
            if STOP_REQUESTED.is_set():
                raise InterruptedError("operator stop requested")
            if abort.is_set():
                raise RuntimeError("telemetry receiver stopped unexpectedly while enabling")
            transition_sample = _motion_watchdog(
                diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                enable_transition_cursors, require_enabled=False,
                min_response_rate_pct=args.min_joint_response_rate_pct,
                center_positions=(first_positions
                                  if getattr(args, "mode", "opposition") == "sine" else None),
                max_excursion_rad=(args.max_initial_error_rad
                                   if getattr(args, "mode", "opposition") == "sine" else None),
                max_velocity_rad_s=(SINE_STATIONARY_MAX_VELOCITY_RAD_S
                                    if getattr(args, "mode", "opposition") == "sine" else None))
            enable_wait_watchdog_checks += 1
            frame = diagnostic_receiver.latest()
            if (frame is not None and int(frame.header.seq) != diagnostic_seq_before
                    and len(_enabled_nids(frame)) == 20):
                active_metadata["enable_wait_final_telemetry_check"] = transition_sample
                break
            time.sleep(0.005)
        else:
            frame = diagnostic_receiver.latest()
            enabled_nids = _enabled_nids(frame) if frame is not None else []
            raise RuntimeError(f"enable timed out; enabled NIDs: {enabled_nids}")
        active_metadata["enable_wait_watchdog_checks"] = enable_wait_watchdog_checks
        diagnostic_record = diagnostic_receiver.latest_record()
        diagnostic_age_ms = (time.perf_counter_ns() - diagnostic_record[1]) / 1e6
        if diagnostic_age_ms > args.watchdog_max_age_ms:
            raise RuntimeError(
                f"post-enable joint_diagnostics is stale ({diagnostic_age_ms:.1f} ms old)")
        frame = diagnostic_record[3]
        _joints_by_nid(frame)
        enabled_nids = set(_enabled_nids(frame))
        if enabled_nids != set(EXPECTED_MOTOR_NIDS):
            raise RuntimeError(
                "not all expected motors remained enabled before publisher open; "
                f"enabled NIDs: {sorted(enabled_nids)}")
        post_enable_faults = _diagnostic_faults(frame)
        if post_enable_faults:
            raise RuntimeError(f"joint fault/limit appeared during enable: {post_enable_faults}")
        state_record, state_age_ms = _wait_for_fresh_record(
            state_receiver, state_seq_before, abort, timeout_s=1.0,
            max_age_ms=args.watchdog_max_age_ms)
        active_metadata["enable_transition_telemetry_check"] = _motion_watchdog(
            diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
            enable_transition_cursors, require_enabled=False,
            min_response_rate_pct=args.min_joint_response_rate_pct,
            center_positions=(first_positions
                              if getattr(args, "mode", "opposition") == "sine" else None),
            max_excursion_rad=(args.max_initial_error_rad
                               if getattr(args, "mode", "opposition") == "sine" else None),
            max_velocity_rad_s=(SINE_STATIONARY_MAX_VELOCITY_RAD_S
                                if getattr(args, "mode", "opposition") == "sine" else None))
        diagnostic_record = diagnostic_receiver.latest_record()
        state_record = state_receiver.latest_record()
        diagnostic_age_ms = (time.perf_counter_ns() - diagnostic_record[1]) / 1e6
        state_age_ms = (time.perf_counter_ns() - state_record[1]) / 1e6
        if (diagnostic_age_ms > args.watchdog_max_age_ms
                or state_age_ms > args.watchdog_max_age_ms):
            raise RuntimeError(
                "telemetry became stale while validating the enable transition: "
                f"diagnostics={diagnostic_age_ms:.3f} ms, state={state_age_ms:.3f} ms")
        frame = diagnostic_record[3]
        enabled_nids = set(_enabled_nids(frame))
        if enabled_nids != set(EXPECTED_MOTOR_NIDS):
            raise RuntimeError(
                "not all expected motors remained enabled before publisher open; "
                f"enabled NIDs: {sorted(enabled_nids)}")
        post_enable = _validate_active_frames(
            frame, state_record[3], first_positions, args.max_initial_error_rad)
        if getattr(args, "mode", "opposition") == "sine":
            post_enable_positions, post_enable_velocities = _validated_stationary_state(
                state_record[3],
                max_velocity_rad_s=SINE_STATIONARY_MAX_VELOCITY_RAD_S)
            post_enable["stationary_positions_rad"] = post_enable_positions
            post_enable["stationary_velocities_rad_s"] = post_enable_velocities
        post_enable["state_host_age_ms"] = state_age_ms
        post_enable["diagnostic_host_age_ms"] = diagnostic_age_ms
        active_metadata["post_enable_pre_publish"] = post_enable

        # Keep the cursor mutated by the transition watchdog.  Any frame that
        # arrives after its snapshot remains unseen and is therefore checked
        # by the first command-loop watchdog before the first send.
        watchdog_cursors = enable_transition_cursors
        publisher_open_start = time.perf_counter_ns()
        publisher = hand.joint_command().publish()
        active_metadata["publisher_open_duration_ns"] = time.perf_counter_ns() - publisher_open_start
        active_metadata["publisher_dtype"] = str(publisher.dtype)
        phase.value = "measure"
        # Give the first target one full period for construction. Subsequent
        # target construction happens immediately after the preceding send,
        # so that watchdog work consumes the available period rather than
        # being added on top of it.
        deadline = time.perf_counter_ns() + period_ns
        previous_send_enter = None
        watchdog_stats = {
            "max_allowed_age_ms": args.watchdog_max_age_ms,
            "checks": 0,
            "failures": 0,
            "diagnostic_frames_checked": 0,
            "state_frames_checked": 0,
            "max_diagnostic_age_ms": 0.0,
            "max_state_age_ms": 0.0,
            "max_tracking_error_rad": 0.0,
            "max_measured_velocity_rad_s": 0.0,
            "max_center_excursion_rad": 0.0,
        }
        active_metadata["motion_watchdog"] = watchdog_stats

        def run_watchdog(expected_positions):
            try:
                sample = _motion_watchdog(
                    diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                    watchdog_cursors, expected_positions, args.max_tracking_error_rad,
                    min_response_rate_pct=args.min_joint_response_rate_pct,
                    center_positions=(first_positions
                                      if getattr(args, "mode", "opposition") == "sine" else None),
                    max_excursion_rad=(SINE_MAX_EXCURSION_RAD
                                       if getattr(args, "mode", "opposition") == "sine" else None),
                    max_velocity_rad_s=(SINE_MAX_VELOCITY_RAD_S
                                        if getattr(args, "mode", "opposition") == "sine" else None))
            except Exception as error:
                watchdog_stats["failures"] += 1
                watchdog_stats["last_failure"] = {
                    "host_ns": time.perf_counter_ns(),
                    "error": str(error),
                }
                raise
            watchdog_stats["checks"] += 1
            watchdog_stats["diagnostic_frames_checked"] += \
                sample["diagnostic_frames_checked"]
            watchdog_stats["state_frames_checked"] += sample["state_frames_checked"]
            watchdog_stats["max_diagnostic_age_ms"] = max(
                watchdog_stats["max_diagnostic_age_ms"], sample["diagnostic_age_ms"])
            watchdog_stats["max_state_age_ms"] = max(
                watchdog_stats["max_state_age_ms"], sample["state_age_ms"])
            if sample["tracking_error_rad"] is not None:
                watchdog_stats["max_tracking_error_rad"] = max(
                    watchdog_stats["max_tracking_error_rad"],
                    sample["tracking_error_rad"])
            watchdog_stats["max_measured_velocity_rad_s"] = max(
                watchdog_stats["max_measured_velocity_rad_s"],
                sample["max_measured_velocity_rad_s"])
            watchdog_stats["max_center_excursion_rad"] = max(
                watchdog_stats["max_center_excursion_rad"],
                sample["max_center_excursion_rad"])
            return sample

        previous_target = tuple(float(value) for value in first_positions)
        for index in range(count):
            rebased = False
            if (args.scheduler in ("no-burst", "bounded-catchup")
                    and previous_send_enter is not None):
                minimum_ratio = (1.0 if args.scheduler == "no-burst"
                                 else SINE_MIN_SEND_INTERVAL_RATIO)
                minimum_deadline = previous_send_enter + round(period_ns * minimum_ratio)
                if minimum_deadline > deadline:
                    deadline = minimum_deadline
                    rebased = True
            read_start = time.perf_counter_ns()
            qpos = tuple(float(value) for value in module._read_qpos(stream))
            if len(qpos) != 20 or not all(math.isfinite(value) for value in qpos):
                raise RuntimeError(
                    f"runtime trajectory frame {index} must contain exactly 20 finite positions")
            build_start = time.perf_counter_ns()
            commands = [sdk.JointCommand(float(position), 0.0, 0.0) for position in qpos]
            build_return = time.perf_counter_ns()
            while True:
                if STOP_REQUESTED.is_set():
                    raise InterruptedError("operator stop requested")
                if abort.is_set():
                    raise RuntimeError("telemetry receiver stopped unexpectedly")
                now = time.perf_counter_ns()
                remaining = deadline - now
                if remaining <= 0:
                    break
                time.sleep(min(remaining, period_ns) / 1e9)
            original_deadline = deadline
            wake_lateness = max(0, now - deadline)
            if (max_command_lateness_ms is not None
                    and wake_lateness >= max_command_lateness_ms * 1e6):
                active_metadata["timing_abort"] = {
                    "reason": "command_lateness",
                    "frame_index": index,
                    "lateness_ns": wake_lateness,
                    "limit_ns": int(max_command_lateness_ms * 1e6),
                    "stage": "scheduler_wake",
                }
                raise RuntimeError(
                    f"command frame {index} woke {wake_lateness / 1e6:.3f} ms late; "
                    f"active benchmark limit is {max_command_lateness_ms:.3f} ms")
            if wake_lateness >= period_ns:
                deadline = now
                rebased = True
            watchdog_start = time.perf_counter_ns()
            watchdog = run_watchdog(previous_target)
            watchdog_return = time.perf_counter_ns()
            if STOP_REQUESTED.is_set():
                raise InterruptedError("operator stop requested")
            if abort.is_set():
                raise RuntimeError("telemetry receiver stopped unexpectedly")
            send_enter = time.perf_counter_ns()
            command_lateness = max(0, send_enter - deadline)
            if (max_command_lateness_ms is not None
                    and command_lateness >= max_command_lateness_ms * 1e6):
                active_metadata["timing_abort"] = {
                    "reason": "command_lateness",
                    "frame_index": index,
                    "lateness_ns": command_lateness,
                    "wake_lateness_ns": wake_lateness,
                    "limit_ns": int(max_command_lateness_ms * 1e6),
                    "stage": "immediately_before_send",
                }
                raise RuntimeError(
                    f"command frame {index} would send {command_lateness / 1e6:.3f} ms "
                    f"late after its safety check and command build; active benchmark "
                    f"limit is {max_command_lateness_ms:.3f} ms")
            send_exception = None
            try:
                publisher.send(commands)
            except Exception as exception:
                send_exception = exception
            send_return = time.perf_counter_ns()
            records.append({
                "phase": "measure",
                "index": index,
                "source_frame_index": index,
                "scheduled_ns": original_deadline,
                "wake_ns": now,
                "read_start_ns": read_start,
                "build_start_ns": build_start,
                "build_return_ns": build_return,
                "send_enter_ns": send_enter,
                "send_return_ns": send_return,
                "watchdog_start_ns": watchdog_start,
                "watchdog_return_ns": watchdog_return,
                "wake_lateness_ns": wake_lateness,
                "lateness_ns": command_lateness,
                "read_duration_ns": build_start - read_start,
                "build_duration_ns": build_return - build_start,
                "send_call_duration_ns": send_return - send_enter,
                "deadline_rebased": rebased,
                "send_ok": send_exception is None,
                "error": str(send_exception) if send_exception is not None else None,
                "watchdog_ok": True,
                "watchdog_error": None,
                "watchdog_diagnostic_seq": watchdog["diagnostic_seq"],
                "watchdog_state_seq": watchdog["state_seq"],
                "watchdog_diagnostic_age_ms": watchdog["diagnostic_age_ms"],
                "watchdog_state_age_ms": watchdog["state_age_ms"],
                "watchdog_tracking_error_rad": watchdog["tracking_error_rad"],
                "target_positions_rad": qpos,
            })
            if send_exception is not None:
                raise send_exception
            previous_target = qpos
            previous_send_enter = send_enter
            send_call_ns = send_return - send_enter
            if (max_send_call_ms is not None
                    and send_call_ns >= max_send_call_ms * 1e6):
                active_metadata["timing_abort"] = {
                    "reason": "send_call_duration",
                    "frame_index": index,
                    "send_call_duration_ns": send_call_ns,
                    "limit_ns": int(max_send_call_ms * 1e6),
                }
                raise RuntimeError(
                    f"command frame {index} send call took {send_call_ns / 1e6:.3f} ms; "
                    f"active benchmark limit is {max_send_call_ms:.3f} ms")
            deadline += period_ns
        final_feedback_timeout_s = max(0.01, args.watchdog_max_age_ms / 1000.0)
        _wait_for_fresh_record(
            diagnostic_receiver, watchdog_cursors["diagnostic_last_seq"], abort,
            timeout_s=final_feedback_timeout_s, max_age_ms=args.watchdog_max_age_ms)
        _wait_for_fresh_record(
            state_receiver, watchdog_cursors["state_last_seq"], abort,
            timeout_s=final_feedback_timeout_s, max_age_ms=args.watchdog_max_age_ms)
        active_metadata["post_final_command_feedback"] = run_watchdog(previous_target)
    finally:
        phase.value = "cleanup"
        if enable_attempted:
            _disable_and_confirm(
                hand, diagnostic_receiver, cleanup, "disable_and_confirm", timeout_s=0.25)
        if publisher is not None:
            try:
                publisher.close()
                cleanup.append({"operation": "close_publisher", "ok": True})
            except Exception as error:
                cleanup.append({"operation": "close_publisher", "ok": False, "error": str(error)})
        try:
            stream.close()
            cleanup.append({"operation": "close_trajectory", "ok": True})
        except Exception as error:
            cleanup.append({"operation": "close_trajectory", "ok": False, "error": str(error)})
    return records


def _command_summary(records, requested_hz, scheduler):
    successful = [record for record in records if record["send_ok"]]
    send_enters = [record["send_enter_ns"] for record in successful]
    longest = None
    if len(send_enters) > 1:
        intervals = [after - before for before, after in zip(send_enters, send_enters[1:])]
        longest = max(range(len(intervals)), key=intervals.__getitem__) + 1
    tracking_errors = [record["watchdog_tracking_error_rad"] for record in records
                       if record.get("watchdog_tracking_error_rad") is not None]
    return {
        "requested_rate_hz": requested_hz,
        "scheduler": scheduler,
        "attempts": len(records),
        "successful_sends": len(successful),
        "failed_sends": len(records) - len(successful),
        "send_enter_cadence": cadence_ns(send_enters),
        "wake_cadence": cadence_ns([record["wake_ns"] for record in records]),
        "wake_lateness": distribution_ns([
            record.get("wake_lateness_ns", record["lateness_ns"]) for record in records
        ]),
        "lateness": distribution_ns([record["lateness_ns"] for record in records]),
        "replay_read": distribution_ns([record["read_duration_ns"] for record in records]),
        "command_build": distribution_ns([record["build_duration_ns"] for record in records]),
        "native_send_call": distribution_ns([record["send_call_duration_ns"] for record in records]),
        "watchdog_validation": distribution_ns([
            record["watchdog_return_ns"] - record["watchdog_start_ns"] for record in records
        ]),
        "target_prepare": distribution_ns([
            record.get("build_return_ns", record["send_enter_ns"]) - record["read_start_ns"]
            for record in records
        ]),
        "scheduled_pipeline": distribution_ns([
            record["send_return_ns"] - record["read_start_ns"] for record in records
        ]),
        "deadline_rebases": sum(bool(record["deadline_rebased"]) for record in records),
        "longest_send_interval_ending_at_index": longest,
        "maximum_watchdog_tracking_error_rad": (
            max(tracking_errors) if tracking_errors else None),
        "maximum_watchdog_tracking_error_deg": (
            math.degrees(max(tracking_errors)) if tracking_errors else None),
    }


def _telemetry_summary(receiver, phase="measure"):
    records = [record for record in receiver.records if record[0] == phase]
    host = [record[1] for record in records]
    device = [int(record[3].header.timestamp_us) * 1000 for record in records]
    sequences = [int(record[3].header.seq) for record in records]
    sequence, _classes = _sequence_analysis(sequences)
    host_device_interval_error = [
        (host_after - host_before) - (device_after - device_before)
        for host_before, host_after, device_before, device_after
        in zip(host, host[1:], device, device[1:])
    ]
    frame_ids = [str(record[3].header.frame_id) for record in records]
    return {
        "samples": len(records),
        "host_delivery_cadence": cadence_ns(host),
        "device_timestamp_cadence": cadence_ns(device),
        "recv_and_decode_call": distribution_ns([record[2] for record in records]),
        "host_minus_device_interval": signed_distribution_ns(host_device_interval_error),
        "sequence": sequence,
        "frame_id_changes": sum(a != b for a, b in zip(frame_ids, frame_ids[1:])),
        "native_receiver_lag_events": len([
            event for event in receiver.lag_events if event["phase"] == phase
        ]),
        "native_receiver_lagged_frames": sum(
            event["lagged_frames"] for event in receiver.lag_events if event["phase"] == phase
        ),
        "receiver_errors": [event for event in receiver.errors if event["phase"] == phase],
    }


def _sine_response_summary(state_receiver, center_positions):
    records = [record for record in state_receiver.records if record[0] == "measure"]
    center_positions = tuple(float(value) for value in center_positions)
    samples = [[] for _ in range(20)]
    velocities = [[] for _ in range(20)]
    for record in records:
        joints = _joints_in_dense_command_order(record[3])
        for dense_index, joint in enumerate(joints):
            samples[dense_index].append(float(joint.position))
            velocities[dense_index].append(float(joint.velocity))
    per_joint = []
    for dense_index, (center, positions, speeds) in enumerate(
            zip(center_positions, samples, velocities)):
        if not positions:
            per_joint.append({
                "dense_index": dense_index,
                "nid": EXPECTED_MOTOR_NIDS[dense_index],
                "samples": 0,
            })
            continue
        offsets = [position - center for position in positions]
        per_joint.append({
            "dense_index": dense_index,
            "nid": EXPECTED_MOTOR_NIDS[dense_index],
            "samples": len(positions),
            "center_rad": center,
            "minimum_position_rad": min(positions),
            "maximum_position_rad": max(positions),
            "peak_to_peak_rad": max(positions) - min(positions),
            "peak_to_peak_deg": math.degrees(max(positions) - min(positions)),
            "positive_peak_offset_rad": max(offsets),
            "negative_peak_offset_rad": min(offsets),
            "maximum_absolute_offset_rad": max(abs(value) for value in offsets),
            "maximum_absolute_offset_deg": math.degrees(
                max(abs(value) for value in offsets)),
            "maximum_absolute_velocity_rad_s": max(abs(value) for value in speeds),
            "final_center_error_rad": abs(positions[-1] - center),
            "final_center_error_deg": math.degrees(abs(positions[-1] - center)),
        })
    populated = [item for item in per_joint if item.get("samples", 0)]
    return {
        "state_frames": len(records),
        "per_joint": per_joint,
        "minimum_observed_peak_offset_deg": (
            min(item["maximum_absolute_offset_deg"] for item in populated)
            if populated else None),
        "maximum_observed_peak_offset_deg": (
            max(item["maximum_absolute_offset_deg"] for item in populated)
            if populated else None),
        "maximum_observed_velocity_rad_s": (
            max(item["maximum_absolute_velocity_rad_s"] for item in populated)
            if populated else None),
        "maximum_final_center_error_deg": (
            max(item["final_center_error_deg"] for item in populated)
            if populated else None),
    }


def _sine_acceptance(report):
    sine = report.get("sine") or {}
    command = report.get("command") or {}
    cadence = command.get("send_enter_cadence") or {}
    command_intervals = cadence.get("intervals") or {}
    health = report.get("communication_health") or {}
    checks = {
        "normal_completion": report.get("status") == "complete",
        "exact_successful_send_count": (
            command.get("successful_sends") == sine.get("selected_frames")),
        "zero_failed_sends": command.get("failed_sends") == 0,
        "send_rate_995_to_1005_hz": (
            cadence.get("rate_hz") is not None
            and 995.0 <= cadence["rate_hz"] <= 1005.0),
        "send_interval_p99_at_most_1_2_ms": (
            command_intervals.get("p99_ms") is not None
            and command_intervals["p99_ms"] <= 1.2),
        "send_interval_max_below_2_ms": (
            command_intervals.get("max_ms") is not None
            and command_intervals["max_ms"] < 2.0),
        "send_interval_min_at_least_0_8_ms": (
            command_intervals.get("min_ms") is not None
            and command_intervals["min_ms"] >= 0.8),
        "zero_joint_timeout_delta": health.get("joint_comm_timeouts_delta") == 0,
        "zero_internal_node_timeout_delta": health.get("internal_node_timeouts_delta") == 0,
        "zero_e2e_loss_delta": health.get("e2e_lost_delta") == 0,
        "zero_e2e_reorder_delta": health.get("e2e_reordered_delta") == 0,
        "zero_e2e_duplicate_delta": health.get("e2e_duplicates_delta") == 0,
        "zero_sdk_drop_delta": health.get("sdk_dropped_delta") == 0,
        "zero_rpc_retry_delta": health.get("rpc_retries_delta") == 0,
        "zero_rpc_timeout_delta": health.get("rpc_timeouts_delta") == 0,
        "zero_comm_get_failure_delta": health.get("comm_get_failures_delta") == 0,
        "zero_internal_crc_error_delta": health.get("internal_crc_errors_delta") == 0,
        "zero_internal_frame_format_error_delta": (
            health.get("internal_frame_format_errors_delta") == 0),
        "zero_internal_uart_error_delta": health.get("internal_uart_errors_delta") == 0,
        "minimum_joint_response_at_least_80_pct": (
            health.get("minimum_joint_response_rate_pct_observed") is not None
            and health["minimum_joint_response_rate_pct_observed"] >= 80),
        "zero_joint_error_codes": not health.get("joint_error_codes_observed"),
        "zero_joint_limit_frames": health.get("joint_limit_flag_frames") == 0,
        "twenty_online_motors": health.get("internal_online_motor_nodes_final") == 20,
    }
    for name in ("joint_diagnostics", "joint_states"):
        telemetry = (report.get("telemetry") or {}).get(name) or {}
        device_cadence = telemetry.get("device_timestamp_cadence") or {}
        device_intervals = device_cadence.get("intervals") or {}
        host_cadence = telemetry.get("host_delivery_cadence") or {}
        host_intervals = host_cadence.get("intervals") or {}
        sequence = telemetry.get("sequence") or {}
        checks[f"{name}_device_rate_999_to_1001_hz"] = (
            device_cadence.get("rate_hz") is not None
            and 999.0 <= device_cadence["rate_hz"] <= 1001.0)
        checks[f"{name}_device_interval_p99_at_most_1_1_ms"] = (
            device_intervals.get("p99_ms") is not None
            and device_intervals["p99_ms"] <= 1.1)
        checks[f"{name}_device_interval_max_at_most_1_5_ms"] = (
            device_intervals.get("max_ms") is not None
            and device_intervals["max_ms"] <= 1.5)
        checks[f"{name}_host_rate_995_to_1005_hz"] = (
            host_cadence.get("rate_hz") is not None
            and 995.0 <= host_cadence["rate_hz"] <= 1005.0)
        checks[f"{name}_host_interval_p99_at_most_1_5_ms"] = (
            host_intervals.get("p99_ms") is not None
            and host_intervals["p99_ms"] <= 1.5)
        checks[f"{name}_host_interval_max_below_2_ms"] = (
            host_intervals.get("max_ms") is not None
            and host_intervals["max_ms"] < 2.0)
        checks[f"{name}_zero_sequence_anomalies"] = not any((
            sequence.get("missing_sequence_values"),
            sequence.get("duplicate_transitions"),
            sequence.get("reordered_transitions"),
            telemetry.get("native_receiver_lagged_frames"),
            telemetry.get("receiver_errors"),
        ))
    response = sine.get("response") or {}
    per_joint = response.get("per_joint") or []
    required_peak_deg = 0.8 * float(sine.get("amplitude_deg_peak", 5.0))
    exact_response_nids = (
        len(per_joint) == 20
        and [item.get("nid") for item in per_joint] == list(EXPECTED_MOTOR_NIDS)
        and all(item.get("samples", 0) > 0 for item in per_joint)
    )
    tracking_checks = {
        "exact_20_joint_response_set": exact_response_nids,
        "all_joints_reached_positive_lobe": (
            exact_response_nids and all(
                math.degrees(item.get("positive_peak_offset_rad", -math.inf))
                >= required_peak_deg for item in per_joint)),
        "all_joints_reached_negative_lobe": (
            exact_response_nids and all(
                math.degrees(item.get("negative_peak_offset_rad", math.inf))
                <= -required_peak_deg for item in per_joint)),
        "no_joint_exceeded_7_deg_from_center": (
            response.get("maximum_observed_peak_offset_deg") is not None
            and response["maximum_observed_peak_offset_deg"] <= 7.0),
        "final_pose_within_1_deg_of_center": (
            response.get("maximum_final_center_error_deg") is not None
            and response["maximum_final_center_error_deg"] <= 1.0),
        "measured_speed_at_most_0_55_rad_s": (
            response.get("maximum_observed_velocity_rad_s") is not None
            and response["maximum_observed_velocity_rad_s"] <= SINE_MAX_VELOCITY_RAD_S),
        "watchdog_tracking_error_below_limit": (
            command.get("maximum_watchdog_tracking_error_rad") is not None
            and command["maximum_watchdog_tracking_error_rad"]
            <= report["config"]["max_tracking_error_rad"]),
    }
    return {
        "communication_checks": checks,
        "communication_pass": all(checks.values()),
        "tracking_checks": tracking_checks,
        "tracking_pass": all(tracking_checks.values()),
        "firmware_command_application_proven": False,
        "interpretation": (
            "A pass establishes near-1-kHz host publishing with healthy 1-kHz returned "
            "telemetry and low-frequency physical tracking; no command acknowledgement "
            "exists to prove every 1-ms command was applied by firmware."),
    }


def _telemetry_correlation(receivers, phase="measure"):
    by_name = {receiver.name: receiver for receiver in receivers}
    diagnostic = by_name.get("joint_diagnostics")
    state = by_name.get("joint_states")
    if diagnostic is None or state is None:
        return None

    def pairs(receiver):
        return [
            (int(record[3].header.seq), int(record[3].header.timestamp_us))
            for record in receiver.records if record[0] == phase
        ]

    diagnostic_pairs = pairs(diagnostic)
    state_pairs = pairs(state)
    matching = sum((Counter(diagnostic_pairs) & Counter(state_pairs)).values())
    return {
        "phase": phase,
        "joint_diagnostics_samples": len(diagnostic_pairs),
        "joint_states_samples": len(state_pairs),
        "matching_seq_and_device_timestamp_pairs": matching,
        "state_pair_match_fraction": (matching / len(state_pairs) if state_pairs else None),
        "note": ("Matching pairs are correlated projections of the same device frames, "
                 "not independent evidence of two separate 1 kHz links."),
    }


def _communication_summary(before, after):
    if not before or not after:
        return None
    finger_counter_fields = (
        "tx_frame_total", "rx_frame_total", "tx_byte_total", "rx_byte_total",
        "crc_error_total", "frame_format_error_total", "uart_hw_error_total",
    )
    node_counter_fields = ("request_total", "response_ok_total", "timeout_total")
    transfer_counter_fields = ("tx_total", "timeout_total")
    result = {"fingers": []}
    for index, (old, new) in enumerate(zip(before.get("fingers", []), after.get("fingers", []))):
        finger = {
            "finger": FINGER_NAMES[index] if index < len(FINGER_NAMES) else str(index),
            "raw_before": old,
            "raw_after": new,
            "counter_changes": {
                name: {"before": old.get(name), "after": new.get(name),
                       "delta": counter_change(old.get(name), new.get(name)),
                       "status": "ok" if counter_change(old.get(name), new.get(name)) is not None
                                 else "reset_or_wrap"}
                for name in finger_counter_fields
            },
            "transfer_stats": [],
            "nodes": [],
        }
        for transfer_index, (old_transfer, new_transfer) in enumerate(zip(
                old.get("transfer_stats", []), new.get("transfer_stats", []))):
            finger["transfer_stats"].append({
                "index": transfer_index,
                "raw_before": old_transfer,
                "raw_after": new_transfer,
                "counter_changes": {
                    name: {"before": old_transfer.get(name), "after": new_transfer.get(name),
                           "delta": counter_change(old_transfer.get(name), new_transfer.get(name)),
                           "status": "ok" if counter_change(old_transfer.get(name), new_transfer.get(name)) is not None
                                     else "reset_or_wrap"}
                    for name in transfer_counter_fields
                },
            })
        for node_index, (old_node, new_node) in enumerate(zip(
                old.get("nodes", []), new.get("nodes", []))):
            finger["nodes"].append({
                "index": node_index,
                "raw_before": old_node,
                "raw_after": new_node,
                "counter_changes": {
                    name: {"before": old_node.get(name), "after": new_node.get(name),
                           "delta": counter_change(old_node.get(name), new_node.get(name)),
                           "status": "ok" if counter_change(old_node.get(name), new_node.get(name)) is not None
                                     else "reset_or_wrap"}
                    for name in node_counter_fields
                },
            })
        result["fingers"].append(finger)
    return result


def _diagnostic_stream_delta(receiver):
    records = [record for record in receiver.records if record[0] == "measure"]
    if not records:
        return None
    first, last = to_plain(records[0][3]), to_plain(records[-1][3])
    counter_fields = (
        "e2e_received", "e2e_lost", "e2e_reordered", "e2e_duplicates",
        "rpc_total", "rpc_retries", "rpc_timeouts", "comm_get_failures", "sdk_dropped",
    )
    changes = {
        name: {"before": first["comm"].get(name), "after": last["comm"].get(name),
               "delta": counter_change(first["comm"].get(name), last["comm"].get(name))}
        for name in counter_fields
    }
    changes["tactile_timeout_total"] = [
        {"finger": FINGER_NAMES[index], "before": old, "after": new,
         "delta": counter_change(old, new)}
        for index, (old, new) in enumerate(zip(
            first["comm"].get("tactile_timeout_total", []),
            last["comm"].get("tactile_timeout_total", [])))
    ]
    joint_before = {int(joint["nid"]): joint for joint in first["joints"]}
    joint_after = {int(joint["nid"]): joint for joint in last["joints"]}
    joint_changes = []
    for nid in sorted(set(joint_before) | set(joint_after)):
        old, new = joint_before.get(nid), joint_after.get(nid)
        joint_changes.append({
            "nid": nid,
            "before": old,
            "after": new,
            "comm_timeout_delta": None if old is None or new is None else
                counter_change(old.get("comm_timeout_total"), new.get("comm_timeout_total")),
        })
    observed = {}
    max_age_ms = 0
    max_window_loss_x100 = 0
    for _phase, _host_ns, _call_ns, frame in records:
        max_age_ms = max(max_age_ms, int(frame.comm.age_ms))
        max_window_loss_x100 = max(max_window_loss_x100, int(frame.comm.e2e_window_loss_x100))
        for joint in frame.joints:
            item = observed.setdefault(int(joint.nid), {
                "nid": int(joint.nid),
                "min_comm_response_rate_pct": 100,
                "max_comm_timeout_total": 0,
                "error_codes_observed": set(),
                "limit_flag_frames": 0,
            })
            item["min_comm_response_rate_pct"] = min(
                item["min_comm_response_rate_pct"], int(joint.comm_response_rate_pct))
            item["max_comm_timeout_total"] = max(
                item["max_comm_timeout_total"], int(joint.comm_timeout_total))
            if joint.error_code_current:
                item["error_codes_observed"].add(int(joint.error_code_current))
            status = joint.status_word
            if (status.position_limit_active or status.velocity_limit_active
                    or status.current_limit_active):
                item["limit_flag_frames"] += 1
    observed_joint_health = []
    for nid in sorted(observed):
        item = observed[nid]
        item["error_codes_observed"] = sorted(item["error_codes_observed"])
        observed_joint_health.append(item)
    return {"first": first, "last": last, "comm_counter_changes": changes,
            "joint_changes": joint_changes,
            "observed_max_comm_age_ms": max_age_ms,
            "observed_max_e2e_window_loss_x100": max_window_loss_x100,
            "observed_joint_health": observed_joint_health}


def _communication_health(communication, diagnostic):
    result = {
        "internal_online_motor_nodes_final": None,
        "internal_crc_errors_delta": None,
        "internal_frame_format_errors_delta": None,
        "internal_uart_errors_delta": None,
        "internal_node_timeouts_delta": None,
        "joint_comm_timeouts_delta": None,
        "minimum_joint_response_rate_pct_observed": None,
        "joint_error_codes_observed": [],
        "joint_limit_flag_frames": 0,
    }
    if communication:
        fingers = communication.get("fingers", [])

        def sum_finger_counter(name):
            values = [finger["counter_changes"][name]["delta"] for finger in fingers]
            return sum(values) if values and all(value is not None for value in values) else None

        result.update({
            "internal_online_motor_nodes_final": sum(
                int(node["raw_after"].get("online", 0))
                for finger in fingers for node in finger.get("nodes", [])[:4]),
            "internal_crc_errors_delta": sum_finger_counter("crc_error_total"),
            "internal_frame_format_errors_delta": sum_finger_counter("frame_format_error_total"),
            "internal_uart_errors_delta": sum_finger_counter("uart_hw_error_total"),
        })
        node_timeouts = [
            node["counter_changes"]["timeout_total"]["delta"]
            for finger in fingers for node in finger.get("nodes", [])[:4]
        ]
        result["internal_node_timeouts_delta"] = (
            sum(node_timeouts) if node_timeouts and all(value is not None for value in node_timeouts)
            else None)
    if diagnostic:
        timeout_deltas = [item["comm_timeout_delta"] for item in diagnostic.get("joint_changes", [])]
        result["joint_comm_timeouts_delta"] = (
            sum(timeout_deltas) if timeout_deltas and all(value is not None for value in timeout_deltas)
            else None)
        observed = diagnostic.get("observed_joint_health", [])
        if observed:
            result["minimum_joint_response_rate_pct_observed"] = min(
                item["min_comm_response_rate_pct"] for item in observed)
            result["joint_error_codes_observed"] = sorted({
                code for item in observed for code in item["error_codes_observed"]
            })
            result["joint_limit_flag_frames"] = sum(
                item["limit_flag_frames"] for item in observed)
        for name in ("e2e_lost", "e2e_reordered", "e2e_duplicates", "sdk_dropped",
                     "rpc_retries", "rpc_timeouts", "comm_get_failures"):
            change = diagnostic.get("comm_counter_changes", {}).get(name)
            result[name + "_delta"] = change.get("delta") if change else None
    return result


def _comm_online_joint_count(comm_diag):
    if not comm_diag or len(comm_diag.get("fingers", [])) != 5:
        return None
    fingers = comm_diag["fingers"]
    if any(len(finger.get("nodes", [])) < 4 for finger in fingers):
        return None
    return sum(int(node.get("online", 0)) for finger in fingers for node in finger["nodes"][:4])


def _write_commands(path, session_id, records):
    fields = [
        "session_id", "phase", "index", "source_frame_index",
        "scheduled_ns", "wake_ns", "read_start_ns", "build_start_ns", "build_return_ns",
        "send_enter_ns",
        "send_return_ns", "watchdog_start_ns", "watchdog_return_ns",
        "wake_lateness_ns", "lateness_ns",
        "read_duration_ns", "build_duration_ns",
        "send_call_duration_ns", "deadline_rebased", "send_ok", "error",
        "watchdog_ok", "watchdog_error", "watchdog_diagnostic_seq", "watchdog_state_seq",
        "watchdog_diagnostic_age_ms", "watchdog_state_age_ms",
        "watchdog_tracking_error_rad", "target_positions_rad",
    ]
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {"session_id": session_id, **record}
            if row.get("target_positions_rad") is not None:
                row["target_positions_rad"] = json.dumps(
                    list(row["target_positions_rad"]), separators=(",", ":"))
            writer.writerow(row)


def _write_telemetry(path, session_id, receivers, *, log_full_frames=False):
    fields = [
        "session_id", "stream", "phase", "host_recv_ns", "recv_call_ns",
        "device_seq", "device_timestamp_us", "frame_id", "num_joints",
        "seq_delta", "seq_class", "estimated_missing", "frame_json",
    ]
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for receiver in receivers:
            classes = [None] * len(receiver.records)
            phase_indices = {}
            for index, record in enumerate(receiver.records):
                phase_indices.setdefault(record[0], []).append(index)
            for indices in phase_indices.values():
                sequences = [int(receiver.records[index][3].header.seq) for index in indices]
                _summary, phase_classes = _sequence_analysis(sequences)
                for index, classification in zip(indices, phase_classes):
                    classes[index] = classification
            last_index = len(receiver.records) - 1
            for index, (record, classification) in enumerate(zip(receiver.records, classes)):
                phase, host_ns, call_ns, frame = record
                include_detail = (log_full_frames or index in (0, last_index)
                                  or classification["class"] not in ("first", "contiguous",
                                                                     "contiguous_wrap"))
                writer.writerow({
                    "session_id": session_id,
                    "stream": receiver.name,
                    "phase": phase,
                    "host_recv_ns": host_ns,
                    "recv_call_ns": call_ns,
                    "device_seq": int(frame.header.seq),
                    "device_timestamp_us": int(frame.header.timestamp_us),
                    "frame_id": str(frame.header.frame_id),
                    "num_joints": int(getattr(frame, "num_joints", len(frame.joints))),
                    "seq_delta": classification["delta"],
                    "seq_class": classification["class"],
                    "estimated_missing": classification["estimated_missing"],
                    "frame_json": (json.dumps(to_plain(frame), separators=(",", ":"))
                                   if include_detail else ""),
                })


def _write_diag_jsonl(path, session_id, records):
    with Path(path).open("w") as stream:
        for record in records:
            stream.write(json.dumps({"schema_version": SCHEMA_VERSION,
                                     "session_id": session_id, **record},
                                    separators=(",", ":")) + "\n")


def _metric(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _print_summary(summary, output_dir):
    device = summary.get("device", {})
    print("\nConnected benchmark summary")
    print(f"  Device: {device.get('serial_number', 'unknown')} | discovered endpoint "
          f"{device.get('transport', 'unknown')} {device.get('address', 'unknown')} | "
          f"firmware {device.get('firmware_version', 'unknown')}")
    print(f"  Mode: {summary.get('mode')} | status: {summary.get('status')}")
    command = summary.get("command")
    if command:
        cadence = command["send_enter_cadence"]
        intervals = cadence.get("intervals") or {}
        print(f"  Host publisher calls: {command['successful_sends']}/{command['attempts']} returned; "
              f"{_metric(cadence.get('rate_hz'))} Hz; "
              f"p99={_metric(intervals.get('p99_ms'))} ms; "
              f"max={_metric(intervals.get('max_ms'))} ms; "
              f"rebases={command['deadline_rebases']}")
        calls = command.get("native_send_call") or {}
        print(f"  Send call: p50={_metric(calls.get('p50_ms'))} ms; "
              f"p99={_metric(calls.get('p99_ms'))} ms; "
              f"max={_metric(calls.get('max_ms'))} ms")
    for name, telemetry in summary.get("telemetry", {}).items():
        cadence = telemetry["host_delivery_cadence"]
        device_cadence = telemetry["device_timestamp_cadence"]
        intervals = cadence.get("intervals") or {}
        device_intervals = device_cadence.get("intervals") or {}
        sequence = telemetry["sequence"]
        print(f"  {name} RX: {cadence['samples']} frames; "
              f"host={_metric(cadence.get('rate_hz'))} Hz; "
              f"device-ts={_metric(device_cadence.get('rate_hz'))} Hz; "
              f"host-p99/max={_metric(intervals.get('p99_ms'))}/"
              f"{_metric(intervals.get('max_ms'))} ms; "
              f"device-p99/max={_metric(device_intervals.get('p99_ms'))}/"
              f"{_metric(device_intervals.get('max_ms'))} ms; "
              f"missing={sequence['missing_sequence_values']}; "
              f"duplicates={sequence['duplicate_transitions']}; "
              f"reordered={sequence['reordered_transitions']}; "
              f"native-lagged={telemetry['native_receiver_lagged_frames']}")
    correlation = summary.get("telemetry_correlation") or {}
    if correlation:
        print("  Stream correlation: "
              f"{correlation['matching_seq_and_device_timestamp_pairs']}/"
              f"{correlation['joint_states_samples']} state frames share diagnostics "
              "sequence+device timestamps (correlated, not independent links)")
    diagnostic = summary.get("diagnostic_stream_delta") or {}
    changes = diagnostic.get("comm_counter_changes", {})
    if changes:
        labels = ("e2e_lost", "e2e_reordered", "e2e_duplicates", "sdk_dropped",
                  "rpc_retries", "rpc_timeouts", "comm_get_failures")
        print("  Telemetry/SDK counters: " + ", ".join(
            f"{name}={changes[name]['delta']}" for name in labels if name in changes
        ))
    health = summary.get("communication_health") or {}
    if health:
        print("  Internal health: "
              f"online-motors={health.get('internal_online_motor_nodes_final')}; "
              f"joint-timeouts={health.get('joint_comm_timeouts_delta')}; "
              f"node-timeouts={health.get('internal_node_timeouts_delta')}; "
              f"CRC/format/UART-errors={health.get('internal_crc_errors_delta')}/"
              f"{health.get('internal_frame_format_errors_delta')}/"
              f"{health.get('internal_uart_errors_delta')}; "
              f"min-response={health.get('minimum_joint_response_rate_pct_observed')}%")
    sine_acceptance = summary.get("sine_acceptance") or {}
    if sine_acceptance:
        response = (summary.get("sine") or {}).get("response") or {}
        print("  Sine result: "
              f"communication-pass={sine_acceptance.get('communication_pass')}; "
              f"tracking-pass={sine_acceptance.get('tracking_pass')}; "
              f"observed peak range="
              f"{_metric(response.get('minimum_observed_peak_offset_deg'))}.."
              f"{_metric(response.get('maximum_observed_peak_offset_deg'))} deg; "
              f"final-center-error-max="
              f"{_metric(response.get('maximum_final_center_error_deg'))} deg")
    if command and command.get("successful_sends", 0) >= 2:
        print("  Scope: host publisher-call cadence and returned telemetry were measured; "
              "firmware command application was not acknowledged.")
    elif summary.get("mode") in ACTIVE_MODES:
        print("  Scope: active mode did not complete enough successful publisher calls to "
              "measure command cadence; any returned telemetry is reported separately.")
    elif summary.get("validity", {}).get("telemetry_delivery_measured"):
        print("  Scope: returned telemetry and device/SDK diagnostics were measured; "
              "no motor commands were sent.")
    else:
        print("  Scope: no measured-phase telemetry was captured; no motor commands were sent.")
    print(f"  Logs: {output_dir}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("observe", "opposition", "sine"), default="observe",
                        help="observe is read-only; opposition and sine enable and move the hand")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--sn", help="exact Hand 2 serial number")
    selector.add_argument("--address", help="exact discovered device address")
    parser.add_argument("--confirm-motion", metavar="SERIAL",
                        help="active modes only; must exactly equal --sn")
    parser.add_argument("--duration-s", type=float, default=10.0,
                        help="observe measurement or sine motion duration")
    parser.add_argument("--warmup-s", type=float, default=None,
                        help="telemetry warmup excluded from summaries")
    parser.add_argument("--settle-s", type=float, default=1.0,
                        help="telemetry collection after active commands stop")
    parser.add_argument("--frames", type=int,
                        help="opposition frames to send; omitted replays the complete recording")
    parser.add_argument("--rate-hz", type=float, default=1000.0,
                        help="active command target rate, at most 1000 Hz; sine requires 1000")
    parser.add_argument(
        "--scheduler", choices=("opposition", "no-burst", "bounded-catchup"),
        default=None,
        help=("opposition matches the normal example; no-burst enforces full-period "
              "spacing; bounded-catchup permits no interval below 80%% of a period"))
    parser.add_argument("--telemetry", choices=("diagnostics", "both", "none"), default=None,
                        help="streams to record; active mode always requires both")
    parser.add_argument("--request-telemetry-hz", type=int,
                        help="explicitly request a device stream rate; omitted preserves the current/default rate")
    parser.add_argument("--poll-us", type=float, default=100.0,
                        help="sleep after an empty native subscription poll")
    parser.add_argument("--diag-interval-s", type=float,
                        help="periodic comm_diag GET interval; omitted means baseline/final snapshots only")
    parser.add_argument("--max-initial-error-rad", type=float, default=None,
                        help="refuse active motion if its first target differs more than this from measured pose")
    parser.add_argument("--watchdog-max-age-ms", type=float, default=20.0,
                        help="stop active replay if diagnostics or state feedback is this stale")
    parser.add_argument("--max-tracking-error-rad", type=float, default=None,
                        help="stop active motion if measured pose differs this much from the prior target")
    parser.add_argument("--max-command-lateness-ms", type=float,
                        help="abort active motion before a send this late; sine defaults to 1 ms")
    parser.add_argument("--max-send-call-ms", type=float,
                        help="abort active motion after an SDK send call this slow; sine defaults to 1 ms")
    parser.add_argument("--min-joint-response-rate-pct", type=int, default=80,
                        help="stop active replay below this per-joint communication response rate")
    parser.add_argument("--max-replay-step-rad", type=float, default=0.02,
                        help="refuse opposition if any adjacent replay target step exceeds this")
    parser.add_argument("--sine-amplitude-deg", type=float, default=5.0,
                        help="sine-mode peak amplitude in degrees; capped at 5")
    parser.add_argument("--sine-frequency-hz", type=float, default=0.5,
                        help="sine-mode frequency; capped at 1 Hz")
    parser.add_argument("--sine-ramp-s", type=float, default=2.0,
                        help="raised-cosine amplitude ramp at each end of sine mode")
    parser.add_argument("--log-full-frames", action="store_true",
                        help="store every decoded telemetry frame (large); default logs timing rows plus boundary/anomaly details")
    parser.add_argument(
        "--disable-cyclic-gc", action="store_true",
        help=("disable Python cyclic garbage collection during capture and restore it "
              "during cleanup; sine mode enables this automatically"))
    parser.add_argument("--output-dir", type=Path,
                        help="output directory; defaults to build/connected-timing/<UTC timestamp>")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.warmup_s is None:
        args.warmup_s = 10.0 if args.mode == "sine" else 2.0
    if args.max_initial_error_rad is None:
        args.max_initial_error_rad = math.radians(2.0) if args.mode == "sine" else 0.1
    if args.max_tracking_error_rad is None:
        args.max_tracking_error_rad = math.radians(2.0) if args.mode == "sine" else 0.5
    numeric_positive = (("--duration-s", args.duration_s), ("--warmup-s", args.warmup_s),
                        ("--settle-s", args.settle_s), ("--poll-us", args.poll_us),
                        ("--max-initial-error-rad", args.max_initial_error_rad),
                        ("--watchdog-max-age-ms", args.watchdog_max_age_ms),
                        ("--max-tracking-error-rad", args.max_tracking_error_rad),
                        ("--max-replay-step-rad", args.max_replay_step_rad),
                        ("--sine-amplitude-deg", args.sine_amplitude_deg),
                        ("--sine-frequency-hz", args.sine_frequency_hz),
                        ("--sine-ramp-s", args.sine_ramp_s))
    for name, value in numeric_positive:
        if not math.isfinite(value) or value < 0 or (name == "--poll-us" and value == 0):
            parser.error(f"{name} must be finite and {'positive' if name == '--poll-us' else 'nonnegative'}")
    if args.duration_s == 0:
        parser.error("--duration-s must be positive")
    if args.watchdog_max_age_ms == 0:
        parser.error("--watchdog-max-age-ms must be positive")
    for name, value in (("--max-command-lateness-ms", args.max_command_lateness_ms),
                        ("--max-send-call-ms", args.max_send_call_ms)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{name} must be finite and positive")
    if args.frames is not None and args.frames < 2:
        parser.error("--frames must be at least 2")
    if not math.isfinite(args.rate_hz) or not 0 < args.rate_hz <= 1000:
        parser.error("--rate-hz must be finite and in (0, 1000]")
    if args.request_telemetry_hz is not None and not 1 <= args.request_telemetry_hz <= 1000:
        parser.error("--request-telemetry-hz must be in [1, 1000]")
    if args.request_telemetry_hz is not None and args.telemetry == "none":
        parser.error("--request-telemetry-hz requires a telemetry stream")
    if args.diag_interval_s is not None and (not math.isfinite(args.diag_interval_s)
                                             or args.diag_interval_s < 0):
        parser.error("--diag-interval-s must be finite and nonnegative")
    if not 0 <= args.min_joint_response_rate_pct <= 100:
        parser.error("--min-joint-response-rate-pct must be in [0, 100]")
    if args.mode in ACTIVE_MODES:
        if not args.sn:
            parser.error(f"{args.mode} mode requires --sn; automatic device selection is not allowed")
        if args.confirm_motion != args.sn:
            parser.error(f"{args.mode} mode requires --confirm-motion to exactly match --sn")
        if args.request_telemetry_hz != 1000:
            parser.error(f"{args.mode} mode requires --request-telemetry-hz 1000 for both watchdog streams")
        minimum_warmup_s = 10.0 if args.mode == "sine" else 0.5
        if args.warmup_s < minimum_warmup_s:
            parser.error(
                f"{args.mode} mode requires --warmup-s of at least "
                f"{minimum_warmup_s:g} seconds")
        if args.telemetry not in (None, "both"):
            parser.error(f"{args.mode} mode requires --telemetry both")
        args.telemetry = "both"
        if args.mode == "sine":
            if args.frames is not None:
                parser.error("--frames is only valid in opposition mode")
            if args.rate_hz != 1000.0:
                parser.error("sine mode requires --rate-hz 1000")
            if args.scheduler not in (None, "bounded-catchup"):
                parser.error("sine mode requires --scheduler bounded-catchup")
            if not 0 < args.sine_amplitude_deg <= 5.0:
                parser.error("--sine-amplitude-deg must be in (0, 5]")
            if not 0 < args.sine_frequency_hz <= 1.0:
                parser.error("--sine-frequency-hz must be in (0, 1]")
            if args.max_tracking_error_rad >= math.radians(args.sine_amplitude_deg):
                parser.error(
                    "sine mode requires --max-tracking-error-rad below the sine amplitude")
            if args.max_initial_error_rad >= math.radians(args.sine_amplitude_deg):
                parser.error(
                    "sine mode requires --max-initial-error-rad below the sine amplitude")
            if args.max_tracking_error_rad > math.radians(2.0):
                parser.error("sine mode caps --max-tracking-error-rad at 2 degrees")
            if args.max_initial_error_rad > math.radians(2.0):
                parser.error("sine mode caps --max-initial-error-rad at 2 degrees")
            if args.watchdog_max_age_ms > 20.0:
                parser.error("sine mode caps --watchdog-max-age-ms at 20")
            if args.min_joint_response_rate_pct < 80:
                parser.error(
                    "sine mode requires --min-joint-response-rate-pct of at least 80")
            if args.sine_ramp_s > args.duration_s / 2.0:
                parser.error("--sine-ramp-s cannot exceed half of --duration-s")
            intervals = args.duration_s * args.rate_hz
            if not math.isclose(intervals, round(intervals), rel_tol=0.0, abs_tol=1e-7):
                parser.error("sine duration must contain an integer number of 1 kHz intervals")
            args.scheduler = "bounded-catchup"
            args.log_full_frames = True
            args.disable_cyclic_gc = True
            if args.max_command_lateness_ms is None:
                args.max_command_lateness_ms = 1.0
            if args.max_send_call_ms is None:
                args.max_send_call_ms = 1.0
            if args.max_command_lateness_ms > 1.0 or args.max_send_call_ms > 1.0:
                parser.error("sine mode timing abort thresholds cannot exceed 1 ms")
        else:
            args.scheduler = args.scheduler or "opposition"
    elif args.confirm_motion is not None:
        parser.error("--confirm-motion is only valid in an active mode")
    else:
        args.telemetry = args.telemetry or "diagnostics"
        args.scheduler = args.scheduler or "opposition"
        if args.frames is not None:
            parser.error("--frames is only valid in opposition mode")
    if args.mode != "sine" and (args.sine_amplitude_deg != 5.0
                                or args.sine_frequency_hz != 0.5
                                or args.sine_ramp_s != 2.0):
        parser.error("--sine-* options are only valid in sine mode")
    if args.diag_interval_s is None:
        args.diag_interval_s = 0.0
    return args


def _select_device(args, devices, sdk):
    hands = [device for device in devices if device.device_type == sdk.DeviceType.WujiHand2]
    if args.sn:
        matches = [device for device in hands if device.sn == args.sn]
    elif args.address:
        matches = [device for device in hands if device.address == args.address]
    else:
        matches = hands
    if len(matches) != 1:
        selector = f"serial {args.sn!r}" if args.sn else (
            f"address {args.address!r}" if args.address else "automatic selection")
        available = ", ".join(f"{device.sn}@{device.address}" for device in hands) or "none"
        raise RuntimeError(f"{selector} matched {len(matches)} Hand 2 devices; available: {available}")
    return matches[0]


def run(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (args.output_dir or ROOT / "build/connected-timing" / stamp).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    session_id = str(uuid.uuid4())
    wall_started = _utc_now()
    mono_started = time.perf_counter_ns()
    cpu_started = time.process_time()
    usage_started = resource.getrusage(resource.RUSAGE_SELF)
    report = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "status": "running",
        "mode": args.mode,
        "started_utc": wall_started,
        "host_monotonic_anchor_ns": mono_started,
        "config": {key: to_plain(value) for key, value in vars(args).items()},
        "validity": {
            "host_publish_cadence_measured": args.mode in ACTIVE_MODES,
            "telemetry_delivery_measured": args.telemetry != "none",
            "firmware_command_receipt_verified": False,
            "firmware_command_application_verified": False,
            "hard_realtime": False,
            "one_way_latency_measured": False,
        },
        "clock_domains": {
            "host": "time.perf_counter_ns, monotonic, local Python process",
            "device": "FrameHeader.timestamp_us, analyzed only as intervals",
            "warning": "host and device timestamps are not subtracted as absolute one-way latency",
        },
        "limitations": [
            "A successful publisher call means the local SDK/transport accepted the command, not that firmware applied it.",
            "Telemetry sequence gaps can result from the device, network, native queue, or Python consumer.",
            "The e2e and sdk_dropped diagnostic counters describe inbound telemetry/SDK handling, not command receipt.",
            "joint_states and joint_diagnostics can be correlated projections of the same device frame; their pair correlation is reported.",
            "comm_diag GET calls add their own RPC traffic; every call and duration is logged.",
            "Telemetry receive/decode threads add host CPU and scheduler load compared with the ordinary opposition script.",
            "Opposition mode sends a validated immutable replay snapshot; sine mode generates an immutable measured-pose-centered trajectory.",
            "Normal macOS and Python scheduling are not hard real-time.",
        ],
        "errors": [],
        "cleanup": [],
    }
    manager = hand = sampler = None
    receivers = []
    subscriptions = []
    command_records = []
    active_cleanup = []
    active_metadata = {}
    replay_info = None
    sine_info = None
    controller_restore = None
    abort = threading.Event()
    phase = Phase()
    changed_rate_subscriptions = []
    result_code = 1
    pending_error = None
    interrupted = False
    sdk = None
    device = None
    baseline_diag = final_diag = None
    gc_initially_enabled = gc.isenabled()
    gc_disabled_by_benchmark = False
    if args.disable_cyclic_gc and gc_initially_enabled:
        gc.disable()
        gc_disabled_by_benchmark = True
    report["python_gc"] = {
        "initially_enabled": gc_initially_enabled,
        "disabled_during_capture": bool(args.disable_cyclic_gc),
        "counts_at_start": list(gc.get_count()),
        "thresholds": list(gc.get_threshold()),
    }
    if args.disable_cyclic_gc:
        print("Python cyclic GC disabled for the capture; it will be restored during cleanup.")
    try:
        import wuji_sdk as sdk_module
        sdk = sdk_module
        from wuji_macos._native import backend_info

        try:
            distribution_version = importlib.metadata.version("wuji-macos")
        except importlib.metadata.PackageNotFoundError:
            distribution_version = None

        report["host"] = {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
            "python_executable": sys.executable,
            "sdk_module": sdk.__file__,
            "sdk_distribution_version": distribution_version,
            "backend": backend_info(),
            "wuji_zenoh_proto_environment": os.environ.get("WUJI_ZENOH_PROTO"),
            "script": str(Path(__file__).resolve()),
            "script_sha256": _sha256(__file__),
        }
        manager = sdk.SdkManager.instance()
        scan_start = time.perf_counter_ns()
        devices = manager.scan()
        scan_return = time.perf_counter_ns()
        report["discovery"] = {
            "duration_ns": scan_return - scan_start,
            "devices": [to_plain(item) for item in devices],
        }
        device = _select_device(args, devices, sdk)
        connect_kwargs = {"sn": device.sn} if args.sn or not args.address else {"address": device.address}
        connect_start = time.perf_counter_ns()
        hand = manager.connect(**connect_kwargs, device_name="connected_benchmark",
                               options=sdk.ConnectOptions(enable_bridge=False))
        connect_return = time.perf_counter_ns()
        if args.mode in ACTIVE_MODES and hand.serial_number != args.sn:
            raise RuntimeError(
                f"connected serial {hand.serial_number!r} does not match confirmed serial {args.sn!r}")
        info = hand.info
        try:
            hardware_version = to_plain(hand.hw_version().get())
        except Exception as error:
            hardware_version = {"error": str(error)}
        try:
            configured_ip = to_plain(hand.ip().get())
        except Exception as error:
            configured_ip = {"error": str(error)}
        handedness = str(hand.handedness().get()).lower()
        report["device"] = {
            "serial_number": hand.serial_number,
            "firmware_version": info.firmware_version,
            "hardware_version": hardware_version,
            "configured_ip": configured_ip,
            "address": device.address,
            "transport": str(device.transport_type),
            "discovered_device_type": str(device.device_type),
            "handedness": handedness,
            "connect_duration_ns": connect_return - connect_start,
            "enable_bridge": False,
            "is_connected_after_setup": bool(hand.is_connected),
        }
        print(f"Connected to {hand.serial_number} via {device.transport_type} at {device.address}")
        sampler = DiagnosticSampler(hand, phase, abort, args.diag_interval_s)
        preflight_diag = sampler.sample("preflight")
        if preflight_diag["error"]:
            raise RuntimeError(f"comm_diag preflight failed: {preflight_diag['error']}")
        online = _comm_online_joint_count(preflight_diag["value"])
        report["device"]["online_joints_from_comm_diag"] = online
        if args.mode in ACTIVE_MODES and online != 20:
            raise RuntimeError(
                f"{args.mode} requires 20 online joints; comm_diag reports {online}")

        stream_names = []
        if args.telemetry in ("diagnostics", "both"):
            stream_names.append("joint_diagnostics")
        if args.telemetry == "both":
            stream_names.append("joint_states")
        for name in stream_names:
            subscription = getattr(hand, name)().subscribe()
            subscriptions.append(subscription)
            report.setdefault("subscriptions", []).append({
                "name": name,
                "stream_id": int(subscription.stream_id),
                "dtype": str(subscription.dtype),
            })
            receiver = Receiver(name, subscription, phase, abort, args.poll_us)
            receivers.append(receiver)
            receiver.start()
        if args.request_telemetry_hz is not None:
            if not subscriptions:
                raise RuntimeError("--request-telemetry-hz requires at least one telemetry stream")
            applied_rates = {}
            for subscription_record, subscription in zip(
                    report.get("subscriptions", []), subscriptions):
                actual_rate = subscription.set_rate(args.request_telemetry_hz)
                changed_rate_subscriptions.append((subscription_record["name"], subscription))
                applied_rates[subscription_record["name"]] = actual_rate
            report["telemetry_rate"] = {
                "requested_hz": args.request_telemetry_hz,
                "device_applied_hz_by_stream": applied_rates,
            }
            print(f"Telemetry rate: requested {args.request_telemetry_hz} Hz; "
                  f"device applied {applied_rates}")
        else:
            report["telemetry_rate"] = {"requested_hz": None, "device_applied_hz": None,
                                        "note": "device current/default rate preserved"}

        phase.value = "warmup"
        print(f"Warming telemetry for {args.warmup_s:.1f} s ...")
        _wait(args.warmup_s, abort)
        for receiver in receivers:
            if receiver.latest() is None:
                raise RuntimeError(f"no {receiver.name} telemetry arrived during warmup")
        if args.mode in ACTIVE_MODES:
            active_metadata["warmup_streams"] = {}
            for receiver in receivers:
                warmup_summary = _telemetry_summary(receiver, phase="warmup")
                active_metadata["warmup_streams"][receiver.name] = warmup_summary
                device_rate = warmup_summary["device_timestamp_cadence"].get("rate_hz")
                device_intervals = (
                    warmup_summary["device_timestamp_cadence"].get("intervals") or {})
                host_intervals = warmup_summary["host_delivery_cadence"].get("intervals") or {}
                sequence = warmup_summary["sequence"]
                minimum_samples = (math.floor(args.warmup_s * 995.0)
                                   if args.mode == "sine" else 100)
                rate_low, rate_high = ((999.0, 1001.0)
                                       if args.mode == "sine" else (950.0, 1050.0))
                if (warmup_summary["samples"] < minimum_samples or device_rate is None
                        or not rate_low <= device_rate <= rate_high):
                    raise RuntimeError(
                        f"{receiver.name} warmup did not establish a 1 kHz device stream: "
                        f"samples={warmup_summary['samples']} (minimum {minimum_samples}), "
                        f"rate={device_rate} (required [{rate_low}, {rate_high}])")
                if (args.mode == "sine"
                        and (device_intervals.get("p99_ms") is None
                             or device_intervals["p99_ms"] > 1.1
                             or device_intervals.get("max_ms") is None
                             or device_intervals["max_ms"] > 1.5)):
                    raise RuntimeError(
                        f"{receiver.name} warmup device timestamp jitter was too large: "
                        f"p99={device_intervals.get('p99_ms')} ms, "
                        f"max={device_intervals.get('max_ms')} ms")
                if (args.mode == "sine"
                        and (host_intervals.get("p99_ms") is None
                             or host_intervals["p99_ms"] > 1.5
                             or host_intervals.get("max_ms") is None
                             or host_intervals["max_ms"] >= args.watchdog_max_age_ms)):
                    raise RuntimeError(
                        f"{receiver.name} warmup host delivery jitter was too large: "
                        f"p99={host_intervals.get('p99_ms')} ms, "
                        f"max={host_intervals.get('max_ms')} ms; limits are 1.5 ms p99 "
                        f"and less than {args.watchdog_max_age_ms:g} ms maximum")
                if (sequence["missing_sequence_values"]
                        or sequence["duplicate_transitions"]
                        or sequence["reordered_transitions"]
                        or warmup_summary["native_receiver_lagged_frames"]
                        or warmup_summary["receiver_errors"]):
                    raise RuntimeError(
                        f"{receiver.name} warmup was not lossless: "
                        f"sequence={sequence}, native_lagged="
                        f"{warmup_summary['native_receiver_lagged_frames']}, "
                        f"errors={warmup_summary['receiver_errors']}")
                warmup_records = [record for record in receiver.records
                                  if record[0] == "warmup"]
                warmup_timeout_totals = {}
                warmup_comm_counters = {}
                for record in warmup_records:
                    frame = record[3]
                    if receiver.name == "joint_diagnostics":
                        _joints_by_nid(frame)
                        faults = _diagnostic_faults(frame)
                        if faults:
                            raise RuntimeError(
                                f"joint fault/limit appeared during warmup: {faults}")
                        for joint in frame.joints:
                            nid = int(joint.nid)
                            response_rate = int(joint.comm_response_rate_pct)
                            if response_rate < args.min_joint_response_rate_pct:
                                raise RuntimeError(
                                    f"NID {nid} warmup response rate is {response_rate}%; "
                                    f"minimum is {args.min_joint_response_rate_pct}%")
                            timeout_total = int(joint.comm_timeout_total)
                            initial_timeout_total = warmup_timeout_totals.setdefault(
                                nid, timeout_total)
                            if timeout_total != initial_timeout_total:
                                raise RuntimeError(
                                    f"NID {nid} comm_timeout_total changed during warmup: "
                                    f"{initial_timeout_total} -> {timeout_total}")
                        comm = getattr(frame, "comm", None)
                        for name in DIAGNOSTIC_FAILURE_COUNTERS:
                            value = int(getattr(comm, name, 0))
                            initial = warmup_comm_counters.setdefault(name, value)
                            if value != initial:
                                raise RuntimeError(
                                    f"{name} counter changed during warmup: "
                                    f"{initial} -> {value}")
                    else:
                        _validated_state_positions(frame)
        baseline_diag_record = sampler.sample("baseline")
        baseline_diag = baseline_diag_record["value"]
        if baseline_diag_record["error"]:
            raise RuntimeError(f"baseline comm_diag failed: {baseline_diag_record['error']}")
        sampler.start()

        if args.mode == "observe":
            phase.value = "measure"
            print(f"Observing the link read-only for {args.duration_s:.1f} s ...")
            _wait(args.duration_s, abort)
        else:
            diagnostic_receiver = next(item for item in receivers if item.name == "joint_diagnostics")
            state_receiver = next(item for item in receivers if item.name == "joint_states")
            replay_bytes = None
            if args.mode == "opposition":
                trajectory_module, opposition_path = _load_opposition_module()
                side = trajectory_module._side_for_hand(hand)
                replay_info, replay_bytes = _inspect_replay(
                    trajectory_module, side, args.frames, args.rate_hz,
                    args.max_replay_step_rad, retain_bytes=True)
                replay_path = ROOT / f"examples/python/wuji_hand_2/6_opposition/data/{side}.replay"
                replay_info.update({"type": "opposition", "side": side,
                                    "path": str(replay_path),
                                    "opposition_script": str(opposition_path),
                                    "opposition_script_sha256": _sha256(opposition_path)})
                motion_info = replay_info
                first_positions = replay_info["first_positions"]
                report["replay"] = replay_info
                print(f"Active opposition benchmark: {replay_info['selected_frames']} frames at "
                      f"{args.rate_hz:g} Hz on exact serial {args.sn}")
            else:
                trajectory_module = None
                side = handedness
                motion_info = None
                first_positions = None
                print(f"Active sine benchmark: {args.sine_amplitude_deg:g} degree peak, "
                      f"{args.sine_frequency_hz:g} Hz, every joint, "
                      f"{args.duration_s:g} s at {args.rate_hz:g} Hz on exact serial "
                      f"{args.sn}")
            pre_countdown_diagnostic_record = diagnostic_receiver.latest_record()
            pre_countdown_state_record = state_receiver.latest_record()
            diagnostic_frame = pre_countdown_diagnostic_record[3]
            state_frame = pre_countdown_state_record[3]
            if args.mode == "sine":
                pre_countdown_positions, pre_countdown_velocities = \
                    _validated_stationary_state(state_frame)
                first_positions = pre_countdown_positions
                active_metadata["pre_countdown_stationary"] = {
                    "state_seq": int(state_frame.header.seq),
                    "state_timestamp_us": int(state_frame.header.timestamp_us),
                    "positions_rad": pre_countdown_positions,
                    "velocities_rad_s": pre_countdown_velocities,
                }
            motion_preflight = _validate_active_frames(
                diagnostic_frame, state_frame, first_positions,
                args.max_initial_error_rad)
            active_metadata["pre_countdown_ready_check"] = _require_all_ready(
                diagnostic_frame, "active preflight")
            if motion_info is not None:
                motion_info["pre_countdown"] = motion_preflight
            countdown_cursors = _watchdog_cursors_after(
                diagnostic_receiver, pre_countdown_diagnostic_record,
                state_receiver, pre_countdown_state_record)
            print("Motors will enable and the hand will move in 3 seconds. Ctrl+C to abort.")
            diagnostic_seq = int(diagnostic_frame.header.seq)
            state_seq = int(state_frame.header.seq)
            _wait(3.0, abort)
            diagnostic_record, diagnostic_age_ms = _wait_for_fresh_record(
                diagnostic_receiver, diagnostic_seq, abort,
                max_age_ms=args.watchdog_max_age_ms)
            state_record, state_age_ms = _wait_for_fresh_record(
                state_receiver, state_seq, abort,
                max_age_ms=args.watchdog_max_age_ms)
            countdown_check = _motion_watchdog(
                diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                countdown_cursors, require_enabled=False, require_ready=True,
                min_response_rate_pct=args.min_joint_response_rate_pct)
            if motion_info is not None:
                motion_info["countdown_telemetry_check"] = countdown_check
            diagnostic_record = diagnostic_receiver.latest_record()
            state_record = state_receiver.latest_record()
            diagnostic_age_ms = (time.perf_counter_ns() - diagnostic_record[1]) / 1e6
            state_age_ms = (time.perf_counter_ns() - state_record[1]) / 1e6
            if (diagnostic_age_ms > args.watchdog_max_age_ms
                    or state_age_ms > args.watchdog_max_age_ms):
                raise RuntimeError(
                    "telemetry became stale while validating the countdown window: "
                    f"diagnostics={diagnostic_age_ms:.3f} ms, "
                    f"state={state_age_ms:.3f} ms")
            if args.mode == "sine":
                center_positions, center_velocities = _validated_stationary_state(
                    state_record[3])
                drift = [abs(after - before) for after, before in zip(
                    center_positions, pre_countdown_positions)]
                max_drift = max(drift)
                if max_drift > args.max_initial_error_rad:
                    dense_index = drift.index(max_drift)
                    raise RuntimeError(
                        f"sine center moved {max_drift:.3f} rad during countdown at dense "
                        f"joint {dense_index} (NID {EXPECTED_MOTOR_NIDS[dense_index]}); "
                        f"limit is {args.max_initial_error_rad:.3f} rad")
                trajectory_module, sine_info = _build_sine_trajectory(
                    center_positions, args.rate_hz, args.duration_s,
                    args.sine_amplitude_deg, args.sine_frequency_hz,
                    args.sine_ramp_s, args.max_replay_step_rad,
                    joint_limit_guard_rad=SINE_JOINT_LIMIT_GUARD_RAD)
                first_positions = sine_info["first_positions"]
                sine_info["center_state_seq"] = int(state_record[3].header.seq)
                sine_info["center_state_timestamp_us"] = int(
                    state_record[3].header.timestamp_us)
                sine_info["center_state_host_age_ms"] = state_age_ms
                sine_info["center_velocities_rad_s"] = center_velocities
                sine_info["pre_countdown_to_center_max_drift_rad"] = max_drift
                sine_info["pre_countdown"] = motion_preflight
                sine_info["countdown_telemetry_check"] = countdown_check
                motion_info = sine_info
                report["sine"] = sine_info
            pre_controller_check = _motion_watchdog(
                diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                countdown_cursors, require_enabled=False, require_ready=True,
                min_response_rate_pct=args.min_joint_response_rate_pct,
                center_positions=(first_positions if args.mode == "sine" else None),
                max_excursion_rad=(args.max_initial_error_rad
                                   if args.mode == "sine" else None),
                max_velocity_rad_s=(SINE_STATIONARY_MAX_VELOCITY_RAD_S
                                    if args.mode == "sine" else None))
            active_metadata["pre_controller_write_telemetry_check"] = pre_controller_check
            diagnostic_record = diagnostic_receiver.latest_record()
            state_record = state_receiver.latest_record()
            diagnostic_age_ms = (time.perf_counter_ns() - diagnostic_record[1]) / 1e6
            state_age_ms = (time.perf_counter_ns() - state_record[1]) / 1e6
            motion_info["post_countdown_pre_enable"] = _validate_active_frames(
                diagnostic_record[3], state_record[3], first_positions,
                args.max_initial_error_rad)
            motion_info["post_countdown_pre_enable"].update({
                "diagnostic_host_age_ms": diagnostic_age_ms,
                "state_host_age_ms": state_age_ms,
            })
            active_metadata["pre_controller_write_ready_check"] = _require_all_ready(
                diagnostic_record[3], "controller-parameter update")
            if args.mode == "sine":
                controller_restore = _capture_controller_settings(hand)
                active_metadata["controller_before_run"] = {
                    **to_plain(controller_restore),
                    "note": ("captured immediately before the safety write; restored only "
                             "after fresh telemetry confirms every motor disabled"),
                }
                active_metadata["post_controller_capture_telemetry_check"] = \
                    _motion_watchdog(
                        diagnostic_receiver, state_receiver, args.watchdog_max_age_ms,
                        countdown_cursors, require_enabled=False, require_ready=True,
                        min_response_rate_pct=args.min_joint_response_rate_pct,
                        center_positions=first_positions,
                        max_excursion_rad=args.max_initial_error_rad,
                        max_velocity_rad_s=SINE_STATIONARY_MAX_VELOCITY_RAD_S)
                active_metadata["post_controller_capture_ready_check"] = _require_all_ready(
                    diagnostic_receiver.latest_record()[3],
                    "controller-parameter update after snapshot")
                if STOP_REQUESTED.is_set():
                    raise InterruptedError("operator stop requested before controller update")
                if abort.is_set():
                    raise RuntimeError(
                        "telemetry receiver stopped unexpectedly before controller update")
                active_metadata["controller_for_run"] = \
                    _apply_sine_controller_settings(hand)
            else:
                active_metadata["controller_before_run"] = {
                    "effort_limit": to_plain(hand.effort_limit().get()),
                    "mit_params": to_plain(hand.mit_params().get()),
                    "note": "recorded without modification in opposition mode",
                }
            _run_opposition(args, sdk, hand, trajectory_module, side, abort, phase,
                            diagnostic_receiver, state_receiver, command_records,
                            active_cleanup, active_metadata,
                            first_positions, replay_bytes,
                            watchdog_cursors=countdown_cursors)
            report["cleanup"].extend(active_cleanup)
            disable_confirmation = next(
                (item for item in reversed(active_cleanup)
                 if item.get("operation") == "disable_and_confirm_telemetry_confirmation"),
                None)
            if not disable_confirmation or not disable_confirmation.get("ok"):
                raise RuntimeError(
                    "immediate post-trajectory disable was not confirmed; skipping settle work")
            report["active_control"] = active_metadata
            phase.value = "settle"
            _wait(args.settle_s, abort)

        phase.value = "final"
        sampler.stop()
        final_diag_record = sampler.sample("final")
        final_diag = final_diag_record["value"]
        if final_diag_record["error"]:
            raise RuntimeError(f"final comm_diag failed: {final_diag_record['error']}")
        result_code = 0
        report["status"] = "complete"
    except (KeyboardInterrupt, InterruptedError) as error:
        interrupted = True
        STOP_REQUESTED.set()
        pending_error = error
        result_code = 130
        report["status"] = "interrupted"
        report["errors"].append(str(error))
    except Exception as error:
        pending_error = error
        result_code = 1
        report["status"] = "error"
        report["errors"].append(str(error))
    finally:
        # Confirm a final disabled state before stopping telemetry. If the
        # sender's immediate disable was already unconfirmed, escalate directly
        # instead of waiting through another normal retry window.
        motors_confirmed_disabled = False
        if args.mode in ACTIVE_MODES and hand is not None:
            diagnostic_receiver_for_cleanup = next(
                (item for item in receivers if item.name == "joint_diagnostics"), None)
            immediate_confirmation = next(
                (item for item in reversed(active_cleanup)
                 if item.get("operation") == "disable_and_confirm_telemetry_confirmation"),
                None)
            immediate_disable_failed = (
                immediate_confirmation is not None and not immediate_confirmation.get("ok"))
            if immediate_disable_failed:
                disable_confirmed = False
            else:
                disable_confirmed = _disable_and_confirm(
                    hand, diagnostic_receiver_for_cleanup, report["cleanup"],
                    "safety_disable_retry", timeout_s=0.25)
            motors_confirmed_disabled = disable_confirmed
            if not disable_confirmed:
                emergency_started = time.perf_counter_ns()
                try:
                    hand.emergency_stop()
                    report["cleanup"].append({
                        "operation": "emergency_stop_fallback",
                        "ok": True,
                        "call_duration_ns": time.perf_counter_ns() - emergency_started,
                    })
                except Exception as error:
                    report["cleanup"].append({
                        "operation": "emergency_stop_fallback",
                        "ok": False,
                        "call_duration_ns": time.perf_counter_ns() - emergency_started,
                        "error": str(error),
                    })
                fallback_confirmed = _disable_and_confirm(
                    hand, diagnostic_receiver_for_cleanup, report["cleanup"],
                    "post_emergency_stop_disable", timeout_s=0.25)
                motors_confirmed_disabled = fallback_confirmed
                if not fallback_confirmed:
                    print("SAFETY WARNING: telemetry did not confirm that every motor is "
                          "disabled after normal disable retries and an emergency-stop "
                          "fallback. Keep clear of the hand and verify the hardware state "
                          "before removing power.", file=sys.stderr)
            if controller_restore is not None:
                if not motors_confirmed_disabled:
                    report["cleanup"].append({
                        "operation": "restore_controller_parameters",
                        "ok": False,
                        "error": ("skipped because fresh telemetry did not confirm that all "
                                  "motors were disabled"),
                    })
                else:
                    restore_started = time.perf_counter_ns()
                    try:
                        restore_result = _restore_controller_settings(
                            hand, controller_restore)
                        report["cleanup"].append({
                            "operation": "restore_controller_parameters",
                            "ok": True,
                            "call_duration_ns": time.perf_counter_ns() - restore_started,
                            **restore_result,
                        })
                    except Exception as error:
                        report["cleanup"].append({
                            "operation": "restore_controller_parameters",
                            "ok": False,
                            "call_duration_ns": time.perf_counter_ns() - restore_started,
                            "error": str(error),
                        })
        abort.set()
        if sampler is not None:
            if not sampler.stop():
                report["cleanup"].append({"operation": "stop_diag_sampler", "ok": False,
                                          "error": "thread did not stop"})
        for receiver in receivers:
            stopped = receiver.stop()
            report["cleanup"].append({"operation": f"stop_{receiver.name}_receiver", "ok": stopped})
        for stream_name, subscription in changed_rate_subscriptions:
            try:
                restored = subscription.set_rate(0)
                report["cleanup"].append({
                    "operation": "restore_telemetry_rate",
                    "stream": stream_name,
                    "stream_id": int(subscription.stream_id),
                    "ok": True,
                    "device_applied_hz": restored,
                })
            except Exception as error:
                report["cleanup"].append({
                    "operation": "restore_telemetry_rate",
                    "stream": stream_name,
                    "stream_id": int(subscription.stream_id),
                    "ok": False,
                    "error": str(error),
                })
        for subscription in subscriptions:
            try:
                subscription.close()
                report["cleanup"].append({"operation": "close_subscription", "ok": True})
            except Exception as error:
                report["cleanup"].append({"operation": "close_subscription", "ok": False,
                                          "error": str(error)})
        if hand is not None:
            try:
                report.setdefault("device", {})["is_connected_before_disconnect"] = bool(hand.is_connected)
            except Exception as error:
                report.setdefault("device", {})["is_connected_before_disconnect_error"] = str(error)
            try:
                hand.disconnect()
                report["cleanup"].append({"operation": "disconnect", "ok": True})
            except Exception as error:
                report["cleanup"].append({"operation": "disconnect", "ok": False,
                                          "error": str(error)})
                try:
                    manager.disconnect_all()
                    report["cleanup"].append({"operation": "disconnect_all_fallback", "ok": True})
                except Exception as fallback_error:
                    report["cleanup"].append({"operation": "disconnect_all_fallback", "ok": False,
                                              "error": str(fallback_error)})
        if gc_disabled_by_benchmark:
            gc.enable()
            report["cleanup"].append({
                "operation": "restore_python_cyclic_gc",
                "ok": True,
                "enabled": gc.isenabled(),
            })
        report["python_gc"]["enabled_after_cleanup"] = gc.isenabled()
        report["python_gc"]["counts_after_cleanup"] = list(gc.get_count())

        cpu_s = time.process_time() - cpu_started
        wall_s = (time.perf_counter_ns() - mono_started) / 1e9
        usage_final = resource.getrusage(resource.RUSAGE_SELF)
        report["finished_utc"] = _utc_now()
        report["elapsed_s"] = wall_s
        report["process_cpu_s"] = cpu_s
        report["process_cpu_to_wall_ratio"] = cpu_s / wall_s if wall_s else None
        report["resource_usage"] = {
            "max_rss": usage_final.ru_maxrss,
            "voluntary_context_switches_delta": usage_final.ru_nvcsw - usage_started.ru_nvcsw,
            "involuntary_context_switches_delta": usage_final.ru_nivcsw - usage_started.ru_nivcsw,
        }
        report["telemetry"] = {receiver.name: _telemetry_summary(receiver) for receiver in receivers}
        report["telemetry_correlation"] = _telemetry_correlation(receivers)
        successful_command_count = sum(record.get("send_ok", False)
                                       for record in command_records)
        report["validity"]["host_publish_cadence_measured"] = \
            successful_command_count >= 2
        report["validity"]["telemetry_delivery_measured"] = any(
            telemetry.get("samples", 0) > 0 for telemetry in report["telemetry"].values())
        report["receiver_lag_events"] = {
            receiver.name: receiver.lag_events for receiver in receivers if receiver.lag_events
        }
        report["receiver_errors"] = {
            receiver.name: receiver.errors for receiver in receivers if receiver.errors
        }
        report["command"] = (_command_summary(command_records, args.rate_hz, args.scheduler)
                             if command_records else None)
        if active_metadata:
            report["active_control"] = active_metadata
        for item in active_cleanup:
            if item not in report["cleanup"]:
                report["cleanup"].append(item)
        if replay_info is not None:
            report["replay"] = replay_info
        report["comm_diag"] = {
            "samples": sampler.records if sampler is not None else [],
            "baseline_to_final": _communication_summary(baseline_diag, final_diag),
        }
        diagnostic_receiver = next((item for item in receivers
                                    if item.name == "joint_diagnostics"), None)
        report["diagnostic_stream_delta"] = (
            _diagnostic_stream_delta(diagnostic_receiver) if diagnostic_receiver else None)
        report["communication_health"] = _communication_health(
            report["comm_diag"]["baseline_to_final"], report["diagnostic_stream_delta"])
        if sine_info is not None:
            state_receiver = next(
                (item for item in receivers if item.name == "joint_states"), None)
            if state_receiver is not None:
                sine_info["response"] = _sine_response_summary(
                    state_receiver, sine_info["center_positions_rad"])
            report["sine"] = sine_info

        cleanup_failures = [item for item in report["cleanup"] if not item.get("ok", False)]
        if cleanup_failures:
            report["cleanup_failures"] = cleanup_failures
            cleanup_message = "cleanup failed: " + "; ".join(
                f"{item.get('operation')}: {item.get('error', 'unknown failure')}"
                for item in cleanup_failures
            )
            report["errors"].append(cleanup_message)
            if result_code == 0:
                result_code = 1
                report["status"] = "cleanup_error"
        if sine_info is not None:
            report["sine_acceptance"] = _sine_acceptance(report)
            acceptance = report["sine_acceptance"]
            if (result_code == 0
                    and (not acceptance["communication_pass"]
                         or not acceptance["tracking_pass"])):
                result_code = 2
                report["status"] = "benchmark_failed"
                report["errors"].append(
                    "sine benchmark completed safely but did not pass all communication "
                    "and tracking acceptance checks")

        try:
            _write_commands(output_dir / "commands.csv", session_id, command_records)
            _write_telemetry(output_dir / "telemetry.csv", session_id, receivers,
                             log_full_frames=args.log_full_frames)
            _write_diag_jsonl(output_dir / "comm_diag.jsonl", session_id,
                              sampler.records if sampler is not None else [])
            _write_json(output_dir / "session.json", {
                key: report.get(key) for key in (
                    "schema_version", "session_id", "mode", "started_utc", "finished_utc",
                    "host_monotonic_anchor_ns", "config", "host", "discovery", "device",
                    "python_gc",
                    "subscriptions", "telemetry_rate", "replay", "sine", "sine_acceptance",
                    "active_control", "clock_domains",
                    "limitations", "validity")
                if key in report
            })
            _write_json(output_dir / "summary.json", report)
        except Exception as write_error:
            print(f"error writing benchmark logs: {write_error}", file=sys.stderr)
            result_code = 1
            report["status"] = "error"
            report["errors"].append(f"writing benchmark logs failed: {write_error}")
            if sine_info is not None:
                report["sine_acceptance"] = _sine_acceptance(report)
            try:
                _write_json(output_dir / "summary.json", report)
            except Exception:
                pass
        _print_summary(report, output_dir)
        if pending_error is not None and not interrupted:
            print(f"error: {pending_error}", file=sys.stderr)
    return result_code


def _on_signal(_signum, _frame):
    STOP_REQUESTED.set()


def main(argv=None):
    STOP_REQUESTED.clear()
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    raise SystemExit(main())
