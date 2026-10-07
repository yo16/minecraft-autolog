"""セッションフォルダ、イベントの受け渡し(EventBus)、writer スレッド、session.json。

各モジュールは EventBus.emit() にイベント(dict)を渡すだけ。セッション中なら
そのセッションのキューに入り、writer スレッドだけが events.jsonl に追記する。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

Event = dict[str, Any]
TICKS_PER_SECOND = 20
STATUS_INTERVAL_S = 60
SESSION_JSON_INTERVAL_S = 300


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def session_id(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d_%H.%M.%S")


class EventBus:
    """イベントの集約点。セッションが無い間のイベントは少しだけ溜め、セッション開始時に渡す。

    Minecraft は窓より先に latest.log を書き始める(ログイン行など)ので、
    窓の検出 = セッション開始より少し前のイベントも拾えるようにする。
    """

    def __init__(self, prebuffer_s: float = 120):
        self._lock = threading.Lock()
        self._session: Session | None = None
        self._buffer: deque[tuple[float, Event]] = deque()
        self._prebuffer_s = prebuffer_s
        self._listeners: list[Callable[[Event], None]] = []

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """全イベントを受け取る関数を登録する(セッションの有無に関係なく、emit したスレッドで呼ばれる)。"""
        self._listeners.append(listener)

    def emit(self, event: Event) -> None:
        event.setdefault("time", now_iso())
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                log.exception("イベントの購読者で例外: %s", event.get("kind"))
        with self._lock:
            if self._session is not None:
                self._session.put(event)
                return
            now = time.monotonic()
            self._buffer.append((now, event))
            while self._buffer and now - self._buffer[0][0] > self._prebuffer_s:
                self._buffer.popleft()

    def attach(self, session: Session) -> None:
        with self._lock:
            for _, event in self._buffer:
                session.put(event)
            self._buffer.clear()
            self._session = session

    def detach(self) -> None:
        with self._lock:
            self._session = None


class SessionSummary:
    """イベントを順に受けてセッションの集計を作る(writer スレッドから使う。Minecraft なしでテストできる)。"""

    COUNTED = {
        "frame": "frames",
        "note": "notes",
        "screenshot": "screenshots",
        "advancement": "advancements",
        "death": "deaths",
        "pause": "pauses",
    }

    def __init__(self) -> None:
        self.counts = {name: 0 for name in self.COUNTED.values()}
        self.minecraft: dict[str, str] = {}
        # ワールドごとの最初と最後の統計。途中でワールドを切り替えても増分を足せるように分ける
        self.stats: dict[str, dict[str, dict[str, int]]] = {}
        self._first_world: str | None = None

    def add(self, event: Event) -> None:
        kind = event.get("kind")
        if kind in self.COUNTED:
            self.counts[self.COUNTED[kind]] += 1
        if kind == "world_start":
            for key in ("version", "world"):
                if event.get(key):
                    self.minecraft[key] = event[key]
        if kind in ("login", "world_start", "minecraft_state") and event.get("player"):
            self.minecraft["player"] = event["player"]
        if kind == "minecraft_state":
            for key in ("version", "world"):
                if event.get(key) and key not in self.minecraft:
                    self.minecraft[key] = event[key]
        if kind == "stats":
            world = event.get("world") or "?"
            sample = {"play_time": int(event["play_time"]), "total_world_time": int(event["total_world_time"])}
            entry = self.stats.setdefault(world, {"first": sample})
            entry["last"] = sample
            if self._first_world is None:
                self._first_world = world

    def _increment_s(self, key: str) -> int:
        ticks = sum(max(0, e["last"][key] - e["first"][key]) for e in self.stats.values())
        return round(ticks / TICKS_PER_SECOND)

    @property
    def play_time_s(self) -> int:
        return self._increment_s("play_time")

    @property
    def world_open_s(self) -> int:
        return self._increment_s("total_world_time")

    def time_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {"play_time_s": self.play_time_s, "world_open_s": self.world_open_s}
        if self._first_world is not None:
            first = self.stats[self._first_world]
            block["stats_first"] = first["first"]
            block["stats_last"] = first["last"]
        if len(self.stats) > 1:
            block["worlds"] = self.stats
        return block


def describe(event: Event) -> str:
    """コンソール表示用の 1 行。"""
    detail = {k: v for k, v in event.items() if k not in ("time", "kind", "thread", "level")}
    text = " ".join(f"{k}={v}" for k, v in detail.items())
    return f"{event['time'][11:19]}  {event['kind']:<16}{text}"


class Session:
    """1 セッション = Minecraft の窓が開いてから閉じるまで。seasons/<season>/sessions/<開始時刻>/ に記録する。"""

    def __init__(self, season_dir: Path, started: datetime, header: Event, config_block: dict[str, Any]):
        self.started = started
        sessions_dir = season_dir / "sessions"
        self.id = session_id(started)
        path = sessions_dir / self.id
        suffix = 1
        while path.exists():
            suffix += 1
            path = sessions_dir / f"{self.id}_{suffix}"
        self.id = path.name
        self.dir = path
        self.frames_dir = path / "frames"
        self.audio_dir = path / "audio"
        self.events_path = path / "events.jsonl"
        self.season = season_dir.name
        self.summary = SessionSummary()
        self._header = header
        self._config_block = config_block
        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"writer-{self.id}", daemon=True)
        self._end: str | None = None
        self._end_event: Event | None = None

    def start(self) -> None:
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._write_session_json()
        self._thread.start()
        self.put({"time": iso(self.started), "kind": "session_start", "season": self.season, **self._header})

    def put(self, event: Event) -> None:
        self._queue.put(event)

    def close(self, timeout: float = 30) -> Event | None:
        """残りのイベントを書き切り、session_end を書いて session.json を確定する。"""
        self._queue.put(None)
        self._thread.join(timeout)
        return self._end_event

    def _run(self) -> None:
        last_status = last_json = time.monotonic()
        with self.events_path.open("a", encoding="utf-8") as out:
            while True:
                try:
                    event = self._queue.get(timeout=1)
                except queue.Empty:
                    pass
                else:
                    if event is None:
                        end = self._build_end_event()
                        self._write_event(out, end)
                        self._end_event = end
                        self._end = end["time"]
                        break
                    self._write_event(out, event)
                now = time.monotonic()
                if now - last_status >= STATUS_INTERVAL_S:
                    last_status = now
                    self._print_status()
                if now - last_json >= SESSION_JSON_INTERVAL_S:
                    last_json = now
                    self._write_session_json()
        self._write_session_json()

    def _write_event(self, out, event: Event) -> None:
        try:
            self.summary.add(event)
        except (KeyError, TypeError, ValueError):
            log.exception("集計できないイベント: %s", event)
        out.write(json.dumps(event, ensure_ascii=False) + "\n")
        out.flush()
        if event.get("kind") != "frame":  # 5 秒おきのフレームは 1 分ごとの状況表示にまとめる
            print(describe(event), flush=True)

    def _build_end_event(self) -> Event:
        return {
            "time": now_iso(),
            "kind": "session_end",
            "frames": self.summary.counts["frames"],
            "notes": self.summary.counts["notes"],
            "play_time_s": self.summary.play_time_s,
            "world_open_s": self.summary.world_open_s,
        }

    def _print_status(self) -> None:
        s = self.summary
        play = s.play_time_s
        print(
            f"{datetime.now():%H:%M:%S}  -- 状況: 画像 {s.counts['frames']} 枚、メモ {s.counts['notes']} 件、"
            f"F2 {s.counts['screenshots']} 枚、実プレイ {play // 60} 分 {play % 60} 秒",
            flush=True,
        )

    def _write_session_json(self) -> None:
        data = {
            "id": self.id,
            "season": self.season,
            "start": iso(self.started),
            "end": self._end,
            "minecraft": self.summary.minecraft,
            "counts": self.summary.counts,
            "time": self.summary.time_block(),
            "config": self._config_block,
        }
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "session.json")
