"""CLI: python -m autolog start [--once]

export / devices / transcribe / status / purge は M2・M3 で追加する(設計 §5)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m autolog", description="Minecraft のプレイを自動で記録する")
    parser.add_argument("--config", type=Path, help="設定ファイル(既定: リポジトリ直下の config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="常駐して、Minecraft の窓を見つけたら記録する")
    start.add_argument("--once", action="store_true", help="1 セッション記録したら終了する")
    start.add_argument("--title", help="タイトル部分一致で対象の窓を選ぶ(Minecraft 以外で動作確認するとき)")

    args = parser.parse_args()

    # pywin32 / windows-capture の読み込みは重いので、コマンドが決まってから行う
    from .app import App, setup_logging
    from .window import setup_console_and_dpi

    setup_console_and_dpi()
    cfg = load_config(args.config)
    setup_logging(cfg)
    if args.command == "start":
        App(cfg, title_override=args.title).run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
