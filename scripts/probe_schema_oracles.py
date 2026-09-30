#!/usr/bin/env python3
"""Compare telemetry payloads with actual pinned native code, network denied.

This build-only probe borrows native memory for retained serializers, and reads
all meaningful fields from a fixed-layout native parser result. It does not
initialize a manager, discover devices, or transmit anything.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class RustVec(C.Structure):
    _fields_ = [("capacity", C.c_uint64), ("data", C.c_void_p), ("length", C.c_uint64)]


class Header(C.Structure):
    _fields_ = [("frame_id", RustVec), ("timestamp_us", C.c_uint64), ("seq", C.c_uint32)]


class Imu(C.Structure):
    _fields_ = [("header", Header), ("orientation", C.c_double * 4),
                ("orientation_covariance", C.c_double * 9),
                ("angular_velocity", C.c_double * 3),
                ("angular_velocity_covariance", C.c_double * 9),
                ("linear_acceleration", C.c_double * 3),
                ("linear_acceleration_covariance", C.c_double * 9)]


class JointDiagnostic(C.Structure):
    _fields_ = [("status_word", C.c_uint32), ("current", C.c_float),
                ("vbus_v_fb", C.c_float), ("mcu_temp_c_fb", C.c_float),
                ("comm_timeout_total", C.c_uint32), ("error_code_current", C.c_uint16),
                ("nid", C.c_uint8), ("comm_response_rate_pct", C.c_uint8)]


class CommSummary(C.Structure):
    _fields_ = [("e2e_received", C.c_uint32), ("e2e_lost", C.c_uint32),
                ("rpc_total", C.c_uint32), ("tactile_timeout_total", C.c_uint32 * 5),
                ("sdk_dropped", C.c_uint32), ("age_ms", C.c_uint16),
                ("e2e_reordered", C.c_uint16), ("e2e_duplicates", C.c_uint16),
                ("e2e_window_loss_x100", C.c_uint16), ("rpc_retries", C.c_uint16),
                ("rpc_timeouts", C.c_uint16), ("comm_get_failures", C.c_uint16),
                ("tactile_online_mask", C.c_uint8), ("tactile_response_rate_pct", C.c_uint8 * 5)]


class Diagnostics(C.Structure):
    _fields_ = [("header", Header), ("joints", RustVec), ("comm", CommSummary),
                ("num_joints", C.c_uint8)]


HEADER = {"seq": 300, "timestamp_us": 1234567890123, "frame_id": 'hand2"\nbase'}


def serialize_samples():
    """Native offsets are independent of Python/Postcard declaration order."""
    assert C.sizeof(Header) == 40 and C.sizeof(Imu) == 336
    assert Imu.orientation.offset == 40 and Imu.linear_acceleration_covariance.offset == 264
    assert C.sizeof(JointDiagnostic) == 24 and C.sizeof(CommSummary) == 56
    assert C.sizeof(Diagnostics) == 128 and Diagnostics.num_joints.offset == 120
    text = C.create_string_buffer(HEADER["frame_id"].encode())
    header = Header(RustVec(len(text.value), C.addressof(text), len(text.value)), HEADER["timestamp_us"], HEADER["seq"])
    orientation = dict(zip(("x", "y", "z", "w"), [0.125, -0.25, 0.5, -1.0]))
    angular = dict(zip(("x", "y", "z"), [1.25, -2.5, 3.75]))
    linear = dict(zip(("x", "y", "z"), [-4.25, 5.5, -6.75]))
    covariances = [[(k + 1) * (-1 if k % 2 else 1) / (2 ** j) for k in range(9)] for j in (2, 3, 4)]
    imu = Imu(header, (C.c_double * 4)(*orientation.values()), (C.c_double * 9)(*covariances[0]),
              (C.c_double * 3)(*angular.values()), (C.c_double * 9)(*covariances[1]),
              (C.c_double * 3)(*linear.values()), (C.c_double * 9)(*covariances[2]))
    imu_value = {"header": HEADER, "orientation": orientation, "orientation_covariance": covariances[0],
                 "angular_velocity": angular, "angular_velocity_covariance": covariances[1],
                 "linear_acceleration": linear, "linear_acceleration_covariance": covariances[2]}
    joints = [{"nid": 0, "status_word": 0x12345678, "current": -0.25, "vbus_v_fb": 24.5,
               "mcu_temp_c_fb": 37.75, "error_code_current": 300, "comm_response_rate_pct": 97,
               "comm_timeout_total": 123456},
              {"nid": 23, "status_word": 0x87654321, "current": 1.5, "vbus_v_fb": 23.25,
               "mcu_temp_c_fb": -12.5, "error_code_current": 65535, "comm_response_rate_pct": 83,
               "comm_timeout_total": 654321}]
    native_joints = (JointDiagnostic * 2)(*(JointDiagnostic(**j) for j in joints))
    comm = {"age_ms": 300, "tactile_online_mask": 21, "e2e_received": 123456,
            "e2e_lost": 1234, "e2e_reordered": 17, "e2e_duplicates": 19,
            "e2e_window_loss_x100": 321, "rpc_total": 654321, "rpc_retries": 23,
            "rpc_timeouts": 29, "comm_get_failures": 31, "tactile_response_rate_pct": [91, 92, 93, 94, 95],
            "tactile_timeout_total": [128, 257, 513, 1025, 2049], "sdk_dropped": 65537}
    native_comm = CommSummary(**{k: ((C.c_uint32 * 5)(*v) if k == "tactile_timeout_total" else
                                    (C.c_uint8 * 5)(*v) if k == "tactile_response_rate_pct" else v)
                                 for k, v in comm.items()})
    diagnostics = Diagnostics(header, RustVec(2, C.addressof(native_joints), 2), native_comm, 2)
    # Keep all pointer owners alive through generator suspension.
    yield 261, "ImuData", imu, imu_value, "0xafaa10"
    yield 332, "JointDiagnosticsFrame", diagnostics, {"header": HEADER, "num_joints": 2, "joints": joints, "comm": comm}, "0xa6a390"


def communication_sample():
    fingers = []
    for finger in range(5):
        n = 1000 * (finger + 1)
        item = dict(zip(("tx_frame_total", "rx_frame_total", "tx_byte_total", "rx_byte_total", "crc_error_total",
                         "frame_format_error_total", "uart_hw_error_total"), range(n + 1, n + 8)))
        item.update(tx_frame_per_sec=n + 8, rx_frame_per_sec=n + 9, tx_kbps=n + 10,
                    rx_kbps=n + 11, error_per_sec=n + 12)
        item["transfer_stats"] = [{"tx_total": n + 20 + j, "timeout_total": n + 30 + j,
                                    "tx_per_sec": n + 40 + j, "timeout_rate_pct": 10 + j} for j in range(5)]
        item["nodes"] = [{"node_type": j, "online": (finger + j) % 2,
                            "ms_since_last_response": n + 50 + j, "request_total": n + 60 + j,
                            "response_ok_total": n + 70 + j, "timeout_total": n + 80 + j,
                            "response_rate_pct": 80 + j, "request_per_sec": n + 90 + j} for j in range(5)]
        fingers.append(item)
    return {"fingers": fingers}


def read_communication_native(memory):
    """Field offsets from native parser return stores, ignoring padding bytes."""
    def field(fmt, offset):
        return struct.unpack_from("<" + fmt, memory, offset)[0]
    fingers = []
    for f in range(5):
        base = f * 224
        result = {name: field("I", base + offset) for name, offset in {
            "tx_frame_total": 120, "rx_frame_total": 124, "tx_byte_total": 128,
            "rx_byte_total": 132, "crc_error_total": 136, "frame_format_error_total": 140,
            "uart_hw_error_total": 144, "tx_kbps": 148, "rx_kbps": 152}.items()}
        result.update({name: field("H", base + offset) for name, offset in {
            "tx_frame_per_sec": 216, "rx_frame_per_sec": 218, "error_per_sec": 220}.items()})
        result["transfer_stats"] = [{name: field(fmt, base + 156 + j * 12 + offset)
            for name, fmt, offset in [("tx_total", "I", 0), ("timeout_total", "I", 4),
                                      ("tx_per_sec", "H", 8), ("timeout_rate_pct", "B", 10)]} for j in range(5)]
        result["nodes"] = [{name: field(fmt, base + j * 24 + offset) for name, fmt, offset in [
            ("ms_since_last_response", "I", 0), ("request_total", "I", 4),
            ("response_ok_total", "I", 8), ("timeout_total", "I", 12),
            ("request_per_sec", "H", 16), ("node_type", "B", 18),
            ("online", "B", 19), ("response_rate_pct", "B", 20)]} for j in range(5)]
        fingers.append(result)
    return {"fingers": fingers}


def probe(library, bridge_path, output_path):
    from wuji_sdk import _codec
    bridge = C.CDLL(str(bridge_path))
    serial = bridge.wuji_schema_oracle_serialize
    serial.argtypes = [C.c_char_p, C.c_uint16, C.c_void_p, C.c_size_t, C.c_void_p,
                       C.c_size_t, C.POINTER(C.c_size_t)]
    serial.restype = C.c_int
    library_bytes = str(library).encode()
    fixtures = []
    for schema, name, memory, value, offset in serialize_samples():
        output, written = C.create_string_buffer(8192), C.c_size_t()
        result = serial(library_bytes, schema, C.byref(memory), C.sizeof(memory), output, len(output), C.byref(written))
        assert result == 0, (schema, result)
        payload = output.raw[:written.value]
        assert _codec.encode(schema, value) == payload, (schema, payload.hex(), _codec.encode(schema, value).hex())
        assert _codec.decode(schema, payload) == value, (schema, _codec.decode(schema, payload), value)
        fixtures.append({"schema_id": schema, "type": "wuji." + name, "value": value,
                         "postcard_hex": payload.hex(), "native_serializer": offset,
                         "native_roundtrip": False, "oracle_source": "scripts/probe_schema_oracles.py"})
    decode = bridge.wuji_schema_oracle_comm_decode
    decode.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t]
    decode.restype = C.c_int
    value = communication_sample()
    payload = _codec.encode(282, value)
    output, source = C.create_string_buffer(1120), C.create_string_buffer(payload)
    assert decode(library_bytes, source, len(payload), output, len(output)) == 0
    decoded = read_communication_native(output.raw)
    assert decoded == value, (decoded, value)
    # Native parsing rejects truncated fixed arrays; do not infer this from the
    # Python decoder's behavior alone.
    for length in (0, 1, len(payload) - 1):
        assert decode(library_bytes, source, length, output, len(output)) == 3
    fixtures.append({"schema_id": 282, "type": "wuji.HandCommunicationDiagnostics", "value": value,
                     "postcard_hex": payload.hex(), "native_parser": "0xa9b420",
                     "native_parser_field_oracle": True, "native_roundtrip": False,
                     "oracle_source": "scripts/probe_schema_oracles.py"})
    output_path.write_text(json.dumps(fixtures, indent=2) + "\n")
    print(json.dumps({"fixtures": len(fixtures), "schemas": [x["schema_id"] for x in fixtures],
                      "output": str(output_path), "network_operations_requested": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=ROOT / "build/native-reuse/libwuji_cli_reuse.dylib")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--installed", action="store_true", help="Use only the installed Python package, without adding src to sys.path")
    args = parser.parse_args()
    build = ROOT / "build/schema-oracles"
    bridge = build / "libwuji_schema_oracle.dylib"
    if args.child:
        if not args.installed:
            sys.path.insert(0, str(ROOT / "src"))
        probe(args.library.resolve(), bridge, build / "native-codec-fixtures.json")
        return
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run(["xcrun", "clang", "-arch", "arm64", "-dynamiclib", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "native/schema_oracle.c"), str(ROOT / "native/device_bridge.S"), "-o", str(bridge)], check=True)
    subprocess.run(["sandbox-exec", "-p", "(version 1) (allow default) (deny network*)", sys.executable,
                    *(["-I"] if args.installed else []), str(Path(__file__).resolve()), "--child",
                    "--library", str(args.library.resolve()), *(["--installed"] if args.installed else [])], check=True)


if __name__ == "__main__":
    main()
