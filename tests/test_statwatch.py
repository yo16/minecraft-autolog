import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autolog.statwatch import ResumeEstimator, find_stats_file, read_play_times

T0 = datetime(2026, 9, 27, 21, 33, 45)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class ResumeEstimatorTest(unittest.TestCase):
    def test_pause_then_resume(self):
        est = ResumeEstimator()
        est.on_pause(at(0))
        self.assertEqual(est.on_stats(at(0.3), 261811), [])  # 一時停止の保存
        # 46.5 秒止まって再開し、その後 300 秒遊んで次の保存
        (event,) = est.on_stats(at(0.3 + 46.5 + 300), 261811 + 300 * 20)
        self.assertEqual(event["kind"], "resume_estimated")
        self.assertEqual(event["pause_at"], "2026-09-27T21:33:45.000")
        self.assertEqual(event["time"], "2026-09-27T21:34:31.800")
        self.assertEqual(event["paused_s"], 46.8)

    def test_stats_read_before_pause_line(self):
        # ログ行は秒の切り捨てで、ファイル更新を先に読むこともある
        est = ResumeEstimator()
        est.on_stats(at(0.4), 1000)
        est.on_pause(at(0))
        (event,) = est.on_stats(at(100.4), 1000 + 40 * 20)
        self.assertEqual(event["paused_s"], 60.4)

    def test_still_paused_save_does_not_resume(self):
        est = ResumeEstimator()
        est.on_pause(at(0))
        est.on_stats(at(0.2), 5000)
        self.assertEqual(est.on_stats(at(30), 5000), [])

    def test_two_pauses_back_to_back(self):
        # 1 回目の停止 → 再開 → 2 回目の停止。2 回目の保存で 1 回目の解除時刻が分かる
        est = ResumeEstimator()
        est.on_pause(at(0))
        est.on_stats(at(0.2), 5000)
        est.on_pause(at(80))  # 20 秒止まり、60 秒遊んで再び停止
        (event,) = est.on_stats(at(80.2), 5000 + 60 * 20)
        self.assertEqual(event["paused_s"], 20.2)
        # 2 回目の停止は 2 回目の保存と対応づいており、次の保存で解除が分かる
        (event2,) = est.on_stats(at(200.2), 5000 + 60 * 20 + 100 * 20)
        self.assertEqual(event2["pause_at"], "2026-09-27T21:35:05.000")
        self.assertEqual(event2["paused_s"], 20.2)

    def test_resume_never_before_pause(self):
        est = ResumeEstimator()
        est.on_pause(at(0))
        est.on_stats(at(0.2), 5000)
        (event,) = est.on_stats(at(5), 5000 + 10 * 20)  # 経過より多く増えた(時計のずれなど)
        self.assertEqual(event["paused_s"], 0.0)

    def test_autosave_without_pause(self):
        est = ResumeEstimator()
        self.assertEqual(est.on_stats(at(0), 100), [])
        self.assertEqual(est.on_stats(at(300), 6100), [])


class StatsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saves = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_stats(self, world: str, play: int, total: int) -> Path:
        folder = self.saves / world / "players" / "stats"
        folder.mkdir(parents=True)
        path = folder / "dcc3a910-0ebb-41b1-a762-1acfe6dc070f.json"
        data = {"stats": {"minecraft:custom": {"minecraft:play_time": play, "minecraft:total_world_time": total}}, "DataVersion": 1}
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_read(self):
        path = self.write_stats("20260920", 225311, 499804)
        self.assertEqual(read_play_times(path), (225311, 499804))

    def test_find_by_world_and_fallback(self):
        a = self.write_stats("20260724", 1, 1)
        b = self.write_stats("20260920", 2, 2)
        self.assertEqual(find_stats_file(self.saves, "20260724"), a)
        newest = max([a, b], key=lambda p: p.stat().st_mtime)
        self.assertEqual(find_stats_file(self.saves, "存在しない"), newest)
        self.assertEqual(find_stats_file(self.saves, None), newest)

    def test_broken_json(self):
        path = self.write_stats("w", 1, 1)
        path.write_text('{"stats": {', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_play_times(path)


if __name__ == "__main__":
    unittest.main()
