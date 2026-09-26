"""Minecraft に F2(スクリーンショット)を送り、実際に PNG が保存されたかを確認する。

使い方:
  python poc/send_f2.py                          # SendInput で 1 回送る(Minecraft が前面にあること)
  python poc/send_f2.py --method postmessage     # PostMessage で送る(前面でなくても届くかの検証)
  python poc/send_f2.py --method postmessage --with-scancode
  python poc/send_f2.py --count 5 --interval 3   # 5 回、3 秒おき

判定は「.minecraft/screenshots に新しい PNG が現れたか」と「latest.log に保存メッセージが
追記されたか」の 2 つで行う。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes

import win32gui

from common import LATEST_LOG, SCREENSHOT_DIR, find_minecraft_window, setup_console_and_dpi

user32 = ctypes.windll.user32
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]

VK_F2 = 0x71
SCAN_F2 = 0x3C
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_input(flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=VK_F2, wScan=SCAN_F2, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def send_input_f2() -> None:
    """前面ウィンドウに対して、実キーボードと同じ経路で F2 を押して離す。"""
    inputs = (INPUT * 2)(_key_input(0), _key_input(KEYEVENTF_KEYUP))
    sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if sent != 2:
        raise ctypes.WinError()


def post_message_f2(hwnd: int, with_scancode: bool) -> None:
    """指定ウィンドウに WM_KEYDOWN / WM_KEYUP を直接投げる(前面でなくても届く可能性がある)。

    SDL3 は lParam のスキャンコードが 0 のとき MapVirtualKey で補完し、その場合は
    RAW キーボード入力が有効でもイベントを流す実装なので、既定ではスキャンコードを付けない。
    """
    scan = SCAN_F2 if with_scancode else 0
    lparam_down = 1 | (scan << 16)
    lparam_up = 1 | (scan << 16) | (1 << 30) | (1 << 31)
    if not user32.PostMessageW(hwnd, WM_KEYDOWN, VK_F2, lparam_down):
        raise ctypes.WinError()
    time.sleep(0.03)
    if not user32.PostMessageW(hwnd, WM_KEYUP, VK_F2, lparam_up):
        raise ctypes.WinError()


def snapshot() -> tuple[set[str], int]:
    files = {p.name for p in SCREENSHOT_DIR.glob("*.png")} if SCREENSHOT_DIR.exists() else set()
    log_size = LATEST_LOG.stat().st_size if LATEST_LOG.exists() else 0
    return files, log_size


def new_log_lines(since: int) -> list[str]:
    if not LATEST_LOG.exists():
        return []
    with LATEST_LOG.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(since)
        return [line.rstrip("\n") for line in f if "スクリーンショット" in line or "screenshot" in line.lower()]


def wait_for_screenshot(before_files: set[str], timeout: float) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        current = {p.name for p in SCREENSHOT_DIR.glob("*.png")} if SCREENSHOT_DIR.exists() else set()
        new = sorted(current - before_files)
        if new:
            return new, time.perf_counter() - t0
        time.sleep(0.1)
    return [], timeout


def main() -> int:
    setup_console_and_dpi()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--method", choices=["sendinput", "postmessage"], default="sendinput")
    parser.add_argument("--with-scancode", action="store_true", help="PostMessage の lParam にスキャンコード 0x3C を含める")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=3.0, help="複数回送るときの間隔(秒)")
    parser.add_argument("--timeout", type=float, default=3.0, help="PNG が現れるのを待つ秒数")
    parser.add_argument("--focus", action="store_true", help="SendInput の前に Minecraft を前面に出す")
    parser.add_argument("--title", help="タイトル部分一致で対象を選ぶ(Minecraft 以外で試すとき)")
    args = parser.parse_args()

    w = find_minecraft_window(args.title)
    if w is None:
        print("対象ウィンドウが見つかりません。Minecraft を起動してワールドに入ってから実行してください。")
        return 1
    print(w.describe())

    ok = 0
    for i in range(args.count):
        if i:
            time.sleep(args.interval)
        if args.method == "sendinput":
            if not w.is_foreground and args.focus:
                win32gui.SetForegroundWindow(w.hwnd)
                time.sleep(0.3)
            if not w.is_foreground:
                print(f"[{i + 1}/{args.count}] Minecraft が前面ではないので SendInput は送りません(--focus で前面化できます)")
                continue
        files_before, log_before = snapshot()
        t_sent = time.strftime("%H:%M:%S")
        if args.method == "sendinput":
            send_input_f2()
        else:
            post_message_f2(w.hwnd, args.with_scancode)
        new_files, elapsed = wait_for_screenshot(files_before, args.timeout)
        log_lines = new_log_lines(log_before)
        if new_files:
            ok += 1
            print(f"[{i + 1}/{args.count}] {t_sent} OK  {elapsed:.2f}s  新規PNG={new_files}  log={log_lines}")
        else:
            print(f"[{i + 1}/{args.count}] {t_sent} NG  {args.timeout:.0f}s 待っても PNG が増えませんでした  log={log_lines}")

    print(f"結果: {ok}/{args.count} 回成功 (method={args.method}, with_scancode={args.with_scancode})")
    return 0 if ok == args.count else 2


if __name__ == "__main__":
    sys.exit(main())
