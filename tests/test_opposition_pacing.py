"""Deterministic pacing checks for the ordinary replay, without SDK/hardware."""
import importlib.util
import io
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "opposition_pacing_example", ROOT / "examples/python/wuji_hand_2/6_opposition/opposition.py")
opposition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opposition)


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += round(seconds * 1_000_000_000)


def replay(count):
    return io.BytesIO(b"".join(
        opposition.FRAME.pack(index, *[float(index + joint / 32) for joint in range(20)])
        for index in range(count)
    ))


class OppositionPacingTests(unittest.TestCase):
    def test_command_work_uses_period_and_every_frame_is_preserved(self):
        fake = FakeClock()
        starts, positions = [], []

        def send(qpos):
            starts.append(fake.now)
            positions.append(qpos)
            fake.now += 300_000

        summary = opposition._play_replay(replay(5), 5, send, clock=fake.clock, sleep=fake.sleep)
        self.assertEqual(starts, [0, 1_000_000, 2_000_000, 3_000_000, 4_000_000])
        self.assertEqual(positions, [tuple(float(i + j / 32) for j in range(20)) for i in range(5)])
        self.assertEqual(fake.sleeps, [0.0007] * 4)
        self.assertEqual(summary, {"sent_frames": 5, "rate_hz": 1000.0,
            "p99_interval_ms": 1.0, "max_interval_ms": 1.0, "overruns": 0})

    def test_large_sleep_overshoot_rebases_without_a_catchup_burst(self):
        fake = FakeClock()
        starts = []

        def sleep(seconds):
            fake.sleep(seconds)
            if len(fake.sleeps) == 1:
                fake.now += 2_500_000

        summary = opposition._play_replay(replay(4), 4, lambda _: starts.append(fake.now),
            clock=fake.clock, sleep=sleep)
        self.assertEqual(starts, [0, 3_500_000, 4_500_000, 5_500_000])
        self.assertEqual(summary["sent_frames"], 4)
        self.assertEqual(summary["overruns"], 1)
        self.assertEqual(summary["max_interval_ms"], 3.5)

    def test_slow_send_rebases_after_a_full_period_miss(self):
        fake = FakeClock()
        starts = []

        def send(_):
            starts.append(fake.now)
            if len(starts) == 1:
                fake.now += 2_000_000

        summary = opposition._play_replay(replay(4), 4, send, clock=fake.clock, sleep=fake.sleep)
        self.assertEqual(starts, [0, 2_000_000, 3_000_000, 4_000_000])
        self.assertEqual(summary["overruns"], 1)

    def test_early_wakeup_waits_for_the_remaining_time(self):
        fake = FakeClock()
        starts = []

        def sleep(seconds):
            fake.sleeps.append(seconds)
            fake.now += 400_000 if len(fake.sleeps) == 1 else round(seconds * 1_000_000_000)

        opposition._play_replay(replay(2), 2, lambda _: starts.append(fake.now),
            clock=fake.clock, sleep=sleep)
        self.assertEqual(starts, [0, 1_000_000])
        self.assertEqual(fake.sleeps, [0.001, 0.0006])

    def test_stop_during_wait_prevents_the_next_command(self):
        fake = FakeClock()
        positions = []

        def sleep(seconds):
            fake.sleep(seconds)

        with self.assertRaisesRegex(InterruptedError, "operator stop requested"):
            opposition._play_replay(replay(3), 3, positions.append,
                clock=fake.clock, sleep=sleep, stop_requested=lambda: bool(fake.sleeps))
        self.assertEqual(len(positions), 1)

    def test_zero_and_single_frame_have_no_sleep_or_rate_estimate(self):
        for count in (0, 1):
            with self.subTest(count=count):
                fake = FakeClock()
                positions = []
                summary = opposition._play_replay(replay(count), count, positions.append,
                    clock=fake.clock, sleep=fake.sleep)
                self.assertEqual(len(positions), count)
                self.assertEqual(fake.sleeps, [])
                self.assertIsNone(summary["rate_hz"])
                self.assertIsNone(summary["p99_interval_ms"])
                self.assertIsNone(summary["max_interval_ms"])


if __name__ == "__main__":
    unittest.main()
