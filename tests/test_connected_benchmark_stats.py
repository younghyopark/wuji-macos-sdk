"""Hardware-free statistics checks for the connected Hand 2 benchmark."""

from __future__ import annotations

import copy
from contextlib import redirect_stderr
import importlib.util
import io
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wuji_sdk import _types


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_hand2_connected.py"
SPEC = importlib.util.spec_from_file_location("benchmark_hand2_connected", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class DistributionTests(unittest.TestCase):
    def test_empty_and_single_value_distributions(self):
        self.assertIsNone(benchmark.distribution_ns([]))

        result = benchmark.distribution_ns([1_250_000])
        self.assertEqual(result["count"], 1)
        for name in ("mean_ms", "min_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"):
            self.assertEqual(result[name], 1.25)

    def test_cadence_reports_one_ms_intervals_and_long_gap(self):
        result = benchmark.cadence_ns([0, 1_000_000, 2_000_000, 8_000_000])

        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["span_s"], 0.008)
        self.assertEqual(result["rate_hz"], 375.0)
        intervals = result["intervals"]
        self.assertEqual(intervals["count"], 3)
        self.assertEqual(intervals["p50_ms"], 1.0)
        self.assertEqual(intervals["max_ms"], 6.0)
        self.assertEqual(intervals["over_5_ms"], 1)

    def test_signed_residuals_keep_their_sign_and_report_absolute_size(self):
        result = benchmark.signed_distribution_ns([-1_000_000, 1_000_000])

        self.assertEqual(result["mean_ms"], 0.0)
        self.assertEqual(result["min_ms"], -1.0)
        self.assertEqual(result["max_ms"], 1.0)
        self.assertEqual(result["absolute"]["mean_ms"], 1.0)


class SequenceTests(unittest.TestCase):
    def test_contiguous_sequences(self):
        result = benchmark.sequence_summary([10, 11, 12])

        self.assertEqual(result["contiguous_transitions"], 2)
        self.assertEqual(result["forward_gap_events"], 0)
        self.assertEqual(result["missing_sequence_values"], 0)
        self.assertEqual(result["duplicate_transitions"], 0)
        self.assertEqual(result["reordered_transitions"], 0)

    def test_unresolved_forward_gap(self):
        result = benchmark.sequence_summary([10, 13])

        self.assertEqual(result["forward_gap_events"], 1)
        self.assertEqual(result["missing_sequence_values"], 2)
        self.assertEqual(result["reordered_transitions"], 0)

    def test_duplicate_sequence(self):
        result = benchmark.sequence_summary([10, 10])

        self.assertEqual(result["duplicate_transitions"], 1)
        self.assertEqual(result["missing_sequence_values"], 0)

    def test_late_sequence_recovers_a_provisional_gap(self):
        result = benchmark.sequence_summary([10, 12, 11, 13])

        self.assertEqual(result["forward_gap_events"], 1)
        self.assertEqual(result["reordered_transitions"], 1)
        self.assertEqual(result["recovered_reordered_values"], 1)
        self.assertEqual(result["missing_sequence_values"], 0)

    def test_u32_wrap_is_contiguous(self):
        result = benchmark.sequence_summary([0xFFFFFFFE, 0xFFFFFFFF, 0])

        self.assertEqual(result["contiguous_transitions"], 2)
        self.assertEqual(result["u32_wrap_transitions"], 1)
        self.assertEqual(result["forward_gap_events"], 0)
        self.assertEqual(result["missing_sequence_values"], 0)
        self.assertEqual(result["backward_or_restart_events"], 0)


class CounterTests(unittest.TestCase):
    def test_positive_zero_and_backward_changes(self):
        self.assertEqual(benchmark.counter_change(7, 12), 5)
        self.assertEqual(benchmark.counter_change(7, 7), 0)
        self.assertIsNone(benchmark.counter_change(12, 7))


class JointMappingTests(unittest.TestCase):
    def test_sparse_nids_are_reordered_to_dense_command_order(self):
        joints = [SimpleNamespace(nid=nid, position=float(index))
                  for index, nid in enumerate(benchmark.EXPECTED_MOTOR_NIDS)]
        frame = SimpleNamespace(num_joints=20, joints=list(reversed(joints)))

        positions = benchmark._positions_in_dense_command_order(frame)

        self.assertEqual(positions, list(map(float, range(20))))

    def test_unexpected_sparse_nid_is_rejected(self):
        joints = [SimpleNamespace(nid=nid, position=0.0)
                  for nid in benchmark.EXPECTED_MOTOR_NIDS]
        joints[-1] = SimpleNamespace(nid=25, position=0.0)
        frame = SimpleNamespace(num_joints=20, joints=joints)

        with self.assertRaisesRegex(RuntimeError, "exact 20 motor NIDs"):
            benchmark._positions_in_dense_command_order(frame)


class ReplayValidationTests(unittest.TestCase):
    @staticmethod
    def module_for(frames):
        class Stream:
            def __init__(self):
                self.frames = list(frames)
                self.closed = False

            def close(self):
                self.closed = True

        stream = Stream()

        class Module:
            @staticmethod
            def _open_replay(side):
                return stream, len(frames)

            @staticmethod
            def _read_qpos(open_stream):
                return open_stream.frames.pop(0)

        return Module, stream

    def test_full_replay_is_selected_when_frame_limit_is_omitted(self):
        frames = [[0.0] * 20, [0.001] * 20, [0.002] * 20]
        module, stream = self.module_for(frames)

        result = benchmark._inspect_replay(module, "right", None, 1000.0, 0.02)

        self.assertEqual(result["selected_frames"], 3)
        self.assertAlmostEqual(result["max_adjacent_step_rad"], 0.001)
        self.assertTrue(stream.closed)

    def test_out_of_range_replay_position_is_rejected(self):
        frame = [0.0] * 20
        frame[0] = 2.0
        module, stream = self.module_for([frame, frame])

        with self.assertRaisesRegex(RuntimeError, "outside documented"):
            benchmark._inspect_replay(module, "right", None, 1000.0, 0.02)
        self.assertTrue(stream.closed)

    def test_large_adjacent_replay_step_is_rejected(self):
        first = [0.0] * 20
        second = [0.0] * 20
        second[0] = 0.03
        module, stream = self.module_for([first, second])

        with self.assertRaisesRegex(RuntimeError, "step is"):
            benchmark._inspect_replay(module, "right", None, 1000.0, 0.02)
        self.assertTrue(stream.closed)


class SineTrajectoryTests(unittest.TestCase):
    @staticmethod
    def safe_centers():
        return [(low + high) / 2.0 for low, high in benchmark.JOINT_LIMITS_RAD]

    def build(self, centers=None):
        return benchmark._build_sine_trajectory(
            centers or self.safe_centers(), 1000.0, 10.0, 5.0, 0.5, 2.0,
            0.02, joint_limit_guard_rad=benchmark.SINE_JOINT_LIMIT_GUARD_RAD)

    def test_sine_has_inclusive_endpoints_and_full_five_degree_peaks(self):
        centers = self.safe_centers()
        trajectory, info = self.build(centers)

        self.assertEqual(info["selected_frames"], 10_001)
        self.assertEqual(info["interval_count"], 10_000)
        self.assertEqual(info["first_positions"], centers)
        self.assertEqual(info["last_positions"], centers)
        positive = benchmark._sine_positions(
            tuple(centers), 2500, 1000.0, 10.0,
            math.radians(5.0), 0.5, 2.0)
        negative = benchmark._sine_positions(
            tuple(centers), 3500, 1000.0, 10.0,
            math.radians(5.0), 0.5, 2.0)
        for center, high, low in zip(centers, positive, negative):
            self.assertAlmostEqual(high - center, math.radians(5.0), places=12)
            self.assertAlmostEqual(low - center, -math.radians(5.0), places=12)
        self.assertLessEqual(info["max_adjacent_step_rad"], 0.00028)
        self.assertEqual(info["joint_limit_guard_rad"],
                         benchmark.SINE_JOINT_LIMIT_GUARD_RAD)
        self.assertTrue(all(math.isclose(value, math.radians(5.0), abs_tol=1e-12)
                            for value in info["achieved_target_peak_offsets_rad"]))

        stream, count = trajectory._open_replay("right")
        self.assertEqual(count, 10_001)
        self.assertEqual(trajectory._read_qpos(stream), tuple(centers))
        stream.close()

    def test_sine_center_is_an_immutable_snapshot(self):
        centers = self.safe_centers()
        expected = tuple(centers)
        trajectory, _info = self.build(centers)
        centers[0] += 1.0

        stream, _count = trajectory._open_replay("right")
        self.assertEqual(trajectory._read_qpos(stream), expected)
        stream.close()

    def test_sine_refuses_a_joint_limit_envelope_plus_guard(self):
        centers = self.safe_centers()
        low, _high = benchmark.JOINT_LIMITS_RAD[0]
        centers[0] = (low + math.radians(5.0)
                      + benchmark.SINE_JOINT_LIMIT_GUARD_RAD - 1e-6)

        with self.assertRaisesRegex(RuntimeError, "NID 1"):
            self.build(centers)

    def test_sine_refuses_target_speed_above_measured_velocity_guard(self):
        with self.assertRaisesRegex(RuntimeError, "target speed"):
            benchmark._build_sine_trajectory(
                self.safe_centers(), 1000.0, 10.0, 5.0, 1.0, 0.01,
                0.02, joint_limit_guard_rad=benchmark.SINE_JOINT_LIMIT_GUARD_RAD)


class ControllerSettingsTests(unittest.TestCase):
    class Resource:
        def __init__(self, values, *, mit=False, ignore_set=False):
            self.values = list(values)
            self.mit = mit
            self.ignore_set = ignore_set
            self.set_calls = []

        def get(self):
            if self.mit:
                return [SimpleNamespace(kp=kp, kd=kd) for kp, kd in self.values]
            return list(self.values)

        def set(self, value):
            self.set_calls.append(value)
            if self.ignore_set:
                return
            if self.mit:
                self.values = (list(value) if isinstance(value, list)
                               else [tuple(value)] * 20)
            else:
                self.values = (list(value) if isinstance(value, (list, tuple))
                               else [float(value)] * 20)

    class Hand:
        def __init__(self, effort, mit, *, ignore_effort_set=False):
            self.effort = ControllerSettingsTests.Resource(
                effort, ignore_set=ignore_effort_set)
            self.mit = ControllerSettingsTests.Resource(mit, mit=True)

        def effort_limit(self):
            return self.effort

        def mit_params(self):
            return self.mit

    def test_heterogeneous_controller_snapshot_applies_safe_values_and_restores(self):
        effort = [0.25 + index * 0.01 for index in range(20)]
        mit = [(3.0 + index * 0.1, 0.01 + index * 0.001) for index in range(20)]
        hand = self.Hand(effort, mit)

        snapshot = benchmark._capture_controller_settings(hand)
        applied = benchmark._apply_sine_controller_settings(hand)
        self.assertTrue(applied["readback_verified_all_20"])
        self.assertEqual(hand.effort.values, [benchmark.SINE_SAFE_EFFORT_LIMIT_A] * 20)
        self.assertEqual(hand.mit.values,
                         [(benchmark.SINE_SAFE_KP, benchmark.SINE_SAFE_KD)] * 20)

        restored = benchmark._restore_controller_settings(hand, snapshot)
        self.assertTrue(restored["readback_verified_all_20"])
        self.assertEqual(hand.effort.values, effort)
        self.assertEqual(hand.mit.values, mit)

    def test_controller_snapshot_rejects_unrestorable_negative_value(self):
        effort = [0.5] * 20
        effort[7] = -0.1
        hand = self.Hand(effort, [(3.0, 0.05)] * 20)

        with self.assertRaisesRegex(RuntimeError, "finite nonnegative"):
            benchmark._capture_controller_settings(hand)

    def test_controller_apply_requires_readback_match(self):
        hand = self.Hand([1.0] * 20, [(5.0, 0.01)] * 20,
                         ignore_effort_set=True)

        with self.assertRaisesRegex(RuntimeError, "did not verify"):
            benchmark._apply_sine_controller_settings(hand)


class ActiveSafetyHelperTests(unittest.TestCase):
    @staticmethod
    def status(ext_state=2, *, limit=False):
        return SimpleNamespace(
            ext_state=ext_state,
            ext_state_name="Enabled" if ext_state == 2 else "Ready",
            position_limit_active=limit,
            velocity_limit_active=False,
            current_limit_active=False,
        )

    @classmethod
    def diagnostic(cls, seq, *, ext_state=2, fault_nid=None, limit_nid=None):
        return SimpleNamespace(
            header=SimpleNamespace(seq=seq, timestamp_us=seq * 1000),
            num_joints=20,
            joints=[SimpleNamespace(
                nid=nid,
                status_word=cls.status(ext_state, limit=nid == limit_nid),
                error_code_current=7 if nid == fault_nid else 0,
                comm_timeout_total=0,
                comm_response_rate_pct=100,
            ) for nid in benchmark.EXPECTED_MOTOR_NIDS],
            comm=SimpleNamespace(**{
                name: 0 for name in benchmark.DIAGNOSTIC_FAILURE_COUNTERS
            }),
        )

    @staticmethod
    def state(seq, positions=None, velocities=None):
        positions = [0.0] * 20 if positions is None else list(positions)
        velocities = [0.0] * 20 if velocities is None else list(velocities)
        return SimpleNamespace(
            header=SimpleNamespace(seq=seq, timestamp_us=seq * 1000),
            num_joints=20,
            joints=[SimpleNamespace(nid=nid, position=positions[index],
                                    velocity=velocities[index])
                    for index, nid in enumerate(benchmark.EXPECTED_MOTOR_NIDS)],
        )

    class MutableReceiver:
        def __init__(self, name, frame):
            self.name = name
            self.records = []
            self.lag_events = []
            self.push(frame)

        def push(self, frame, age_ms=0.0):
            host_ns = time.perf_counter_ns() - int(age_ms * 1e6)
            self.records.append(("measure", host_ns, 0, frame))

        def latest_record(self):
            return self.records[-1]

    def test_watchdog_catches_transient_fault_even_if_latest_frame_is_healthy(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        cursors = {"diagnostic": len(diagnostics.records),
                   "state": len(states.records)}
        diagnostics.push(self.diagnostic(2, fault_nid=1))
        diagnostics.push(self.diagnostic(3))
        states.push(self.state(2))

        with self.assertRaisesRegex(RuntimeError, "faults/limits"):
            benchmark._motion_watchdog(diagnostics, states, 100.0, cursors)

    def test_watchdog_rejects_stale_feedback(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        diagnostics.records.clear()
        states.records.clear()
        diagnostics.push(self.diagnostic(2), age_ms=250.0)
        states.push(self.state(2), age_ms=250.0)

        with self.assertRaisesRegex(RuntimeError, "stale"):
            benchmark._motion_watchdog(diagnostics, states, 100.0)

    def test_watchdog_rejects_sequence_gap_and_native_lag(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        cursors = {"diagnostic": 1, "state": 1}
        diagnostics.push(self.diagnostic(3))
        states.push(self.state(2))
        with self.assertRaisesRegex(RuntimeError, "sequence discontinuity"):
            benchmark._motion_watchdog(diagnostics, states, 100.0, cursors)

        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        diagnostics.lag_events.append({"lagged_frames": 1})
        with self.assertRaisesRegex(RuntimeError, "receiver lag"):
            benchmark._motion_watchdog(
                diagnostics, states, 100.0, {"diagnostic": 1, "state": 1})

    def test_watchdog_rejects_nonfinite_state_and_tracking_divergence(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        nonfinite = self.state(1)
        nonfinite.joints[0].position = float("nan")
        states = self.MutableReceiver("joint_states", nonfinite)
        with self.assertRaisesRegex(RuntimeError, "not finite"):
            benchmark._motion_watchdog(diagnostics, states, 100.0)

        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        with self.assertRaisesRegex(RuntimeError, "tracking error"):
            benchmark._motion_watchdog(
                diagnostics, states, 100.0, expected_positions=[1.0] * 20,
                max_tracking_error_rad=0.5)

    def test_watchdog_rejects_motor_timeout_increment_and_low_response_rate(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        cursors = benchmark._watchdog_cursors_after(
            diagnostics, diagnostics.latest_record(), states, states.latest_record())
        timeout_frame = self.diagnostic(2)
        timeout_frame.joints[0].comm_timeout_total = 1
        diagnostics.push(timeout_frame)
        states.push(self.state(2))
        with self.assertRaisesRegex(RuntimeError, "comm_timeout_total change"):
            benchmark._motion_watchdog(diagnostics, states, 100.0, cursors)

        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        low_rate_frame = self.diagnostic(2)
        low_rate_frame.joints[0].comm_response_rate_pct = 79
        diagnostics.push(low_rate_frame)
        states.push(self.state(2))
        with self.assertRaisesRegex(RuntimeError, "response rate"):
            benchmark._motion_watchdog(
                diagnostics, states, 100.0,
                benchmark._watchdog_cursors_after(
                    diagnostics, diagnostics.records[0], states, states.records[0]),
                min_response_rate_pct=80)

    def test_watchdog_rejects_comm_counter_change_velocity_and_excursion(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        cursors = benchmark._watchdog_cursors_after(
            diagnostics, diagnostics.latest_record(), states, states.latest_record())
        changed = self.diagnostic(2)
        changed.comm.e2e_lost = 1
        diagnostics.push(changed)
        states.push(self.state(2))
        with self.assertRaisesRegex(RuntimeError, "e2e_lost counter change"):
            benchmark._motion_watchdog(diagnostics, states, 100.0, cursors)

        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        diagnostics.push(self.diagnostic(2))
        velocities = [0.0] * 20
        velocities[3] = 0.56
        states.push(self.state(2, velocities=velocities))
        with self.assertRaisesRegex(RuntimeError, "velocity"):
            benchmark._motion_watchdog(
                diagnostics, states, 100.0,
                center_positions=[0.0] * 20,
                max_excursion_rad=math.radians(7.0),
                max_velocity_rad_s=0.55)

    def test_ready_gate_rejects_enabled_motor_and_transient_enabled_frame(self):
        enabled = self.diagnostic(1, ext_state=2)
        with self.assertRaisesRegex(RuntimeError, "Ready state"):
            benchmark._require_all_ready(enabled, "test")

        diagnostic_receiver = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1, ext_state=1))
        diagnostic_receiver.push(self.diagnostic(2, ext_state=2))
        diagnostic_receiver.push(self.diagnostic(3, ext_state=1))
        state_receiver = self.MutableReceiver("joint_states", self.state(1))
        state_receiver.push(self.state(2))
        state_receiver.push(self.state(3))
        cursors = benchmark._watchdog_cursors_after(
            diagnostic_receiver, diagnostic_receiver.records[0],
            state_receiver, state_receiver.records[0])

        with self.assertRaisesRegex(RuntimeError, "outside Ready state"):
            benchmark._motion_watchdog(
                diagnostic_receiver, state_receiver, 100.0, cursors,
                require_enabled=False, require_ready=True)

        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))
        states = self.MutableReceiver("joint_states", self.state(1))
        diagnostics.push(self.diagnostic(2))
        positions = [0.0] * 20
        positions[4] = math.radians(7.1)
        states.push(self.state(2, positions=positions))
        with self.assertRaisesRegex(RuntimeError, "center excursion"):
            benchmark._motion_watchdog(
                diagnostics, states, 100.0,
                center_positions=[0.0] * 20,
                max_excursion_rad=math.radians(7.0),
                max_velocity_rad_s=0.55)

    def test_disable_is_confirmed_by_a_new_all_ready_frame(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))

        class Hand:
            def disable(inner_self):
                diagnostics.push(self.diagnostic(2, ext_state=1))

        cleanup = []
        confirmed = benchmark._disable_and_confirm(
            Hand(), diagnostics, cleanup, "test_disable", timeout_s=0.01, poll_s=0.0)

        self.assertTrue(confirmed)
        self.assertEqual([item["operation"] for item in cleanup], [
            "test_disable_request", "test_disable_telemetry_confirmation",
        ])
        self.assertTrue(all(item["ok"] for item in cleanup))

    def test_disable_return_without_all_ready_frame_is_a_failure(self):
        diagnostics = self.MutableReceiver(
            "joint_diagnostics", self.diagnostic(1))

        class Hand:
            def disable(inner_self):
                diagnostics.push(self.diagnostic(2, ext_state=2))

        cleanup = []
        confirmed = benchmark._disable_and_confirm(
            Hand(), diagnostics, cleanup, "test_disable", timeout_s=0.001, poll_s=0.0)

        self.assertFalse(confirmed)
        self.assertTrue(cleanup[0]["ok"])
        self.assertFalse(cleanup[1]["ok"])
        self.assertEqual(cleanup[1]["last_enabled_nids"],
                         list(benchmark.EXPECTED_MOTOR_NIDS))


class OppositionRunnerTests(unittest.TestCase):
    def test_pre_enable_handoff_does_not_rebaseline_a_timeout_increment(self):
        diagnostics = ActiveSafetyHelperTests.MutableReceiver(
            "joint_diagnostics",
            ActiveSafetyHelperTests.diagnostic(1, ext_state=1))
        states = ActiveSafetyHelperTests.MutableReceiver(
            "joint_states", ActiveSafetyHelperTests.state(1))
        cursors = benchmark._watchdog_cursors_after(
            diagnostics, diagnostics.latest_record(), states, states.latest_record())
        timeout_frame = ActiveSafetyHelperTests.diagnostic(2, ext_state=1)
        timeout_frame.joints[0].comm_timeout_total = 1
        diagnostics.push(timeout_frame)
        states.push(ActiveSafetyHelperTests.state(2))

        class FakeStream:
            closed = False

            def close(inner_self):
                inner_self.closed = True

        stream = FakeStream()
        module = SimpleNamespace(_open_replay=lambda side: (stream, 1))

        class Hand:
            def enable(inner_self):
                self.fail("enable must not run after a missed timeout increment")

        args = SimpleNamespace(
            rate_hz=1000.0, frames=1, watchdog_max_age_ms=100.0,
            min_joint_response_rate_pct=80)
        benchmark.STOP_REQUESTED.clear()
        self.addCleanup(benchmark.STOP_REQUESTED.clear)

        with self.assertRaisesRegex(RuntimeError, "comm_timeout_total change"):
            benchmark._run_opposition(
                args, SimpleNamespace(), Hand(), module, "right", threading.Event(),
                benchmark.Phase(), diagnostics, states, [], [], {}, [0.0] * 20,
                watchdog_cursors=cursors)

        self.assertTrue(stream.closed)

    def test_two_frame_run_uses_only_fakes_and_retains_timing_rows(self):
        calls = []
        sent = []

        class FakeStream:
            def __init__(self):
                self.frames = [tuple(0.0 for _ in range(20)),
                               tuple(0.001 * i for i in range(20))]
                self.closed = False

            def close(self):
                self.closed = True
                calls.append("close_stream")

        stream = FakeStream()

        class FakeModule:
            @staticmethod
            def _open_replay(side):
                self.assertEqual(side, "right")
                return stream, len(stream.frames)

            @staticmethod
            def _read_qpos(open_stream):
                self.assertIs(open_stream, stream)
                return open_stream.frames.pop(0)

        class FakeJointCommand:
            def __init__(self, position, velocity, effort):
                self.position = position
                self.velocity = velocity
                self.effort = effort

        class FakePublisher:
            dtype = "JointCommandFrame"

            def send(self, commands):
                calls.append("send")
                sent.append(commands)
                diagnostic_receiver.push(make_diagnostic(ext_state=2))
                state_receiver.push(make_state())

            def close(self):
                calls.append("close_publisher")

        def status(ext_state):
            return SimpleNamespace(ext_state=ext_state,
                ext_state_name="Enabled" if ext_state == 2 else "Ready",
                position_limit_active=False, velocity_limit_active=False,
                current_limit_active=False)

        sequence = {"diagnostic": 0, "state": 0}

        def make_diagnostic(ext_state):
            sequence["diagnostic"] += 1
            return SimpleNamespace(
                header=SimpleNamespace(seq=sequence["diagnostic"],
                                       timestamp_us=sequence["diagnostic"] * 1000),
                num_joints=20,
                joints=[SimpleNamespace(nid=nid, status_word=status(ext_state),
                                        error_code_current=0)
                        for nid in benchmark.EXPECTED_MOTOR_NIDS])

        def make_state():
            sequence["state"] += 1
            return SimpleNamespace(
                header=SimpleNamespace(seq=sequence["state"],
                                       timestamp_us=sequence["state"] * 1000),
                num_joints=20,
                joints=[SimpleNamespace(nid=nid, position=0.0)
                        for nid in benchmark.EXPECTED_MOTOR_NIDS])

        class FakeReceiver:
            def __init__(self, name, frame):
                self.name = name
                self.records = []
                self.push(frame)

            def push(self, frame):
                self.records.append(("measure", time.perf_counter_ns(), 0, frame))

            def latest(self):
                return self.records[-1][3]

            def latest_record(self):
                return self.records[-1]

        diagnostic_receiver = FakeReceiver("joint_diagnostics", make_diagnostic(ext_state=1))
        state_receiver = FakeReceiver("joint_states", make_state())
        publisher = FakePublisher()

        class FakeHand:
            def enable(self):
                calls.append("enable")
                diagnostic_receiver.push(make_diagnostic(ext_state=2))
                state_receiver.push(make_state())

            def disable(self):
                calls.append("disable")
                diagnostic_receiver.push(make_diagnostic(ext_state=1))

            def joint_command(self):
                return SimpleNamespace(publish=lambda: (calls.append("publish") or publisher))

        args = SimpleNamespace(rate_hz=1_000_000_000, frames=2, scheduler="no-burst",
                               max_initial_error_rad=0.1, watchdog_max_age_ms=100.0,
                               max_tracking_error_rad=0.5,
                               min_joint_response_rate_pct=80)
        sdk = SimpleNamespace(JointCommand=FakeJointCommand)
        abort = threading.Event()
        phase = benchmark.Phase()
        records, cleanup, metadata = [], [], {}
        benchmark.STOP_REQUESTED.clear()
        self.addCleanup(benchmark.STOP_REQUESTED.clear)

        returned = benchmark._run_opposition(
            args, sdk, FakeHand(), FakeModule, "right", abort, phase,
            diagnostic_receiver, state_receiver, records, cleanup, metadata,
            [0.0] * 20)

        self.assertIs(returned, records)
        self.assertEqual(calls, ["enable", "publish", "send", "send", "disable",
                                 "close_publisher", "close_stream"])
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(len(commands) == 20 for commands in sent))
        self.assertEqual([command.position for command in sent[0]], [0.0] * 20)
        self.assertTrue(all(command.velocity == 0.0 and command.effort == 0.0
                            for commands in sent for command in commands))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row["send_ok"] for row in records))
        self.assertEqual([row["index"] for row in records], [0, 1])
        for row in records:
            self.assertLessEqual(row["build_start_ns"], row["send_enter_ns"])
            self.assertLessEqual(row["send_enter_ns"], row["send_return_ns"])
        self.assertEqual(len(cleanup), 4)
        self.assertEqual(cleanup[0]["operation"], "disable_and_confirm_request")
        self.assertTrue(cleanup[0]["ok"])
        self.assertEqual(cleanup[1]["operation"],
                         "disable_and_confirm_telemetry_confirmation")
        self.assertTrue(cleanup[1]["ok"])
        self.assertEqual(cleanup[2:], [
            {"operation": "close_publisher", "ok": True},
            {"operation": "close_trajectory", "ok": True},
        ])
        self.assertTrue(stream.closed)
        self.assertEqual(phase.value, "cleanup")
        self.assertIn("enable_call_duration_ns", metadata)
        self.assertIn("publisher_open_duration_ns", metadata)

    def test_bounded_catchup_holds_one_ms_send_intervals_with_0_1_ms_watchdog(self):
        class FakeClock:
            def __init__(self):
                self.now_ns = 1_000_000_000

            def perf_counter_ns(self):
                return self.now_ns

            def monotonic(self):
                return self.now_ns / 1e9

            def sleep(self, seconds):
                self.now_ns += int(round(seconds * 1e9))

        clock = FakeClock()
        sequence = {"diagnostic": 0, "state": 0}

        def make_diagnostic(ext_state):
            sequence["diagnostic"] += 1
            return ActiveSafetyHelperTests.diagnostic(
                sequence["diagnostic"], ext_state=ext_state)

        def make_state():
            sequence["state"] += 1
            return ActiveSafetyHelperTests.state(sequence["state"])

        class FakeReceiver:
            def __init__(self, name, frame):
                self.name = name
                self.records = []
                self.lag_events = []
                self.push(frame)

            def push(self, frame):
                self.records.append(("measure", clock.now_ns, 0, frame))

            def latest(self):
                return self.records[-1][3]

            def latest_record(self):
                return self.records[-1]

        diagnostic_receiver = FakeReceiver(
            "joint_diagnostics", make_diagnostic(ext_state=1))
        state_receiver = FakeReceiver("joint_states", make_state())

        class FakeStream:
            def __init__(self):
                self.remaining = 5
                self.closed = False

            def close(self):
                self.closed = True

        stream = FakeStream()

        class FakeModule:
            @staticmethod
            def _open_replay(side):
                self.assertEqual(side, "right")
                return stream, stream.remaining

            @staticmethod
            def _read_qpos(open_stream):
                self.assertIs(open_stream, stream)
                if open_stream.remaining <= 0:
                    raise EOFError("fake trajectory exhausted")
                open_stream.remaining -= 1
                return (0.0,) * 20

        class FakeJointCommand:
            def __init__(self, position, velocity, effort):
                self.position = position
                self.velocity = velocity
                self.effort = effort

        send_enter_ns = []
        test_case = self

        class FakePublisher:
            dtype = "JointCommandFrame"

            def send(self, commands):
                test_case.assertEqual(len(commands), 20)
                send_enter_ns.append(clock.now_ns)
                diagnostic_receiver.push(make_diagnostic(ext_state=2))
                state_receiver.push(make_state())

            def close(self):
                pass

        publisher = FakePublisher()

        class FakeHand:
            def enable(self):
                diagnostic_receiver.push(make_diagnostic(ext_state=2))
                state_receiver.push(make_state())

            def disable(self):
                diagnostic_receiver.push(make_diagnostic(ext_state=1))

            def joint_command(self):
                return SimpleNamespace(publish=lambda: publisher)

        def fake_watchdog(*_args, **_kwargs):
            clock.now_ns += 100_000
            return {
                "diagnostic_seq": sequence["diagnostic"],
                "state_seq": sequence["state"],
                "diagnostic_age_ms": 0.0,
                "state_age_ms": 0.0,
                "diagnostic_frames_checked": 1,
                "state_frames_checked": 1,
                "tracking_error_rad": 0.0,
                "max_measured_velocity_rad_s": 0.0,
                "max_center_excursion_rad": 0.0,
            }

        def fake_wait_for_fresh_record(receiver, *_args, **_kwargs):
            return receiver.latest_record(), 0.0

        args = SimpleNamespace(
            mode="sine", rate_hz=1000.0, frames=None,
            scheduler="bounded-catchup", max_initial_error_rad=math.radians(2.0),
            watchdog_max_age_ms=100.0,
            max_tracking_error_rad=math.radians(2.0),
            min_joint_response_rate_pct=80,
            max_command_lateness_ms=1.0, max_send_call_ms=1.0)
        benchmark.STOP_REQUESTED.clear()
        self.addCleanup(benchmark.STOP_REQUESTED.clear)
        records = []

        with (patch.object(benchmark.time, "perf_counter_ns", clock.perf_counter_ns),
              patch.object(benchmark.time, "monotonic", clock.monotonic),
              patch.object(benchmark.time, "sleep", clock.sleep),
              patch.object(benchmark, "_motion_watchdog", fake_watchdog),
              patch.object(benchmark, "_wait_for_fresh_record",
                           fake_wait_for_fresh_record)):
            benchmark._run_opposition(
                args, SimpleNamespace(JointCommand=FakeJointCommand), FakeHand(),
                FakeModule, "right", threading.Event(), benchmark.Phase(),
                diagnostic_receiver, state_receiver, records, [], {}, [0.0] * 20)

        intervals_ns = [after - before for before, after in
                        zip(send_enter_ns, send_enter_ns[1:])]
        self.assertEqual(len(send_enter_ns), 5)
        self.assertEqual(intervals_ns, [1_000_000] * 4)
        self.assertNotIn(1_100_000, intervals_ns)
        self.assertEqual(benchmark.cadence_ns(send_enter_ns)["rate_hz"], 1000.0)
        self.assertEqual([record["send_enter_ns"] for record in records], send_enter_ns)
        self.assertTrue(stream.closed)


class SineAcceptanceTests(unittest.TestCase):
    @staticmethod
    def green_report():
        def cadence():
            return {
                "samples": 10_001,
                "rate_hz": 1000.0,
                "intervals": {
                    "count": 10_000,
                    "min_ms": 1.0,
                    "p99_ms": 1.0,
                    "max_ms": 1.0,
                },
            }

        telemetry = {}
        for name in ("joint_diagnostics", "joint_states"):
            telemetry[name] = {
                "device_timestamp_cadence": cadence(),
                "host_delivery_cadence": cadence(),
                "sequence": {
                    "missing_sequence_values": 0,
                    "duplicate_transitions": 0,
                    "reordered_transitions": 0,
                },
                "native_receiver_lagged_frames": 0,
                "receiver_errors": [],
            }

        per_joint = [{
            "dense_index": dense_index,
            "nid": nid,
            "samples": 10_001,
            "positive_peak_offset_rad": math.radians(5.0),
            "negative_peak_offset_rad": -math.radians(5.0),
        } for dense_index, nid in enumerate(benchmark.EXPECTED_MOTOR_NIDS)]

        return {
            "status": "complete",
            "config": {"max_tracking_error_rad": math.radians(2.0)},
            "sine": {
                "selected_frames": 10_001,
                "amplitude_deg_peak": 5.0,
                "response": {
                    "state_frames": 10_001,
                    "per_joint": per_joint,
                    "minimum_observed_peak_offset_deg": 5.0,
                    "maximum_observed_peak_offset_deg": 5.0,
                    "maximum_observed_velocity_rad_s": 0.3,
                    "maximum_final_center_error_deg": 0.25,
                },
            },
            "command": {
                "successful_sends": 10_001,
                "failed_sends": 0,
                "send_enter_cadence": cadence(),
                "maximum_watchdog_tracking_error_rad": math.radians(1.0),
            },
            "communication_health": {
                "joint_comm_timeouts_delta": 0,
                "internal_node_timeouts_delta": 0,
                "e2e_lost_delta": 0,
                "e2e_reordered_delta": 0,
                "e2e_duplicates_delta": 0,
                "sdk_dropped_delta": 0,
                "rpc_retries_delta": 0,
                "rpc_timeouts_delta": 0,
                "comm_get_failures_delta": 0,
                "internal_crc_errors_delta": 0,
                "internal_frame_format_errors_delta": 0,
                "internal_uart_errors_delta": 0,
                "minimum_joint_response_rate_pct_observed": 100,
                "joint_error_codes_observed": [],
                "joint_limit_flag_frames": 0,
                "internal_online_motor_nodes_final": 20,
            },
            "telemetry": telemetry,
        }

    def test_green_report_passes_every_acceptance_check(self):
        result = benchmark._sine_acceptance(self.green_report())

        self.assertTrue(result["communication_pass"])
        self.assertTrue(result["tracking_pass"])
        self.assertTrue(all(result["communication_checks"].values()))
        self.assertTrue(all(result["tracking_checks"].values()))
        self.assertFalse(result["firmware_command_application_proven"])

    def test_each_health_cadence_and_lobe_mutation_fails_its_named_check(self):
        cases = [
            ("send count", "communication_checks", "exact_successful_send_count",
             lambda report: report["command"].__setitem__("successful_sends", 10_000)),
            ("RPC retry", "communication_checks", "zero_rpc_retry_delta",
             lambda report: report["communication_health"].__setitem__(
                 "rpc_retries_delta", 1)),
            ("RPC timeout", "communication_checks", "zero_rpc_timeout_delta",
             lambda report: report["communication_health"].__setitem__(
                 "rpc_timeouts_delta", 1)),
            ("comm GET failure", "communication_checks", "zero_comm_get_failure_delta",
             lambda report: report["communication_health"].__setitem__(
                 "comm_get_failures_delta", 1)),
            ("internal CRC", "communication_checks", "zero_internal_crc_error_delta",
             lambda report: report["communication_health"].__setitem__(
                 "internal_crc_errors_delta", 1)),
            ("internal frame format", "communication_checks",
             "zero_internal_frame_format_error_delta",
             lambda report: report["communication_health"].__setitem__(
                 "internal_frame_format_errors_delta", 1)),
            ("internal UART", "communication_checks", "zero_internal_uart_error_delta",
             lambda report: report["communication_health"].__setitem__(
                 "internal_uart_errors_delta", 1)),
            ("joint response rate", "communication_checks",
             "minimum_joint_response_at_least_80_pct",
             lambda report: report["communication_health"].__setitem__(
                 "minimum_joint_response_rate_pct_observed", 79)),
            ("joint error code", "communication_checks", "zero_joint_error_codes",
             lambda report: report["communication_health"].__setitem__(
                 "joint_error_codes_observed", [7])),
            ("joint limit", "communication_checks", "zero_joint_limit_frames",
             lambda report: report["communication_health"].__setitem__(
                 "joint_limit_flag_frames", 1)),
            ("response NID", "tracking_checks", "exact_20_joint_response_set",
             lambda report: report["sine"]["response"]["per_joint"][7].__setitem__(
                 "nid", 999)),
            ("positive lobe", "tracking_checks", "all_joints_reached_positive_lobe",
             lambda report: report["sine"]["response"]["per_joint"][7].__setitem__(
                 "positive_peak_offset_rad", math.radians(3.99))),
            ("negative lobe", "tracking_checks", "all_joints_reached_negative_lobe",
             lambda report: report["sine"]["response"]["per_joint"][7].__setitem__(
                 "negative_peak_offset_rad", -math.radians(3.99))),
        ]
        for stream_name in ("joint_diagnostics", "joint_states"):
            cases.extend((
                (f"{stream_name} host rate", "communication_checks",
                 f"{stream_name}_host_rate_995_to_1005_hz",
                 lambda report, name=stream_name: report["telemetry"][name][
                     "host_delivery_cadence"].__setitem__("rate_hz", 994.9)),
                (f"{stream_name} host p99", "communication_checks",
                 f"{stream_name}_host_interval_p99_at_most_1_5_ms",
                 lambda report, name=stream_name: report["telemetry"][name][
                     "host_delivery_cadence"]["intervals"].__setitem__(
                         "p99_ms", 1.5001)),
                (f"{stream_name} host max", "communication_checks",
                 f"{stream_name}_host_interval_max_below_2_ms",
                 lambda report, name=stream_name: report["telemetry"][name][
                     "host_delivery_cadence"]["intervals"].__setitem__(
                         "max_ms", 2.0)),
            ))

        for name, section, check_name, mutate in cases:
            with self.subTest(name=name):
                report = copy.deepcopy(self.green_report())
                mutate(report)

                result = benchmark._sine_acceptance(report)

                self.assertFalse(result[section][check_name])
                pass_name = ("communication_pass" if section == "communication_checks"
                             else "tracking_pass")
                self.assertFalse(result[pass_name])


class CliSafetyTests(unittest.TestCase):
    def assert_parse_error(self, argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            benchmark.parse_args(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_opposition_requires_an_exact_serial_confirmation(self):
        serial = "WH2-EXACT-TEST"
        args = benchmark.parse_args([
            "--mode", "opposition", "--sn", serial, "--confirm-motion", serial,
            "--request-telemetry-hz", "1000",
        ])

        self.assertEqual(args.sn, serial)
        self.assertEqual(args.confirm_motion, serial)
        self.assertEqual(args.telemetry, "both")
        self.assert_parse_error(["--mode", "opposition", "--sn", serial])
        self.assert_parse_error([
            "--mode", "opposition", "--sn", serial,
            "--confirm-motion", serial.lower(), "--request-telemetry-hz", "1000",
        ])
        self.assert_parse_error([
            "--mode", "opposition", "--address", "127.0.0.1:7447",
            "--confirm-motion", serial, "--request-telemetry-hz", "1000",
        ])
        self.assert_parse_error([
            "--mode", "opposition", "--sn", serial, "--confirm-motion", serial,
        ])

    def test_observe_defaults_do_not_request_a_telemetry_rate_change(self):
        args = benchmark.parse_args([])

        self.assertEqual(args.mode, "observe")
        self.assertEqual(args.telemetry, "diagnostics")
        self.assertIsNone(args.request_telemetry_hz)
        self.assertIsNone(args.frames)
        self.assertEqual(args.watchdog_max_age_ms, 20.0)
        self.assertEqual(args.max_tracking_error_rad, 0.5)
        self.assertEqual(args.min_joint_response_rate_pct, 80)
        self.assertFalse(args.disable_cyclic_gc)

    def test_sine_defaults_and_motion_gates(self):
        serial = "WH2-SINE-TEST"
        base = [
            "--mode", "sine", "--sn", serial, "--confirm-motion", serial,
            "--request-telemetry-hz", "1000",
        ]
        args = benchmark.parse_args(base)

        self.assertEqual(args.telemetry, "both")
        self.assertEqual(args.scheduler, "bounded-catchup")
        self.assertEqual(args.rate_hz, 1000.0)
        self.assertEqual(args.duration_s, 10.0)
        self.assertEqual(args.warmup_s, 10.0)
        self.assertEqual(args.sine_amplitude_deg, 5.0)
        self.assertEqual(args.sine_frequency_hz, 0.5)
        self.assertEqual(args.sine_ramp_s, 2.0)
        self.assertAlmostEqual(args.max_initial_error_rad, math.radians(2.0))
        self.assertAlmostEqual(args.max_tracking_error_rad, math.radians(2.0))
        self.assertEqual(args.max_command_lateness_ms, 1.0)
        self.assertEqual(args.max_send_call_ms, 1.0)
        self.assertTrue(args.log_full_frames)
        self.assertTrue(args.disable_cyclic_gc)

        invalid_suffixes = (
            ["--rate-hz", "999"],
            ["--frames", "100"],
            ["--telemetry", "diagnostics"],
            ["--sine-amplitude-deg", "6"],
            ["--sine-frequency-hz", "1.1"],
            ["--sine-ramp-s", "6"],
            ["--warmup-s", "9.9"],
            ["--max-command-lateness-ms", "1.1"],
            ["--max-send-call-ms", "1.1"],
            ["--max-tracking-error-rad", str(math.radians(5.0))],
            ["--max-tracking-error-rad", str(math.radians(2.01))],
            ["--max-initial-error-rad", str(math.radians(2.01))],
            ["--watchdog-max-age-ms", "20.1"],
            ["--min-joint-response-rate-pct", "79"],
        )
        for suffix in invalid_suffixes:
            with self.subTest(suffix=suffix):
                self.assert_parse_error(base + suffix)
        self.assert_parse_error([
            "--mode", "sine", "--sn", serial,
            "--request-telemetry-hz", "1000",
        ])


class SerializationTests(unittest.TestCase):
    def test_to_plain_preserves_every_schema_282_field(self):
        fixtures = json.loads((ROOT / "tests/data/schema-oracle-fixtures.json").read_text())
        fixture = next(item for item in fixtures if item["schema_id"] == 282)
        modeled = _types._from_native(fixture["type"].rsplit(".", 1)[-1], fixture["value"])

        plain = benchmark.to_plain(modeled)

        self.assertEqual(plain, fixture["value"])
        self.assertEqual(json.loads(json.dumps(plain)), fixture["value"])


class ReceiverTests(unittest.TestCase):
    def test_lag_event_is_recorded_and_receiver_continues_to_next_frame(self):
        frame = object()

        class LagThenFrameSubscription:
            def __init__(self):
                self.calls = 0
                self.frame_returned = threading.Event()

            def recv(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Stream lagged by 7 frame(s)")
                if self.calls == 2:
                    self.frame_returned.set()
                    return frame
                return None

        subscription = LagThenFrameSubscription()
        phase = benchmark.Phase()
        phase.value = "measure"
        abort = threading.Event()
        receiver = benchmark.Receiver("diagnostics", subscription, phase, abort, poll_us=100)
        self.addCleanup(receiver.stop)

        receiver.start()
        self.assertTrue(subscription.frame_returned.wait(1.0), "receiver did not continue after lag")
        self.assertTrue(receiver.stop(), "receiver thread did not stop")

        self.assertFalse(abort.is_set())
        self.assertEqual(receiver.errors, [])
        self.assertEqual(len(receiver.lag_events), 1)
        self.assertEqual(receiver.lag_events[0]["phase"], "measure")
        self.assertEqual(receiver.lag_events[0]["lagged_frames"], 7)
        self.assertEqual(len(receiver.records), 1)
        self.assertEqual(receiver.records[0][0], "measure")
        self.assertIs(receiver.records[0][3], frame)

    def test_counted_lag_is_recorded_without_an_exception(self):
        frame = object()

        class CountingSubscription:
            def __init__(self):
                self.calls = 0
                self.lagged_frames = 0
                self.frame_returned = threading.Event()

            def recv(self):
                self.calls += 1
                if self.calls == 1:
                    self.lagged_frames += 197  # recv() skipped a backlog
                    self.frame_returned.set()
                    return frame
                return None

        subscription = CountingSubscription()
        phase = benchmark.Phase()
        phase.value = "measure"
        receiver = benchmark.Receiver("diagnostics", subscription, phase, threading.Event(), poll_us=100)
        self.addCleanup(receiver.stop)
        receiver.start()
        self.assertTrue(subscription.frame_returned.wait(1.0))
        self.assertTrue(receiver.stop())
        self.assertEqual([event["lagged_frames"] for event in receiver.lag_events], [197])
        self.assertIs(receiver.records[0][3], frame)


if __name__ == "__main__":
    unittest.main()
