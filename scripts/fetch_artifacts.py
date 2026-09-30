#!/usr/bin/env python3
"""Fetch pinned upstream files inside this repository; never install or run them."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(name, entry):
    directory = ARTIFACTS / name / entry["version"]
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / entry["filename"]
    if not archive.exists():
        partial = archive.with_suffix(archive.suffix + ".part")
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "wuji-macos-research"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            if digest(partial) != entry["sha256"]:
                raise RuntimeError(f"SHA-256 mismatch: {name}")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if digest(archive) != entry["sha256"]:
        raise RuntimeError(f"SHA-256 mismatch: {archive}")
    print(f"Verified {archive.relative_to(ROOT)}")
    if name == "cli-macos-arm64":
        with tarfile.open(archive) as bundle:
            members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "wuji"]
            if len(members) != 1:
                raise RuntimeError("Expected exactly one regular wuji executable")
            # Copy only the expected file; never trust archive paths or links.
            with bundle.extractfile(members[0]) as source, (directory / "wuji").open("wb") as output:
                shutil.copyfileobj(source, output)
            (directory / "wuji").chmod(0o755)
        print(f"Extracted {(directory / 'wuji').relative_to(ROOT)}")
    return archive


def main():
    manifest = json.loads((ROOT / "artifacts.lock.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Artifact keys; defaults to all pinned artifacts")
    args = parser.parse_args()
    for name in args.names or manifest["artifacts"]:
        if name not in manifest["artifacts"]:
            parser.error(f"Unknown artifact: {name}")
        fetch(name, manifest["artifacts"][name])


if __name__ == "__main__":
    main()
