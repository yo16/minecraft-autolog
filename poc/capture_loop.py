"""Minecraft のウィンドウを Windows Graphics Capture で一定間隔に静止画保存する。

使い方:
  python poc/capture_loop.py                         # 5 秒おきに poc/captures/ へ WebP 保存(Ctrl+C で終了)
  python poc/capture_loop.py --interval 2 --duration 60
  python poc/capture_loop.py --format jpg --quality 85
  python poc/capture_loop.py --title エクスプローラー    # 他のウィンドウで動作確認

ファイル名は F2 のスクショと同じ "YYYY-MM-DD_HH.MM.SS.拡張子" 形式。
保存のたびに captures/index.jsonl に 1 行追記する。
ウィンドウが隠れていても撮れるが、最小化中は撮れない(Windows Graphics Capture の仕様)。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import cv2
import numpy
import win32gui
from windows_capture import Frame, InternalCaptureControl, WindowsCapture

from common import find_minecraft_window, setup_console_and_dpi

DWMWA_EXTENDED_FRAME_BOUNDS = 9


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


def main() -> int:
    setup_console_and_dpi()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=5.0, help="保存間隔(秒)")
    parser.add_argument("--duration", type=float, default=0, help="実行時間(秒)。0 なら Ctrl+C か窓が閉じるまで")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "captures"))
    parser.add_argument("--format", choices=["webp", "jpg", "png"], default="webp")
    parser.add_argument("--quality", type=int, default=80, help="webp/jpg の品質(1-100)")
    parser.add_argument("--no-crop", action="store_true", help="タイトルバーを含めた窓全体を保存する")
    parser.add_argument("--title", help="タイトル部分一致で対象を選ぶ(Minecraft 以外で試すとき)")
    args = parser.parse_args()

    w = find_minecraft_window(args.title)
    if w is None:
        print("対象ウィンドウが見つかりません。Minecraft を起動してワールドに入ってから実行してください。")
        return 1
    if w.is_minimized:
        print("ウィンドウが最小化されています。Windows Graphics Capture は最小化中の窓を撮れないので、元に戻してください。")
        return 1
    print(w.describe())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.jsonl"
    encode_params = {
        "webp": [cv2.IMWRITE_WEBP_QUALITY, args.quality],
        "jpg": [cv2.IMWRITE_JPEG_QUALITY, args.quality],
        "png": [cv2.IMWRITE_PNG_COMPRESSION, 3],
    }[args.format]

    stats = {"frames": 0, "saved": 0, "bytes": 0, "last_save": 0.0, "last_frame": 0.0, "crop_warned": False}
    crop_box, expected_size = client_crop_box(w.hwnd)

    capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=w.hwnd)

    @capture.event
    def on_frame_arrived(frame: Frame, control: InternalCaptureControl) -> None:
        now = time.time()
        stats["frames"] += 1
        stats["last_frame"] = now
        if now - stats["last_save"] < args.interval:
            return
        stats["last_save"] = now

        image = frame.frame_buffer  # BGRA, 高さ x 幅 x 4
        if not args.no_crop:
            if (frame.width, frame.height) == expected_size:
                x0, y0, x1, y1 = crop_box
                image = image[y0:y1, x0:x1]
            elif not stats["crop_warned"]:
                stats["crop_warned"] = True
                print(f"注意: 画像サイズ {frame.width}x{frame.height} が想定 {expected_size} と違うので切り抜きません")
        bgr = numpy.ascontiguousarray(image[:, :, :3])

        ok, encoded = cv2.imencode(f".{args.format}", bgr, encode_params)
        if not ok:
            print("エンコードに失敗しました")
            return
        stamp = datetime.fromtimestamp(now)
        name = stamp.strftime("%Y-%m-%d_%H.%M.%S") + f".{args.format}"
        path = out / name
        path.write_bytes(encoded.tobytes())
        size = path.stat().st_size
        stats["saved"] += 1
        stats["bytes"] += size
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": stamp.isoformat(timespec="milliseconds"),
                "file": name,
                "bytes": size,
                "width": int(bgr.shape[1]),
                "height": int(bgr.shape[0]),
            }, ensure_ascii=False) + "\n")
        print(f"{stamp:%H:%M:%S} 保存 {name} {bgr.shape[1]}x{bgr.shape[0]} {size / 1024:.0f}KB (受信フレーム累計 {stats['frames']})")

    @capture.event
    def on_closed() -> None:
        print("ウィンドウが閉じられたのでキャプチャを終了します")

    control = capture.start_free_threaded()
    started = time.time()
    warned_static = False
    try:
        while not control.is_finished():
            time.sleep(0.5)
            if args.duration and time.time() - started >= args.duration:
                break
            idle = time.time() - stats["last_frame"] if stats["last_frame"] else 0
            if idle > 2 * args.interval and not warned_static:
                warned_static = True
                print("画面に変化がないためフレームが届いていません(一時停止中など)。変化があれば再開します")
            elif idle <= args.interval:
                warned_static = False
    except KeyboardInterrupt:
        pass
    finally:
        control.stop()

    elapsed = time.time() - started
    avg_kb = stats["bytes"] / stats["saved"] / 1024 if stats["saved"] else 0
    print(
        f"終了: {elapsed:.0f} 秒間に受信 {stats['frames']} フレーム、保存 {stats['saved']} 枚、"
        f"平均 {avg_kb:.0f}KB/枚、合計 {stats['bytes'] / 1024 / 1024:.1f}MB → {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
