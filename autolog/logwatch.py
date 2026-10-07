"""latest.log の監視(poc/tail_log.py から)。

ファイルは開きっぱなしにせず、0.5 秒ごとに開いて前回位置から読んで閉じる。
Minecraft は起動時に latest.log をリネームして作り直すので、掴みっぱなしだとそれを妨げるため。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .session import Event, EventBus, iso

log = logging.getLogger(__name__)

LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] \[([^\]]*)/(\w+)\]: (.*)$")
SCREENSHOT_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")
SERVER_LEVEL = re.compile(r"ServerLevel\[(?P<world>.+?)\]")

CHAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("screenshot", re.compile(r"^(?:スクリーンショットを(?P<file>.+?)として保存しました|Saved screenshot as (?P<file_en>.+))$")),
    ("chat", re.compile(r"^<(?P<player>[^>]+)> (?P<text>.*)$")),
    ("advancement", re.compile(
        r"^(?:(?P<player>.+?)が(?P<frame>進捗|目標|挑戦)\[(?P<name>.+?)\]を(?:達成|完了)しました"
        r"|(?P<player_en>.+?) has (?P<frame_en>made the advancement|reached the goal|completed the challenge) \[(?P<name_en>.+?)\])$"
    )),
]
FRAMES = {
    "進捗": "task", "目標": "goal", "挑戦": "challenge",
    "made the advancement": "task", "reached the goal": "goal", "completed the challenge": "challenge",
}
# 英語 UI でプレイヤー名から始まるが死亡ではないもの
NOT_DEATH_EN = (" joined the game", " left the game", " has made", " has reached", " has completed")

PAUSE = re.compile(r"Saving and pausing game")
WORLD_START = re.compile(r"Starting integrated minecraft server version (?P<version>.+)")
WORLD_STOP = re.compile(r"^(?P<detail>Stopping server|Stopping!)$")
LOGIN = re.compile(r"^Setting user: (?P<player>.+)$")


def resolve_time(hour: int, minute: int, second: int, now: datetime) -> datetime:
    """ログ行の時刻に日付を補う。現在より 1 時間以上先なら前日の行とみなす(0 時またぎ)。"""
    t = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if t - now > timedelta(hours=1):
        t -= timedelta(days=1)
    return t


def screenshot_time(file_name: str) -> datetime | None:
    """F2 のファイル名 "2026-09-27_21.12.40.png"(重複時は "_1" 付き)から撮影時刻を取る。"""
    m = SCREENSHOT_NAME.match(file_name)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _fields(m: re.Match[str]) -> dict[str, str]:
    return {k.removesuffix("_en"): v for k, v in m.groupdict().items() if v is not None}


class LogParser:
    """latest.log の行を順に受けてイベントにする。プレイヤー名・バージョン・ワールド名を状態として持つ。"""

    def __init__(self) -> None:
        self.player: str | None = None
        self.version: str | None = None
        self.world: str | None = None
        self.world_open = False
        self._pending_start: Event | None = None  # ワールド名(ServerLevel[...])が出るまで待つ world_start

    def state(self) -> dict[str, Any]:
        return {"player": self.player, "version": self.version, "world": self.world, "world_open": self.world_open}

    def feed(self, line: str, now: datetime) -> list[Event]:
        m = LINE.match(line)
        if not m:
            return []
        hh, mm, ss, _thread, _level, message = m.groups()
        t = resolve_time(int(hh), int(mm), int(ss), now)
        if message.startswith("[CHAT] "):
            return [self._chat(message.removeprefix("[CHAT] "), t)]
        return self._system(message, t)

    def _chat(self, text: str, t: datetime) -> Event:
        for kind, pattern in CHAT_PATTERNS:
            m = pattern.match(text)
            if not m:
                continue
            fields = _fields(m)
            if kind == "screenshot":
                shot = screenshot_time(fields["file"])
                return {"time": iso(shot or t), "kind": kind, "file": fields["file"], "logged_at": iso(t)}
            if kind == "advancement":
                fields["frame"] = FRAMES[fields["frame"]]
            return {"time": iso(t), "kind": kind, **fields}
        if self._is_death(text):
            return {"time": iso(t), "kind": "death", "player": self.player, "message": text}
        return {"time": iso(t), "kind": "system_chat", "text": text}

    def _is_death(self, text: str) -> bool:
        """死亡メッセージの推定: 自分の名前で始まり、「は」(日本語)か空白(英語)が続くもの。"""
        if not self.player or not text.startswith(self.player):
            return False
        rest = text[len(self.player):]
        if rest.startswith("は"):
            return True
        return rest.startswith(" ") and not rest.startswith(NOT_DEATH_EN)

    def _system(self, message: str, t: datetime) -> list[Event]:
        events: list[Event] = []
        if PAUSE.search(message):
            events.append({"time": iso(t), "kind": "pause"})
        elif m := WORLD_START.search(message):
            self.version = m["version"].strip()
            self._pending_start = {"time": iso(t), "kind": "world_start", "version": self.version}
        elif m := WORLD_STOP.match(message):
            if self.world_open:
                self.world_open = False
                events.append({"time": iso(t), "kind": "world_stop", "detail": m["detail"]})
        elif m := LOGIN.match(message):
            self.player = m["player"].strip()
            events.append({"time": iso(t), "kind": "login", "player": self.player})
        if self._pending_start is not None and (m := SERVER_LEVEL.search(message)):
            self.world = m["world"]
            self.world_open = True
            start = self._pending_start
            self._pending_start = None
            start["world"] = self.world
            if self.player:
                start["player"] = self.player
            events.append(start)
        return events


def read_new(path: Path, pos: int) -> tuple[list[str], int]:
    """pos 以降を読んで (行リスト, 新しい pos) を返す。途中で切れた最終行は次回に回す。"""
    with path.open("rb") as f:
        f.seek(pos)
        data = f.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], pos
    return data[: cut + 1].decode("utf-8", errors="replace").splitlines(), pos + cut + 1


def file_identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_ino, st.st_dev


class LogWatcher:
    """latest.log を追いかけてイベントを EventBus に出すスレッド。"""

    def __init__(self, path: Path, bus: EventBus, poll_s: float = 0.5):
        self.path = path
        self.bus = bus
        self.poll_s = poll_s
        self.parser = LogParser()
        self._identity: tuple[int, int] | None = None
        self._pos = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="logwatch", daemon=True)

    def restore(self) -> None:
        """既存の latest.log を全部読んで状態(プレイヤー名・バージョン・ワールド名)だけ復元する。イベントは出さない。"""
        if not self.path.exists():
            return
        self._identity = file_identity(self.path)
        now = datetime.fromtimestamp(self.path.stat().st_mtime)
        lines, self._pos = read_new(self.path, 0)
        for line in lines:
            self.parser.feed(line, now)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except OSError as e:
                log.debug("latest.log を読めませんでした: %s", e)
            except Exception:
                log.exception("logwatch で例外")
                self.bus.emit({"kind": "warning", "message": "latest.log の解析で例外が起きました(autolog.log 参照)"})
            self._stop.wait(self.poll_s)

    def _poll(self) -> None:
        if not self.path.exists():
            return
        current = file_identity(self.path)
        if current != self._identity or self.path.stat().st_size < self._pos:
            if self._identity is not None:
                print("latest.log が作り直されました(Minecraft の起動)。先頭から読みます", flush=True)
            self._identity, self._pos = current, 0
            self.parser = LogParser()
        lines, self._pos = read_new(self.path, self._pos)
        now = datetime.now()
        for line in lines:
            for event in self.parser.feed(line, now):
                self.bus.emit(event)
