import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autolog.config import resolve_season
from autolog.session import EventBus, Session, SessionSummary


class SessionSummaryTest(unittest.TestCase):
    def test_counts_and_play_time(self):
        s = SessionSummary()
        for event in [
            {"kind": "minecraft_state", "player": "greenbanana17", "world": "20260920"},
            {"kind": "stats", "world": "20260920", "play_time": 225311, "total_world_time": 499804},
            {"kind": "frame"},
            {"kind": "frame"},
            {"kind": "pause"},
            {"kind": "screenshot"},
            {"kind": "world_start", "version": "26.3", "world": "20260920"},
            {"kind": "stats", "world": "20260920", "play_time": 345551, "total_world_time": 650224},
        ]:
            s.add(event)
        self.assertEqual(s.counts["frames"], 2)
        self.assertEqual(s.counts["pauses"], 1)
        self.assertEqual(s.counts["screenshots"], 1)
        self.assertEqual(s.minecraft, {"player": "greenbanana17", "world": "20260920", "version": "26.3"})
        self.assertEqual(s.play_time_s, 6012)
        self.assertEqual(s.world_open_s, 7521)
        block = s.time_block()
        self.assertEqual(block["stats_first"], {"play_time": 225311, "total_world_time": 499804})
        self.assertNotIn("worlds", block)

    def test_world_switch_adds_increments(self):
        s = SessionSummary()
        for world, play in [("a", 0), ("a", 2000), ("b", 10000), ("b", 10400)]:
            s.add({"kind": "stats", "world": world, "play_time": play, "total_world_time": play})
        self.assertEqual(s.play_time_s, 120)
        self.assertIn("worlds", s.time_block())


class SessionWriterTest(unittest.TestCase):
    def test_events_and_session_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            season_dir = Path(tmp) / "season02"
            bus = EventBus()
            bus.emit({"kind": "login", "player": "greenbanana17"})  # セッション前のイベントも渡る
            session = Session(season_dir, datetime(2026, 9, 27, 21, 3, 14), {"hwnd": 1}, {"capture_interval_s": 5})
            session.start()
            bus.attach(session)
            bus.emit({"kind": "stats", "world": "w", "play_time": 0, "total_world_time": 0})
            bus.emit({"kind": "stats", "world": "w", "play_time": 1200, "total_world_time": 1400})
            bus.detach()
            end = session.close()

            self.assertEqual(session.dir, season_dir / "sessions" / "2026-09-27_21.03.14")
            lines = [json.loads(l) for l in session.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["kind"] for e in lines], ["session_start", "login", "stats", "stats", "session_end"])
            self.assertEqual(lines[0]["season"], "season02")
            self.assertEqual(end["play_time_s"], 60)
            self.assertEqual(end["world_open_s"], 70)
            data = json.loads((session.dir / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(data["end"], end["time"])
            self.assertEqual(data["minecraft"]["player"], "greenbanana17")
            self.assertEqual(data["time"]["play_time_s"], 60)

    def test_same_second_gets_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = datetime(2026, 9, 27, 21, 3, 14)
            first = Session(Path(tmp), started, {}, {})
            first.start()
            first.close()
            second = Session(Path(tmp), started, {}, {})
            self.assertEqual(second.id, "2026-09-27_21.03.14_2")


class ResolveSeasonTest(unittest.TestCase):
    def test_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "log").mkdir()
            self.assertEqual(resolve_season("", log_dir, "20260920"), "world-20260920")
            (log_dir / "log" / "import_state.json").write_text('{"current_season": "season02"}', encoding="utf-8")
            self.assertEqual(resolve_season("", log_dir, "20260920"), "season02")
            self.assertEqual(resolve_season("season09", log_dir, "20260920"), "season09")
        self.assertEqual(resolve_season("", None, None), "unknown")


if __name__ == "__main__":
    unittest.main()
