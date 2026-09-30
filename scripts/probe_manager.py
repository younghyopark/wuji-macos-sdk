#!/usr/bin/env python3
"""Build and probe the native manager inside a no-network/write-contained sandbox."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/manager-probe"


def exercise_users(bridge, manager, state):
    """Exercise real registry operations using only users created by this probe."""
    pointer = ctypes.POINTER(ctypes.c_void_p)
    tail = [pointer, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_create_user_json.argtypes = [ctypes.c_void_p] + [ctypes.c_char_p] * 3 + tail
    bridge.wuji_manager_update_user_json.argtypes = [ctypes.c_void_p] + [ctypes.c_char_p] * 4 + [ctypes.c_uint] + tail
    bridge.wuji_manager_delete_user.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_tactile_paths_json.argtypes = [ctypes.c_void_p] + [ctypes.c_char_p] * 3 + tail
    for name in ["create_user_json", "update_user_json", "delete_user", "tactile_paths_json"]:
        getattr(bridge, "wuji_manager_" + name).restype = ctypes.c_int

    def call_json(operation, *args, expected=0):
        result = ctypes.c_void_p()
        error = ctypes.create_string_buffer(4096)
        code = getattr(bridge, "wuji_manager_" + operation)(manager, *args, ctypes.byref(result), error, len(error))
        try:
            value = json.loads(ctypes.string_at(result)) if result.value else None
            print(json.dumps({"user_operation": operation, "code": code, "value": value, "error": error.value.decode()}), flush=True)
            assert code == expected and (value is not None if expected == 0 else value is None)
            return value
        finally:
            if result.value:
                bridge.wuji_manager_free_string(result)

    tag = uuid.uuid4().hex
    name = ("Offline probe " + tag).encode()
    external = ("external-" + tag).encode()
    user_id = None
    try:
        created = call_json("create_user_json", name, b'quoted "note"\nline', external)
        user_id = created["user_id"].encode()
        assert created["display_name"] == name.decode() and created["description"] == 'quoted "note"\nline'
        assert created["external_id"] == external.decode() and not created["is_default"]
        call_json("create_user_json", name, None, None, expected=1)
        call_json("create_user_json", b"   ", None, None, expected=1)
        changed = call_json("update_user_json", user_id, name + b" renamed", b"updated note", external + b"-updated", 0)
        assert changed["display_name"] == name.decode() + " renamed"
        assert changed["description"] == "updated note" and changed["external_id"] == external.decode() + "-updated"
        unchanged = call_json("update_user_json", user_id, None, None, None, 0)
        assert unchanged["description"] == "updated note" and unchanged["external_id"] == external.decode() + "-updated"
        cleared = call_json("update_user_json", user_id, None, None, None, 3)
        assert cleared.get("description") is None and cleared.get("external_id") is None
        call_json("update_user_json", user_id, None, b"invalid", None, 1, expected=-1)
        call_json("update_user_json", b"nonexistent-offline-user", None, None, None, 0, expected=1)
        for kind in [b"tactile_dir", b"tactile_run_dir", b"tactile_model_paths"]:
            paths = call_json("tactile_paths_json", kind, user_id, b"MOCKHAND2ABCDEF0")
            for path in paths.values() if isinstance(paths, dict) else [paths]:
                assert Path(path).is_relative_to(state)
            call_json("tactile_paths_json", kind, user_id, b"../escape", expected=1)
        call_json("switch_user_json", user_id)
        error = ctypes.create_string_buffer(4096)
        code = bridge.wuji_manager_delete_user(manager, user_id, error, len(error))
        print(json.dumps({"user_operation": "delete_current_user", "code": code, "error": error.value.decode()}), flush=True)
        assert code == 0
        user_id = None
        assert call_json("current_user_json")["is_default"]
        code = bridge.wuji_manager_delete_user(manager, b"", error, len(error))
        assert code == 1 and error.value
        print(json.dumps({"user_operation": "delete_default", "code": code, "error": error.value.decode()}), flush=True)
    finally:
        if user_id is not None:
            error = ctypes.create_string_buffer(4096)
            code = bridge.wuji_manager_delete_user(manager, user_id, error, len(error))
            print(json.dumps({"user_cleanup_code": code, "error": error.value.decode()}), flush=True)


def child(scan, connect, users=False):
    state = BUILD / "state"
    state.mkdir(parents=True, exist_ok=True)
    original_home = os.environ.get("HOME")
    bridge = ctypes.CDLL(str(BUILD / "libwuji_manager_bridge.dylib"))
    library_path = str(ROOT / "build/native-reuse/libwuji_cli_reuse.dylib").encode()
    bridge.wuji_set_log_level.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_set_log_level.restype = ctypes.c_int
    log_error = ctypes.create_string_buffer(1024)
    # Filters are configured before any manager or state initialization.
    sdk = ctypes.CDLL(os.fsdecode(library_path))
    log_filter = ctypes.c_size_t.in_dll(sdk, "_RNvCs6LChmAdslVW_3log20MAX_LOG_LEVEL_FILTER")
    tracing_filter = ctypes.c_size_t.in_dll(sdk, "_RNvNtCs9mYr3JH1WmH_12tracing_core8metadata9MAX_LEVEL")
    for level, value in [(b"off", 0), (b"error", 1), (b"warn", 2), (b"warning", 2), (b"info", 3), (b"DEBUG", 4), (b"trace", 5)]:
        assert bridge.wuji_set_log_level(library_path, level, log_error, len(log_error)) == 0
        assert log_filter.value == value and tracing_filter.value == 5 - value
    assert bridge.wuji_set_log_level(library_path, b"invalid", log_error, len(log_error)) == -1
    assert log_filter.value == 5 and tracing_filter.value == 0
    assert bridge.wuji_set_log_level(library_path, b"info", log_error, len(log_error)) == 0
    print(json.dumps({"native_log_filter_checks": 8, "log_filter": log_filter.value, "tracing_filter": tracing_filter.value}), flush=True)
    bridge.wuji_manager_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_open.restype = ctypes.c_int
    bridge.wuji_manager_probe_scan.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_probe_scan.restype = ctypes.c_int
    bridge.wuji_manager_scan_json.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_scan_json.restype = ctypes.c_int
    bridge.wuji_manager_probe_current_user.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_probe_current_user.restype = ctypes.c_int
    bridge.wuji_manager_free_string.argtypes = [ctypes.c_void_p]
    bridge.wuji_manager_probe_connect.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_probe_connect.restype = ctypes.c_int
    bridge.wuji_manager_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_connect.restype = ctypes.c_int
    bridge.wuji_manager_disconnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_disconnect.restype = ctypes.c_int
    bridge.wuji_manager_disconnect_all.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_disconnect_all.restype = ctypes.c_int
    bridge.wuji_manager_probe_switch_default.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_probe_switch_default.restype = ctypes.c_int
    for name in ["wuji_manager_current_user_json", "wuji_manager_list_users_json"]:
        getattr(bridge, name).argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
        getattr(bridge, name).restype = ctypes.c_int
    bridge.wuji_manager_switch_user_json.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_size_t]
    bridge.wuji_manager_switch_user_json.restype = ctypes.c_int
    manager = ctypes.c_void_p()
    error = ctypes.create_string_buffer(1024)
    code = bridge.wuji_manager_open(str(ROOT / "build/native-reuse/libwuji_cli_reuse.dylib").encode(), str(state).encode(), ctypes.byref(manager), error, len(error))
    report = {"open_code": code, "manager_present": bool(manager.value), "error": error.value.decode(), "process_home_unchanged": os.environ.get("HOME") == original_home}
    print(json.dumps(report), flush=True)
    if code != 0:
        return 1
    again = ctypes.c_void_p()
    second = bridge.wuji_manager_open(str(ROOT / "build/native-reuse/libwuji_cli_reuse.dylib").encode(), str(state).encode(), ctypes.byref(again), error, len(error))
    assert second == 0 and again.value == manager.value
    print(json.dumps({"repeat_open_code": second, "same_handle": again.value == manager.value}), flush=True)
    user = ctypes.c_void_p()
    user_code = bridge.wuji_manager_probe_current_user(manager, ctypes.byref(user), error, len(error))
    assert user_code == 0 and user
    print(json.dumps({"current_user_code": user_code, "current_user": ctypes.string_at(user).decode() if user else None, "error": error.value.decode()}), flush=True)
    if user:
        bridge.wuji_manager_free_string(user)
    switched = ctypes.create_string_buffer(128)
    switch_code = bridge.wuji_manager_probe_switch_default(manager, switched, len(switched))
    assert switch_code == 0
    print(json.dumps({"switch_default_code": switch_code, "raw": switched.raw.hex()}), flush=True)
    for name in ["wuji_manager_current_user_json", "wuji_manager_list_users_json"]:
        data = ctypes.c_void_p()
        code = getattr(bridge, name)(manager, ctypes.byref(data), error, len(error))
        assert code == 0 and data
        parsed = json.loads(ctypes.string_at(data))
        assert (isinstance(parsed, list) and parsed and parsed[0]["is_default"]) if "list" in name else parsed["is_default"]
        print(json.dumps({"user_operation": name, "code": code, "data": json.loads(ctypes.string_at(data)) if data else None, "error": error.value.decode()}), flush=True)
        if data: bridge.wuji_manager_free_string(data)
    for user_id in [b"", b"nonexistent-offline-user"]:
        data = ctypes.c_void_p()
        code = bridge.wuji_manager_switch_user_json(manager, user_id, ctypes.byref(data), error, len(error))
        assert (code == 0 and data) if not user_id else (code == 1 and not data and b"not found" in error.value)
        print(json.dumps({"switch_user": user_id.decode(), "code": code, "data": json.loads(ctypes.string_at(data)) if data else None, "error": error.value.decode()}), flush=True)
        if data: bridge.wuji_manager_free_string(data)
    if users:
        exercise_users(bridge, manager, state)
    if scan:
        result = ctypes.create_string_buffer(64)
        scan_code = bridge.wuji_manager_probe_scan(manager, result, len(result), error, len(error))
        assert scan_code == 0
        print(json.dumps({"scan_code": scan_code, "raw": result.raw.hex(), "error": error.value.decode()}), flush=True)
        scan_json = ctypes.c_void_p()
        code = bridge.wuji_manager_scan_json(manager, ctypes.byref(scan_json), error, len(error))
        assert code == 0 and json.loads(ctypes.string_at(scan_json)) == []
        print(json.dumps({"scan_json_code": code, "devices": json.loads(ctypes.string_at(scan_json)) if scan_json else None, "error": error.value.decode()}), flush=True)
        if scan_json:
            bridge.wuji_manager_free_string(scan_json)
    if connect:
        for variant, target, name in [(0, b"no-such-wuji-device-offline-test", b""), (0, b"no-such-wuji-device-offline-test", b"hand"), (1, b"invalid-address", b"hand"), (1, b"192.0.2.1", b"hand"), (1, b"192.0.2.1:7447", b"hand")]:
            result = ctypes.create_string_buffer(64)
            code = bridge.wuji_manager_probe_connect(manager, variant, target, name, result, len(result), error, len(error))
            assert code == 1 and error.value
            print(json.dumps({"connect_code": code, "variant": variant, "name": name.decode(), "target": target.decode(), "raw": result.raw.hex(), "error": error.value.decode()}), flush=True)
        for kind, target, options in [(b"sn", b"no-such-wuji-device-offline-test", None), (b"address", b"192.0.2.1:7447", b'{"timeout_ms":100,"retry_count":0,"enable_bridge":false,"auto_time_sync_interval_ms":null}'), (b"address", b"192.0.2.1:7447", b'{"auto_time_sync_interval_ms":1}'), (b"handedness", b"Left", b'{}'), (b"sn", b"never-send", b'{"unknown":4}')]:
            device = ctypes.c_void_p()
            metadata = ctypes.c_void_p()
            code = bridge.wuji_manager_connect(manager, kind, target, b"offline_hand", options, ctypes.byref(device), ctypes.byref(metadata), error, len(error))
            print(json.dumps({"public_connect_code": code, "kind": kind.decode(), "target": target.decode(), "options": options.decode() if options else None, "device_present": bool(device.value), "error": error.value.decode()}), flush=True)
            assert code != 0 and not device.value and not metadata.value
        code = bridge.wuji_manager_disconnect(manager, b"missing-offline-device", error, len(error))
        assert code == 0
        print(json.dumps({"disconnect_missing_code": code, "error": error.value.decode()}), flush=True)
        code = bridge.wuji_manager_disconnect_all(manager, error, len(error))
        assert code == 0
        print(json.dumps({"disconnect_all_code": code, "error": error.value.decode()}), flush=True)
    assert os.environ.get("HOME") == original_home
    assert (state / ".wuji/sdk/users/registry.toml").is_file()
    print(json.dumps({"state_files": [str(p.relative_to(state)) for p in sorted(state.rglob("*"))], "process_home_unchanged": os.environ.get("HOME") == original_home}), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--scan", action="store_true", help="Attempt discovery only inside the sandbox denying network and IOKit access")
    parser.add_argument("--connect-errors", action="store_true", help="Exercise invalid/unreachable targets with network and IOKit denied")
    parser.add_argument("--users", action="store_true", help="Exercise native user CRUD and tactile path resolution inside repo-contained state")
    args = parser.parse_args()
    if args.child:
        return child(args.scan, args.connect_errors, args.users)
    BUILD.mkdir(parents=True, exist_ok=True)
    subprocess.run(["xcrun", "clang", "-arch", "arm64", "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib", "native/manager_bridge.c", "native/manager_bridge.S", "native/rust_format.c", "-o", str(BUILD / "libwuji_manager_bridge.dylib")], cwd=ROOT, check=True)
    policy = '(version 1) (allow default) (deny network*) (deny iokit-open) (deny file-write*) (allow file-write* (subpath ' + json.dumps(str(ROOT)) + '))'
    command = ["/usr/bin/sandbox-exec", "-p", policy, sys.executable, str(Path(__file__).resolve()), "--child"]
    if args.scan:
        command.append("--scan")
    if args.connect_errors:
        command.append("--connect-errors")
    if args.users:
        command.append("--users")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45)
    (BUILD / "probe.stdout").write_text(result.stdout)
    (BUILD / "probe.stderr").write_text(result.stderr)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    print(f"probe process exit: {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
