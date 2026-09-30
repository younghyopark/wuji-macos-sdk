#!/usr/bin/env python3
"""Map the native bridge's pinned image offsets and symbol names to a new CLI build.

Exported functions and dlsym names map by Rust symbol path with crate hashes
ignored. Internal functions map through their exported callers: the call
sequences of the old and new caller are aligned, and the call that matched the
old target gives the new one; independent callers vote. Every mapped function
gets a shape check (instruction count and mnemonic similarity).

    python scripts/map_abi.py OLD_CLI NEW_CLI OUT_DIR

Writes OUT_DIR/abi-map.json and prints a review table. It never edits sources.
"""
from __future__ import annotations

import bisect
import collections
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = 0x100000000
SOURCES = ["native/manager_bridge.c", "native/device_bridge.c", "native/header_bridge.c",
           "native/rust_format.c", "native/schema_oracle.c", "native/zenoh_fixture_bridge.c"]
# Image offsets appear as `base + 0x...`, in tables, and as call arguments, so take
# every large hex constant in code (comments hold Linux addresses) except known values.
CONSTANT_RE = re.compile(r"\b0x([0-9a-fA-F]{5,})u?\b")
NOT_ADDRESSES = {0x10FFFF, 0x60000020, 0x8000000000000000}
NAME_RE = re.compile(r'"(_R[A-Za-z0-9_]+)"')


def norm(name: str) -> str:
    return re.sub(r"Cs[0-9A-Za-z]+_", "Cs_", name)


class Binary:
    def __init__(self, path: Path) -> None:
        self.path = path
        out = subprocess.run(["xcrun", "dyld_info", "-exports", str(path)], capture_output=True, text=True).stdout
        self.by_offset: dict[int, list[str]] = collections.defaultdict(list)
        self.by_name: dict[str, int] = {}
        self.by_norm: dict[str, set[int]] = collections.defaultdict(set)
        for line in out.splitlines():
            m = re.match(r"\s*0x([0-9A-Fa-f]+)\s+_(\S+)", line)  # drop the Mach-O underscore
            if m:
                offset, name = int(m.group(1), 16), m.group(2)
                self.by_offset[offset].append(name)
                self.by_name[name] = offset
                self.by_norm[norm(name)].add(offset)
        self.starts = sorted(self.by_offset)
        self._cache: dict[int, list[tuple[int, str, str]]] = {}

    def extent(self, offset: int) -> tuple[int, int]:
        """The export containing `offset`, and the next export after `offset`.

        The window is capped relative to `offset`, so internal functions far
        from the preceding export still disassemble."""
        i = bisect.bisect_right(self.starts, offset)
        start = self.starts[i - 1] if i else offset
        end = self.starts[i] if i < len(self.starts) else offset + 0x4000
        return start, min(end, offset + 0x20000)

    def disassemble(self, start: int, end: int | None = None) -> list[tuple[int, str, str]]:
        if start not in self._cache:
            if end is None:
                _, end = self.extent(start)
            text = subprocess.run(
                ["xcrun", "llvm-objdump", "-d", "--no-show-raw-insn", f"--start-address={BASE + start:#x}",
                 f"--stop-address={BASE + end:#x}", str(self.path)], capture_output=True, text=True).stdout
            rows = []
            for line in text.splitlines():
                m = re.match(r"\s*([0-9a-f]+):\s+(\S+)\s*(.*)", line)
                if m:
                    rows.append((int(m.group(1), 16) - BASE, m.group(2), m.group(3)))
            self._cache[start] = rows
        return self._cache[start]

    def calls(self, start: int) -> list[int]:
        return [int(m.group(1), 16) - BASE for _, op, args in self.disassemble(start)
                if op == "bl" and (m := re.match(r"0x([0-9a-f]+)", args))]


def callers_of(disassembly: Path, target: int) -> list[int]:
    pattern = f"bl\\s+0x{BASE + target:x}\\b"
    out = subprocess.run(["grep", "-E", "-o", f"^[0-9a-f]+:.*{pattern}", str(disassembly)],
                         capture_output=True, text=True).stdout
    return [int(line.split(":")[0], 16) - BASE for line in out.splitlines()]


def shape(binary: Binary, offset: int, count: int = 40) -> list[str]:
    return [op for _, op, _ in binary.disassemble(offset)[:count]]


def main() -> int:
    old_path, new_path, out_dir = map(Path, sys.argv[1:4])
    old, new = Binary(old_path), Binary(new_path)
    old_dis = out_dir / "old-full-disassembly.txt"
    if not old_dis.exists():
        old_dis.write_text(subprocess.run(["xcrun", "llvm-objdump", "-d", "--no-show-raw-insn", str(old_path)],
                                          capture_output=True, text=True).stdout)

    def export_map(offset: int) -> int | None:
        found = {o for n in old.by_offset.get(offset, []) for o in new.by_norm.get(norm(n), ())}
        return found.pop() if len(found) == 1 else None

    offsets, names = collections.defaultdict(list), set()
    for source in SOURCES:
        text = (ROOT / source).read_text()
        names.update(NAME_RE.findall(text))
        code = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        for number, line in enumerate(code.splitlines(), 1):
            for m in CONSTANT_RE.finditer(line.split("//")[0]):
                value = int(m.group(1), 16)
                if value not in NOT_ADDRESSES:
                    offsets[value].append(f"{Path(source).stem}:{number}")

    result = {"old": str(old_path), "new": str(new_path), "names": {}, "offsets": {}}
    for name in sorted(names):
        found = sorted(new.by_norm.get(norm(name), ()))
        new_names = sorted({n for o in found for n in new.by_offset[o] if norm(n) == norm(name)})
        result["names"][name] = new_names[0] if len(new_names) == 1 else {"candidates": new_names}

    for offset in sorted(offsets):
        entry = {"uses": offsets[offset], "old_name": (old.by_offset.get(offset) or [None])[0]}
        mapped = export_map(offset) if entry["old_name"] else None
        if mapped is not None:
            entry.update(new=hex(mapped), method="export")
        elif entry["old_name"] is None:
            votes = collections.Counter()
            for site in callers_of(old_dis, offset)[:40]:
                caller, _ = old.extent(site)
                new_caller = export_map(caller)
                if new_caller is None:
                    continue
                old_calls, new_calls = old.calls(caller), new.calls(new_caller)
                tokens_old = [norm(old.by_offset[c][0]) if c in old.by_offset else "?" for c in old_calls]
                tokens_new = [norm(new.by_offset[c][0]) if c in new.by_offset else "?" for c in new_calls]
                matcher = difflib.SequenceMatcher(a=tokens_old, b=tokens_new, autojunk=False)
                for tag, a0, a1, b0, b1 in matcher.get_opcodes():
                    if tag in ("equal", "replace") and a1 - a0 == b1 - b0:
                        for i, j in zip(range(a0, a1), range(b0, b1)):
                            if old_calls[i] == offset:
                                votes[new_calls[j]] += 1
            if votes:
                best, count = votes.most_common(1)[0]
                entry.update(new=hex(best), method="callers", votes=dict((hex(k), v) for k, v in votes.items()))
        if "new" in entry:
            a, b = shape(old, offset), shape(new, int(entry["new"], 16))
            entry["shape_similarity"] = round(difflib.SequenceMatcher(a=a, b=b).ratio(), 2)
        result["offsets"][hex(offset)] = entry

    (out_dir / "abi-map.json").write_text(json.dumps(result, indent=1) + "\n")
    print(f"names: {sum(isinstance(v, str) for v in result['names'].values())}/{len(result['names'])} mapped")
    for offset, entry in result["offsets"].items():
        flag = "" if entry.get("shape_similarity", 0) >= 0.8 else "  <-- review"
        print(f"{offset:>10} -> {entry.get('new', 'UNMAPPED'):>10} {entry.get('method', ''):8s} "
              f"sim={entry.get('shape_similarity', '-')!s:5s} votes={entry.get('votes', '')} "
              f"{(entry['old_name'] or '<internal>')[:60]}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
