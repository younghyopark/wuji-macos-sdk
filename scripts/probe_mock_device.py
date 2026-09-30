#!/usr/bin/env python3
"""Exercise real native SDK handles against an explicit localhost UDP fixture.

This is a minimal protocol fixture with no physical sensors or actuators. It
can advertise a uint32 GET/SET resource. It proves native connection, transport,
and ownership paths; it does not prove physical Hand 2 interoperability.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/mock-device-probe"
LIBRARY = ROOT / "build/native-reuse/libwuji_cli_reuse.dylib"

# These unequal-field samples were checked by the native RealtimeStreamFrame
# deserializer and JointStateFrame serializer in scripts/probe_device.py.
REALTIME_SAMPLE = bytes.fromhex(
    "ac02cb89ec8ff7230b68616e6432220a6261736502020081010000803e0000c0bf00003040"
    "0000003f0000c4410000fa41810217818002000000bf00005040000090c00000403f0000ca410000f4418104")
JOINT_STATES_SAMPLE = bytes.fromhex(
    "ac02cb89ec8ff7230b68616e6432220a626173650202000000803e0000c0bf0000304017000000bf00005040000090c0")
FINGERTIP_INFO_SAMPLE = bytes.fromhex(
    "ac02cb89ec8ff7230b68616e6432220a62617365b9600a57756a69202274697022d9020000f742"
    "0f7b226b696e64223a2274657374227d")
FINGERTIP_INFO_VALUE = {
    "header": {"seq": 300, "timestamp_us": 1234567890123, "frame_id": 'hand2"\nbase'},
    "digest": 12345, "model": 'Wuji "tip"', "device_type": 345,
    "rate_hz": 123.5, "format": '{"kind":"test"}',
}


def wait(condition, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.002)


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(payload):
    value = 0
    for index, byte in enumerate(payload):
        if index >= 5:
            raise ValueError("Fixture uint32 varint is too long")
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            if index + 1 != len(payload) or value >= 1 << 32:
                raise ValueError("Fixture uint32 payload is malformed")
            return value
    raise ValueError("Fixture uint32 varint is incomplete")


def read_varints(payload):
    values, start = [], 0
    for index, byte in enumerate(payload):
        if byte < 128:
            values.append(read_varint(payload[start:index + 1]))
            start = index + 1
    if start != len(payload):
        raise ValueError("Fixture varint sequence is incomplete")
    return values


class MockWujiUDP:
    """Context-managed protocol fixture bound exclusively to 127.0.0.1.

    The schema and frame layouts were checked against native deserializers and
    the real request/response state machine in CLI 2026.8.31. Native CRC code is
    used so the fixture does not introduce a second checksum implementation.
    Connection and an optional scalar resource are implemented. Its writes only
    update this fixture's in-memory integer.
    """
    serial = "MOCKHAND2ABCDEF0"
    session_id = 0x12345678

    def __init__(self, library_path=LIBRARY, *, advertise_resources=False, advertise_stream=False,
                 advertise_fingertip=False, advertise_publisher=False):
        self.advertise_resources = advertise_resources
        self.advertise_stream = advertise_stream
        self.advertise_fingertip = advertise_fingertip
        self.advertise_publisher = advertise_publisher
        self.publish_requests = []
        self.published = []
        self.canceled_publish = []
        self.reject_publisher_cancel = False
        self.fingertip_selector = None
        self.fingertip_requests = []
        self.scalar_value = 42
        self.library = C.CDLL(str(library_path))
        self.crc = getattr(self.library, "_RNvNtCsgTcy8zmbXBL_9wuji_core3crc18compute_tail_crc16")
        self.crc.argtypes, self.crc.restype = [C.c_void_p, C.c_size_t], C.c_uint16
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.1)
        self.address = "127.0.0.1:" + str(self.socket.getsockname()[1])
        self.packets = []
        self.failures = []
        self.peer = None
        self.stream_sequence = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        # Discover: device ID, firmware triplet, protocol version, serial[16], IP[4].
        self.discover_body = varint(2) + bytes([1, 0, 0]) + varint(1) + self.serial.encode().ljust(16, b"\x00") + bytes([127, 0, 0, 1])
        # Hello: session ID, timeout, retry count, initial device clock.
        self.hello_body = varint(self.session_id) + varint(1000) + bytes([0]) + varint(0)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Local mock thread did not stop")

    def _checksum(self, frame):
        buffer = C.create_string_buffer(bytes(frame))
        return self.crc(buffer, len(frame))

    def emit_stream(self, payload, *, timestamp=0):
        """Publish one simulated native stream packet to the connected client."""
        if not self.advertise_stream or self.peer is None:
            raise RuntimeError("Fixture stream is not connected")
        body = varint(7) + varint(timestamp) + bytes(payload)
        frame = bytearray(18)
        frame[:3] = b"\x57\x10\x50"
        self.stream_sequence += 1
        struct.pack_into("<II", frame, 4, self.session_id, self.stream_sequence)
        struct.pack_into("<H", frame, 16, len(body))
        frame.extend(body + b"\x00\x00")
        struct.pack_into("<H", frame, len(frame) - 2, self._checksum(frame))
        self.socket.sendto(frame, self.peer)
        self.packets.append({"stream": frame.hex()})

    def _response(self, request):
        if len(request) < 20 or request[0] != 0x57 or request[1] >> 4 != 1:
            raise ValueError("Malformed native request header")
        if struct.unpack_from("<H", request, 16)[0] + 20 != len(request):
            raise ValueError("Native request payload length mismatch")
        if struct.unpack_from("<H", request, len(request) - 2)[0] != self._checksum(request):
            raise ValueError("Native request CRC mismatch")
        kind = request[2]
        # Response prefix is status:u16 and payload codec/version:u8.
        prefix = b"\x00\x00\x01"
        if kind == 0:
            body = self.discover_body
        elif kind == 2:
            body = self.hello_body
        elif kind == 12:
            # Echo the request clock into the three time-sync timestamps.
            body = request[19:-2] * 3
        elif kind == 8 and (self.advertise_resources or self.advertise_stream or self.advertise_fingertip or self.advertise_publisher):
            resources = []
            for resource_id, name, access, schema in (
                [(1, b"fixture_value", 3, 3)] if self.advertise_resources else []) + (
                [(2, b"realtime_stream", 4, 291)] if self.advertise_stream else []) + (
                [(5, b"joint_command", 8, 315)] if self.advertise_publisher else []):
                resources.append(varint(resource_id) + varint(len(name)) + name + bytes([access]) + varint(schema) + bytes([0, 0]))
            body = bytes([1, 0, 0, 2 if self.advertise_fingertip else 1, 1, len(resources)]) + b"".join(resources)
            if self.advertise_fingertip:
                body += bytes([2, 2])
                for resource_id, name, access, schema in [(3, b"info_req", 2, 336), (4, b"info_chunk", 1, 337)]:
                    body += varint(resource_id) + varint(len(name)) + name + bytes([access]) + varint(schema) + bytes([0, 0])
        elif kind == 10 and (self.advertise_resources or self.advertise_stream or self.advertise_fingertip or self.advertise_publisher):
            body = bytes([1, 0, 0, 2 if self.advertise_fingertip else 1, 0, 1, 0])
            if self.advertise_fingertip:
                body += bytes([1, 2, 9]) + b"fingertip"
        elif kind in (8, 10):
            # Page version=1, page index=0, last page=0, zero records.
            body = b"\x01\x00\x00\x00"
        elif kind in (4, 6, 14):
            body = b""
        elif kind == 32 and self.advertise_fingertip and request[18:-2] == b"\x01\x00\x04" and request[14:16] == b"\x01\x00":
            if self.fingertip_selector is None:
                raise ValueError("Fingertip chunk GET without selector SET")
            finger, offset = self.fingertip_selector
            chunk = FINGERTIP_INFO_SAMPLE[offset:offset + 17]
            body = struct.pack("<H", 337) + bytes([finger]) + varint(offset)
            body += varint(len(FINGERTIP_INFO_SAMPLE)) + varint(len(chunk)) + chunk
        elif kind == 34 and self.advertise_fingertip and request[18:23] == b"\x01\x00\x00\x03\x00" and request[14:16] == b"\x01\x00":
            finger, offset = request[23], read_varint(request[24:-2])
            if finger > 4 or offset > len(FINGERTIP_INFO_SAMPLE):
                raise ValueError("Invalid fingertip fixture selector")
            self.fingertip_selector = (finger, offset)
            self.fingertip_requests.append(self.fingertip_selector)
            body = varint(336)
        elif kind == 32 and self.advertise_resources:
            if request[18:-2] != b"\x01\x00\x01":
                raise ValueError("Unknown fixture GET resource")
            body = struct.pack("<H", 3) + varint(self.scalar_value)
        elif kind == 34 and self.advertise_resources:
            if request[18:23] != b"\x01\x00\x00\x01\x00":
                raise ValueError("Unknown fixture SET resource")
            self.scalar_value = read_varint(request[23:-2])
            body = varint(3)
        elif kind == 48 and self.advertise_publisher:
            fields = read_varints(request[18:-2])
            if len(fields) != 4 or fields[0] != 1 or fields[2:] != [5, 0]:
                raise ValueError(f"Invalid publisher negotiation {fields}")
            if struct.unpack_from("<H", request, 14)[0] != 0:
                raise ValueError("Publisher negotiation used the wrong node")
            self.publish_requests.append(fields)
            body = varint(315) + varint(9) + varint(0)
        elif kind == 80 and self.advertise_publisher:
            payload = request[18:-2]
            if len(payload) < 10 or struct.unpack_from("<H", payload)[0] != 9:
                raise ValueError("Published stream header is malformed")
            self.published.append({"stream_id": 9, "timestamp_us": struct.unpack_from("<Q", payload, 2)[0],
                "sequence": struct.unpack_from("<I", request, 8)[0], "payload": payload[10:].hex()})
            return None  # Stream publication has transport completion, not an RPC reply.
        elif kind == 52 and self.advertise_publisher and read_varints(request[18:-2])[-1:] == [9]:
            fields = read_varints(request[18:-2])
            if len(fields) != 3 or fields[0] != 1:
                raise ValueError(f"Invalid publisher cancellation {fields}")
            self.canceled_publish.append(fields)
            if self.reject_publisher_cancel:
                prefix = b"\x01\x00\x01"
            body = b""
        elif kind == 50 and self.advertise_stream:
            body = varint(291) + varint(7) + varint(100)
        elif kind == 52 and self.advertise_stream:
            body = b""
        else:
            # Explicit protocol error for unsupported fixture operations.
            self.failures.append(f"Unsupported fixture request type {kind}")
            prefix, body = b"\x01\x00\x01", b""
        response = bytearray(request[:18])
        response[2] = kind + 1
        if kind == 2:
            struct.pack_into("<I", response, 4, self.session_id)
        response_body = prefix + body
        struct.pack_into("<H", response, 16, len(response_body))
        response.extend(response_body + b"\x00\x00")
        struct.pack_into("<H", response, len(response) - 2, self._checksum(response))
        return bytes(response)

    def _serve(self):
        while not self.stopped.is_set():
            try:
                request, peer = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            if peer[0] != "127.0.0.1":
                self.failures.append("A non-loopback peer reached the fixture")
                continue
            try:
                self.peer = peer
                reply = self._response(request)
                self.packets.append({"request": request.hex(), **({"reply": reply.hex()} if reply is not None else {})})
                if reply is not None:
                    self.socket.sendto(reply, peer)
            except Exception as exc:
                self.failures.append(f"{exc}; request={request.hex()}")


def probe(library_path=LIBRARY, advertise_resources=False, advertise_stream=False,
          advertise_fingertip=False, advertise_publisher=False):
    manager_lib = C.CDLL(str(BUILD / "libwuji_manager_bridge.dylib"))
    device_lib = C.CDLL(str(BUILD / "libwuji_device_bridge.dylib"))
    p, n, s, pp = C.c_void_p, C.c_size_t, C.c_char_p, C.POINTER(C.c_void_p)
    for name, signature in {
        "wuji_manager_open": [s, s, pp, p, n],
        "wuji_manager_connect": [p, s, s, s, s, pp, pp, p, n],
        "wuji_manager_disconnect": [p, s, p, n],
        "wuji_manager_disconnect_all": [p, p, n],
    }.items():
        fn = getattr(manager_lib, name)
        fn.argtypes, fn.restype = signature, C.c_int
    for name in ("wuji_manager_free_string", "wuji_manager_release_device"):
        fn = getattr(manager_lib, name)
        fn.argtypes, fn.restype = [p], None
    device_lib.wuji_device_is_connected.argtypes = [s, p, C.POINTER(C.c_int), p, n]
    device_lib.wuji_device_resources_json.argtypes = [s, p, C.c_int, p, n, C.POINTER(n), p, n]
    device_lib.wuji_device_info_json.argtypes = [s, p, p, n, C.POINTER(n), p, n]
    device_lib.wuji_device_nodes_json.argtypes = [s, p, p, n, C.POINTER(n), p, n]
    device_lib.wuji_device_request_raw.argtypes = [s, p, C.c_int, s, p, n, pp, C.POINTER(n), p, n]
    device_lib.wuji_device_subscribe.argtypes = [s, p, s, pp, p, n]
    device_lib.wuji_device_buffer_free.argtypes, device_lib.wuji_device_buffer_free.restype = [p], None
    device_lib.wuji_subscription_stream_id.argtypes = [p, C.POINTER(C.c_uint16)]
    device_lib.wuji_subscription_set_rate.argtypes = [p, C.c_uint16, C.POINTER(C.c_uint16), p, n]
    device_lib.wuji_subscription_close.argtypes, device_lib.wuji_subscription_close.restype = [p], None
    device_lib.wuji_subscription_try_recv.argtypes = [p, pp, C.POINTER(n), C.POINTER(C.c_uint64), p, n]
    device_lib.wuji_device_is_virtual.argtypes = [s, p, s, C.POINTER(C.c_int), p, n]
    device_lib.wuji_hand2_fingertip_info_json.argtypes = [s, p, C.c_uint8, pp, p, n]
    device_lib.wuji_device_publisher_open.argtypes = [s, p, s, pp, p, n]
    device_lib.wuji_publisher_stream_id.argtypes = [p, C.POINTER(C.c_uint16)]
    device_lib.wuji_publisher_send.argtypes = [p, p, n, p, n]
    device_lib.wuji_publisher_send_joint_commands.argtypes = [p, C.POINTER(C.c_float), n, p, n]
    device_lib.wuji_publisher_close.argtypes = [p, p, n]
    library_arg = os.fsencode(library_path)
    state = BUILD / "state"
    state.mkdir(parents=True, exist_ok=True)
    original_home = os.environ.get("HOME")
    manager, device, metadata = p(), p(), p()
    error = C.create_string_buffer(2048)
    with MockWujiUDP(library_path, advertise_resources=advertise_resources, advertise_stream=advertise_stream,
                     advertise_fingertip=advertise_fingertip, advertise_publisher=advertise_publisher) as mock:
        code = manager_lib.wuji_manager_open(library_arg, os.fsencode(state), C.byref(manager), error, len(error))
        assert code == 0, error.value
        code = manager_lib.wuji_manager_connect(manager, b"address", mock.address.encode(), b"loopback_fixture",
            b'{"timeout_ms":100,"retry_count":0,"enable_bridge":false,"auto_time_sync_interval_ms":null}',
            C.byref(device), C.byref(metadata), error, len(error))
        assert code == 0 and device and metadata, (code, error.value)
        info = json.loads(C.string_at(metadata))
        manager_lib.wuji_manager_free_string(metadata)
        assert info["device_name"] == "loopback_fixture" and info["device_type"] == "WujiHand2", info
        assert info["sn"] == mock.serial, info
        connected = C.c_int()
        assert device_lib.wuji_device_is_connected(library_arg, device, C.byref(connected), error, len(error)) == 0
        assert connected.value == 1
        output, needed = C.create_string_buffer(65536), n()
        code = device_lib.wuji_device_resources_json(library_arg, device, 1, output, len(output), C.byref(needed), error, len(error))
        assert code == 0, (code, error.value)
        resources = json.loads(output.value)
        code = device_lib.wuji_device_info_json(library_arg, device, output, len(output), C.byref(needed), error, len(error))
        assert code == 0, (code, error.value)
        device_info = json.loads(output.value)
        code = device_lib.wuji_device_nodes_json(library_arg, device, output, len(output), C.byref(needed), error, len(error))
        assert code == 0, (code, error.value)
        nodes = json.loads(output.value)
        if advertise_resources:
            assert {"path": "fixture_value", "schema_id": 3, "can_get": True,
                "can_set": True, "can_sub": False, "can_pub": False, "can_exec": False, "serde_format": 0} in resources, resources
            assert {"path": ""} in nodes, nodes
            result, length = p(), n()
            code = device_lib.wuji_device_request_raw(library_arg, device, 0, b"fixture_value", None, 0,
                C.byref(result), C.byref(length), error, len(error))
            assert code == 0, (code, error.value)
            try:
                assert C.string_at(result, length.value) == varint(42)
            finally:
                device_lib.wuji_device_buffer_free(result)
            payload_bytes = varint(123456)
            payload = C.create_string_buffer(payload_bytes)
            result, length = p(), n()
            code = device_lib.wuji_device_request_raw(library_arg, device, 1, b"fixture_value", payload, len(payload_bytes),
                C.byref(result), C.byref(length), error, len(error))
            assert code == 0 and mock.scalar_value == 123456, (code, error.value)
            device_lib.wuji_device_buffer_free(result)
            result, length = p(), n()
            code = device_lib.wuji_device_request_raw(library_arg, device, 0, b"fixture_value", None, 0,
                C.byref(result), C.byref(length), error, len(error))
            assert code == 0, (code, error.value)
            try:
                assert C.string_at(result, length.value) == payload_bytes
            finally:
                device_lib.wuji_device_buffer_free(result)
        if advertise_publisher:
            publisher, stream_id = p(), C.c_uint16()
            code = device_lib.wuji_device_publisher_open(library_arg, device, b"joint_command",
                C.byref(publisher), error, len(error))
            assert code == 0 and publisher, (code, error.value, mock.failures)
            assert device_lib.wuji_publisher_stream_id(publisher, C.byref(stream_id)) == 0
            assert stream_id.value == 9
            # The SDK's Device::publish reference-counts a shared stream: a
            # second publisher opens, and closing it leaves the first usable.
            duplicate = p()
            assert device_lib.wuji_device_publisher_open(library_arg, device, b"joint_command",
                C.byref(duplicate), error, len(error)) == 0 and duplicate, error.value
            assert device_lib.wuji_publisher_close(duplicate, error, len(error)) == 0, error.value
            floats = [float(i - 30) * 0.125 for i in range(60)]
            commands = (C.c_float * 60)(*floats)
            invalid = (C.c_float * 60)(*floats)
            invalid[5] = float("nan")
            assert device_lib.wuji_publisher_send_joint_commands(publisher, invalid, 60, error, len(error)) == -1
            for seq in range(2):
                code = device_lib.wuji_publisher_send_joint_commands(publisher, commands, 60, error, len(error))
                assert code == 0, (code, error.value, mock.failures)
            generic_payload = varint(300) + struct.pack("<60f", *floats)
            generic_buffer = C.create_string_buffer(generic_payload)
            assert device_lib.wuji_publisher_send(publisher, generic_buffer, len(generic_payload), error, len(error)) == 0, error.value
            assert device_lib.wuji_publisher_close(publisher, error, len(error)) == 0, error.value
            deadline = time.monotonic() + 1
            while len(mock.published) < 3 and time.monotonic() < deadline:
                time.sleep(0.002)
            assert len(mock.published) == 3, (mock.published, mock.failures)
            assert [item["payload"] for item in mock.published] == [
                (varint(seq) + struct.pack("<60f", *floats)).hex() for seq in (0, 1, 300)]
            assert all(item["timestamp_us"] > 0 for item in mock.published)
            assert mock.published[1]["sequence"] == mock.published[0]["sequence"] + 1
            wait(lambda: len(mock.canceled_publish) == 1)
            assert len(mock.canceled_publish) == 1, mock.canceled_publish
            # The SDK cancels in the background, so a device-rejected cancel is
            # logged natively and close still releases the handle; a new
            # publisher can then be created and closed independently.
            publisher = p()
            assert device_lib.wuji_device_publisher_open(library_arg, device, b"joint_command",
                C.byref(publisher), error, len(error)) == 0, error.value
            mock.reject_publisher_cancel = True
            assert device_lib.wuji_publisher_close(publisher, error, len(error)) == 0, error.value
            wait(lambda: len(mock.canceled_publish) == 2)
            mock.reject_publisher_cancel = False
            publisher = p()
            assert device_lib.wuji_device_publisher_open(library_arg, device, b"joint_command",
                C.byref(publisher), error, len(error)) == 0, error.value
            assert device_lib.wuji_publisher_close(publisher, error, len(error)) == 0, error.value
            wait(lambda: len(mock.canceled_publish) == 3)
            assert (len(mock.publish_requests), len(mock.canceled_publish)) == (4, 3), (
                mock.publish_requests, mock.canceled_publish)
        if advertise_fingertip:
            # SDK 2026.9.22 removed WujiHand2::get_fingertip_info; the bridge
            # reports that without sending any request.
            fingertip_json = p()
            code = device_lib.wuji_hand2_fingertip_info_json(library_arg, device, 2, C.byref(fingertip_json), error, len(error))
            assert code == 2 and not fingertip_json and b"removed get_fingertip_info" in error.value, (code, error.value)
            assert mock.fingertip_requests == [], mock.fingertip_requests
        if advertise_stream:
            subscription = p()
            code = device_lib.wuji_device_subscribe(library_arg, device, b"realtime_stream", C.byref(subscription), error, len(error))
            assert code == 0 and subscription, (code, error.value)
            stream_id = C.c_uint16()
            assert device_lib.wuji_subscription_stream_id(subscription, C.byref(stream_id)) == 0
            assert stream_id.value == 7
            result, length, lagged = p(), n(), C.c_uint64()
            assert device_lib.wuji_subscription_try_recv(subscription, C.byref(result), C.byref(length), C.byref(lagged), error, len(error)) == 1
            virtual = C.c_int()
            assert device_lib.wuji_device_is_virtual(library_arg, device, b"joint_states", C.byref(virtual), error, len(error)) == 0 and virtual.value == 1
            virtual_subscription = p()
            code = device_lib.wuji_device_subscribe(library_arg, device, b"joint_states", C.byref(virtual_subscription), error, len(error))
            assert code == 0 and virtual_subscription, (code, error.value)
            for rate_subscription in (subscription, virtual_subscription):
                actual_rate = C.c_uint16()
                code = device_lib.wuji_subscription_set_rate(rate_subscription, 50, C.byref(actual_rate), error, len(error))
                # The fixture intentionally advertises no rate-control
                # capability. Both real native routes must preserve that error.
                assert code == 3 and b"rate control" in error.value, (code, error.value)
            sample = REALTIME_SAMPLE
            mock.emit_stream(sample)
            deadline = time.monotonic() + 2
            while True:
                code = device_lib.wuji_subscription_try_recv(subscription, C.byref(result), C.byref(length), C.byref(lagged), error, len(error))
                if code != 1 or time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
            assert code == 0, (code, error.value)
            try:
                assert C.string_at(result, length.value) == sample
            finally:
                device_lib.wuji_device_buffer_free(result)
            deadline = time.monotonic() + 2
            while True:
                code = device_lib.wuji_subscription_try_recv(virtual_subscription, C.byref(result), C.byref(length), C.byref(lagged), error, len(error))
                if code != 1 or time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
            assert code == 0, (code, error.value)
            try:
                assert C.string_at(result, length.value) == JOINT_STATES_SAMPLE, C.string_at(result, length.value).hex()
            finally:
                device_lib.wuji_device_buffer_free(result)
            device_lib.wuji_subscription_close(virtual_subscription)
            device_lib.wuji_subscription_close(subscription)
        for operation in (0, 1, 2):
            result, length = p(), n()
            code = device_lib.wuji_device_request_raw(library_arg, device, operation, b"fixture_missing_resource", None, 0,
                C.byref(result), C.byref(length), error, len(error))
            assert code != 0 and not result, (code, error.value)
        subscription = p()
        code = device_lib.wuji_device_subscribe(library_arg, device, b"fixture_missing_resource", C.byref(subscription), error, len(error))
        assert code != 0 and not subscription, (code, error.value)
        assert manager_lib.wuji_manager_disconnect(manager, b"loopback_fixture", error, len(error)) == 0, error.value
        assert device_lib.wuji_device_is_connected(library_arg, device, C.byref(connected), error, len(error)) == 0
        assert connected.value == 0
        manager_lib.wuji_manager_release_device(device)
        # Idempotent release must not double-drop a Rust clone.
        manager_lib.wuji_manager_release_device(device)
        assert manager_lib.wuji_manager_disconnect_all(manager, error, len(error)) == 0
    assert os.environ.get("HOME") == original_home
    assert not mock.failures, mock.failures
    report = {"native_connect": "ok", "metadata": info, "device_info": device_info, "nodes": nodes, "resources": resources,
              "publisher_negotiate_send_cancel": "ok" if advertise_publisher else "not requested",
              "publisher_records": mock.published,
              "scalar_get_set_readback": "ok" if advertise_resources else "not requested",
              "stream_physical_and_virtual_frames": "ok" if advertise_stream else "not requested",
              "fingertip_info": "removed upstream (SDK 2026.9.22)" if advertise_fingertip else "not requested",
              "native_rate_capability_errors": 2 if advertise_stream else 0,
              "fingertip_selectors": mock.fingertip_requests,
              "native_missing_resource_errors": 4, "disconnect_and_drop": "ok",
              "process_home_unchanged": True, "packets": mock.packets,
              "fixture_only": True, "physical_hardware_tested": False}
    (BUILD / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report, "packets": len(mock.packets)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--library", type=Path, default=LIBRARY)
    parser.add_argument("--resources", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--fingertip", action="store_true")
    parser.add_argument("--publisher", action="store_true")
    args = parser.parse_args()
    if args.child:
        probe(args.library.resolve(), args.resources, args.stream, args.fingertip, args.publisher)
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    for name in ("manager", "device"):
        subprocess.run(["xcrun", "clang", "-arch", "arm64", "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib",
                        str(ROOT / f"native/{name}_bridge.c"), str(ROOT / f"native/{name}_bridge.S"),
                        str(ROOT / "native/rust_format.c"), "-o", str(BUILD / f"libwuji_{name}_bridge.dylib")], check=True)
    policy = ('(version 1) (allow default) (deny network*) '
              '(allow network-outbound (remote ip "localhost:*")) '
              '(allow network-inbound (local ip "localhost:*")) '
              '(deny iokit-open) (deny file-write*) (allow file-write* (subpath ' + json.dumps(str(ROOT)) + '))')
    subprocess.run(["/usr/bin/sandbox-exec", "-p", policy, sys.executable, str(Path(__file__).resolve()),
                    "--child", "--library", str(args.library.resolve())] + (["--resources"] if args.resources else []) + (["--stream"] if args.stream else []) + (["--fingertip"] if args.fingertip else []) + (["--publisher"] if args.publisher else []), check=True, timeout=30)


if __name__ == "__main__":
    main()
