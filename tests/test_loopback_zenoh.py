"""Installed publisher API using a real native Zenoh Session and synthetic peer."""
import asyncio
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import unittest

import wuji_sdk as sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from probe_zenoh_publisher import MockWujiZenoh, fixture_backend


@unittest.skipUnless(os.environ.get("WUJI_MACOS_LOOPBACK_TEST") == "1", "requires loopback-only verifier sandbox")
class InstalledZenohPublisherTests(unittest.TestCase):
    def test_native_postcard_and_sweep_bytes(self):
        self._exercise("postcard")

    def test_native_json_publishing(self):
        self._exercise("json")

    def _exercise(self, serde_format):
        backend = fixture_backend()
        manager = sdk.SdkManager(backend=backend)
        original_home = os.environ.get("HOME")
        with MockWujiZenoh(serde_format=serde_format) as fixture:
            hand = manager.connect(address=fixture.endpoint, device_name="zenoh_" + serde_format,
                options=sdk.ConnectOptions(timeout_ms=1000, retry_count=0, enable_bridge=False,
                                           auto_time_sync_interval_ms=None))
            try:
                self.assertIsInstance(hand, sdk.WujiHand2)
                self.assertEqual(hand.serial_number, fixture.serial)
                self.assertTrue(hand.is_connected)
                joints = [sdk.JointCommand(i / 8, -i / 4, i / 2) for i in range(20)]
                publisher = hand.joint_command().publish()
                second = hand.joint_command().publish()
                try:
                    with self.assertRaises(ValueError):
                        publisher.send([sdk.JointCommand(float("nan"), 0, 0)] * 20)
                    publisher.send(joints)
                    asyncio.run(publisher.send_async(joints))
                    second.send(joints)  # A separate handle has its own sequence.
                    self.assertTrue(fixture.wait_samples(3), fixture.published)
                    if serde_format == "postcard":
                        payload = struct.pack("<60f", *[v for j in joints for v in (j.position, j.velocity, j.effort)])
                        self.assertEqual([p["payload"] for p in fixture.published],
                            [(bytes([seq]) + payload).hex() for seq in (0, 1, 0)])
                    else:
                        records = [json.loads(bytes.fromhex(p["payload"])) for p in fixture.published]
                        self.assertEqual([p["seq"] for p in records], [0, 1, 0])
                        for record in records:
                            self.assertEqual(record["joints"], [dict(position=j.position, velocity=j.velocity, effort=j.effort) for j in joints])
                finally:
                    publisher.close()
                    second.close()
                with self.assertRaises(sdk.WujiException):
                    publisher.send(joints)

                generic = hand.publish("joint_command")
                try:
                    generic.send({"seq": 513, "joints": joints})
                    self.assertTrue(fixture.wait_samples(4), fixture.published)
                finally:
                    generic.close()
                generic_payload = bytes.fromhex(fixture.published[3]["payload"])
                if serde_format == "postcard":
                    self.assertEqual(generic_payload, b"\x81\x04" + payload)
                else:
                    self.assertEqual(json.loads(generic_payload), {"seq": 513,
                        "joints": [dict(position=j.position, velocity=j.velocity, effort=j.effort) for j in joints]})
                    # Float32 JSON can exceed the separate UDP packet limit.
                    # Exercise all 20 entries through the native schema codec.
                    values = [ctypes.c_float(v).value for v in (-1.2345678e30, -2.3456789e30, -3.4567892e30)]
                    large_joints = [sdk.JointCommand(*values) for _ in range(20)]
                    publisher = hand.joint_command().publish()
                    try:
                        publisher.send(large_joints)
                        self.assertTrue(fixture.wait_samples(5), fixture.published)
                    finally:
                        publisher.close()
                    large_payload = bytes.fromhex(fixture.published[4]["payload"])
                    self.assertGreater(len(large_payload), 1400)
                    record = json.loads(large_payload)
                    self.assertEqual(record["seq"], 0)
                    self.assertEqual(len(record["joints"]), 20)
                    for joint in record["joints"]:
                        self.assertEqual([ctypes.c_float(joint[name]).value for name in ("position", "velocity", "effort")], values)

                if serde_format == "postcard":
                    # Import only functions from the unchanged example. Its
                    # connection/motor-enable path never runs in this fixture.
                    spec = importlib.util.spec_from_file_location("mit_sweep_fixture", ROOT / "examples/python/wuji_hand_2/7.mit_sweep.py")
                    sweep = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(sweep)
                    frames = sweep.generate_sweep()
                    self.assertEqual(len(frames), 101)
                    publisher = hand.joint_command().publish()
                    try:
                        sweep.send_sweep(frames, publisher, sdk, sleep=lambda _: None)
                        self.assertTrue(fixture.wait_samples(4 + len(frames), timeout=5), len(fixture.published))
                    finally:
                        publisher.close()
                    expected = [(bytes([seq]) + struct.pack("<60f", *[v for position in frame for v in (position, 0.0, 0.0)])).hex()
                                for seq, frame in enumerate(frames)]
                    self.assertEqual([p["payload"] for p in fixture.published[4:]], expected)
                self.assertTrue(all(p["key"] == fixture.key("joint_command") for p in fixture.published))
                self.assertTrue(all(p["encoding"] == "zenoh/bytes" for p in fixture.published))
                # Closing a publisher must remain safe after its manager has
                # disconnected the device and released the connection lease.
                publisher = hand.joint_command().publish()
                try:
                    hand.disconnect()
                finally:
                    publisher.close()
                publisher.close()
            finally:
                hand.disconnect()
            self.assertFalse(hand.is_connected)
            self.assertEqual(fixture.failures, [])
            self.assertEqual(os.environ.get("HOME"), original_home)


if __name__ == "__main__":
    unittest.main()
