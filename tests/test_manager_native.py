"""Real SDK manager tests; run only through the hardware-denying verifier."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import wuji_sdk

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("WUJI_MACOS_OFFLINE_TEST") == "1", "requires scripts/verify_wheel.py sandbox")
class NativeManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_home = os.environ.get("HOME")
        cls.manager = wuji_sdk.SdkManager.instance()

    def test_singleton_and_containment(self):
        self.assertIs(self.manager, wuji_sdk.SdkManager.instance())
        self.assertEqual(os.environ.get("HOME"), self.original_home)
        self.assertTrue(Path(os.environ["WUJI_MACOS_STATE_DIR"]).is_dir())

    def test_scan_with_network_and_iokit_denied(self):
        self.assertEqual(self.manager.scan(), [])

    def test_native_connect_error(self):
        with self.assertRaisesRegex(wuji_sdk.WujiException, "not found"):
            self.manager.connect(sn="no-such-wuji-device-offline-test", device_name="hand2")
        self.assertEqual(self.manager.get_connected_devices(), [])

    def test_handedness_selector_reaches_native_discovery(self):
        with self.assertRaisesRegex(wuji_sdk.WujiException, "not found"):
            self.manager.connect(handedness=wuji_sdk.Handedness.Left, device_name="left_hand")

    def test_log_level_validation_and_native_filter(self):
        wuji_sdk.set_log_level("warning")
        wuji_sdk.set_log_level("info")
        with self.assertRaises(ValueError):
            wuji_sdk.set_log_level("not-a-level")

    def test_address_error_and_options(self):
        with self.assertRaisesRegex(wuji_sdk.WujiException, "7447"):
            self.manager.connect(address="192.0.2.1", device_name="hand2",
                                 options=wuji_sdk.ConnectOptions(enable_bridge=False, auto_time_sync_interval_ms=None))

    def test_auto_connect_does_not_treat_alias_as_type(self):
        with self.assertRaisesRegex(wuji_sdk.WujiException, "no device"):
            self.manager.auto_connect("arbitrary_alias")

    def test_disconnect_all(self):
        self.manager.disconnect_all()
        self.assertEqual(self.manager.get_connected_devices(), [])

    def test_user_registry_is_native_and_contained(self):
        user = self.manager.switch_to_default_user()
        self.assertEqual(user["user_id"], "")
        self.assertEqual(self.manager.current_user(), user)
        self.assertIn(user, self.manager.list_users())
        registry = Path(os.environ["WUJI_MACOS_STATE_DIR"]) / ".wuji/sdk/users/registry.toml"
        self.assertTrue(registry.is_file())
        self.assertEqual(os.environ.get("HOME"), self.original_home)

    def test_user_crud_and_tactile_paths_use_native_registry(self):
        name = "macos-verifier-" + uuid.uuid4().hex
        user = self.manager.create_user(name, description='fixture "note"', external_id=name)
        user_id = user["user_id"]
        try:
            self.assertEqual(user["display_name"], name)
            self.assertEqual(user["description"], 'fixture "note"')
            updated = self.manager.update_user(user_id, display_name=name + "-updated",
                                                clear_description=True, clear_external_id=True)
            self.assertEqual(updated["display_name"], name + "-updated")
            self.assertIsNone(updated["description"])
            self.assertIsNone(updated["external_id"])
            self.assertEqual(self.manager.switch_user(user_id), updated)
            self.assertEqual(self.manager.current_user()["user_id"], user_id)
            root = Path(os.environ["WUJI_MACOS_STATE_DIR"]).resolve()
            models = self.manager.tactile_model_paths(user_id, "FIXTURE_HAND2")
            self.assertEqual(set(models), {"dir", "safetensors", "npz"})
            paths = [self.manager.tactile_dir(user_id, "FIXTURE_HAND2"),
                     self.manager.tactile_run_dir(user_id, "FIXTURE_HAND2"), *models.values()]
            self.assertTrue(all(Path(path).resolve().is_relative_to(root) for path in paths))
            with self.assertRaises(wuji_sdk.WujiException):
                self.manager.tactile_dir(user_id, "../../outside")
        finally:
            self.manager.delete_user(user_id)
        self.assertEqual(self.manager.current_user()["user_id"], "")
        self.assertNotIn(user_id, [item["user_id"] for item in self.manager.list_users()])

    def test_switch_to_nonexistent_user_returns_native_error(self):
        with self.assertRaises(wuji_sdk.WujiException):
            self.manager.switch_user("no-such-wuji-sdk-user")

    def test_original_readonly_examples_no_device_branch(self):
        # These two originals only subscribe to feedback. The inherited OS
        # sandbox denies both network and USB, so this tests real no-device
        # behavior, not successful subscription or physical hand operation.
        for name in ("0.subscribe_callback.py", "1.subscribe_async.py"):
            with self.subTest(example=name):
                result = subprocess.run([sys.executable, str(ROOT / "examples/python/wuji_hand_2" / name)],
                                        cwd=ROOT, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("No devices found", result.stdout)


if __name__ == "__main__":
    unittest.main()
