"""latest.log を追いかけて、記録に使えるイベントを時刻付きで表示する。

拾うもの: F2 スクショの保存、プレイヤーのチャット、進捗達成、一時停止(Esc メニュー)、
ワールドの開始/終了、ログイン、それ以外の [CHAT] システムメッセージ(死亡メッセージなど)。

使い方:
  python poc/tail_log.py                       # 追記を待ち受ける(Ctrl+C で終了)
  python poc/tail_log.py --from-start          # 既存の内容も最初から出してから待ち受ける
  python poc/tail_log.py --from-start --no-follow
  python poc/tail_log.py --json                # 1 行 1 JSON で出す

ファイルは開きっぱなしにせず、0.5 秒ごとに開いて差分を読んで閉じる。
Minecraft は起動時に latest.log をリネームして新しく作り直すので、掴みっぱなしだと
そのリネームを妨げてしまうため。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from common import LATEST_LOG, setup_console_and_dpi

LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[([^\]]*)/(\w+)\]: (.*)$")

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("screenshot", re.compile(r"\[CHAT\] (?:スクリーンショットを(?P<file>.+?)として保存しました|Saved screenshot as (?P<file_en>.+))")),
    ("chat", re.compile(r"\[CHAT\] <(?P<player>[^>]+)> (?P<text>.*)")),
    ("advancement", re.compile(r"\[CHAT\] (?:(?P<player>.+?)が進捗\[(?P<name>.+?)\]を達成しました|(?P<player_en>.+?) has made the advancement \[(?P<name_en>.+?)\])")),
    ("pause", re.compile(r"Saving and pausing game")),
    ("world_start", re.compile(r"Starting integrated minecraft server version (?P<version>.+)")),
    ("world_stop", re.compile(r"(?P<detail>Stopping server|Stopping!)")),
    ("login", re.compile(r"Setting user: (?P<player>.+)")),
    ("system_chat", re.compile(r"\[CHAT\] (?P<text>.*)")),
]


def classify(message: str) -> tuple[str, dict[str, str]] | None:
    for kind, pattern in PATTERNS:
        m = pattern.search(message)
        if m:
            fields = {k.removesuffix("_en"): v for k, v in m.groupdict().items() if v is not None}
            return kind, fields
    return None


def parse(line: str, day: date) -> dict | None:
    m = LINE.match(line)
    if not m:
        return None
    hhmmss, thread, level, message = m.groups()
    hit = classify(message)
    if hit is None:
        return None
    kind, fields = hit
    return {"time": f"{day.isoformat()}T{hhmmss}", "kind": kind, **fields, "thread": thread, "level": level}


def read_new(path: Path, pos: int) -> tuple[list[str], int]:
    """pos 以降を読んで (行リスト, 新しい pos) を返す。途中で切れた最終行は次回に回す。"""
    with path.open("rb") as f:
        f.seek(pos)
        data = f.read()
    if not data:
        return [], pos
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], pos
    chunk, consumed = data[: cut + 1], cut + 1
    return chunk.decode("utf-8", errors="replace").splitlines(), pos + consumed


def file_identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_ino, st.st_dev


def emit(event: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return
    detail = {k: v for k, v in event.items() if k not in ("time", "kind", "thread", "level")}
    text = " ".join(f"{k}={v}" for k, v in detail.items())
    print(f"{event['time'][11:]}  {event['kind']:<12}{text}", flush=True)


def main() -> int:
    setup_console_and_dpi()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default=str(LATEST_LOG))
    parser.add_argument("--from-start", action="store_true", help="既存の内容も最初から出す")
    parser.add_argument("--no-follow", action="store_true", help="追記を待たずに終了する")
    parser.add_argument("--json", action="store_true", help="1 行 1 JSON で出す")
    parser.add_argument("--poll", type=float, default=0.5, help="ポーリング間隔(秒)")
    args = parser.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"ログがありません: {path}")
        return 1

    identity = file_identity(path)
    pos = 0 if args.from_start else path.stat().st_size
    # ログの行には時刻しかないので日付は補う。過去分はファイルの更新日、追記分は読んだ日
    day = datetime.fromtimestamp(path.stat().st_mtime).date() if args.from_start else date.today()
    print(f"監視中: {path} (開始位置 {pos} バイト)", file=sys.stderr, flush=True)

    try:
        while True:
            if path.exists():
                current = file_identity(path)
                size = path.stat().st_size
                if current != identity or size < pos:
                    print("latest.log が作り直されました(ゲーム起動)。先頭から読み直します", file=sys.stderr, flush=True)
                    identity, pos, day = current, 0, date.today()
                lines, pos = read_new(path, pos)
                for line in lines:
                    event = parse(line, day)
                    if event:
                        emit(event, args.json)
                if lines and not args.from_start:
                    day = date.today()
            if args.no_follow:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
