#!/usr/bin/env python3
"""Passively measure a Hand 2 joint-command topic with eclipse-zenoh.

This observer only declares a subscriber. It never imports wuji_sdk, publishes,
queries device resources, or enables motors. Times are observer Python callback
times, not arrival times at the hand's firmware. Routing can make a separate
observer unable to see commands; zero samples is inconclusive.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import threading
import time


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SN = "WH2KA01260825022"


def decode_sequence(payload):
    """Validate the schema-315 Postcard length and decode its u32 sequence."""
    value = 0
    for index, byte in enumerate(payload[:5]):
        if index == 4 and byte > 0x0F:
            raise ValueError("sequence exceeds u32")
        value |= (byte & 0x7F) << (7 * index)
        if byte < 0x80:
            if index and byte == 0:
                raise ValueError("sequence varint is not canonical")
            if len(payload) != index + 1 + 20 * 3 * 4:
                raise ValueError("payload is not a schema-315 Postcard frame")
            return value
    raise ValueError("sequence varint is incomplete")


def interval_summary(timestamps_ns):
    """Summarize adjacent observer callback intervals using linear quantiles."""
    intervals = [(b - a) / 1_000_000 for a, b in zip(timestamps_ns, timestamps_ns[1:])]
    if not intervals:
        return {"interval_count": 0, "mean_rate_hz": None}
    ordered = sorted(intervals)

    def quantile(fraction):
        index = (len(ordered) - 1) * fraction
        low = int(index)
        return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)

    duration_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1_000_000_000
    return {
        "interval_count": len(intervals),
        "mean_rate_hz": len(intervals) / duration_s if duration_s > 0 else None,
        "min_ms": ordered[0], "mean_ms": statistics.fmean(intervals),
        "stdev_ms": statistics.pstdev(intervals),
        "p50_ms": quantile(0.5), "p95_ms": quantile(0.95),
        "p99_ms": quantile(0.99), "p999_ms": quantile(0.999),
        "max_ms": ordered[-1],
        "intervals_below_0_5_ms": sum(value < 0.5 for value in intervals),
        "intervals_above_1_5_ms": sum(value > 1.5 for value in intervals),
        "intervals_above_2_ms": sum(value > 2 for value in intervals),
        "intervals_above_5_ms": sum(value > 5 for value in intervals),
    }


def sequence_summary(records):
    """Classify observed seq transitions; gaps do not prove device-side loss."""
    valid = [record for record in records if record["sequence"] is not None]
    counts = {"valid_frames": len(valid), "contiguous_transitions": 0,
              "forward_gap_events": 0, "missing_sequence_values": 0,
              "duplicate_transitions": 0, "backward_or_restart_events": 0,
              "u32_wrap_transitions": 0}
    events = []
    for previous, current in zip(valid, valid[1:]):
        before, after = previous["sequence"], current["sequence"]
        delta = (after - before) & 0xFFFFFFFF
        if delta == 1:
            counts["contiguous_transitions"] += 1
            if after < before:
                counts["u32_wrap_transitions"] += 1
            continue
        if delta == 0:
            kind = "duplicate"
            counts["duplicate_transitions"] += 1
        elif delta < 0x80000000:
            kind = "forward_gap"
            counts["forward_gap_events"] += 1
            counts["missing_sequence_values"] += delta - 1
        else:
            kind = "backward_or_restart"
            counts["backward_or_restart_events"] += 1
        events.append({"kind": kind, "before": before, "after": after,
                       "callback_monotonic_ns": current["callback_monotonic_ns"]})
    return {**counts, "events": events}


class CommandObserver:
    """Keep callback work small; format and calculate statistics after capture."""

    def __init__(self):
        self._records = []
        self._lock = threading.Lock()

    def callback(self, sample):
        timestamp_ns = time.monotonic_ns()
        payload = b""
        try:
            payload = bytes(sample.payload)
            sequence = decode_sequence(payload)
            error = None
        except Exception as exc:
            sequence = None
            error = str(exc)
        record = {"callback_monotonic_ns": timestamp_ns,
                  "payload_length": len(payload), "sequence": sequence}
        if error:
            record["decode_error"] = error
        with self._lock:
            self._records.append(record)

    def records(self):
        with self._lock:
            return list(self._records)


def observer_config(zenoh, endpoint):
    config = zenoh.Config()
    for key, value in {
        # The physical Hand 2 accepted a peer session; a client session timed
        # out against the same live endpoint. Discovery remains disabled.
        "mode": "peer", "connect/endpoints": [endpoint],
        "connect/timeout_ms": 10000, "connect/exit_on_failure": True,
        "listen/endpoints": [], "scouting/multicast/enabled": False,
        "scouting/gossip/enabled": False, "transport/shared_memory/enabled": False,
    }.items():
        config.insert_json5(key, json.dumps(value))
    return config


def monitor(*, address, protocol, sn, seconds, output):
    try:
        import zenoh
    except ImportError as exc:
        raise RuntimeError(
            "This optional observer needs eclipse-zenoh. From the repository use "
            ".venv-verify/bin/python examples/macos/monitor_joint_commands.py, or "
            "install eclipse-zenoh==1.9.0 into your chosen environment."
        ) from exc
    endpoint = f"{protocol}/{address}"
    key = f"wuji/{sn}/joint_command"
    observer = CommandObserver()
    opened_utc = datetime.now(timezone.utc).isoformat()
    session = zenoh.open(observer_config(zenoh, endpoint))
    subscriber = None
    interrupted = False
    started_ns = None
    try:
        subscriber = session.declare_subscriber(key, observer.callback)
        started_ns = time.monotonic_ns()
        print(f"READY observing {key} via {endpoint} for {seconds:g} s. "
              "Callback times measure this observer, not firmware arrival.", flush=True)
        deadline_ns = started_ns + int(seconds * 1_000_000_000)
        while True:
            remaining_s = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining_s <= 0:
                break
            time.sleep(min(remaining_s, 0.25))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        try:
            if subscriber is not None:
                subscriber.undeclare()
        finally:
            session.close()
    ended_ns = time.monotonic_ns()
    records = observer.records()
    timestamps = [record["callback_monotonic_ns"] for record in records]
    report = {
        "kind": "passive_zenoh_joint_command_observer", "endpoint": endpoint,
        "key": key, "opened_utc": opened_utc, "requested_seconds": seconds,
        "capture_started_monotonic_ns": started_ns, "closed_monotonic_ns": ended_ns,
        "interrupted": interrupted, "sample_count": len(records),
        "result": "samples_observed" if records else "inconclusive_no_samples",
        "timestamp_meaning": "Python observer callback entry; not hand firmware arrival or publisher send time",
        "limitations": [
            "This subscriber adds a connection and network traffic and can perturb the measured system.",
            "Zenoh routing may not deliver another peer's publications to this observer; zero samples is inconclusive.",
            "Callback spacing includes this observer's scheduling and GIL delays.",
            "Sequence gaps are missing observations, not proof the hand missed commands.",
            "Backward sequences may be reordering, publisher restart, or multiple publishers; no source is inferred.",
            "Only schema-315 Postcard frames with a u32 sequence and 60 float32 values are decoded.",
        ],
        "payload_length_counts": dict(Counter(record["payload_length"] for record in records)),
        "decode_errors": dict(Counter(record["decode_error"] for record in records if "decode_error" in record)),
        "callback_intervals": interval_summary(timestamps),
        "sequence": sequence_summary(records), "samples": records,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    if records:
        intervals = report["callback_intervals"]
        rate = intervals["mean_rate_hz"]
        if rate is not None:
            print(f"Observed {len(records)} samples: {rate:.1f} Hz mean; "
                  f"callback p99 {intervals['p99_ms']:.3f} ms; max {intervals['max_ms']:.3f} ms.")
        else:
            print(f"Observed {len(records)} sample; more are needed to estimate cadence.")
    else:
        print("Observed zero samples: inconclusive. The topic may be idle or not routed to this observer.")
    print(f"Report: {output}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="192.168.1.111:7447")
    parser.add_argument("--protocol", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--sn", default=DEFAULT_SN)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "build/opposition-timing/live-observer.json")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.sn):
        parser.error("--sn must be an exact serial number, without slashes or wildcards")
    if "/" in args.address or not args.address:
        parser.error("--address must be host:port without a protocol prefix")
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600:
        parser.error("--seconds must be greater than zero and at most 3600")
    try:
        monitor(**vars(args))
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
