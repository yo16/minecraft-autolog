"""画面の定期保存(poc/capture_loop.py から)。Windows Graphics Capture で撮るので Minecraft のフォーカスは奪わない。

フレームは画面が変化したときだけ届く。interval_s ごとに 1 枚、クライアント領域に切り抜いて保存する。
最小化中は撮れない(Windows Graphics Capture の仕様)。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy
import win32gui
from windows_capture import Frame, InternalCaptureControl, WindowsCapture

from . import window
from .session import EventBus, iso

log = logging.getLogger(__name__)

DWMWA_EXTENDED_FRAME_BOUNDS = 9
THUMB_SIZE = (64, 36)  # 差分計算用の縮小サイズ(16:9)


def frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """DWM が実際に描画している窓の外周(見えない 7px の枠を除いた矩形)。キャプチャ画像はこの範囲になる。"""
    rect = wintypes.RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if hr != 0:
        return win32gui.GetWindowRect(hwnd)
    return rect.left, rect.top, rect.right, rect.bottom


def client_crop_box(hwnd: int) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """キャプチャ画像内でのクライアント領域 (x0, y0, x1, y1) と、期待されるキャプチャ画像サイズを返す。"""
    fl, ft, fr, fb = frame_bounds(hwnd)
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    return (cx - fl, cy - ft, cx - fl + cw, cy - ft + ch), (fr - fl, fb - ft)


def thumbnail(bgr: numpy.ndarray) -> numpy.ndarray:
    """差分計算用の縮小グレースケール。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, THUMB_SIZE, interpolation=cv2.INTER_AREA)


def frame_diff(prev: numpy.ndarray | None, cur: numpy.ndarray) -> float | None:
    """縮小グレースケール同士の平均絶対差(0〜1)。直前の画像がなければ None。"""
    if prev is None or prev.shape != cur.shape:
        return None
    return float(numpy.mean(cv2.absdiff(prev, cur))) / 255.0


def frame_file_name(t: datetime, ext: str) -> str:
    """F2 のスクショと同じ "YYYY-MM-DD_HH.MM.SS.拡張子"。"""
    return t.strftime("%Y-%m-%d_%H.%M.%S") + f".{ext}"


class Capturer:
    """1 セッション分の撮影。最小化から戻れば撮影を再開し、窓が閉じたら止まる。"""

    def __init__(self, hwnd: int, frames_dir: Path, bus: EventBus, cfg: dict[str, Any]):
        self.hwnd = hwnd
        self.frames_dir = frames_dir
        self.bus = bus
        self.interval_s = float(cfg["interval_s"])
        self.ext = str(cfg["format"])
        self.static_threshold = float(cfg["static_threshold"])
        quality = int(cfg["quality"])
        self.encode_params = {
            "webp": [cv2.IMWRITE_WEBP_QUALITY, quality],
            "jpg": [cv2.IMWRITE_JPEG_QUALITY, quality],
            "png": [cv2.IMWRITE_PNG_COMPRESSION, 3],
        }[self.ext]
        self._last_save = 0.0
        self._prev_thumb: numpy.ndarray | None = None
        self._crop_warned = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(10)

    def _run(self) -> None:
        try:
            self._supervise()
        except Exception:
            log.exception("capture で例外")
            self.bus.emit({"kind": "warning", "message": "画面の保存で例外が起きたので撮影を止めました(autolog.log 参照)"})

    def _supervise(self) -> None:
        """最小化を見張りながらキャプチャを開始・再開する。"""
        minimized_warned = False
        while not self._stop.is_set() and window.is_window(self.hwnd):
            if window.is_minimized(self.hwnd):
                if not minimized_warned:
                    minimized_warned = True
                    self.bus.emit({"kind": "warning", "message": "ウィンドウが最小化されているため撮影できません"})
                self._stop.wait(1)
                continue
            if minimized_warned:
                minimized_warned = False
                self.bus.emit({"kind": "info", "message": "ウィンドウが元に戻ったので撮影を再開します"})
            self._capture_until_closed_or_minimized()

    def _capture_until_closed_or_minimized(self) -> None:
        capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=self.hwnd)

        @capture.event
        def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
            try:
                self._on_frame(frame)
            except Exception:
                log.exception("フレームの保存で例外")

        @capture.event
        def on_closed() -> None:
            log.info("キャプチャ対象の窓が閉じました")

        control = capture.start_free_threaded()
        try:
            while not control.is_finished() and not self._stop.is_set():
                if not window.is_window(self.hwnd) or window.is_minimized(self.hwnd):
                    break
                self._stop.wait(0.5)
        finally:
            control.stop()

    def _on_frame(self, frame: Frame) -> None:
        now = time.time()
        if now - self._last_save < self.interval_s:
            return
        self._last_save = now

        image = frame.frame_buffer  # BGRA, 高さ x 幅 x 4
        # 窓の大きさが変わってもよいように毎回求める(Win32 呼び出し数回で軽い)
        crop_box, expected_size = client_crop_box(self.hwnd)
        if (frame.width, frame.height) == expected_size:
            x0, y0, x1, y1 = crop_box
            image = image[y0:y1, x0:x1]
        elif not self._crop_warned:
            self._crop_warned = True
            log.warning("画像サイズ %sx%s が想定 %s と違うので切り抜きません", frame.width, frame.height, expected_size)
        bgr = numpy.ascontiguousarray(image[:, :, :3])

        ok, encoded = cv2.imencode(f".{self.ext}", bgr, self.encode_params)
        if not ok:
            log.warning("エンコードに失敗しました")
            return
        stamp = datetime.fromtimestamp(now)
        path = self.frames_dir / frame_file_name(stamp, self.ext)
        if path.exists():  # 間隔を 1 秒未満にしたときの重複
            path = path.with_stem(f"{path.stem}_{int(now * 1000) % 1000:03d}")
        path.write_bytes(encoded.tobytes())

        thumb = thumbnail(bgr)
        diff = frame_diff(self._prev_thumb, thumb)
        self._prev_thumb = thumb
        self.bus.emit({
            "time": iso(stamp),
            "kind": "frame",
            "file": f"{self.frames_dir.name}/{path.name}",
            "bytes": path.stat().st_size,
            "diff": None if diff is None else round(diff, 4),
            "static": diff is not None and diff < self.static_threshold,
            "foreground": window.is_foreground(self.hwnd),
        })
