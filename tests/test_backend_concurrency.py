"""Explicit blocking call fixtures for Python ownership locks, no native I/O."""
import asyncio
import threading
import unittest
from types import SimpleNamespace

from wuji_sdk._api import Subscription
from wuji_sdk._backend import NativeBackend
from wuji_sdk import WujiException


class BlockingBackend(NativeBackend):
    def __init__(self):
        self._lock = threading.RLock()
        self._manager_lock = threading.RLock()
        self._manager = 9
        self._devices = {1: {"device_name": "hand_a"}, 2: {"device_name": "hand_b"}}
        self._device_locks = {key: threading.RLock() for key in self._devices}
        self._subscriptions = {10: {}}
        self._subscription_locks = {10: threading.RLock()}
        self.rpc_entered, self.rpc_resume = threading.Event(), threading.Event()
        self.recv_entered, self.recv_resume = threading.Event(), threading.Event()
        self.block_receive = False
        self.released, self.closed = [], []
        self.manager_lib = SimpleNamespace(wuji_manager_release_device=self.released.append)
        self.device_lib = SimpleNamespace(wuji_subscription_close=self.closed.append)

    def _device_call(self, handle, operation, arguments):
        if handle not in self._devices:
            raise WujiException("Invalid or released device handle")
        if operation == "get":
            self.rpc_entered.set()
            if not self.rpc_resume.wait(3):
                raise AssertionError("Test RPC was not released")
            return 7
        return super()._device_call(handle, operation, arguments)

    def _subscription_call(self, handle, operation, arguments):
        if operation == "recv":
            if handle not in self._subscriptions:
                raise WujiException("StreamClosed")
            self.recv_entered.set()
            if self.block_receive and not self.recv_resume.wait(3):
                raise AssertionError("Test receiver was not released")
            return None
        return super()._subscription_call(handle, operation, arguments)


class BackendOwnershipConcurrencyTests(unittest.TestCase):
    def start_rpc(self, backend):
        errors = []

        def run():
            try:
                # Exercise the real manager -> device routing path too.
                backend.call("manager", 9, "get", path="hand_a.fixture")
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(backend.rpc_entered.wait(1))
        self.addCleanup(worker.join, 4)
        self.addCleanup(backend.rpc_resume.set)
        return worker, errors

    def test_blocked_routed_rpc_does_not_block_other_stream_or_event_loop(self):
        backend = BlockingBackend()
        worker, errors = self.start_rpc(backend)
        sub = Subscription(backend, 10, "uint8", 4)

        async def scenario():
            waiter = asyncio.create_task(sub.recv_async())
            ticks = 0
            try:
                for _ in range(8):
                    await asyncio.sleep(0.002)
                    ticks += 1
                self.assertFalse(backend.rpc_resume.is_set())
                self.assertTrue(backend.recv_entered.is_set())
                self.assertFalse(waiter.done())
                return ticks
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

        try:
            self.assertEqual(asyncio.run(scenario()), 8)
            self.assertTrue(worker.is_alive())
            self.assertEqual(errors, [])
        finally:
            backend.rpc_resume.set()
            worker.join(1)
            sub.close()
        self.assertFalse(worker.is_alive())

    def test_release_waits_for_same_device_operation(self):
        backend = BlockingBackend()
        worker, errors = self.start_rpc(backend)
        released = threading.Event()

        def release():
            backend.release_device(1)
            released.set()

        closer = threading.Thread(target=release)
        closer.start()
        try:
            self.assertFalse(released.wait(0.02))
            self.assertEqual(backend.released, [])
            backend.rpc_resume.set()
            self.assertTrue(released.wait(1))
            self.assertEqual(backend.released, [1])
            backend.release_device(1)
            self.assertEqual(backend.released, [1])
            with self.assertRaisesRegex(WujiException, "released"):
                backend.call("device", 1, "get", path="fixture")
        finally:
            backend.rpc_resume.set()
            worker.join(1)
            closer.join(1)
        self.assertEqual(errors, [])

    def test_subscription_close_waits_for_its_receiver_only(self):
        backend = BlockingBackend()
        backend.block_receive = True
        receiver = threading.Thread(target=backend.call, args=("subscription", 10, "recv"))
        receiver.start()
        self.assertTrue(backend.recv_entered.wait(1))
        closed = threading.Event()

        def close():
            backend.call("subscription", 10, "close")
            closed.set()

        closer = threading.Thread(target=close)
        closer.start()
        try:
            self.assertFalse(closed.wait(0.02))
            # Releasing another device must not need the subscription lock.
            backend.release_device(2)
            self.assertEqual(backend.released, [2])
            backend.recv_resume.set()
            self.assertTrue(closed.wait(1))
            self.assertEqual(backend.closed, [10])
            backend.call("subscription", 10, "close")
            self.assertEqual(backend.closed, [10])
            with self.assertRaisesRegex(WujiException, "StreamClosed"):
                backend.call("subscription", 10, "recv")
        finally:
            backend.recv_resume.set()
            receiver.join(1)
            closer.join(1)


if __name__ == "__main__":
    unittest.main()
