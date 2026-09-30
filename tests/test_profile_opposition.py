"""The instrumented runner preserves payloads and vendor cleanup ordering."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wuji_sdk

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_opposition", ROOT / "examples/macos/profile_opposition.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProfileOppositionTests(unittest.TestCase):
    def test_records_original_frames_and_preserves_cleanup(self):
        calls, frames = [], []
        publisher = SimpleNamespace(send=lambda joints: frames.append(joints),
                                    close=lambda: calls.append("close"))
        hand = SimpleNamespace(
            online_joints_count=lambda: SimpleNamespace(get=lambda: 20),
            handedness=lambda: SimpleNamespace(get=lambda: "left"),
            enable=lambda: calls.append("enable"), disable=lambda: calls.append("disable"),
            joint_command=lambda: SimpleNamespace(publish=lambda: publisher))
        manager = SimpleNamespace(auto_connect=lambda name: hand,
                                  disconnect_all=lambda: calls.append("disconnect_all"))
        fake_sdk = SimpleNamespace(SdkManager=SimpleNamespace(instance=lambda: manager),
                                   JointCommand=wuji_sdk.JointCommand, __file__="explicit-test-sdk")
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as directory:
            path = Path(directory) / "timing.json"
            with patch.dict(sys.modules, {"wuji_sdk": fake_sdk}), \
                 patch.object(sys, "argv", ["profile_opposition.py", "--mode", "original", "--frames", "2", "--output", str(path)]), \
                 patch.object(profile.signal, "signal"):
                self.assertEqual(profile.main(), 0)
            report = json.loads(path.read_text())
        self.assertEqual(calls, ["enable", "disable", "close", "disconnect_all"])
        self.assertEqual(report["mode"], "original")
        self.assertEqual(report["send_start"]["samples"], 2)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(len(frame) == 20 for frame in frames))
        self.assertTrue(all(j.velocity == 0 and j.effort == 0 for frame in frames for j in frame))
        import struct
        payload = (ROOT / "examples/python/wuji_hand_2/6_opposition/data/left.replay").read_bytes()
        expected = [struct.unpack_from("<I20f", payload, 16 + index * 84)[1:] for index in range(2)]
        self.assertEqual([[j.position for j in frame] for frame in frames], [list(values) for values in expected])


if __name__ == "__main__":
    unittest.main()
