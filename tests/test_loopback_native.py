"""Installed Python API through the real SDK and an explicit local UDP fixture."""
import os
import asyncio
from pathlib import Path
import sys
import struct
import time
import unittest

import wuji_sdk as sdk
from wuji_macos._native import LIB_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from probe_mock_device import MockWujiUDP, REALTIME_SAMPLE


def wait_for(condition, timeout=2.0):
    """The SDK queues sends and cancels publishers in the background."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


@unittest.skipUnless(os.environ.get("WUJI_MACOS_LOOPBACK_TEST") == "1", "requires loopback-only verifier sandbox")
class InstalledNativeLoopbackTests(unittest.TestCase):
    def test_real_handshake_resource_requests_and_lifecycle(self):
        original_home = os.environ.get("HOME")
        manager = sdk.SdkManager.instance()
        with MockWujiUDP(LIB_DIR / "libwuji_cli_reuse.dylib", advertise_resources=True,
                         advertise_stream=True, advertise_fingertip=True, advertise_publisher=True) as fixture:
            hand = manager.connect(address=fixture.address, device_name="protocol_fixture",
                options=sdk.ConnectOptions(timeout_ms=100, retry_count=0, enable_bridge=False,
                                           auto_time_sync_interval_ms=None))
            try:
                self.assertIsInstance(hand, sdk.WujiHand2)
                self.assertEqual(hand.serial_number, fixture.serial)
                self.assertTrue(hand.is_connected)
                self.assertEqual(hand.info.serial_number, fixture.serial)
                self.assertEqual(hand.info.firmware_version, "1.0.0")
                self.assertCountEqual([n.path for n in hand.nodes()], ["", "fingertip"])
                resources = {r.path: r for r in hand.resources()}
                self.assertEqual(set(resources), {"fixture_value", "realtime_stream", "joint_states", "joint_diagnostics",
                                                 "fingertip/info_req", "fingertip/info_chunk", "joint_command"})
                self.assertTrue(resources["fixture_value"].can_set)
                self.assertEqual({p.path for p in hand.params()}, {"fixture_value", "fingertip/info_req", "fingertip/info_chunk"})
                self.assertEqual({t.path for t in hand.topics()}, {"realtime_stream", "joint_states", "joint_diagnostics", "joint_command"})
                self.assertEqual(hand.get("fixture_value"), 42)
                hand.set("fixture_value", 123456)
                self.assertEqual(hand.get("fixture_value"), 123456)
                self.assertEqual(manager.get("protocol_fixture.fixture_value"), 123456)
                # SDK 2026.9.22 removed WujiHand2::get_fingertip_info; the port
                # reports that instead of rebuilding the chunked request itself.
                with self.assertRaisesRegex(sdk.WujiException, "removed get_fingertip_info"):
                    hand.get_fingertip_info(2)
                self.assertEqual(fixture.fingertip_requests, [])
                # Commands are sent only to this explicitly local fixture.
                # Compare exact wire bytes, not just successful Python calls.
                joints = [sdk.JointCommand(i / 8, -i / 4, i / 2) for i in range(20)]
                payload = struct.pack("<60f", *[v for j in joints for v in (j.position, j.velocity, j.effort)])
                publisher = hand.joint_command().publish()
                try:
                    # The SDK's own Device::publish negotiates a stream per
                    # publisher, so a second one opens and closes independently.
                    hand.joint_command().publish().close()
                    with self.assertRaises(ValueError):
                        publisher.send([sdk.JointCommand(float("nan"), 0, 0)] * 20)
                    publisher.send(joints)
                    asyncio.run(publisher.send_async(joints))
                finally:
                    publisher.close()
                self.assertTrue(wait_for(lambda: len(fixture.published) >= 2))
                self.assertEqual([p["payload"] for p in fixture.published],
                                 [(bytes([sequence]) + payload).hex() for sequence in (0, 1)])
                self.assertTrue(all(p["timestamp_us"] > 0 for p in fixture.published))
                self.assertEqual(fixture.published[1]["sequence"], fixture.published[0]["sequence"] + 1)
                generic = manager.publish("protocol_fixture.joint_command")
                try:
                    generic.send({"seq": 513, "joints": joints})
                finally:
                    generic.close()
                self.assertTrue(wait_for(lambda: len(fixture.published) == 3 and len(fixture.canceled_publish) == 2))
                self.assertEqual(fixture.published[-1]["payload"], (b"\x81\x04" + payload).hex())
                self.assertEqual(len(fixture.publish_requests), 3)
                # The fixture grants every publisher the same stream ID; the SDK
                # reference-counts it, so the two typed publishers share one cancel.
                self.assertEqual(len(fixture.canceled_publish), 2)
                subscription = hand.joint_states().subscribe()
                try:
                    self.assertIsNone(subscription.recv())
                    fixture.emit_stream(REALTIME_SAMPLE)

                    async def receive():
                        return await asyncio.wait_for(subscription.recv_async(), timeout=3)

                    frame = asyncio.run(receive())
                    self.assertIsInstance(frame, sdk.JointStateFrame)
                    self.assertEqual(frame.header.seq, 300)
                    self.assertEqual(frame.header.timestamp_us, 1234567890123)
                    self.assertEqual(frame.num_joints, 2)
                    self.assertEqual([j.nid for j in frame.joints], [0, 23])
                    self.assertEqual([j.position for j in frame.joints], [0.25, -0.5])
                    self.assertEqual([j.effort for j in frame.joints], [2.75, -4.5])
                finally:
                    subscription.close()
                with self.assertRaises(sdk.WujiException):
                    hand.execute("fixture_value")  # Native resource lacks can_exec.
                self.assertIs(manager.get_device("protocol_fixture"), hand)
            finally:
                hand.disconnect()
            self.assertFalse(hand.is_connected)
            self.assertEqual(hand.info.serial_number, fixture.serial)
            self.assertIsNone(manager.get_device("protocol_fixture"))
            replacement = manager.connect(address=fixture.address, device_name="protocol_fixture",
                options=sdk.ConnectOptions(timeout_ms=100, retry_count=0, enable_bridge=False,
                                           auto_time_sync_interval_ms=None))
            try:
                hand.disconnect()  # The old handle must not disconnect its alias's replacement.
                self.assertIs(manager.get_device("protocol_fixture"), replacement)
                self.assertTrue(replacement.is_connected)
                self.assertEqual(replacement.get("fixture_value"), 123456)
            finally:
                replacement.disconnect()
            self.assertEqual(fixture.failures, [])
            self.assertEqual(os.environ.get("HOME"), original_home)


if __name__ == "__main__":
    unittest.main()
