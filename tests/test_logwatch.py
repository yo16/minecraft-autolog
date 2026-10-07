import unittest
from datetime import datetime

from autolog.logwatch import LogParser, resolve_time, screenshot_time

NOW = datetime(2026, 9, 27, 21, 30, 0)


def feed_all(parser: LogParser, lines: list[str], now: datetime = NOW) -> list[dict]:
    events = []
    for line in lines:
        events.extend(parser.feed(line, now))
    return events


class ResolveTimeTest(unittest.TestCase):
    def test_same_day(self):
        self.assertEqual(resolve_time(21, 5, 12, NOW), datetime(2026, 9, 27, 21, 5, 12))

    def test_line_before_midnight_read_after_midnight(self):
        now = datetime(2026, 9, 28, 0, 0, 30)
        self.assertEqual(resolve_time(23, 59, 58, now), datetime(2026, 9, 27, 23, 59, 58))

    def test_slightly_ahead_is_same_day(self):
        # PC の時計と数秒ずれている程度なら前日にしない
        self.assertEqual(resolve_time(21, 30, 5, NOW), datetime(2026, 9, 27, 21, 30, 5))


class ScreenshotTimeTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(screenshot_time("2026-09-27_21.12.40.png"), datetime(2026, 9, 27, 21, 12, 40))

    def test_duplicate_suffix(self):
        self.assertEqual(screenshot_time("2026-09-27_21.12.40_1.png"), datetime(2026, 9, 27, 21, 12, 40))

    def test_unknown(self):
        self.assertIsNone(screenshot_time("screenshot.png"))


class LogParserTest(unittest.TestCase):
    # 2026-09-26 の実際の latest.log から抜粋
    SESSION = [
        "[22:27:07] [Render thread/INFO]: Setting user: greenbanana17",
        "[22:27:15] [Worker-Main-15/INFO]: Loaded 1866 advancements",
        "[22:27:15] [Server thread/INFO]: Starting integrated minecraft server version 26.3",
        "[22:27:16] [Server thread/INFO]: Saving chunks for level 'ServerLevel[20260920]'/minecraft:overworld",
        "[22:27:16] [Server thread/INFO]: Saving chunks for level 'ServerLevel[20260920]'/minecraft:the_nether",
        "[22:27:18] [Server thread/INFO]: Saving and pausing game...",
        "[22:27:18] [Server thread/INFO]: Saving chunks for level 'ServerLevel[20260920]'/minecraft:overworld",
        "[22:34:39] [Server thread/INFO]: Stopping server",
        "[22:34:39] [Server thread/INFO]: Stopping singleplayer server as player logged out",
        "[22:34:40] [Server thread/INFO]: Saving chunks for level 'ServerLevel[20260920]'/minecraft:overworld",
        "[22:35:00] [Render thread/INFO]: Stopping!",
    ]

    def test_session_lines(self):
        parser = LogParser()
        now = datetime(2026, 9, 26, 22, 40)
        events = feed_all(parser, self.SESSION, now)
        self.assertEqual([e["kind"] for e in events], ["login", "world_start", "pause", "world_stop"])
        start = events[1]
        self.assertEqual(start["time"], "2026-09-26T22:27:15.000")
        self.assertEqual((start["version"], start["world"], start["player"]), ("26.3", "20260920", "greenbanana17"))
        self.assertEqual(events[3]["detail"], "Stopping server")
        self.assertEqual(parser.state(), {"player": "greenbanana17", "version": "26.3", "world": "20260920", "world_open": False})

    def test_screenshot_uses_file_time(self):
        line = "[21:12:41] [Render thread/INFO]: [CHAT] スクリーンショットを2026-09-27_21.12.40.pngとして保存しました"
        (event,) = LogParser().feed(line, NOW)
        self.assertEqual(event, {
            "time": "2026-09-27T21:12:40.000",
            "kind": "screenshot",
            "file": "2026-09-27_21.12.40.png",
            "logged_at": "2026-09-27T21:12:41.000",
        })

    def test_screenshot_english(self):
        line = "[21:12:41] [Render thread/INFO]: [CHAT] Saved screenshot as 2026-09-27_21.12.40.png"
        (event,) = LogParser().feed(line, NOW)
        self.assertEqual(event["file"], "2026-09-27_21.12.40.png")

    def test_advancement_kinds(self):
        parser = LogParser()
        lines = [
            "[21:20:03] [Render thread/INFO]: [CHAT] greenbanana17が進捗[石器時代]を達成しました",
            "[21:20:04] [Render thread/INFO]: [CHAT] greenbanana17が挑戦[モンスターハンター]を完了しました",
            "[21:20:05] [Render thread/INFO]: [CHAT] greenbanana17 has reached the goal [Sky's the Limit]",
        ]
        events = feed_all(parser, lines)
        self.assertEqual([e["kind"] for e in events], ["advancement"] * 3)
        self.assertEqual([e["frame"] for e in events], ["task", "challenge", "goal"])
        self.assertEqual(events[0]["name"], "石器時代")
        self.assertEqual(events[2]["player"], "greenbanana17")

    def test_death_and_not_death(self):
        parser = LogParser()
        lines = [
            "[21:00:00] [Render thread/INFO]: Setting user: greenbanana17",
            "[21:30:00] [Render thread/INFO]: [CHAT] greenbanana17は高い所から落ちた",
            "[21:30:01] [Render thread/INFO]: [CHAT] greenbanana17がゲームに参加しました",
            "[21:30:02] [Render thread/INFO]: [CHAT] greenbanana17 was slain by Zombie",
            "[21:30:03] [Render thread/INFO]: [CHAT] greenbanana17 joined the game",
            "[21:30:04] [Render thread/INFO]: [CHAT] リスポーン地点を設定しました",
            "[21:30:05] [Render thread/INFO]: [CHAT] <greenbanana17> こんにちは",
        ]
        kinds = [e["kind"] for e in feed_all(parser, lines)]
        self.assertEqual(kinds, ["login", "death", "system_chat", "death", "system_chat", "system_chat", "chat"])

    def test_death_needs_player(self):
        (event,) = LogParser().feed("[21:30:00] [Render thread/INFO]: [CHAT] greenbanana17は溺れた", NOW)
        self.assertEqual(event["kind"], "system_chat")

    def test_stop_without_world_is_ignored(self):
        # タイトル画面から終了したときの "Stopping!" は world_stop にしない
        self.assertEqual(LogParser().feed("[21:30:00] [Render thread/INFO]: Stopping!", NOW), [])

    def test_unrelated_lines(self):
        parser = LogParser()
        self.assertEqual(parser.feed("[21:30:00] [Render thread/INFO]: Loaded 262 advancements", NOW), [])
        self.assertEqual(parser.feed("not a log line", NOW), [])


if __name__ == "__main__":
    unittest.main()
