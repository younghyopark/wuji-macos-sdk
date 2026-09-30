"""Deadline scheduling behavior with a deterministic clock and inert frames."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from replay_timing import play_deadlines


class ReplayDeadlineTests(unittest.TestCase):
    def test_accounts_for_send_time_and_preserves_every_frame(self):
        now = [0]
        starts, sent = [], []

        def sleep(seconds):
            now[0] += round(seconds * 1e9)

        def send(frame):
            starts.append(now[0])
            sent.append(frame)
            now[0] += 250_000

        frames = iter([(i,) * 20 for i in range(4)])
        result = play_deadlines(frames, 4, send, read_qpos=next,
                                clock=lambda: now[0], sleep=sleep)
        self.assertEqual(starts, [0, 1_000_000, 2_000_000, 3_000_000])
        self.assertEqual(sent, [(i,) * 20 for i in range(4)])
        self.assertEqual(result["skipped_frames"], 0)

    def test_full_period_overrun_shifts_future_schedule_without_catchup_burst(self):
        now = [0]
        starts = []

        def sleep(seconds):
            now[0] += round(seconds * 1e9)

        def send(frame):
            starts.append(now[0])
            now[0] += 3_500_000 if frame == 0 else 100_000

        result = play_deadlines(iter(range(4)), 4, send, read_qpos=next,
                                clock=lambda: now[0], sleep=sleep)
        self.assertEqual(starts, [0, 3_500_000, 4_500_000, 5_500_000])
        self.assertEqual(result["schedule_resets"], 1)
        self.assertEqual(result["schedule_shift_ms"], 2.5)

    def test_stop_during_wait_sends_no_additional_frame(self):
        now = [0]
        stopped = [False]
        sent = []

        def sleep(seconds):
            now[0] += round(seconds * 1e9)
            stopped[0] = True

        with self.assertRaises(InterruptedError):
            play_deadlines(iter(range(4)), 4, sent.append, read_qpos=next,
                           clock=lambda: now[0], sleep=sleep, stop_requested=lambda: stopped[0])
        self.assertEqual(sent, [0])


if __name__ == "__main__":
    unittest.main()
