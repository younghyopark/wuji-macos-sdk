#!/usr/bin/env python3
"""Compile and exercise pure SDK schemas/codecs with all network access denied."""
from __future__ import annotations
import argparse
import ctypes as C
import json
from pathlib import Path
import subprocess
import sys
import struct

ROOT = Path(__file__).resolve().parent.parent


def probe(library: Path, bridge_path: Path, output_path: Path) -> None:
    bridge = C.CDLL(str(bridge_path))
    schema = bridge.wuji_device_schema_json
    schema.argtypes = [C.c_char_p, C.c_uint16, C.c_void_p, C.c_size_t,
                       C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    schema.restype = C.c_int
    library_bytes = str(library).encode()
    stream_probe = bridge.wuji_device_offline_stream_probe
    stream_probe.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t]
    stream_probe.restype = C.c_int
    error = C.create_string_buffer(512)
    for _ in range(20):
        code = stream_probe(library_bytes, error, len(error))
        assert code == 0, (code, error.value)
    resource_probe = bridge.wuji_device_offline_resource_probe
    resource_probe.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    resource_probe.restype = C.c_int
    needed = C.c_size_t()
    assert resource_probe(library_bytes, None, 0, C.byref(needed)) == 1
    output = C.create_string_buffer(needed.value)
    assert resource_probe(library_bytes, output, len(output), C.byref(needed)) == 0
    assert json.loads(output.value) == {
        "path": 'hand2."quoted"\nparam', "schema_id": 0x118, "can_get": True,
        "can_set": False, "can_sub": True, "can_pub": False, "can_exec": True,
        "serde_format": 2}
    result = {}
    for schema_id in [0, *range(1, 22), *range(0x100, 0x156),
                      *range(0x211, 0x225), *range(0x8001, 0x8008),
                      0x8101, 0x8102, 0x9001, 0xffff]:
        error = C.create_string_buffer(512)
        needed = C.c_size_t()
        code = schema(library_bytes, schema_id, None, 0, C.byref(needed), error, 512)
        if code == 2:
            continue
        assert code == 1 and needed.value > 1, (schema_id, code, error.value)
        output = C.create_string_buffer(needed.value)
        code = schema(library_bytes, schema_id, output, len(output), C.byref(needed), error, 512)
        assert code == 0, (schema_id, code, error.value)
        result[str(schema_id)] = json.loads(output.value)
    assert result, "No native schemas recovered"
    assert "0" not in result and "65535" not in result
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    codec = bridge.wuji_device_codec
    codec.argtypes = [C.c_char_p, C.c_uint16, C.c_int, C.c_int,
                     C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t,
                     C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    codec.restype = C.c_int

    def convert(schema_id: int, payload: bytes, source_format: int, target_format: int) -> bytes:
        source = C.create_string_buffer(payload)
        output = C.create_string_buffer(65536)
        written = C.c_size_t()
        error = C.create_string_buffer(512)
        code = codec(library_bytes, schema_id, source_format, target_format, source,
                     len(payload), output, len(output), C.byref(written), error, 512)
        assert code == 0, (schema_id, code, error.value)
        return output.raw[:written.value]

    fixtures = []
    examples = [
        (0x118, {"joints": [{"kp": 10.0, "kd": 0.5}, {"kp": 20.0, "kd": 1.0}]}),
        (0x144, {"joints": [0.5, 1.25, -0.25]}),
        (0x145, {"joints": [0, 1, 2, 255]}),
    ]
    for schema_id, value in examples:
        encoded = convert(schema_id, json.dumps(value).encode(), 1, 0)
        decoded = json.loads(convert(schema_id, encoded, 0, 1))
        assert decoded == value, (value, decoded)
        fixtures.append({"schema_id": schema_id, "type": result[str(schema_id)]["title"],
                         "value": value, "postcard_hex": encoded.hex(), "native_roundtrip": True})
    # The native parser should reject missing fields and integer overflow.
    for schema_id, payload in [(0x118, b'{"joints":[{}]}'), (0x145, b'{"joints":[256]}')]:
        source = C.create_string_buffer(payload)
        output = C.create_string_buffer(1024)
        error = C.create_string_buffer(512)
        written = C.c_size_t()
        code = codec(library_bytes, schema_id, 1, 0, source, len(payload), output,
                     len(output), C.byref(written), error, 512)
        assert code == 3 and error.value, (schema_id, code, error.value)

    command_encoder = bridge.wuji_device_joint_command_encode
    command_encoder.argtypes = [C.c_char_p, C.c_uint32, C.POINTER(C.c_float), C.c_size_t,
        C.c_int, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    command_encoder.restype = C.c_int
    values = [float(joint - 10) * 0.25 + axis * 0.125 for joint in range(20) for axis in range(3)]
    commands = (C.c_float * 60)(*values)
    command_value = {"seq": 300, "joints": [dict(zip(["position", "velocity", "effort"],
        values[i:i+3])) for i in range(0, 60, 3)]}
    command_results = []
    for format in (0, 1):
        output = C.create_string_buffer(65536)
        error = C.create_string_buffer(512)
        written = C.c_size_t()
        code = command_encoder(library_bytes, 300, commands, 60, format, output,
                               len(output), C.byref(written), error, 512)
        assert code == 0, (code, error.value)
        command_results.append(output.raw[:written.value])
    assert json.loads(command_results[1]) == command_value
    assert command_results[0] == b'\xac\x02' + struct.pack('<60f', *values)
    fixtures.append({"schema_id": 0x13b, "type": "wuji.JointCommandFrame",
        "value": command_value, "postcard_hex": command_results[0].hex(),
        "native_json_and_postcard": True})
    state_fixture = bridge.wuji_device_joint_state_fixture
    state_fixture.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    state_fixture.restype = C.c_int
    output = C.create_string_buffer(1024)
    written = C.c_size_t()
    assert state_fixture(library_bytes, output, len(output), C.byref(written)) == 0
    state_value = {"header": {"seq": 300, "timestamp_us": 1234567890123, "frame_id": 'hand2"\nbase'},
                   "num_joints": 2, "joints": [
                       {"nid": 0, "position": 0.25, "velocity": -1.5, "effort": 2.75},
                       {"nid": 23, "position": -0.5, "velocity": 3.25, "effort": -4.5}]}
    fixtures.append({"schema_id": 0x14a, "type": "wuji.JointStateFrame", "value": state_value,
                     "postcard_hex": output.raw[:written.value].hex(),
                     "native_postcard_serializer": "image+0xa6a1d0"})
    native_decode = bridge.wuji_device_decode_fixture_json
    native_decode.argtypes = [C.c_char_p, C.c_uint16, C.c_void_p, C.c_size_t,
        C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    native_decode.restype = C.c_int

    def varint(value):
        result = bytearray()
        while value >= 128:
            result.append((value & 127) | 128)
            value >>= 7
        return bytes(result) + bytes([value])

    def string(value):
        encoded = value.encode()
        return varint(len(encoded)) + encoded

    header = varint(300) + varint(1234567890123) + string('hand2"\nbase')
    realtime_entries = [
        {"nid": 0, "status_word": 129, "pos_rad_jnt_fb": 0.25, "vel_rad_s_jnt_fb": -1.5,
         "effort": 2.75, "current": 0.5, "vbus_v_fb": 24.5, "mcu_temp_c_fb": 31.25,
         "error_code_current": 257},
        {"nid": 23, "status_word": 32769, "pos_rad_jnt_fb": -0.5, "vel_rad_s_jnt_fb": 3.25,
         "effort": -4.5, "current": 0.75, "vbus_v_fb": 25.25, "mcu_temp_c_fb": 30.5,
         "error_code_current": 513}]
    realtime_value = {"header": state_value["header"], "num_joints": 2, "joints": realtime_entries}
    realtime_bytes = header + bytes([2, 2])
    for entry in realtime_entries:
        realtime_bytes += bytes([entry["nid"]]) + varint(entry["status_word"])
        realtime_bytes += struct.pack("<6f", *(entry[key] for key in
            ["pos_rad_jnt_fb", "vel_rad_s_jnt_fb", "effort", "current", "vbus_v_fb", "mcu_temp_c_fb"]))
        realtime_bytes += varint(entry["error_code_current"])
    fingertip_value = {"header": state_value["header"], "digest": 12345, "model": 'Wuji "tip"',
                      "device_type": 345, "rate_hz": 123.5, "format": '{"kind":"test"}'}
    fingertip_bytes = header + varint(12345) + string(fingertip_value["model"]) + varint(345)
    fingertip_bytes += struct.pack("<f", 123.5) + string(fingertip_value["format"])
    for schema_id, value, payload in [(291, realtime_value, realtime_bytes), (334, fingertip_value, fingertip_bytes)]:
        output = C.create_string_buffer(8192)
        needed = C.c_size_t()
        code = native_decode(library_bytes, schema_id, payload, len(payload), output, len(output), C.byref(needed))
        assert code == 0, (schema_id, code)
        assert json.loads(output.value) == value, (schema_id, output.value)
        # Native parser, with disassembly-verified field extraction, is the
        # oracle here; this does not claim native serialization was retained.
        fixtures.append({"schema_id": schema_id, "type": result[str(schema_id)]["title"],
                         "value": value, "postcard_hex": payload.hex(),
                         "native_postcard_deserializer": "image+" + ("0xa9b3d0" if schema_id == 291 else "0xa9b37c")})
    fixture_path = output_path.parent / "native-codec-fixtures.json"
    fixture_path.write_text(json.dumps(fixtures, indent=2) + "\n")
    print(json.dumps({"native_schemas": len(result), "json_valid": True,
                      "native_codec_fixtures": len(fixtures), "artifact": str(output_path),
                      "codec_artifact": str(fixture_path),
        "native_stream_empty_lagged_frames_closed": True,
        "native_unit_send_completion_ownership": True,
                      "native_resource_layout_verified": True,
                      "network": "denied"}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path,
                        default=ROOT / "build/native-reuse/libwuji_cli_reuse.dylib")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    build = ROOT / "build/device-probe"
    bridge_path = build / "libwuji_device_bridge.dylib"
    schemas_path = build / "native-schemas.json"
    if args.child:
        probe(args.library.resolve(), bridge_path, schemas_path)
        return
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run(["xcrun", "clang", "-arch", "arm64", "-dynamiclib", "-std=c11",
                    "-Wall", "-Wextra", "-Werror", str(ROOT / "native/device_bridge.c"),
                    str(ROOT / "native/device_bridge.S"), str(ROOT / "native/rust_format.c"),
                    "-o", str(bridge_path)], check=True)
    subprocess.run(["sandbox-exec", "-p", "(version 1) (allow default) (deny network*)",
                    sys.executable, str(Path(__file__).resolve()), "--child", "--library",
                    str(args.library.resolve())], check=True)


if __name__ == "__main__":
    main()
