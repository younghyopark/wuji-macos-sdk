#!/usr/bin/env python3
"""Measure the unchanged opposition workload against a separate local peer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from replay_timing import SendTiming, cadence, distribution_ns, play_deadlines
from probe_zenoh_publisher import MockWujiZenoh, build_fixture_bridge, fixture_backend


def load_example(*, legacy=False):
    path = ROOT / ("examples/upstream_sources/opposition.py" if legacy else
                   "examples/python/wuji_hand_2/6_opposition/opposition.py")
    spec = importlib.util.spec_from_file_location("vendor_opposition", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    # Both variants read the same pinned replay data files.
    example.__file__ = str(ROOT / "examples/python/wuji_hand_2/6_opposition/opposition.py")
    return example, path


def peer():
    class TimedPeer(MockWujiZenoh):
        def __init__(self):
            super().__init__()
            self.samples = []

        def _sample(self, sample):
            received = time.perf_counter_ns()
            payload = bytes(sample.payload)
            seq = shift = 0
            for value in payload:
                seq |= (value & 127) << shift
                if not value & 128:
                    break
                shift += 7
            with self.changed:
                self.samples.append([received, seq, len(payload)])
                self.changed.notify_all()

    with TimedPeer() as fixture:
        print(json.dumps({"ready": True, "endpoint": fixture.endpoint}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["action"] == "start":
                with fixture.changed:
                    fixture.samples.clear()
                print(json.dumps({"ready": True}), flush=True)
            elif request["action"] == "finish":
                with fixture.changed:
                    fixture.changed.wait_for(lambda: len(fixture.samples) >= request["count"], timeout=3)
                    result = {"samples": list(fixture.samples), "failures": fixture.failures}
                print(json.dumps(result), flush=True)
            elif request["action"] == "stop":
                break


def benchmark(args):
    import wuji_sdk as sdk
    from wuji_macos._native import backend_info

    example, example_path = load_example()
    legacy, legacy_path = load_example(legacy=True)
    receiver = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--peer"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def exchange(request):
        receiver.stdin.write(json.dumps(request) + "\n")
        receiver.stdin.flush()
        return json.loads(receiver.stdout.readline())

    manager = None
    report = {"label": args.label, "python": sys.executable, "platform": platform.platform(),
              "sdk": sdk.__file__, "backend": backend_info(), "samples_per_case": args.samples,
              "side": args.side, "example_sha256": hashlib.sha256(example_path.read_bytes()).hexdigest(),
              "legacy_example_sha256": hashlib.sha256(legacy_path.read_bytes()).hexdigest(),
              "transport": "native SDK Zenoh TCP to 127.0.0.1:7447",
              "receiver": "separate Python process; callback timestamps, not firmware timestamps",
              "physical_hardware": False, "cases": []}
    try:
        ready = json.loads(receiver.stdout.readline())
        os.environ["WUJI_ZENOH_PROTO"] = "tcp"
        manager = sdk.SdkManager(backend=fixture_backend())
        hand = manager.connect(address=ready["endpoint"], device_name="timing_fixture",
            options=sdk.ConnectOptions(enable_bridge=False, auto_time_sync_interval_ms=None))
        for mode in args.modes.split(","):
            assert exchange({"action": "start"})["ready"]
            stream, total = example._open_replay(args.side)
            count = min(args.samples, total)
            publisher = hand.joint_command().publish()
            recording = SendTiming(lambda qpos: publisher.send([
                sdk.JointCommand(float(value), 0.0, 0.0) for value in qpos]))
            pacing = None
            cpu_start, wall_start = time.process_time(), time.perf_counter()
            try:
                if mode in ("original", "legacy"):
                    legacy._play_replay(stream, count, recording)
                elif mode == "fixed":
                    pacing = example._play_replay(stream, count, recording)
                elif mode in ("deadline", "deadline_spin"):
                    pacing = play_deadlines(stream, count, recording, read_qpos=example._read_qpos,
                                            spin_us=100 if mode == "deadline_spin" else 0)
                else:
                    raise ValueError(f"unknown mode {mode}")
            finally:
                cpu_s, wall_s = time.process_time() - cpu_start, time.perf_counter() - wall_start
                publisher.close()
                stream.close()
            received = exchange({"action": "finish", "count": count})
            rows = received["samples"]
            seqs = [row[1] for row in rows]
            result = {"mode": mode, **recording.summary(), "receiver": cadence([row[0] for row in rows]),
                      "all_sequences_in_order": seqs == list(range(count)), "receiver_failures": received["failures"],
                      "local_callback_delay": distribution_ns([row[0] - recording.started[row[1]] for row in rows
                                                               if row[1] < len(recording.started)]),
                      "pacing": pacing, "process_cpu_s": cpu_s, "wall_s": wall_s,
                      "raw_send_start_ns": recording.started, "raw_send_return_ns": recording.finished,
                      "raw_received": rows}
            report["cases"].append(result)
            print(json.dumps({key: result[key] for key in ("mode", "send_start", "command_construction_and_send",
                                                          "receiver", "all_sequences_in_order", "pacing")}), flush=True)
    finally:
        if manager:
            manager.disconnect_all()
        if receiver.poll() is None:
            receiver.stdin.write('{"action":"stop"}\n')
            receiver.stdin.flush()
            try:
                receiver.wait(timeout=5)
            except subprocess.TimeoutExpired:
                receiver.terminate()
                receiver.wait(timeout=5)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5000)
    parser.add_argument("--side", choices=("left", "right"), default="left")
    parser.add_argument("--modes", default="legacy,fixed")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output", type=Path, default=ROOT / "build/opposition-timing/benchmark.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--peer", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.peer:
        peer()
    elif args.child:
        benchmark(args)
    else:
        if args.samples < 2:
            parser.error("--samples must be at least 2")
        build_fixture_bridge()
        env = os.environ.copy()
        env.update(TMPDIR=str(ROOT / ".cache/tmp"), WUJI_MACOS_STATE_DIR=str(ROOT / "build/opposition-timing/sdk-state"))
        profile = ('(version 1) (allow default) (deny network*) '
                   '(allow network-outbound (remote ip "localhost:*")) '
                   '(allow network-inbound (local ip "localhost:*")) '
                   '(allow network-bind (local udp "*:7446")) '
                   '(deny iokit-open) (deny file-write*) '
                   '(allow file-write* (subpath ' + json.dumps(str(ROOT)) + '))')
        subprocess.run(["sandbox-exec", "-p", profile, sys.executable, "-I", str(Path(__file__).resolve()),
                        *sys.argv[1:], "--child"], env=env, check=True)


if __name__ == "__main__":
    main()
