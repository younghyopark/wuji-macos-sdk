import unittest
from wuji_sdk import WujiHand2
from wuji_sdk._error_catalog import CATALOG
from wuji_sdk._api import _convert, JointHandle


class StaticHand2Tests(unittest.TestCase):
    def test_sparse_firmware_ids(self):
        self.assertEqual(WujiHand2.joint_id_from_bus_node(0, 1), 0)
        self.assertEqual(WujiHand2.joint_id_from_bus_node(4, 4), 23)
        with self.assertRaises(ValueError):
            WujiHand2.joint_id_from_bus_node(0, 0)

    def test_native_status_bit_mapping_and_joint_labels(self):
        status = _convert("StatusWord", 2 | (1 << 4) | (1 << 6) | (1 << 31))
        self.assertEqual(status.ext_state, 2)
        self.assertEqual(status.ext_state_name, "Enabled")
        self.assertTrue(status.position_limit_active)
        self.assertFalse(status.velocity_limit_active)
        self.assertTrue(status.current_limit_active)
        self.assertEqual(JointHandle(None, 0).label, "thumb_S1")
        self.assertEqual(JointHandle(None, 19).label, "pinky_S4")

    def test_fault_catalog_is_complete_for_pinned_native_image(self):
        self.assertEqual(len(CATALOG), 53)
        self.assertIsNone(WujiHand2.describe_error(0))
        self.assertIsNone(WujiHand2.describe_error(0xFFFF))
        for code, expected in CATALOG.items():
            self.assertEqual(WujiHand2.describe_error(code), expected)
            value = WujiHand2.describe_error(code)
            value["name"] = "mutated by caller"
            self.assertEqual(WujiHand2.describe_error(code), expected)


if __name__ == "__main__":
    unittest.main()
