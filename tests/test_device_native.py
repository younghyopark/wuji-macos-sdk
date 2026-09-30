"""Exercise actual packaged SDK serializers and broadcast-channel ownership."""
import ctypes as C
import json
from pathlib import Path
import unittest

from wuji_macos._native import LIB_DIR, _bridge


class NativeDeviceCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bridge()
        cls.library = str(LIB_DIR / "libwuji_cli_reuse.dylib").encode()
        cls.bridge = C.CDLL(str(LIB_DIR / "libwuji_device_bridge.dylib"))
        p, n = C.c_void_p, C.c_size_t
        cls.bridge.wuji_device_codec.argtypes = [C.c_char_p, C.c_uint16, C.c_int, C.c_int, p, n, p, n, C.POINTER(n), p, n]
        cls.bridge.wuji_device_codec.restype = C.c_int
        cls.bridge.wuji_device_joint_command_encode.argtypes = [C.c_char_p, C.c_uint32, C.POINTER(C.c_float), n, C.c_int, p, n, C.POINTER(n), p, n]
        cls.bridge.wuji_device_joint_command_encode.restype = C.c_int
        cls.bridge.wuji_device_joint_state_fixture.argtypes = [C.c_char_p, p, n, C.POINTER(n)]
        cls.bridge.wuji_device_joint_state_fixture.restype = C.c_int
        cls.bridge.wuji_device_decode_fixture_json.argtypes = [C.c_char_p, C.c_uint16, p, n, p, n, C.POINTER(n)]
        cls.bridge.wuji_device_decode_fixture_json.restype = C.c_int
        cls.bridge.wuji_device_offline_stream_probe.argtypes = [C.c_char_p, p, n]
        cls.bridge.wuji_device_offline_stream_probe.restype = C.c_int

    def test_installed_native_serializers_match_recorded_fixtures(self):
        fixtures = json.loads((Path(__file__).parent / "data/native-codec-fixtures.json").read_text())
        for fixture in fixtures:
            with self.subTest(schema=fixture["schema_id"]):
                output, error, written = C.create_string_buffer(8192), C.create_string_buffer(2048), C.c_size_t()
                if fixture["schema_id"] in (291, 334):
                    payload = bytes.fromhex(fixture["postcard_hex"])
                    source = C.create_string_buffer(payload)
                    code = self.bridge.wuji_device_decode_fixture_json(self.library, fixture["schema_id"], source, len(payload), output, len(output), C.byref(written))
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(output.value), fixture["value"])
                    continue
                if fixture["schema_id"] == 315:
                    values = [joint[field] for joint in fixture["value"]["joints"] for field in ("position", "velocity", "effort")]
                    source = (C.c_float * 60)(*values)
                    code = self.bridge.wuji_device_joint_command_encode(self.library, fixture["value"]["seq"], source, 60, 0, output, len(output), C.byref(written), error, len(error))
                elif fixture["schema_id"] == 330:
                    code = self.bridge.wuji_device_joint_state_fixture(self.library, output, len(output), C.byref(written))
                else:
                    payload = json.dumps(fixture["value"]).encode()
                    source = C.create_string_buffer(payload)
                    code = self.bridge.wuji_device_codec(self.library, fixture["schema_id"], 1, 0, source, len(payload), output, len(output), C.byref(written), error, len(error))
                self.assertEqual(code, 0, error.value.decode())
                self.assertEqual(output.raw[:written.value].hex(), fixture["postcard_hex"])

    def test_native_receiver_handles_empty_lagged_frames_and_close(self):
        error = C.create_string_buffer(2048)
        for _ in range(20):
            self.assertEqual(self.bridge.wuji_device_offline_stream_probe(self.library, error, len(error)), 0, error.value.decode())



if __name__ == "__main__":
    unittest.main()
