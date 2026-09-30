"""Subscription receive semantics with an explicit native-library substitute.

This checks Python dispatch only; it does not exercise the native receiver.
"""
import ctypes
import unittest

from wuji_sdk._backend import NativeBackend


class ScriptedLibrary:
    """Replays native try_recv results: ("lag", n), ("frame", bytes) or ("empty",)."""

    def __init__(self, script):
        self.script = list(script)
        self.buffers = []

    def wuji_subscription_try_recv(self, handle, output, length, lagged, error, capacity):
        kind, *value = self.script.pop(0)
        if kind == "lag":
            lagged._obj.value = value[0]
            return 4
        if kind == "empty":
            return 1
        buffer = ctypes.create_string_buffer(value[0], len(value[0]))
        self.buffers.append(buffer)
        output._obj.value = ctypes.addressof(buffer)
        length._obj.value = len(value[0])
        return 0

    def wuji_device_buffer_free(self, pointer):
        pass


class ReceiveBackend(NativeBackend):
    def __init__(self, script):
        self.device_lib = ScriptedLibrary(script)
        self._subscriptions = {7: {"schema_id": 0, "serde_format": 1}}


class RecvTests(unittest.TestCase):
    def test_lag_is_skipped_and_counted(self):
        backend = ReceiveBackend([("lag", 197), ("frame", b'{"seq": 5}'), ("lag", 3), ("empty",)])
        self.assertEqual(backend._subscription_call(7, "recv", {}), {"seq": 5})
        self.assertEqual(backend._subscription_call(7, "lagged_frames", {}), 197)
        self.assertIsNone(backend._subscription_call(7, "recv", {}))
        self.assertEqual(backend._subscription_call(7, "lagged_frames", {}), 200)


if __name__ == "__main__":
    unittest.main()
