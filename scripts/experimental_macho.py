#!/usr/bin/env python3
"""Experimental, version-pinned CLI-to-dylib research; never operates a device.

This is binary repackaging, not a source SDK build or a supported SDK ABI.
Only writes the requested copy. Keep the official executable untouched.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct
import subprocess
import sys

EXPECTED_SHA256 = "eac56966c0985de4c916083c3afcdb806bd94141f983eaae55dead82726a06f4"  # CLI 2026.9.22
LC_SEGMENT_64 = 0x19
LC_ID_DYLIB = 0xD
LC_LOAD_DYLINKER = 0xE
LC_MAIN = 0x80000028
MH_PIE = 0x200000

PROBE_SOURCE = r"""
import ctypes as C
import json
import sys

bridge = C.CDLL(sys.argv[2])
roundtrip = bridge.wuji_header_roundtrip
roundtrip.argtypes = [C.c_char_p, C.c_void_p, C.c_size_t,
                      C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t]
roundtrip.restype = C.c_int
library_path = sys.argv[1].encode()

# These 18 bytes are inert test data, never a transmitted device packet.
wire = bytes.fromhex('5710123478563412efcdab90112266558877')
error_cases = [(b'', 1), (b'\x00' * 18, 2), (b'W\x20' + b'\x00' * 16, 3)]
for payload, expected_code in [(wire, 0), *error_cases]:
    source = C.create_string_buffer(payload)
    output = C.create_string_buffer(b'Z' * 32)
    error = C.create_string_buffer(512)
    code = roundtrip(library_path, source, len(payload), output, 32, error, 512)
    assert code == expected_code, (code, error.value)
    if code == 0:
        assert output.raw[:18] == wire, output.raw.hex()
        assert output.raw[18:32] == b'Z' * 14, output.raw.hex()
    else:
        assert output.raw[:32] == b'Z' * 32, output.raw.hex()
        assert error.value

source = C.create_string_buffer(wire)
output = C.create_string_buffer(b'Z' * 32)
error = C.create_string_buffer(512)
assert roundtrip(library_path, source, len(wire), output, 17, error, 512) == -1
assert output.raw[:32] == b'Z' * 32
print(json.dumps({'dlopen': 'ok', 'wuji_core_header_roundtrip': 'ok',
                  'wire_hex': wire.hex(), 'decode_error_cases': len(error_cases),
                  'output_bounds': 'ok', 'hardware_access': 'none',
                  'network': 'denied by sandbox-exec'}, indent=2))
"""


def convert(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve() or (
        destination.exists() and source.samefile(destination)
    ):
        raise ValueError("Source and destination must differ")
    data = bytearray(source.read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"Unrecognized input SHA256 {digest}; refuse unreviewed layout")
    magic, cpu, subtype, kind, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<8I", data)
    if (magic, cpu, kind) != (0xFEEDFACF, 0x100000C, 2):
        raise ValueError("Expected thin ARM64 MH_EXECUTE")
    commands = []
    cursor = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, cursor)
        chunk = bytearray(data[cursor:cursor + size])
        cursor += size
        if cmd in (LC_MAIN, LC_LOAD_DYLINKER):
            continue
        if cmd == LC_SEGMENT_64 and chunk[8:24].rstrip(b"\0") == b"__PAGEZERO":
            # Keep the segment index: dyld rebase/bind programs use these indices.
            # A zero-length segment does not reserve the executable's 4 GiB guard.
            struct.pack_into("<Q", chunk, 32, 0)
        commands.append(chunk)
    name = b"@rpath/libwuji_cli_reuse.dylib\0"
    size = (24 + len(name) + 7) & ~7
    commands.append(bytearray(struct.pack("<6I", LC_ID_DYLIB, size, 24, 0, 0, 0)
                              + name + b"\0" * (size - 24 - len(name))))
    replacement = b"".join(commands)
    if len(replacement) > sizeofcmds:
        raise ValueError("New load commands do not fit old command region")
    struct.pack_into("<8I", data, 0, magic, cpu, subtype, 6, len(commands),
                     len(replacement), flags & ~MH_PIE, reserved)
    data[32:32 + sizeofcmds] = replacement + b"\0" * (sizeofcmds - len(replacement))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o755)
    subprocess.run(["codesign", "--force", "--sign", "-", str(destination)], check=True)
    print(f"Derived experimental dylib: {destination}")
    print(f"Original SHA256: {digest}")
    print(f"Derived SHA256: {hashlib.sha256(destination.read_bytes()).hexdigest()}")


def probe(destination: Path) -> None:
    """Call only two pure byte-codec functions in a network-denied process.

    ARM64 disassembly confirms the pinned Rust return values use x8 as the
    indirect result pointer. This bridge is specific to those two functions.
    It makes no claim about the ABI of other SDK functions or future releases.
    """
    native = Path(__file__).resolve().parent.parent / "native"
    bridge = destination.parent / "libwuji_header_bridge.dylib"
    subprocess.run(["xcrun", "clang", "-arch", "arm64", "-dynamiclib", "-std=c11",
                    "-Wall", "-Wextra", "-Werror", str(native / "header_bridge.c"),
                    str(native / "header_bridge.S"), "-o", str(bridge)], check=True)
    subprocess.run(["sandbox-exec", "-p", "(version 1) (allow default) (deny network*)",
                    sys.executable, "-c", PROBE_SOURCE, str(destination.resolve()),
                    str(bridge.resolve())], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--probe", action="store_true",
                        help="Run offline native header codec proof with network denied")
    args = parser.parse_args()
    convert(args.source, args.destination)
    if args.probe:
        probe(args.destination)


if __name__ == "__main__":
    main()
