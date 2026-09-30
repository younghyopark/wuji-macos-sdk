"""Offline checks for examples/macos/pd_controller.py; no device, network, or motion.

The loop tests inject explicit stream/sender/hand substitutes to exercise the
safety logic. They do not show that the firmware follows the commands.
"""
import importlib.util
import json
import math
from pathlib import Path
import random
import threading
import time
import unittest

from wuji_sdk import WujiException, _codec

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pd_controller", ROOT / "examples/macos/pd_controller.py")
pd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pd)

NIDS = [bus * 5 + slot for bus in range(5) for slot in range(1, 5)]


def joint_states_payload(q, seq=131263, frame_id="r_wrist", order=None):
    joints = [{"nid": NIDS[j], "position": q[j], "velocity": 0.01 * j, "effort": -0.02 * j}
              for j in (order or range(len(q)))]
    return _codec.encode(330, {"header": {"seq": seq, "timestamp_us": 1789936356771617,
                                          "frame_id": frame_id},
                               "num_joints": len(joints), "joints": joints})


def f32(value):
    return _codec.decode(9, _codec.encode(9, value))


def healthy_diagnostic(state=2, code=0, status_bits=0):
    return {"joints": [{"nid": nid, "status_word": status_bits | state, "error_code_current": code}
                       for nid in NIDS]}


class DecodeTests(unittest.TestCase):
    def test_matches_python_codec_for_shuffled_full_frame(self):
        rng = random.Random(7)
        q = [rng.uniform(-1, 1) for _ in range(20)]
        order = list(range(20))
        rng.shuffle(order)
        payload = joint_states_payload(q, seq=2**31 + 5, frame_id="x" * 200, order=order)
        seq, stamp, dq, dqd, deff = pd.decode_joint_states(payload)
        reference = _codec.decode(330, payload)
        self.assertEqual((seq, stamp), (reference["header"]["seq"], reference["header"]["timestamp_us"]))
        for joint in reference["joints"]:
            j = pd.dense_index(joint["nid"])
            self.assertEqual((dq[j], dqd[j], deff[j]),
                             (joint["position"], joint["velocity"], joint["effort"]))

    def test_sparse_frame_reports_absent_joints_as_none(self):
        payload = joint_states_payload([0.5] * 20, order=[0, 19])
        _, _, q, _, _ = pd.decode_joint_states(payload)
        self.assertEqual((q[0], q[19], q.count(None)), (0.5, 0.5, 18))

    def test_malformed_payloads_raise(self):
        payload = joint_states_payload([0.0] * 20)
        for bad in (payload[:-1], payload + b"\0", payload[:3]):
            with self.assertRaises(ValueError):
                pd.decode_joint_states(bad)

    def test_nid_mapping_is_the_dense_command_order(self):
        self.assertEqual([pd.dense_index(nid) for nid in NIDS], list(range(20)))
        for nid in (0, 5, 10, 20, 25, 255):
            with self.assertRaises(ValueError):
                pd.dense_index(nid)

    def test_native_diagnostics_fixture_decodes_into_fault_report(self):
        fixture = next(item for item in json.loads((ROOT / "tests/data/schema-oracle-fixtures.json").read_text())
                       if str(item["schema_id"]) == "332")
        frame = _codec.decode(332, bytes.fromhex(fixture["postcard_hex"]))
        faults = pd.diagnostic_faults(frame, require_enabled=False)
        self.assertTrue(any("error 0x012c" in fault for fault in faults))


class PolicyTests(unittest.TestCase):
    def test_diagnostic_faults(self):
        self.assertEqual(pd.diagnostic_faults(healthy_diagnostic(), require_enabled=True), [])
        self.assertIn("Overcurrent", pd.diagnostic_faults(healthy_diagnostic(code=0x2102),
                                                          require_enabled=False)[0])
        self.assertIn("ext_state 1", pd.diagnostic_faults(healthy_diagnostic(state=1),
                                                          require_enabled=True)[0])
        self.assertEqual(pd.diagnostic_faults(healthy_diagnostic(state=1), require_enabled=False), [])
        self.assertIn("limit active", pd.diagnostic_faults(healthy_diagnostic(status_bits=0x40),
                                                           require_enabled=True)[0])

    def test_cosine_segment_starts_and_ends_at_rest_with_matching_feedforward(self):
        q0, q1, duration = [0.1, -0.2], [0.3, -0.2], 0.8
        q, qd, done = pd.cosine_segment(q0, q1, duration, 0.0)
        self.assertEqual((q, qd, done), (q0, [0.0, 0.0], False))
        q, qd, done = pd.cosine_segment(q0, q1, duration, duration)
        self.assertEqual((q, qd, done), (q1, [0.0, 0.0], True))
        t, h = 0.3, 1e-6
        ahead, _, _ = pd.cosine_segment(q0, q1, duration, t + h)
        behind, _, _ = pd.cosine_segment(q0, q1, duration, t - h)
        _, qd, _ = pd.cosine_segment(q0, q1, duration, t)
        self.assertAlmostEqual(qd[0], (ahead[0] - behind[0]) / (2 * h), places=5)

    def test_argument_and_target_validation(self):
        with self.assertRaises(ValueError):
            pd.WujiController(kp=pd.MAX_KP + 1)
        with self.assertRaises(ValueError):
            pd.WujiController(effort_limit=2.0)
        controller = pd.WujiController(effort_limit=0.5)
        with self.assertRaises(ValueError):
            controller.set_now("q_desired", [0.0] * 19)
        with self.assertRaises(ValueError):
            controller.set_now("q_desired", [0.0] * 19 + [math.radians(95)])
        with self.assertRaises(ValueError):
            controller.set_now("effort_ff", [0.6] + [0.0] * 19)
        with self.assertRaises(ValueError):
            controller.set_now("kp", [0.0] * 20)
        controller.error = "fixture fault"
        with self.assertRaisesRegex(WujiException, "fixture fault"):
            controller.set_now("q_desired", [0.0] * 20)

    def test_motion_requires_matching_confirmation(self):
        for argv in (["--confirm-motion", "SN1"], ["SN2", "--confirm-motion", "SN1"]):
            with self.assertRaises(SystemExit):
                pd.main(argv)


class FakeStream:
    def __init__(self, payload):
        self.payload, self.lagged_frames = payload, 0

    def drain(self):
        return self.payload


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, q, qd, effort):
        self.sent.append((list(q), list(qd), list(effort)))


class FakeHand:
    def __init__(self):
        self.calls = []

    def disable(self):
        self.calls.append("disable")


class LoopTests(unittest.TestCase):
    def run_loop(self, measured, *, state_payload="same", diagnostic=None, seconds=0.05):
        controller = pd.WujiController(max_error=math.radians(10), stale_ms=20.0)
        payload = joint_states_payload(measured)
        controller._states = FakeStream(payload if state_payload == "same" else state_payload)
        controller._diagnostics = FakeStream(None)
        controller._diagnostic = (time.perf_counter_ns(), diagnostic or healthy_diagnostic())
        controller._sender, controller.hand = FakeSender(), FakeHand()
        controller._publish_state(time.perf_counter_ns(), *pd.decode_joint_states(payload))
        hold = [f32(v) for v in measured]
        controller.q_desired, controller.qd_desired, controller.effort_ff = hold, [0.0] * 20, [0.0] * 20
        controller._enable_requested = controller._enabled = True
        thread = threading.Thread(target=controller._loop)
        thread.start()
        time.sleep(seconds)
        controller._stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        return controller

    def test_holds_the_target_at_the_loop_rate(self):
        q = [0.01 * j for j in range(20)]
        controller = self.run_loop(q, seconds=0.1)
        self.assertIsNone(controller.error)
        self.assertGreater(len(controller._sender.sent), 40)
        self.assertEqual(controller._sender.sent[-1], ([f32(v) for v in q], [0.0] * 20, [0.0] * 20))
        self.assertEqual(controller.hand.calls, [])

    def test_tracking_error_disables(self):
        measured = [0.0] * 20
        measured[3] = 0.3  # the target stays at zero: 0.3 rad error on joint 3
        controller = pd.WujiController(max_error=math.radians(10))
        payload = joint_states_payload(measured)
        controller._states, controller._diagnostics = FakeStream(payload), FakeStream(None)
        controller._diagnostic = (time.perf_counter_ns(), healthy_diagnostic())
        controller._sender, controller.hand = FakeSender(), FakeHand()
        controller._publish_state(time.perf_counter_ns(), *pd.decode_joint_states(payload))
        controller.q_desired, controller.qd_desired, controller.effort_ff = [0.0] * 20, [0.0] * 20, [0.0] * 20
        controller._enable_requested = controller._enabled = True
        controller._loop()  # returns on its own after the first tick
        self.assertIn("tracking error 0.300", controller.error)
        self.assertEqual((controller.hand.calls, controller._sender.sent), (["disable"], []))

    def test_stale_feedback_disables(self):
        controller = self.run_loop([0.0] * 20, state_payload=None, seconds=0.1)
        self.assertIn("no joint_states", controller.error)
        self.assertEqual(controller.hand.calls, ["disable"])

    def test_diagnostic_fault_disables_before_sending(self):
        controller = self.run_loop([0.0] * 20, diagnostic=healthy_diagnostic(code=0x2102))
        self.assertIn("Overcurrent", controller.error)
        self.assertEqual((controller.hand.calls, controller._sender.sent), (["disable"], []))


if __name__ == "__main__":
    unittest.main()
