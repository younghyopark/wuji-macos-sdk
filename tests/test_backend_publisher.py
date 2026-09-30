"""Publisher ownership and float32 boundary tests with explicit C substitutes."""
import ctypes as C
import threading
from types import SimpleNamespace
import unittest

from wuji_sdk import JointCommand, JointCommandPublisher, WujiException
from wuji_sdk._backend import NativeBackend


class PublisherBoundaryTests(unittest.TestCase):
    def test_waiting_operation_cannot_use_recycled_publisher_pointer(self):
        for operation in ("send", "close"):
            with self.subTest(operation=operation):
                backend = NativeBackend.__new__(NativeBackend)
                backend._lock = threading.RLock()
                replacement_lock = threading.RLock()
                calls = []
                backend._publisher_call = lambda *args: calls.append(args)

                class RecycledWhileWaiting:
                    def __enter__(self):
                        # Deterministically model close/open reusing pointer 7
                        # before a waiting operation acquires its original lock.
                        backend._publisher_locks[7] = replacement_lock

                    def __exit__(self, *_):
                        pass

                backend._publisher_locks = {7: RecycledWhileWaiting()}
                if operation == "close":
                    self.assertIsNone(backend.call("publisher", 7, operation))
                else:
                    with self.assertRaisesRegex(WujiException, "publisher is closed"):
                        backend.call("publisher", 7, operation)
                self.assertEqual(calls, [])
                self.assertIs(backend._publisher_locks[7], replacement_lock)

    def test_open_failure_identifies_the_operation_even_with_empty_native_error(self):
        backend = NativeBackend.__new__(NativeBackend)
        backend._devices = {7: {"device_name": "explicit_fixture"}}
        backend.library_path = b"unused-explicit-test-library"
        backend._resource = lambda handle, path: {"can_pub": True, "schema_id": 315, "serde_format": 0}
        backend.device_lib = SimpleNamespace(wuji_device_publisher_open=lambda *args: 2)
        with self.assertRaisesRegex(WujiException, "opening publisher 'joint_command': Native bridge returned 2"):
            backend._device_call(7, "publish", {"path": "joint_command", "typed_joint_commands": True})

    def backend(self, close_status=0):
        backend = NativeBackend.__new__(NativeBackend)
        backend._lock = threading.RLock()
        backend._publisher_locks = {7: threading.RLock()}
        backend._publishers = {7: {"typed": True}}
        calls = []

        def send(handle, values, length, error, capacity):
            calls.append(("send", list(values[:length])))
            return 0

        def close(handle, error, capacity):
            calls.append(("close", handle))
            if close_status:
                error.value = b"explicit cancellation error after cleanup"
            return close_status

        backend.device_lib = SimpleNamespace(wuji_publisher_send_joint_commands=send,
                                              wuji_publisher_close=close)
        return backend, calls, JointCommandPublisher(backend, 7, "JointCommandFrame")

    def test_typed_commands_keep_twenty_aos_float32_entries(self):
        backend, calls, publisher = self.backend()
        joints = [JointCommand(i + 0.25, -i - 0.5, i / 8) for i in range(20)]
        publisher.send(joints)
        self.assertEqual(calls, [("send", [v for i in range(20) for v in (i + 0.25, -i - 0.5, i / 8)])])
        publisher.close()

    def test_finite_python_double_overflow_is_rejected_before_native_send(self):
        backend, calls, publisher = self.backend()
        with self.assertRaisesRegex(ValueError, "float32"):
            publisher.send([JointCommand(1e100, 0.0, 0.0)] * 20)
        self.assertEqual(calls, [])
        publisher.close()

    def test_failed_cancel_still_releases_publisher_once(self):
        backend, calls, publisher = self.backend(close_status=3)
        with self.assertRaisesRegex(WujiException, "cancellation error"):
            publisher.close()
        publisher.close()
        self.assertEqual(calls, [("close", 7)])
        self.assertEqual(backend._publishers, {})
        self.assertEqual(backend._publisher_locks, {})
        with self.assertRaisesRegex(WujiException, "closed"):
            publisher.send([JointCommand(0.0, 0.0, 0.0)] * 20)


if __name__ == "__main__":
    unittest.main()
