#!/usr/bin/env python3
"""Verify the installed wheel offline, with all writable paths inside the repo."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

from fetch_artifacts import fetch
from probe_zenoh_publisher import build_fixture_bridge

ROOT = Path(__file__).resolve().parents[1]


def main():
    examples = json.loads((ROOT / "examples/upstream.json").read_text())
    modifications = examples.get("local_modifications", {})
    assert set(modifications) <= set(examples["files"])
    for path, expected in examples["files"].items():
        modification = modifications.get(path)
        if modification:
            assert hashlib.sha256((ROOT / modification["upstream_snapshot"]).read_bytes()).hexdigest() == expected, path
            expected = modification["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheel = ROOT / f"dist/wuji_macos-{version}-py3-none-macosx_11_0_arm64.whl"
    if not wheel.is_file():
        raise SystemExit("Build the wheel with scripts/build_macos.py first")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read(next(n for n in names if n.endswith(".dist-info/WHEEL"))).decode()
        assert "Root-Is-Purelib: false" in metadata
        assert "Tag: py3-none-macosx_11_0_arm64" in metadata
        assert "wuji_macos/_lib/libwuji_cli_reuse.dylib" in names
        assert "wuji_macos/_lib/libwuji_header_bridge.dylib" in names
        assert "wuji_macos/_lib/libwuji_manager_bridge.dylib" in names
        assert "wuji_macos/_lib/libwuji_device_bridge.dylib" in names
        assert "wuji_sdk/__init__.py" in names
        assert f"wuji_macos-{version}.dist-info/licenses/LICENSE" in names
        assert f"wuji_macos-{version}.dist-info/licenses/UPSTREAM_LICENSE" in names
        provenance = json.loads(archive.read("wuji_macos/_lib/backend.json"))
        for path, expected in provenance["source_sha256"].items():
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, f"Rebuild changed source: {path}"
    verify = ROOT / ".venv-verify"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(verify)], check=True)
    python = verify / "bin/python"
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX"):
        env.pop(name, None)
    temporary = ROOT / ".cache/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(temporary)
    env["WUJI_MACOS_OFFLINE_TEST"] = "1"
    env["WUJI_MACOS_STATE_DIR"] = str(ROOT / "build/verification-state")
    subprocess.run([
        sys.executable, "-m", "pip", "--python", str(python), "install",
        "--no-index", "--no-deps", "--no-cache-dir", "--force-reinstall", str(wheel),
    ], cwd=ROOT, env=env, check=True)
    fixture_name = "eclipse-zenoh-macos-fixture"
    fixture_entry = json.loads((ROOT / "artifacts.lock.json").read_text())["artifacts"][fixture_name]
    fixture_wheel = fetch(fixture_name, fixture_entry)
    subprocess.run([
        sys.executable, "-m", "pip", "--python", str(python), "install",
        "--no-index", "--no-deps", "--no-cache-dir", str(fixture_wheel),
    ], cwd=ROOT, env=env, check=True)
    build_fixture_bridge()
    # Deny network even if the reused binary were to attempt background work.
    # All test interpreter writes (including pycache) must remain in the repo.
    profile = (
        '(version 1) (allow default) (deny network*) (deny iokit-open) (deny file-write*) '
        f'(allow file-write* (subpath {json.dumps(str(ROOT))}))'
    )
    provenance_check = subprocess.run(["sandbox-exec", "-p", profile, str(python), "-I", "-c",
        "import pathlib, sys, wuji_sdk; p=pathlib.Path(wuji_sdk.__file__).resolve(); "
        "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()), p; print(p)"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True, timeout=20)
    command = ["sandbox-exec", "-p", profile, str(python), "-I", "-m", "unittest",
               "discover", "-s", str(ROOT / "tests"), "-v"]
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    loopback_env = env.copy()
    loopback_env.pop("WUJI_MACOS_OFFLINE_TEST", None)
    loopback_env["WUJI_MACOS_LOOPBACK_TEST"] = "1"
    loopback_profile = (
        '(version 1) (allow default) (deny network*) '
        '(allow network-outbound (remote ip "localhost:*")) '
        '(allow network-inbound (local ip "localhost:*")) '
        '(allow network-bind (local udp "*:7446")) '
        '(deny iokit-open) (deny file-write*) '
        f'(allow file-write* (subpath {json.dumps(str(ROOT))}))'
    )
    loopback = subprocess.run(["sandbox-exec", "-p", loopback_profile, str(python), "-I",
                              "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
                              "-p", "test_loopback*.py", "-v"],
                             cwd=ROOT, env=loopback_env, text=True, capture_output=True, timeout=120)
    print(loopback.stdout, end="")
    print(loopback.stderr, end="", file=sys.stderr)
    monitor = subprocess.run(["sandbox-exec", "-p", loopback_profile, str(python), "-I",
                              "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
                              "-p", "test_command_monitor.py", "-v"],
                             cwd=ROOT, env=loopback_env, text=True, capture_output=True, timeout=30)
    print(monitor.stdout, end="")
    print(monitor.stderr, end="", file=sys.stderr)
    report = {
        "wheel": str(wheel.relative_to(ROOT)), "wheel_tag": "py3-none-macosx_11_0_arm64",
        "installed_wheel_tests_passed": result.returncode == 0 and loopback.returncode == 0 and monitor.returncode == 0,
        "installed_sdk_path": provenance_check.stdout.strip(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "fixture_dependency": {"name": "eclipse-zenoh", "version": fixture_entry["version"],
                               "sha256": fixture_entry["sha256"], "sdk_runtime_dependency": False},
        "network_access": "offline suite denied; separate protocol fixture suite allows localhost only",
        "iokit_access": "denied by sandbox-exec",
        "file_writes": "restricted to repository by sandbox-exec",
        "test_output": result.stdout + result.stderr,
        "loopback_test_output": loopback.stdout + loopback.stderr,
        "monitor_test_output": monitor.stdout + monitor.stderr,
        "physical_hardware_tested": False,
        "unchanged_upstream_example_files": len(examples["files"]) - len(modifications),
        "locally_modified_example_files": modifications,
    }
    (ROOT / "build/verification.json").write_text(json.dumps(report, indent=2) + "\n")
    if result.returncode:
        raise SystemExit(result.returncode)
    if loopback.returncode:
        raise SystemExit(loopback.returncode)
    if monitor.returncode:
        raise SystemExit(monitor.returncode)


if __name__ == "__main__":
    main()
