"""Exercise the installed native wheel, not an implementation copied into tests."""
import random
import unittest
from wuji_macos import NativeHeaderError, backend_info, roundtrip_header


class NativeCodecTests(unittest.TestCase):
    def test_upstream_provenance(self):
        info = backend_info()
        self.assertEqual(info["upstream_version"], "2026.9.22")
        self.assertIn("offline_header_codec", info["capabilities"])

    def test_known_header(self):
        wire = bytes.fromhex("5710123478563412efcdab90112266558877")
        self.assertEqual(roundtrip_header(wire), wire)

    def test_varied_native_roundtrips(self):
        # Fixed magic/version; vary all remaining wire bytes to exercise the
        # recovered Rust struct layout, endian handling, and sret bridge.
        rng = random.Random(42)
        for _ in range(300):
            wire = b"W\x10" + rng.randbytes(16)
            self.assertEqual(roundtrip_header(wire), wire)

    def test_native_rejects_bad_magic(self):
        with self.assertRaises(NativeHeaderError):
            roundtrip_header(bytes(18))

    def test_native_rejects_bad_version(self):
        with self.assertRaises(NativeHeaderError):
            roundtrip_header(b"W\x20" + bytes(16))

    def test_input_boundaries(self):
        for payload in [b"", bytes(17), bytes(19)]:
            with self.assertRaises(ValueError):
                roundtrip_header(payload)
        with self.assertRaises(TypeError):
            roundtrip_header("W" * 18)


if __name__ == "__main__":
    unittest.main()
