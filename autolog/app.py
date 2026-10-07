"""常駐プロセス本体: 各スレッドの起動・セッションの開始と終了・終了処理。"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from . import window
from .capture import Capturer
from .config import Config, resolve_season
from .logwatch import LogWatcher
from .session import EventBus, Session
from .statwatch import StatWatcher

log = logging.getLogger(__name__)

WINDOW_POLL_S = 2
WINDOW_LOST_S = 10  # 窓を見失ってからセッション終了とみなすまで(設計 §9 の 6)


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(cfg.data_dir / "autolog.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)


class App:
    def __init__(self, cfg: Config, title_override: str | None = None):
        self.cfg = cfg
        self.title_override = title_override
        self.bus = EventBus()
        self.logwatch = LogWatcher(cfg.latest_log, self.bus)
        self.statwatch = StatWatcher(cfg.saves_dir, self.bus, world=lambda: self.logwatch.parser.world)
        self.session: Session | None = None
        self.capturer: Capturer | None = None
        self.hwnd: int | None = None

    def run(self, once: bool = False) -> None:
        self.logwatch.restore()
        state = self.logwatch.parser.state()
        print(f"データの保存先: {self.cfg.data_dir}", flush=True)
        print(
            f"latest.log から復元: プレイヤー={state['player']} バージョン={state['version']} "
            f"ワールド={state['world']}{'(開いている)' if state['world_open'] else ''}",
            flush=True,
        )
        self.logwatch.start()
        self.statwatch.start()
        print("Minecraft の窓を待っています(Ctrl+C で終了)", flush=True)
        lost_since: float | None = None
        try:
            while True:
                w = window.find_minecraft_window(self.title_override)
                if w is not None:
                    lost_since = None
                    if self.session is not None and w.hwnd != self.hwnd:
                        print("別の Minecraft の窓に変わったのでセッションを分けます", flush=True)
                        self.end_session()
                    if self.session is None:
                        self.start_session(w)
                elif self.session is not None:
                    if lost_since is None:
                        lost_since = time.monotonic()
                    elif time.monotonic() - lost_since >= WINDOW_LOST_S:
                        lost_since = None
                        self.end_session()
                        if once:
                            break
                        print("Minecraft の窓を待っています(Ctrl+C で終了)", flush=True)
                time.sleep(WINDOW_POLL_S)
        except KeyboardInterrupt:
            print("終了します", flush=True)
        finally:
            if self.session is not None:
                self.end_session()
            self.logwatch.stop()
            self.statwatch.stop()

    def start_session(self, w: window.WindowInfo) -> None:
        state = self.logwatch.parser.state()
        season = resolve_season(self.cfg.season, self.cfg.minecraft_log_dir, state["world"])
        header = {"hwnd": w.hwnd, "client": list(w.client_size), "title": w.title}
        config_block = {
            "capture_interval_s": self.cfg.capture["interval_s"],
            "voice_mode": None,  # voice は M2 で統合する
        }
        session = Session(self.cfg.data_dir / "seasons" / season, datetime.now(), header, config_block)
        session.start()
        self.bus.attach(session)
        known = {k: v for k, v in state.items() if v is not None}
        if known.get("player") or known.get("world"):
            self.bus.emit({"kind": "minecraft_state", **known})
        self.statwatch.emit_current()  # セッション開始時点の実プレイ時間を基準値として記録する
        self.session, self.hwnd = session, w.hwnd
        print(f"セッション開始: {session.dir}", flush=True)
        if w.is_minimized:
            print("窓が最小化されています。元に戻すと撮影が始まります", flush=True)
        self.capturer = Capturer(w.hwnd, session.frames_dir, self.bus, self.cfg.capture)
        self.capturer.start()

    def end_session(self) -> None:
        if self.capturer is not None:
            self.capturer.stop()
            self.capturer = None
        session = self.session
        if session is None:
            return
        self.bus.detach()
        end = session.close()
        self.session, self.hwnd = None, None
        if end is None:
            print(f"セッション終了: 書き込みが時間内に終わりませんでした({session.dir})", flush=True)
            return
        play, opened = end["play_time_s"], end["world_open_s"]
        print(
            f"セッション終了: 画像 {end['frames']} 枚、実プレイ {play // 60} 分 {play % 60} 秒"
            f"(ワールドを開いていた時間 {opened // 60} 分 {opened % 60} 秒) → {session.dir}",
            flush=True,
        )
