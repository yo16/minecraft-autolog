"""Minecraft ウィンドウの検出(poc/common.py から)。"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

import win32gui
import win32process

# 例: "Minecraft 26.3 - シングルプレイ" / "Minecraft* 26.3" (Mod 導入時は * が付く)
TITLE_PATTERN = re.compile(r"^Minecraft\*?(\s|$)")
JAVA_EXES = {"javaw.exe", "java.exe"}

_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def setup_console_and_dpi() -> None:
    """日本語を含む出力で cp932 エラーにならないようにし、座標がピクセルと一致するよう DPI 対応にする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def process_image_path(pid: int) -> str:
    """PID から実行ファイルのフルパスを返す(取得できなければ空文字)。"""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def is_window(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))


def is_foreground(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd


def is_minimized(hwnd: int) -> bool:
    return bool(win32gui.IsIconic(hwnd))


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    exe: str
    rect: tuple[int, int, int, int]  # GetWindowRect (left, top, right, bottom)
    client_size: tuple[int, int]  # クライアント領域 (幅, 高さ)

    @property
    def is_foreground(self) -> bool:
        return is_foreground(self.hwnd)

    @property
    def is_minimized(self) -> bool:
        return is_minimized(self.hwnd)

    def describe(self) -> str:
        w, h = self.client_size
        return (
            f"hwnd={self.hwnd:#x} pid={self.pid} exe={self.exe or '?'} class={self.class_name} "
            f"client={w}x{h} foreground={self.is_foreground} minimized={self.is_minimized} title={self.title!r}"
        )


def _window_info(hwnd: int) -> WindowInfo:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        title=win32gui.GetWindowText(hwnd),
        class_name=win32gui.GetClassName(hwnd),
        exe=os.path.basename(process_image_path(pid)),
        rect=rect,
        client_size=(cr - cl, cb - ct),
    )


def list_windows(title_substring: str | None = None) -> list[WindowInfo]:
    """タイトル付きの可視トップレベルウィンドウを列挙する。title_substring 指定時は部分一致で絞る。"""
    found: list[WindowInfo] = []

    def callback(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if title_substring is not None and title_substring not in title:
            return
        found.append(_window_info(hwnd))

    win32gui.EnumWindows(callback, None)
    return found


def find_minecraft_window(title_override: str | None = None) -> WindowInfo | None:
    """Minecraft のゲームウィンドウを返す。見つからなければ None。

    title_override を渡すとタイトル部分一致だけで探す(Minecraft を起動せずに
    他のアプリで動作確認したいとき用)。
    通常は「タイトルが Minecraft で始まり、プロセスが javaw.exe / java.exe」のものを選ぶ。
    ランチャー("Minecraft Launcher", Minecraft.exe)はここで除外される。
    """
    if title_override:
        wins = list_windows(title_override)
        return wins[0] if wins else None

    candidates = [w for w in list_windows() if TITLE_PATTERN.match(w.title)]
    java = [w for w in candidates if w.exe.lower() in JAVA_EXES]
    if not java:
        # 実行ファイル名が取れなかった場合の保険。ランチャーだけは名前で除外する
        java = [w for w in candidates if not w.exe and "Launcher" not in w.title]
    if not java:
        return None
    # 複数あれば面積が最大のもの
    return max(java, key=lambda w: w.client_size[0] * w.client_size[1])
