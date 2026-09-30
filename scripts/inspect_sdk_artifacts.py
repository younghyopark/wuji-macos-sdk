#!/usr/bin/env python3
"""Inspect pinned WUJI artifacts offline without loading or executing SDK code.

Writes evidence under build/abi-inspection by default. Requires Apple's
Command Line Tools for dyld_info, otool, and llvm-objdump; no Python dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SDK_PREFIX = "__RNvMNtCsdhiNM91e48J_8wuji_sdk"
C_CANDIDATES = (
    "wuji_version",
    "wuji_init",
    "wuji_shutdown",
    "wuji_last_error",
    "wuji_hand_2_decode_joint_status",
    "wuji_hand_2_joint_label",
    "wuji_hand_2_finger_name",
    "wuji_hand_2_joint_id_from_bus_node",
    "wuji_hand_2_describe_error",
)
ENV_CANDIDATES = (
    "WUJI_LOGS_DIR",
    "WUJI_TELEMETRY_DISABLE",
    "WUJI_NO_UPDATE_CHECK",
    "WUJI_API_URL",
    "WUJI_ZENOH_PROTO",
    "WUJI_ZENOH_CHUNK",
    "WUJI_UPDATE_REPO",
    "WUJI_HOME",
    "WUJI_SDK_HOME",
    "WUJI_DATA_DIR",
    "WUJI_CONFIG_DIR",
)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def elf_dynamic_exports(data: bytes) -> set[str]:
    """Read ELF64 little-endian .dynsym without loading a Linux shared object."""
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected ELF64 little-endian C SDK")
    section_offset = struct.unpack_from("<Q", data, 40)[0]
    entry_size, section_count = struct.unpack_from("<HH", data, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, section_offset + i * entry_size)
                for i in range(section_count)]
    names = set()
    for section in sections:
        if section[1] != 11:  # SHT_DYNSYM
            continue
        strings = sections[section[6]]
        table = data[strings[4]:strings[4] + strings[5]]
        for offset in range(section[4], section[4] + section[5], section[9]):
            name_offset, info, other, index, value, size = struct.unpack_from("<IBBHQQ", data, offset)
            if index == 0 or info >> 4 not in (1, 2):
                continue
            end = table.index(b"\0", name_offset)
            names.add(table[name_offset:end].decode())
    return names


def inspect(out: Path) -> dict:
    lock = json.loads((ROOT / "artifacts.lock.json").read_text())
    artifacts = {}
    for key in ("cli-macos-arm64", "sdk-c-linux-arm64", "sdk-python-linux-arm64"):
        spec = lock["artifacts"][key]
        path = ROOT / ".artifacts" / key / spec["version"] / spec["filename"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != spec["sha256"]:
            raise ValueError(f"SHA256 mismatch: {path}")
        artifacts[key] = {"path": str(path.relative_to(ROOT)), "sha256": actual}
    out.mkdir(parents=True, exist_ok=True)

    cli_archive = ROOT / artifacts["cli-macos-arm64"]["path"]
    with tarfile.open(cli_archive) as archive:
        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "wuji"]
        if len(members) != 1:
            raise ValueError("Expected exactly one CLI executable")
        cli_bytes = archive.extractfile(members[0]).read()
    cli = cli_archive.parent / "wuji"
    if cli.read_bytes() != cli_bytes:
        raise ValueError("Extracted CLI does not match its pinned archive")

    c_archive = ROOT / artifacts["sdk-c-linux-arm64"]["path"]
    with tarfile.open(c_archive) as archive:
        names = archive.getnames()
        (out / "c-archive-members.txt").write_text("\n".join(names) + "\n")
        headers = [name for name in names if name.endswith("/include/wuji_sdk.h")]
        if len(headers) != 1:
            raise ValueError("Expected exactly one C header")
        header = archive.extractfile(headers[0]).read().decode()
        (out / "wuji_sdk.h").write_text(header)
        libraries = [name for name in names if name.endswith("/lib/libwuji_sdk_c.so")]
        if len(libraries) != 1:
            raise ValueError("Expected exactly one C shared object")
        c_exports = elf_dynamic_exports(archive.extractfile(libraries[0]).read())
        (out / "c-dynamic-exports.txt").write_text("\n".join(sorted(c_exports)) + "\n")

    wheel = ROOT / artifacts["sdk-python-linux-arm64"]["path"]
    with zipfile.ZipFile(wheel) as archive:
        (out / "python-wheel-members.txt").write_text("\n".join(archive.namelist()) + "\n")
        (out / "__init__.pyi").write_bytes(archive.read("wuji_sdk/__init__.pyi"))
        metadata = next(n for n in archive.namelist() if n.endswith(".dist-info/WHEEL"))
        (out / "python-WHEEL.txt").write_bytes(archive.read(metadata))

    export_text = run("xcrun", "dyld_info", "-exports", str(cli))
    (out / "cli-dyld-exports.txt").write_text(export_text)
    (out / "cli-nm-exports.txt").write_text(run("nm", "-gU", str(cli)))
    (out / "cli-load-commands.txt").write_text(run("otool", "-l", str(cli)))
    exports = {}
    for line in export_text.splitlines():
        match = re.match(r"\s+(0x[0-9A-Fa-f]+)\s+(\S+)", line)
        if match:
            address, symbol = match.groups()
            exports[symbol] = int(address, 16)

    direct = {name: address for name, address in exports.items()
              if name.startswith(SDK_PREFIX)
              and ("7managerNtB2_10SdkManager" in name
                   or "6deviceNtB2_10WujiDevice" in name)}
    direct_report = [
        {"macho_symbol": name, "dlsym_name": name[1:], "image_offset": hex(address)}
        for name, address in direct.items()
    ]
    (out / "sdk-direct-exports.json").write_text(json.dumps(direct_report, indent=2) + "\n")
    disassembly = []
    sorted_addresses = sorted(set(exports.values()))
    for name, address in direct.items():
        if not name.endswith(("8instance", "4scan", "7connect", "7get_raw", "7set_raw", "13get_raw_bytes")):
            continue
        following = next((x for x in sorted_addresses if x > address), address + 256)
        # Static inspection only: bounded initial instructions show parameter and
        # return conventions, without treating a mangled name as a signature.
        end = min(address + 256, following)
        disassembly.append(name + "\n" + run(
            "xcrun", "llvm-objdump", "--disassemble",
            f"--start-address={hex(0x100000000 + address)}",
            f"--stop-address={hex(0x100000000 + end)}", str(cli)))
    (out / "sdk-disassembly.txt").write_text("\n".join(disassembly))

    c_functions = []
    for name in C_CANDIDATES:
        match = re.search(r"^[A-Za-z_][^;{}\n]*\b" + re.escape(name) + r"\([^;]*\);", header, re.MULTILINE)
        c_functions.append({
            "name": name,
            "header_prototype": re.sub(r"\s+", " ", match.group()).strip() if match else None,
            "present_in_linux_c_dynamic_exports": name in c_exports,
            "present_in_cli_export_trie": "_" + name in exports,
        })
    env_markers = {key: cli_bytes.count(key.encode()) for key in ENV_CANDIDATES}
    report = {
        "artifacts": artifacts,
        "cli_executable_sha256": hashlib.sha256(cli_bytes).hexdigest(),
        "cli_export_count": len(exports),
        "linux_c_dynamic_export_count": len(c_exports),
        "direct_sdk_exports": direct_report,
        "c_api_candidates": c_functions,
        "environment_literal_counts": env_markers,
        "interpretation": [
            "The stripped nm symbol table does not enumerate dyld's Rust export trie.",
            "C header prototypes describe the Linux C wrapper, not the Rust CLI ABI.",
            "Literal presence is evidence only; variable semantics are not established by strings.",
            "No SDK function, manager, scan, connection, or hardware operation was executed.",
        ],
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/abi-inspection")
    args = parser.parse_args()
    report = inspect(args.output)
    print(f"Inspected {report['cli_export_count']} CLI exports; evidence: {args.output}")
    print("No SDK code executed.")


if __name__ == "__main__":
    main()
