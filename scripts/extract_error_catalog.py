#!/usr/bin/env python3
"""Extract the immutable fault catalog from the hash-pinned native SDK image.

error_codes::describe@0x9e0d28 searches 53 immutable 104-byte records. Its
disassembly reads code at record+96 and returns a borrowed pointer or NULL.
The six preceding &str pairs are name/severity/clear_policy/desc/cause/resolution;
is_test is at+98. This extractor never creates a manager or opens hardware.
"""
import ctypes as C
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DERIVED_SHA256 = "d782cb2c495dceb64e44802f9617d6ae8243796946cdd6ecfcec192379e36e77"


def extract():
    library_path = ROOT / "build/native-reuse/libwuji_cli_reuse.dylib"
    if hashlib.sha256(library_path.read_bytes()).hexdigest() != DERIVED_SHA256:
        raise RuntimeError("Native image checksum mismatch")
    library = C.CDLL(str(library_path))
    describe = getattr(library, "_RNvNtCsgTcy8zmbXBL_9wuji_core11error_codes8describe")
    describe.argtypes, describe.restype = [C.c_uint16], C.c_void_p
    catalog = {}
    fields = ("name", "severity", "clear_policy", "desc", "cause", "resolution")
    for code in range(65536):
        pointer = describe(code)
        if not pointer:
            continue
        raw = C.string_at(pointer, 104)
        if int.from_bytes(raw[96:98], "little") != code or raw[98] not in (0, 1):
            raise RuntimeError("Native catalog record layout validation failed")
        result = {"code": code, "is_test": bool(raw[98])}
        for i, field in enumerate(fields):
            offset = i * 16
            address = int.from_bytes(raw[offset:offset+8], "little")
            size = int.from_bytes(raw[offset+8:offset+16], "little")
            if size > 8192 or (size and address == 0):
                raise RuntimeError("Native catalog string layout validation failed")
            result[field] = C.string_at(address, size).decode("utf-8")
        if result["severity"] not in ("Warning", "DeferredStop", "ImmediateStop", "Fatal"):
            raise RuntimeError("Unexpected native fault severity")
        if result["clear_policy"] not in ("AutoClear", "ManualClear", "NonClearable"):
            raise RuntimeError("Unexpected native fault clear policy")
        catalog[code] = result
    if len(catalog) != 53 or 0 in catalog:
        raise RuntimeError("Pinned native fault catalog changed")
    destination = ROOT / "src/wuji_sdk/_error_catalog.py"
    destination.write_text('"""Generated from pinned Wuji 2026.9.22 native error_codes::describe.\n\nDo not decode severity from hexadecimal digits; use the native catalog.\n"""\n\nCATALOG = ' + repr(catalog) + '\n')
    print(json.dumps({"catalog_entries": len(catalog), "output": str(destination.relative_to(ROOT)), "example": next(iter(catalog.values()))}, ensure_ascii=False))


if __name__ == "__main__":
    if "--child" in sys.argv:
        extract()
    else:
        profile = '(version 1) (allow default) (deny network*) (deny iokit-open) (deny file-write*) (allow file-write* (subpath ' + json.dumps(str(ROOT)) + '))'
        subprocess.run(["sandbox-exec", "-p", profile, sys.executable, str(Path(__file__).resolve()), "--child"], cwd=ROOT, check=True, timeout=20)
