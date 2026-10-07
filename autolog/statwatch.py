"""saves/<world>/players/stats/<uuid>.json の監視による実プレイ時間と、一時停止の解除時刻の推定。

play_time は一時停止を除いた時間、total_world_time は含む時間(どちらも tick = 1/20 秒)。
Minecraft は Esc のたび・自動保存・終了時にこのファイルを書き直す。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .session import TICKS_PER_SECOND, Event, EventBus, iso, now_iso

log = logging.getLogger(__name__)

# 一時停止のログ行(秒単位)と、その保存によるファイル更新を対応づけるときの許容幅
PAUSE_MATCH_S = 1.0


@dataclass
class _Pause:
    time: datetime
    play_time: int | None = None  # 一時停止の保存で書かれた play_time(まだ読めていなければ None)


class ResumeEstimator:
    """一時停止(pause)と統計の更新(stats)の列から、一時停止の解除時刻を推定する。

    pause(T1)直後の保存で play_time = P1 が書かれ、次の更新(T2, P2)で P2 > P1 なら
    解除 ≈ T2 − (P2 − P1)/20 秒。間に停止が 1 回だけならほぼ正確。
    """

    def __init__(self) -> None:
        self._pending: list[_Pause] = []
        self._last: tuple[datetime, int] | None = None

    def on_pause(self, t: datetime) -> None:
        pause = _Pause(t)
        # ログ行より先にファイル更新を読んでいた場合(ログ行の時刻は秒の切り捨て)
        if self._last is not None and self._last[0] >= t - timedelta(seconds=PAUSE_MATCH_S):
            if not any(p.play_time == self._last[1] for p in self._pending):
                pause.play_time = self._last[1]
        self._pending.append(pause)

    def on_stats(self, t: datetime, play_time: int) -> list[Event]:
        events: list[Event] = []
        resumed = [p for p in self._pending if p.play_time is not None and play_time > p.play_time]
        if resumed:
            # 複数あれば最後の停止だけが正しく求まる(それ以前の停止の解除時刻は分からない)
            p = resumed[-1]
            resume = t - timedelta(seconds=(play_time - p.play_time) / TICKS_PER_SECOND)
            resume = max(resume, p.time)
            events.append({
                "time": iso(resume),
                "kind": "resume_estimated",
                "pause_at": iso(p.time),
                "paused_s": round((resume - p.time).total_seconds(), 1),
            })
            self._pending = [q for q in self._pending if q not in resumed]
        waiting = [p for p in self._pending if p.play_time is None and t >= p.time - timedelta(seconds=PAUSE_MATCH_S)]
        if waiting:
            waiting[-1].play_time = play_time
            # 対応する保存が見つからないまま次の停止が来たものは推定できないので捨てる
            self._pending = [p for p in self._pending if p.play_time is not None]
        self._last = (t, play_time)
        return events

    def reset(self) -> None:
        self._pending.clear()
        self._last = None


def find_stats_file(saves_dir: Path, world: str | None) -> Path | None:
    """そのワールドの統計ファイル。フォルダが見つからなければ全ワールドで最後に更新されたもの。"""
    if world:
        folder = saves_dir / world / "players" / "stats"
        files = list(folder.glob("*.json")) if folder.is_dir() else []
        if files:
            return max(files, key=lambda p: p.stat().st_mtime)
    files = list(saves_dir.glob("*/players/stats/*.json"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def read_play_times(path: Path) -> tuple[int, int]:
    """(play_time, total_world_time) を tick で返す。書き込み途中で壊れていれば ValueError。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    custom = data["stats"]["minecraft:custom"]
    return int(custom.get("minecraft:play_time", 0)), int(custom.get("minecraft:total_world_time", 0))


class StatWatcher:
    """統計ファイルを 1 秒ごとに見て、更新があれば stats イベントを出すスレッド。"""

    def __init__(self, saves_dir: Path, bus: EventBus, world: Callable[[], str | None], poll_s: float = 1.0):
        self.saves_dir = saves_dir
        self.bus = bus
        self.world = world
        self.poll_s = poll_s
        self.estimator = ResumeEstimator()
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._mtime: float | None = None
        self._force = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="statwatch", daemon=True)
        bus.subscribe(self._on_event)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def emit_current(self) -> None:
        """次の確認で更新の有無に関係なく現在値を出す(セッション開始時の基準値)。"""
        self._force = True

    def _on_event(self, event: Event) -> None:
        kind = event.get("kind")
        if kind == "pause":
            with self._lock:
                self.estimator.on_pause(datetime.fromisoformat(event["time"]))
        elif kind == "world_start":
            self._force = True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                log.exception("statwatch で例外")
                self.bus.emit({"kind": "warning", "message": "統計ファイルの読み取りで例外が起きました(autolog.log 参照)"})
                self._stop.wait(10)
            self._stop.wait(self.poll_s)

    def _poll(self) -> None:
        world = self.world()
        path = find_stats_file(self.saves_dir, world)
        if path is None:
            return
        mtime = path.stat().st_mtime
        if path != self._path:
            # 対象が変わっただけでは出さない。基準値はワールド開始・セッション開始の強制読み取りで出る
            self._path, self._mtime = path, mtime
            with self._lock:
                self.estimator.reset()
        if mtime == self._mtime and not self._force:
            return
        try:
            play_time, world_time = read_play_times(path)
        except (OSError, ValueError, KeyError):
            return  # 書き込み途中。次の確認で読み直す
        self._mtime = mtime
        self._force = False
        t = datetime.fromtimestamp(mtime)
        folder_world = path.parent.parent.parent.name
        self.bus.emit({
            "time": now_iso(),
            "kind": "stats",
            "saved_at": iso(t),  # ファイルの更新時刻 = Minecraft が保存した時刻
            "world": folder_world,
            "play_time": play_time,
            "total_world_time": world_time,
        })
        with self._lock:
            events = self.estimator.on_stats(t, play_time)
        for event in events:
            self.bus.emit(event)
