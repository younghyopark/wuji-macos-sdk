#!/usr/bin/env python3
"""Show one function side by side in two CLI builds, with calls named.

    python scripts/abi_diff.py OLD_CLI NEW_CLI OLD_OFFSET|SYMBOL [NEW_OFFSET] [--count N] [--grep REGEX]

Symbols are matched across builds with crate hashes ignored. Immediates in
memory operands are kept verbatim so struct-offset changes stand out.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import map_abi  # noqa: E402


def render(binary: map_abi.Binary, offset: int, count: int, pattern: str | None) -> list[str]:
    rows = []
    for address, op, args in binary.disassemble(offset)[:count]:
        m = re.match(r"0x([0-9a-f]+)", args)
        if op in ("bl", "b") and m:
            target = int(m.group(1), 16) - map_abi.BASE
            names = binary.by_offset.get(target)
            args = f"{target:#x} {map_abi.norm(names[0])[:70] if names else ''}"
        else:
            args = re.sub(r"\s*;.*$", "", args)
        line = f"{address - offset:#06x} {op:6s} {args}"
        if pattern is None or re.search(pattern, line):
            rows.append(line)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("old_cli", type=Path)
    parser.add_argument("new_cli", type=Path)
    parser.add_argument("old")
    parser.add_argument("new", nargs="?")
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--grep")
    args = parser.parse_args()
    old, new = map_abi.Binary(args.old_cli), map_abi.Binary(args.new_cli)
    if args.old.startswith("0x"):
        old_offset = int(args.old, 16)
    else:
        old_offset = next(o for n, o in old.by_name.items() if map_abi.norm(n) == map_abi.norm(args.old))
    if args.new:
        new_offset = int(args.new, 16)
    else:
        names = old.by_offset[old_offset]
        new_offset = next(iter({o for n in names for o in new.by_norm.get(map_abi.norm(n), ())}))
    left = render(old, old_offset, args.count, args.grep)
    right = render(new, new_offset, args.count, args.grep)
    print(f"old {old_offset:#x}{'':50s} new {new_offset:#x}")
    for i in range(max(len(left), len(right))):
        a = left[i] if i < len(left) else ""
        b = right[i] if i < len(right) else ""
        mark = " " if a.split(" ", 1)[-1] == b.split(" ", 1)[-1] else "*"
        print(f"{a[:62]:62s} {mark} {b[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
