#!/usr/bin/env python3
"""Exercise native SDK Zenoh publishing against a loopback-only synthetic peer.

The optional fixture dependency is eclipse-zenoh==1.9.0. No WUJI hardware
library is imported by the peer and received publications only enter a list.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/zenoh-publisher-probe"
LIBRARY = ROOT / "build/native-reuse/libwuji_cli_reuse.dylib"


class MockWujiZenoh:
    serial = "MOCKZENOHAND2ABC"

    def __init__(self, *, serde_format="postcard"):
        self.serde_format = serde_format
        self.published = []
        self.queries = []
        self.failures = []
        self.transport_events = []
        self.changed = threading.Condition()
        self.session = None
        self.handles = []

    def key(self, path):
        return f"wuji/{self.serial}/{path}"

    def __enter__(self):
        import zenoh

        # The pinned SDK classifies port 7447 as a native Zenoh endpoint.
        # Every other interface and destination is denied by the runner.
        self.address = "127.0.0.1:7447"
        self.endpoint = f"tcp/{self.address}"
        self.original_protocol = os.environ.get("WUJI_ZENOH_PROTO")
        os.environ["WUJI_ZENOH_PROTO"] = "tcp"
        config = zenoh.Config()
        for key, value in {
            "mode": "peer", "listen/endpoints": [self.endpoint],
            "connect/endpoints": [], "scouting/multicast/enabled": False,
            "scouting/gossip/enabled": False, "transport/shared_memory/enabled": False,
        }.items():
            config.insert_json5(key, json.dumps(value))
        self.session = zenoh.open(config)
        self.handles.append(self.session.info.declare_transport_events_listener(
            lambda event: self.transport_events.append(str(event))))
        self.handles.append(self.session.liveliness().declare_token(self.key("@alive")))
        self.handles.append(self.session.declare_queryable(self.key("@capability"), self._query, complete=True))
        self.handles.append(self.session.declare_queryable(self.key("@status"), self._query, complete=True))
        self.handles.append(self.session.declare_subscriber(self.key("joint_command"), self._sample))
        return self

    def capability(self):
        return {
            "device_id": 2, "device_proto": "custom", "firmware_version": "1.0.0",
            "serial_number": self.serial, "nodes": [], "resources": [{
                "path": "joint_command", "schema_id": 315, "can_get": False,
                "can_set": False, "can_sub": False, "can_pub": True,
                "can_exec": False, "internal": False, "serde_format": self.serde_format,
                "json_schema": {"title": "JointCommandFrame", "type": "object"},
            }],
        }

    def _query(self, query):
        try:
            key = str(query.key_expr)
            self.queries.append({"key": key, "parameters": str(query.parameters),
                                 "payload": bytes(query.payload).hex() if query.payload else None})
            if key == self.key("@capability"):
                query.reply(key, json.dumps(self.capability()).encode())
            elif key == self.key("@status"):
                query.reply(key, b"online")
            else:
                query.reply_err(b"unsupported synthetic fixture query")
        except Exception as exc:
            self.failures.append(str(exc))

    def _sample(self, sample):
        with self.changed:
            self.published.append({"key": str(sample.key_expr), "payload": bytes(sample.payload).hex(),
                                   "encoding": str(sample.encoding)})
            self.changed.notify_all()

    def wait_samples(self, count, timeout=2.0):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.published) >= count, timeout)

    def __exit__(self, *_):
        for handle in reversed(self.handles):
            handle.undeclare()
        self.handles.clear()
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.original_protocol is None:
            os.environ.pop("WUJI_ZENOH_PROTO", None)
        else:
            os.environ["WUJI_ZENOH_PROTO"] = self.original_protocol


def probe(*, connect_only=False):
    (BUILD / "state").mkdir(parents=True, exist_ok=True)
    manager_lib = C.CDLL(str(BUILD / "libwuji_manager_bridge.dylib"))
    device_lib = C.CDLL(str(BUILD / "libwuji_device_bridge.dylib"))
    p, n, s, pp = C.c_void_p, C.c_size_t, C.c_char_p, C.POINTER(C.c_void_p)
    for name, signature in {
        "wuji_manager_open": [s, s, pp, p, n],
        "wuji_manager_connect": [p, s, s, s, s, pp, pp, p, n],
        "wuji_manager_disconnect_all": [p, p, n],
        "wuji_manager_release_device": [p],
        "wuji_manager_free_string": [p],
    }.items():
        getattr(manager_lib, name).argtypes = signature
    for name, signature in {
        "wuji_device_publisher_open": [s, p, s, pp, p, n],
        "wuji_publisher_send": [p, p, n, p, n],
        "wuji_publisher_send_joint_commands": [p, C.POINTER(C.c_float), n, p, n],
        "wuji_publisher_close": [p, p, n],
    }.items():
        getattr(device_lib, name).argtypes = signature
    manager, device, metadata = p(), p(), p()
    error = C.create_string_buffer(4096)
    original_home = os.environ.get("HOME")
    with MockWujiZenoh() as fixture:
        code = manager_lib.wuji_manager_open(os.fsencode(LIBRARY), os.fsencode(BUILD / "state"),
                                            C.byref(manager), error, len(error))
        assert code == 0, (code, error.value)
        try:
            code = manager_lib.wuji_manager_connect(manager, b"address", fixture.endpoint.encode(), b"zenoh_fixture",
                b'{"timeout_ms":1000,"retry_count":0,"enable_bridge":false,"auto_time_sync_interval_ms":null}',
                C.byref(device), C.byref(metadata), error, len(error))
            print(json.dumps({"connect_code": code, "error": error.value.decode(), "queries": fixture.queries,
                              "peers": [str(zid) for zid in fixture.session.info.peers_zid()],
                              "transports": fixture.transport_events}), flush=True)
            assert code == 0 and device and metadata, (code, error.value)
            info = json.loads(C.string_at(metadata))
            manager_lib.wuji_manager_free_string(metadata)
            print(json.dumps({"metadata": info}), flush=True)
            if not connect_only:
                publisher = p()
                code = device_lib.wuji_device_publisher_open(os.fsencode(LIBRARY), device, b"joint_command",
                                                            C.byref(publisher), error, len(error))
                assert code == 0 and publisher, (code, error.value)
                try:
                    payload = b"\x00\x01\x80\xff"  # inert bytes to fixture only
                    code = device_lib.wuji_publisher_send(publisher, payload, len(payload), error, len(error))
                    assert code == 0, (code, error.value)
                    assert fixture.wait_samples(1), fixture.published
                    assert bytes.fromhex(fixture.published[0]["payload"]) == payload
                finally:
                    code = device_lib.wuji_publisher_close(publisher, error, len(error))
                    assert code == 0, (code, error.value)
        finally:
            code = manager_lib.wuji_manager_disconnect_all(manager, error, len(error))
            assert code == 0, (code, error.value)
            if device:
                manager_lib.wuji_manager_release_device(device)
        report = {"queries": fixture.queries, "published": fixture.published, "failures": fixture.failures,
                  "physical_hardware": False, "process_home_unchanged": os.environ.get("HOME") == original_home}
        (BUILD / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)


def build_fixture_bridge():
    """Compile only the test peer's native connection entry point."""
    BUILD.mkdir(parents=True, exist_ok=True)
    output = BUILD / "libwuji_manager_bridge.dylib"
    subprocess.run(["xcrun", "clang", "-arch", "arm64", "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib",
                    str(ROOT / "native/zenoh_fixture_bridge.c"), str(ROOT / "native/manager_bridge.S"),
                    str(ROOT / "native/rust_format.c"), "-o", str(output)], check=True)
    return output


def fixture_backend():
    """Keep installed Python/native publishing; select only the synthetic peer.

    The extra connection entry point supplies the endpoint and serial directly
    to the real SDK. It avoids multicast discovery, which is denied by the test
    sandbox. Resource lookup, codec, session, PUT and device handles stay native.
    """
    from wuji_sdk._backend import NativeBackend
    backend = NativeBackend()
    backend.manager_lib = C.CDLL(str(BUILD / "libwuji_manager_bridge.dylib"))
    backend._signatures()
    return backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--connect-only", action="store_true")
    args = parser.parse_args()
    if args.child:
        probe(connect_only=args.connect_only)
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    build_fixture_bridge()
    for name in ("device",):
        subprocess.run(["xcrun", "clang", "-arch", "arm64", "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib",
                        str(ROOT / f"native/{name}_bridge.c"), str(ROOT / f"native/{name}_bridge.S"),
                        str(ROOT / "native/rust_format.c"), "-o", str(BUILD / f"libwuji_{name}_bridge.dylib")], check=True)
    policy = ('(version 1) (allow default) (deny network*) '
              '(allow network-outbound (remote ip "localhost:*")) '
              '(allow network-inbound (local ip "localhost:*")) '
              '(allow network-bind (local udp "*:7446")) '
              '(deny iokit-open) (deny file-write*) (allow file-write* (subpath ' + json.dumps(str(ROOT)) + '))')
    subprocess.run(["/usr/bin/sandbox-exec", "-p", policy, sys.executable, str(Path(__file__).resolve()), "--child"]
                   + (["--connect-only"] if args.connect_only else []), check=True, timeout=35)


if __name__ == "__main__":
    main()
