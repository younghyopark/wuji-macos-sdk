"""Hand 2 call traces pinned to the Linux C SDK's documented/recovered behavior.

This is an explicit transport substitute for testing Python dispatch only.
It does not prove that the macOS native client reaches a physical hand.
"""
import math
import unittest

from wuji_sdk._backend import NativeBackend
from wuji_sdk._api import WujiDevice, WujiHand2
from wuji_sdk._codec import encode, decode
from wuji_sdk import WujiException


class TraceBackend(NativeBackend):
    def __init__(self, values=None, fail_on=None):
        self._devices = {123: {"device_name": "hand", "serial_number": "TEST_ONLY", "device_type": "WujiHand2"}}
        self.values = values or {}
        self.trace = []
        self.fail_on = fail_on

    def call(self, scope, handle, operation, **arguments):
        assert scope == "device"
        return self._device_call(handle, operation, arguments)

    def release_device(self, handle):
        pass

    # Paths and schemas as advertised by a live Hand 2 (firmware 2.5.1).
    SCHEMAS = {"effort_limit": 324, "mit_params": 280,
               "thumb/S1/effort_limit": 9, "index/S2/error_code": 2, "pinky/S4/status_word": 3,
               "ring/tactile/tactile_state": 1, "ring/tactile/tactile_type": 1}

    def _resources(self, handle, include_hidden=False):
        return [{"path": path, "schema_id": schema, "serde_format": 0,
                 "can_get": True, "can_set": True} for path, schema in self.SCHEMAS.items()]

    def request_raw(self, handle, operation, path, payload=b""):
        self.trace.append((operation, path, payload))
        if path == self.fail_on:
            raise WujiException("explicit transport-test failure")
        if operation == 0:
            return encode(self.SCHEMAS[path], self.values[path])
        return b""

    def hand(self):
        return WujiHand2(WujiDevice(self, 123, self._devices[123]))


class Hand2DispatchTests(unittest.TestCase):
    def test_reboot_is_the_documented_execute_action(self):
        backend = TraceBackend()
        backend.hand().reboot()
        self.assertEqual(backend.trace, [(2, "reboot", b"")])

    def test_ip_accessor_uses_ip_address_resource(self):
        class IpPaths(TraceBackend):
            def _device_call(self, handle, operation, args):
                if operation in ("get", "set"):
                    self.trace.append((operation, args))
                    return "192.0.2.1" if operation == "get" else None
                return super()._device_call(handle, operation, args)

        backend = IpPaths()
        resource = backend.hand().ip()
        self.assertEqual(resource.get(), "192.0.2.1")
        resource.set("192.0.2.2")
        self.assertEqual(backend.trace, [("get", {"path": "ip_address"}),
                                         ("set", {"path": "ip_address", "value": "192.0.2.2"})])

    def test_typed_fingertip_accessor_uses_documented_nested_path(self):
        class StreamPaths(TraceBackend):
            def _device_call(self, handle, operation, args):
                if operation == "subscribe":
                    self.trace.append(args["path"])
                    return {"handle": 1, "stream_id": 7, "dtype": "FingertipSensorData"}
                return super()._device_call(handle, operation, args)

        backend = StreamPaths()
        hand = backend.hand()
        for finger in ("thumb", "index", "middle", "ring", "pinky"):
            resource = getattr(hand, f"fingertip_{finger}_data")()
            result = resource._call("subscribe")
            self.assertEqual(result["dtype"], "FingertipSensorData")
            self.assertEqual(backend.trace[-1], f"fingertip/{finger}/data")

    def test_whole_hand_and_masked_origin_use_distinct_operations(self):
        backend = TraceBackend()
        hand = backend.hand()
        hand.set_origin()
        mask = [0] * 20
        mask[0] = mask[19] = 1
        hand.clear_origin(mask)
        self.assertEqual(backend.trace, [
            (1, "set_origin", b""),
            (1, "thumb/S1/od/aux/set_origin_ctrl", b"\x02\0\0\0"),
            (1, "pinky/S4/od/aux/set_origin_ctrl", b"\x02\0\0\0"),
        ])

    def test_feed_forward_is_a_u32_raw_set_on_every_joint_in_order(self):
        # Linux C SDK 2026.9.22 wuji_hand_2_set_constant/_set_position_table
        # loop over joints and SET od/action_* with a 4-byte u32 (0x266de8, 0x266e54).
        from wuji_sdk import FeedForwardMode
        backend = TraceBackend()
        hand = backend.hand()
        hand.set_constant(FeedForwardMode.ConstantAndVelocityTable)
        self.assertEqual(len(backend.trace), 20)
        self.assertEqual(backend.trace[0], (1, "thumb/S1/od/action_set_constant", b"\x03\0\0\0"))
        self.assertEqual(backend.trace[19], (1, "pinky/S4/od/action_set_constant", b"\x03\0\0\0"))
        backend.trace.clear()
        hand.set_position_table(False, [1] + [0] * 18 + [1])
        hand.joint(8).constant().set(0)
        hand.joint(9).position_table().set(True)
        self.assertEqual(backend.trace, [
            (1, "thumb/S1/od/action_set_position_table", b"\0\0\0\0"),
            (1, "pinky/S4/od/action_set_position_table", b"\0\0\0\0"),
            (1, "middle/S1/od/action_set_constant", b"\0\0\0\0"),
            (1, "middle/S2/od/action_set_position_table", b"\x01\0\0\0"),
        ])

    def test_feed_forward_validates_mode_and_stops_at_first_failure(self):
        backend = TraceBackend(fail_on="thumb/S2/od/action_set_constant")
        hand = backend.hand()
        with self.assertRaises(OverflowError):
            hand.set_constant(4)
        self.assertEqual(backend.trace, [])
        with self.assertRaisesRegex(WujiException, "transport-test"):
            hand.set_constant(1)
        self.assertEqual([path for _, path, _ in backend.trace],
                         ["thumb/S1/od/action_set_constant", "thumb/S2/od/action_set_constant"])

    def test_masked_action_fails_before_later_joints(self):
        backend = TraceBackend(fail_on="thumb/S2/motor_enable")
        with self.assertRaisesRegex(WujiException, "transport-test"):
            backend.hand().enable([1, 1, 1] + [0] * 17)
        self.assertEqual(backend.trace, [(1, "thumb/S1/motor_enable", b""), (1, "thumb/S2/motor_enable", b"")])

    def test_joint_handle_actions_use_finger_name_prefix(self):
        backend = TraceBackend()
        hand = backend.hand()
        hand.joint(5).disable()
        hand.joint(19).clear_fault()
        self.assertEqual(backend.trace, [(1, "index/S2/motor_disable", b""),
                                         (1, "pinky/S4/clear_fault", b"")])

    def test_per_joint_scalar_resources(self):
        backend = TraceBackend({"thumb/S1/effort_limit": 1.25, "index/S2/error_code": 0x2102,
                                "pinky/S4/status_word": 0x24620072})
        hand = backend.hand()
        self.assertEqual(hand.joint(0).effort_limit().get(), 1.25)
        self.assertEqual(hand.joint(5).error_code().get(), 0x2102)
        status = hand.joint(19).status_word().get()
        self.assertEqual((status.ext_state, status.position_limit_active,
                          status.velocity_limit_active, status.current_limit_active),
                         (2, True, True, True))
        hand.joint(0).effort_limit().set(0.5)
        self.assertEqual(backend.trace[-1], (1, "thumb/S1/effort_limit", encode(9, 0.5)))
        with self.assertRaises(ValueError):
            hand.joint(0).effort_limit().set(-1.0)

    def test_tactile_operations_use_finger_name_paths(self):
        backend = TraceBackend({"ring/tactile/tactile_state": 0, "ring/tactile/tactile_type": 1})
        hand = backend.hand()
        hand.tactile_calibrate()
        self.assertEqual([path for _, path, _ in backend.trace],
                         [f"{finger}/tactile/tactile_calibrate"
                          for finger in ("thumb", "index", "middle", "ring", "pinky")])
        backend.trace.clear()
        status = hand.tactile_status("ring")
        self.assertEqual((int(status.model), int(status.state)), (1, 0))
        self.assertEqual(backend.trace[0], (2, "ring/tactile/tactile_query_status", b""))

    def test_mit_scalar_broadcast_encodes_exactly_twenty_pairs(self):
        backend = TraceBackend()
        backend.hand().mit_params().set((3.0, 0.5))
        operation, path, wire = backend.trace[0]
        self.assertEqual((operation, path), (1, "mit_params"))
        self.assertEqual(decode(280, wire), {"joints": [{"kp": 3.0, "kd": 0.5}] * 20})

    def test_nan_offline_sentinels_preserve_twenty_slots(self):
        effort = [0.5] * 20
        effort[3] = math.nan
        mit = [{"kp": 3.0, "kd": 0.5} for _ in range(20)]
        mit[19]["kd"] = math.nan
        backend = TraceBackend({"effort_limit": {"joints": effort}, "mit_params": {"joints": mit}})
        hand = backend.hand()
        self.assertIsNone(hand.effort_limit().get()[3])
        self.assertEqual(len(hand.effort_limit().get()), 20)
        values = hand.mit_params().get()
        self.assertIsNone(values[19])
        self.assertEqual(values[0].kp, 3.0)


if __name__ == "__main__":
    unittest.main()
