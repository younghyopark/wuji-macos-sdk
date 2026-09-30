"""Public scan conversion with explicit native-format records; no native I/O."""
import unittest

from wuji_sdk import DeviceType, DiscoveredDevice, SdkManager, TransportType, _types


class DiscoveryBackend:
    def __init__(self, records):
        self.records = records

    def call(self, scope, handle, operation, **arguments):
        if (scope, handle, operation) == ("manager", None, "open"):
            return "discovery-fixture"
        if (scope, handle, operation) == ("manager", "discovery-fixture", "scan"):
            return self.records
        raise AssertionError(f"Unexpected backend call: {scope}, {handle}, {operation}")


class DiscoveryConversionTests(unittest.TestCase):
    def test_scan_preserves_all_four_native_transport_variants(self):
        # These are the four variants emitted by the pinned Mac native enum.
        # Zenoh is absent from the vendor stub but occurs in real discovery.
        records = [
            {"sn": "FIXTURE_USB", "address": "fixture-usb", "transport_type": "Usb", "device_type": "WujiHand"},
            {"sn": "FIXTURE_UDP", "address": "192.0.2.10:50001", "transport_type": "Udp", "device_type": "WujiGlove"},
            {"sn": "FIXTURE_ZENOH", "address": "192.0.2.11:7447", "transport_type": "Zenoh", "device_type": "WujiHand2"},
            {"sn": "FIXTURE_CPP", "address": "fixture-native", "transport_type": "CppNative", "device_type": "Unknown"},
        ]
        devices = SdkManager(DiscoveryBackend(records)).scan()
        self.assertTrue(all(isinstance(device, DiscoveredDevice) for device in devices))
        self.assertEqual([device.transport_type for device in devices],
                         [TransportType.Usb, TransportType.Udp, TransportType.Zenoh, TransportType.CppNative])
        self.assertEqual([device.sn for device in devices], [record["sn"] for record in records])
        self.assertEqual([device.address for device in devices], [record["address"] for record in records])
        self.assertEqual([device.sn for device in devices if device.device_type == DeviceType.WujiHand2], ["FIXTURE_ZENOH"])
        self.assertEqual(_types._to_native(devices[2])["transport_type"], "Zenoh")

    def test_unrecognized_transport_is_not_mislabeled_as_udp(self):
        manager = SdkManager(DiscoveryBackend([
            {"sn": "FIXTURE_BAD", "address": "fixture", "transport_type": "Unexpected", "device_type": "WujiHand2"},
        ]))
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            manager.scan()


if __name__ == "__main__":
    unittest.main()
