"""Timing measurement and deadline pacing shared by replay diagnostics."""
from __future__ import annotations

import time


def distribution_ns(values):
    values = sorted(values)
    if not values:
        return None

    def percentile(q):
        position = (len(values) - 1) * q
        index = int(position)
        fraction = position - index
        return (values[index] * (1 - fraction) + values[min(index + 1, len(values) - 1)] * fraction) / 1e6

    return {"count": len(values), "mean_ms": sum(values) / len(values) / 1e6,
            "min_ms": values[0] / 1e6, "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "p99_ms": percentile(.99), "max_ms": values[-1] / 1e6,
            "over_1_5_ms": sum(v > 1_500_000 for v in values),
            "over_2_ms": sum(v > 2_000_000 for v in values),
            "over_5_ms": sum(v > 5_000_000 for v in values),
            "under_0_5_ms": sum(v < 500_000 for v in values)}


def cadence(timestamps):
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    span = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
    return {"samples": len(timestamps), "span_s": span / 1e9,
            "rate_hz": (len(timestamps) - 1) * 1e9 / span if span else None,
            "intervals": distribution_ns(intervals)}


class SendTiming:
    def __init__(self, send):
        self.send = send
        self.started = []
        self.finished = []

    def __call__(self, qpos):
        self.started.append(time.perf_counter_ns())
        try:
            return self.send(qpos)
        finally:
            self.finished.append(time.perf_counter_ns())

    def summary(self):
        return {"send_start": cadence(self.started), "send_return": cadence(self.finished),
                "command_construction_and_send": distribution_ns([b - a for a, b in zip(self.started, self.finished)])}


def play_deadlines(stream, frame_count, send, *, read_qpos, frequency_hz=1000,
                   spin_us=0, stop_requested=lambda: False,
                   clock=time.perf_counter_ns, sleep=time.sleep):
    """Preserve every frame; shift the schedule after a full-period overrun.

    No frames are skipped and no accumulated catch-up burst is sent after a
    missed period. A small optional final spin trades CPU for tighter wakeups.
    This is best-effort pacing, not a real-time scheduling guarantee.
    """
    if not 0 < frequency_hz <= 1000:
        raise ValueError("frequency_hz must be in (0, 1000]")
    period = round(1e9 / frequency_hz)
    spin = round(spin_us * 1000)
    if not 0 <= spin < period:
        raise ValueError("spin_us must be nonnegative and shorter than one period")
    anchor = clock()
    resets, shifted_ns, lateness = 0, 0, []
    for index in range(frame_count):
        qpos = read_qpos(stream)
        due = anchor + index * period
        while True:
            if stop_requested():
                raise InterruptedError("operator stop requested")
            remaining = due - clock()
            if remaining <= 0:
                break
            if remaining > spin:
                sleep((remaining - spin) / 1e9)
        late = max(0, clock() - due)
        lateness.append(late)
        if late >= period:
            anchor += late
            shifted_ns += late
            resets += 1
        send(qpos)
    return {"frequency_hz": frequency_hz, "spin_us": spin_us,
            "schedule_resets": resets, "schedule_shift_ms": shifted_ns / 1e6,
            "lateness": distribution_ns(lateness), "skipped_frames": 0}
