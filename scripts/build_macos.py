#!/usr/bin/env python3
"""Build the pinned experimental native wheel with all artifacts inside the repo."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def source_hashes():
    files = [ROOT / name for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE", "UPSTREAM_LICENSE")]
    files += list((ROOT / "src").rglob("*.py"))
    files += [p for p in (ROOT / "native").iterdir() if p.suffix in (".c", ".h", ".S")]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Build on native Apple Silicon macOS")
    # Standard tool-specific cache/temp settings; no user home or global config
    # is changed. Building does not initialize the SDK manager or hardware.
    temporary = ROOT / ".cache" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(temporary)
    os.environ["PIP_CACHE_DIR"] = str(ROOT / ".cache" / "pip")
    run(sys.executable, "scripts/fetch_artifacts.py", "cli-macos-arm64")
    inputs = source_hashes()
    stage = ROOT / "build" / "wheel-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    for filename in ["pyproject.toml", "setup.py", "README.md", "LICENSE", "UPSTREAM_LICENSE"]:
        shutil.copy2(ROOT / filename, stage / filename)
    shutil.copytree(ROOT / "src", stage / "src", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    native = stage / "src" / "wuji_macos" / "_lib"
    native.mkdir(parents=True, exist_ok=True)
    source = ROOT / ".artifacts" / "cli-macos-arm64" / "2026.9.22" / "wuji"
    run(sys.executable, "scripts/experimental_macho.py", source,
        native / "libwuji_cli_reuse.dylib")
    probe_library = ROOT / "build/native-reuse/libwuji_cli_reuse.dylib"
    probe_library.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(native / "libwuji_cli_reuse.dylib", probe_library)
    run("xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=11.0",
        "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib",
        "native/header_bridge.c", "native/header_bridge.S",
        "-o", native / "libwuji_header_bridge.dylib")
    for component in ("manager", "device"):
        run("xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=11.0",
            "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib",
            f"native/{component}_bridge.c", f"native/{component}_bridge.S",
            "native/rust_format.c", "-o", native / f"libwuji_{component}_bridge.dylib")
    for library in native.glob("*.dylib"):
        run("codesign", "--verify", "--strict", library)
    if source_hashes() != inputs:
        raise SystemExit("Source files changed during the build; rerun after edits finish")
    subprocess.run([sys.executable, "-c", "import wuji_sdk; import wuji_sdk._codec"],
                   cwd=ROOT, env={**os.environ, "PYTHONPATH": str(stage / "src")}, check=True)
    info = {
        "upstream_version": "2026.9.22",
        "upstream_executable_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "method": "pinned Mach-O conversion plus compiled C/ARM64 ABI bridge",
        "capabilities": ["offline_header_codec", "native_manager", "native_user_crud", "native_device_raw_requests",
                         "native_command_codecs", "native_subscription_receiver", "native_hand2_fingertip_metadata",
                         "native_hand2_joint_command_publisher"],
        "publisher": "SDK Device::publish::<JointCommandFrame> and WujiHand2 JointCommandPublisher",
        "hardware_validation": "not performed by build or verifier",
        "source_sha256": inputs,
        "native_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(native.glob("*.dylib"))
        },
    }
    (native / "backend.json").write_text(json.dumps(info, indent=2) + "\n")
    run(sys.executable, "-m", "build", "--wheel", "--no-isolation", stage,
        "--outdir", ROOT / "dist")


if __name__ == "__main__":
    main()
