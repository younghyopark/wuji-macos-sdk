#!/usr/bin/env python3
"""Find functions in a new CLI build that match functions in the old one.

A function's fingerprint is its first N instructions with branch targets and
PC-relative addresses blanked; immediates stay, since they carry struct field
offsets. Every `bl` target and export in the new build is a candidate start.

    python scripts/find_function.py OLD_CLI NEW_CLI OLD_DISASM NEW_DISASM 0xOFFSET [...]
"""
from __future__ import annotations

import collections
from pathlib import Path
import re
import sys

BASE = 0x100000000
LINE = re.compile(r"^([0-9a-f]+):\s+(\S+)\s*(.*)$")


def load(path: Path):
    addresses, instructions = [], []
    for line in path.read_text().splitlines():
        m = LINE.match(line)
        if m:
            addresses.append(int(m.group(1), 16) - BASE)
            op, args = m.group(2), re.sub(r"\s*;.*$", "", m.group(3))
            args = re.sub(r"0x1[0-9a-f]{8}\s*<[^>]*>|0x1[0-9a-f]{8}", "T", args)
            if op in ("adrp", "adr"):
                args = re.sub(r",.*", ", PAGE", args)
            instructions.append(f"{op} {args}")
    return addresses, instructions


def targets(path: Path) -> set[int]:
    found = set()
    for m in re.finditer(r"\sbl\s+0x(1[0-9a-f]{8})", path.read_text()):
        found.add(int(m.group(1), 16) - BASE)
    return found


def main() -> int:
    old_dis, new_dis = Path(sys.argv[3]), Path(sys.argv[4])
    wanted = [int(x, 16) for x in sys.argv[5:]]
    old_addr, old_ins = load(old_dis)
    new_addr, new_ins = load(new_dis)
    old_index = {a: i for i, a in enumerate(old_addr)}
    new_index = {a: i for i, a in enumerate(new_addr)}
    starts = targets(new_dis)
    for size in (24, 16, 10):
        table = collections.defaultdict(list)
        for start in starts:
            i = new_index.get(start)
            if i is not None:
                table[tuple(new_ins[i:i + size])].append(start)
        for offset in list(wanted):
            i = old_index[offset]
            found = table.get(tuple(old_ins[i:i + size]), [])
            if found:
                print(f"{offset:#x}: {len(found)} match(es) on first {size} instructions: "
                      f"{', '.join(hex(f) for f in sorted(found)[:8])}")
                wanted.remove(offset)
    for offset in wanted:
        print(f"{offset:#x}: no fingerprint match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
