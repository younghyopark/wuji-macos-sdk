"""Passive command monitor parsing and localhost-only observation checks."""
import importlib.util
import json
import os
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples/macos/monitor_joint_commands.py"
spec = importlib.util.spec_from_file_location("command_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def encoded_frame(sequence):
    encoded = bytearray()
    while sequence >= 0x80:
        encoded.append((sequence & 0x7F) | 0x80)
        sequence >>= 7
    encoded.append(sequence)
    return bytes(encoded) + struct.pack("<60f", *([0.0] * 60))


class CommandMonitorTests(unittest.TestCase):
    def test_schema315_sequence_boundaries_and_invalid_data(self):
        for sequence in (0, 127, 128, 16383, 16384, 0xFFFFFFFF):
            with self.subTest(sequence=sequence):
                self.assertEqual(monitor.decode_sequence(encoded_frame(sequence)), sequence)
        for payload in (b"", b"\x80", b"\xff\xff\xff\xff\x10" + bytes(240),
                        b"\x80\x00" + bytes(240), encoded_frame(1)[:-1],
                        encoded_frame(1) + b"\0"):
            with self.subTest(payload=payload[:6]):
                with self.assertRaises(ValueError):
                    monitor.decode_sequence(payload)

    def test_gap_duplicate_restart_and_wrap_are_distinct(self):
        records = [{"sequence": value, "callback_monotonic_ns": i}
                   for i, value in enumerate((0, 1, 3, 2, 2))]
        summary = monitor.sequence_summary(records)
        self.assertEqual(summary["forward_gap_events"], 1)
        self.assertEqual(summary["missing_sequence_values"], 1)
        self.assertEqual(summary["duplicate_transitions"], 1)
        self.assertEqual(summary["backward_or_restart_events"], 1)
        summary = monitor.sequence_summary([
            {"sequence": 0xFFFFFFFF, "callback_monotonic_ns": 0},
            {"sequence": 0, "callback_monotonic_ns": 1}])
        self.assertEqual(summary["u32_wrap_transitions"], 1)
        self.assertEqual(summary["forward_gap_events"], 0)

    def test_interval_statistics_include_long_gaps_and_empty_capture(self):
        self.assertIsNone(monitor.interval_summary([])["mean_rate_hz"])
        self.assertIsNone(monitor.interval_summary([123])["mean_rate_hz"])
        summary = monitor.interval_summary([0, 1_000_000, 2_000_000, 8_000_000])
        self.assertEqual(summary["mean_rate_hz"], 375)
        self.assertEqual(summary["p50_ms"], 1)
        self.assertEqual(summary["max_ms"], 6)
        self.assertEqual(summary["intervals_above_5_ms"], 1)


@unittest.skipUnless(os.environ.get("WUJI_MACOS_LOOPBACK_TEST") == "1",
                     "requires a loopback-only network sandbox")
class PassiveZenohMonitorTests(unittest.TestCase):
    def test_observes_synthetic_frames_over_explicit_localhost_connection(self):
        import zenoh

        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        endpoint = f"tcp/127.0.0.1:{port}"
        config = zenoh.Config()
        for key, value in {
            "mode": "peer", "listen/endpoints": [endpoint],
            "connect/endpoints": [], "scouting/multicast/enabled": False,
            "scouting/gossip/enabled": False, "transport/shared_memory/enabled": False,
        }.items():
            config.insert_json5(key, json.dumps(value))
        output = ROOT / "build/opposition-timing/monitor-loopback-test.json"
        session = zenoh.open(config)
        publisher = session.declare_publisher("wuji/MONITORTEST/joint_command")
        child = subprocess.Popen([
            sys.executable, "-I", str(SCRIPT), "--protocol", "tcp", "--address",
            f"127.0.0.1:{port}", "--sn", "MONITORTEST", "--seconds", "2",
            "--output", str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "observer did not become ready")
            ready = child.stdout.readline()
            self.assertTrue(ready.startswith("READY observing wuji/MONITORTEST/joint_command"), ready)
            deadline = time.monotonic() + 1
            while not publisher.matching_status and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(publisher.matching_status)
            # Inert bytes are published only to the synthetic localhost peer.
            for sequence in (0, 1, 3, 2, 2):
                publisher.put(encoded_frame(sequence))
                time.sleep(0.005)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stdout + stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["sample_count"], 5)
            self.assertEqual([sample["sequence"] for sample in report["samples"]], [0, 1, 3, 2, 2])
            self.assertEqual(report["sequence"]["missing_sequence_values"], 1)
            self.assertEqual(report["sequence"]["duplicate_transitions"], 1)
            self.assertEqual(report["sequence"]["backward_or_restart_events"], 1)
            self.assertEqual(report["decode_errors"], {})
            self.assertEqual(report["key"], "wuji/MONITORTEST/joint_command")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()
            publisher.undeclare()
            session.close()


if __name__ == "__main__":
    unittest.main()
