#!/usr/bin/env python3
"""Run the vendor opposition replay with recorded sender timing.

This script enables and moves the hand through the original replay. The default
uses the fixed loop; --mode original selects the saved vendor pacing baseline.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from replay_timing import SendTiming, play_deadlines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("original", "deadline"), default="deadline")
    parser.add_argument("--spin-us", type=float, default=0,
                        help="deadline mode only: optional final busy-wait window (uses additional CPU)")
    parser.add_argument("--frames", type=int, help="optional limit to the first N frames")
    parser.add_argument("--output", type=Path, default=ROOT / "build/opposition-timing/live-sender.json")
    args = parser.parse_args()
    if args.frames is not None and args.frames < 2:
        parser.error("--frames must be at least 2")
    if not 0 <= args.spin_us < 1000:
        parser.error("--spin-us must be in [0, 1000)")
    if args.mode == "original" and args.spin_us:
        parser.error("--spin-us requires --mode deadline")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    import wuji_sdk as sdk
    from wuji_macos._native import backend_info

    source = ROOT / "examples/python/wuji_hand_2/6_opposition/opposition.py"
    spec = importlib.util.spec_from_file_location("vendor_opposition", source)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    fixed_play = example._play_replay
    legacy_path = ROOT / "examples/upstream_sources/opposition.py"
    legacy_spec = importlib.util.spec_from_file_location("upstream_opposition", legacy_path)
    legacy = importlib.util.module_from_spec(legacy_spec)
    legacy_spec.loader.exec_module(legacy)
    original_play = legacy._play_replay
    report = {"mode": args.mode, "spin_us": args.spin_us, "python": sys.executable,
              "sdk": sdk.__file__, "backend": backend_info(),
              "example_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "legacy_example_sha256": hashlib.sha256(legacy_path.read_bytes()).hexdigest(),
              "timestamp_kind": "host command-construction/send start and return, not firmware arrival",
              "positions": "unchanged vendor replay", "velocity_and_effort": "unchanged zeros"}

    def measured_play(stream, frame_count, send, *, sleep=time.sleep, stop_requested=lambda: False):
        count = min(frame_count, args.frames) if args.frames else frame_count
        recorder = SendTiming(send)
        report.update(replay_path=str(Path(stream.name).resolve()), total_file_frames=frame_count,
                      requested_frames=count)
        start = time.perf_counter()
        try:
            if args.mode == "original":
                report["pacing"] = original_play(stream, count, recorder, sleep=sleep, stop_requested=stop_requested)
            elif not args.spin_us:
                report["pacing"] = fixed_play(stream, count, recorder, sleep=sleep, stop_requested=stop_requested)
            else:
                report["pacing"] = play_deadlines(stream, count, recorder, read_qpos=example._read_qpos,
                    spin_us=args.spin_us, stop_requested=stop_requested, sleep=sleep)
            # The wrapper prints its own statistics after the original
            # transaction has completed cleanup.
            return None
        finally:
            report.update(recorder.summary())
            report.update(raw_send_start_ns=recorder.started, raw_send_return_ns=recorder.finished,
                          elapsed_s=time.perf_counter() - start)

    example._play_replay = measured_play
    signal.signal(signal.SIGINT, example._on_sigint)
    print(f"Running original opposition motion with {args.mode} timing; recording to {args.output}", flush=True)
    captured_error = io.StringIO()

    class ErrorTee:
        def write(self, text):
            captured_error.write(text)
            return sys.__stderr__.write(text)

        def flush(self):
            sys.__stderr__.flush()

    try:
        with redirect_stderr(ErrorTee()):
            report["exit_code"] = result = example.main()
    finally:
        # The original run_device finally block handles disable, publisher
        # close, file close and disconnect before this report is written.
        report["error_output"] = captured_error.getvalue()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        summary = report.get("send_start")
        if summary and summary["rate_hz"] is not None:
            intervals = summary["intervals"]
            print(f"{summary['samples']} commands; {summary['rate_hz']:.1f} Hz; "
                  f"interval p50={intervals['p50_ms']:.3f} ms, p99={intervals['p99_ms']:.3f} ms, "
                  f"max={intervals['max_ms']:.3f} ms; gaps >2 ms={intervals['over_2_ms']}")
        print(f"Timing report: {args.output}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
