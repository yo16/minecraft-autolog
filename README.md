# minecraft-autolog

Minecraft(Java 版)の操作ログをなるべく自動で撮るためのツール。
[minecraft-log](https://github.com/yo16/minecraft-log) に手作業で残しているプレイ記録(スクショ + コメントの Markdown)を、
「自動で画面を撮り続ける + 話すだけでコメントを残す」ことで楽に作れるようにするラッパー。

## 状態

M1(画面の定期保存・latest.log の監視・実プレイ時間・記録の書き出し)を実装済み。音声メモ(M2)と export(M3)はこれから。

## 使い方

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m autolog start          # 常駐。Minecraft の窓を見つけたら記録、閉じたら次を待つ。Ctrl+C で終了
.venv\Scripts\python -m autolog start --once   # 1 セッション記録したら終了
.venv\Scripts\python -m unittest discover -s tests -t .   # テスト(Minecraft なしで動く)
```

記録は `%LOCALAPPDATA%\minecraft-autolog\seasons\<シーズン>\sessions\<開始時刻>\` に `events.jsonl`・`session.json`・`frames/` としてできる。設定は [config.toml](config.toml)。

- [docs/M1_実機確認.md](docs/M1_実機確認.md) — M1 を本物の Minecraft で確かめる手順と結果
- [docs/設計.md](docs/設計.md) — 常駐プロセスの構成、データ形式、minecraft-log への受け渡し、実装の順序
- [docs/2026-09-26_技術調査.md](docs/2026-09-26_技術調査.md) — 何ができて何ができないか、採用した方式と却下した方式、実機検証の結果
- [poc/](poc/README.md) — 検証スクリプトと手順、検証結果
