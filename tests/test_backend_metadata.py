"""Native metadata ownership boundary with explicit C-call substitutes."""
import ctypes as C
import json
from types import SimpleNamespace
import unittest

from wuji_sdk._backend import NativeBackend
from wuji_sdk import WujiException


class FingertipMetadataOwnershipTests(unittest.TestCase):
    def test_resource_lookup_accepts_native_dot_and_slash_separators(self):
        backend = NativeBackend.__new__(NativeBackend)
        descriptor = {"path": "fingertip/info_chunk", "can_get": True}
        backend._resources = lambda handle, include_hidden: [descriptor]
        for path in ("fingertip.info_chunk", "fingertip/info_chunk"):
            self.assertIs(backend._resource(7, path), descriptor)

    def backend(self, payload, status=0):
        backend = NativeBackend.__new__(NativeBackend)
        backend._devices = {7: {"device_name": "explicit_fixture"}}
        backend.library_path = b"unused-explicit-test-library"
        allocation = C.create_string_buffer(payload) if payload is not None else None
        freed, calls = [], []

        def native(library, device, finger, output, error, error_capacity):
            calls.append((device, finger))
            C.cast(output, C.POINTER(C.c_void_p)).contents.value = (
                C.addressof(allocation) if allocation is not None else None)
            if status:
                error.value = b"explicit native fixture error"
            return status

        backend.device_lib = SimpleNamespace(
            wuji_hand2_fingertip_info_json=native,
            wuji_device_buffer_free=lambda pointer: freed.append(pointer.value),
        )
        return backend, allocation, freed, calls

    def test_native_json_is_decoded_then_allocation_released(self):
        expected = {"model": "fixture tip", "digest": 300}
        backend, allocation, freed, calls = self.backend(json.dumps(expected).encode())
        result = backend._device_call(7, "get_fingertip_info", {"finger": 4})
        self.assertEqual(result, expected)
        self.assertEqual(calls, [(7, 4)])
        self.assertEqual(freed, [C.addressof(allocation)])

    def test_parse_and_native_errors_still_release_output(self):
        for payload, status, error_type in [(b"{", 0, ValueError), (b"owned error output", 3, WujiException),
                                             (None, 0, WujiException)]:
            with self.subTest(payload=payload, status=status):
                backend, allocation, freed, _ = self.backend(payload, status)
                with self.assertRaises(error_type):
                    backend._device_call(7, "get_fingertip_info", {"finger": 0})
                self.assertEqual(freed, [C.addressof(allocation) if allocation is not None else None])


if __name__ == "__main__":
    unittest.main()
