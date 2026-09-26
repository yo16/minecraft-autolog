"""Minecraft のウィンドウを見つけて情報を表示する。

使い方:
  python poc/probe_window.py                       # Minecraft を探す
  python poc/probe_window.py --title エクスプローラー  # 他のウィンドウで動作確認
  python poc/probe_window.py --all                 # 可視ウィンドウをすべて列挙
"""

from __future__ import annotations

import argparse
import sys

from common import find_minecraft_window, list_windows, setup_console_and_dpi


def main() -> int:
    setup_console_and_dpi()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", help="タイトル部分一致で探す(Minecraft 以外で試すとき)")
    parser.add_argument("--all", action="store_true", help="可視ウィンドウをすべて列挙する")
    args = parser.parse_args()

    if args.all:
        for w in list_windows():
            print(w.describe())
        return 0

    w = find_minecraft_window(args.title)
    if w is None:
        print("対象ウィンドウが見つかりません。Minecraft を起動してワールドに入ってから実行してください。")
        return 1
    print(w.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
