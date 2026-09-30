"""Reproduce the extra telemetry fixtures using the installed native CLI image.

The research adapter is compiled separately; production bridge exports are not
expanded just for a fixture. Importing its probe never modifies sys.path, so
wheel verification continues to use the installed wuji_sdk codec.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

from wuji_macos._native import LIB_DIR

ROOT = Path(__file__).resolve().parents[1]


class ExtraNativePayloadOracles(unittest.TestCase):
    def test_installed_native_telemetry_oracles_match_recorded_bytes(self):
        build = ROOT / "build/installed-schema-oracles"
        build.mkdir(parents=True, exist_ok=True)
        bridge = build / "libwuji_schema_oracle.dylib"
        subprocess.run(["xcrun", "clang", "-arch", "arm64", "-dynamiclib", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "native/schema_oracle.c"), str(ROOT / "native/device_bridge.S"),
                        "-o", str(bridge)], check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location("schema_oracle_probe", ROOT / "scripts/probe_schema_oracles.py")
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        output = build / "native-codec-fixtures.json"
        probe.probe(LIB_DIR / "libwuji_cli_reuse.dylib", bridge, output)
        expected = json.loads((ROOT / "tests/data/schema-oracle-fixtures.json").read_text())
        self.assertEqual(json.loads(output.read_text()), expected)


if __name__ == "__main__":
    unittest.main()
