# 検証スクリプト(poc)

技術調査([docs/2026-09-26_技術調査.md](../docs/2026-09-26_技術調査.md))で「Minecraft を動かさないと確認できない」とした項目を、実機で確かめるためのスクリプト。製品コードではない。

## 準備

```powershell
python -m venv .venv
.venv\Scripts\pip install -r poc\requirements.txt
```

Minecraft(Java 版)を起動してワールドに入った状態で、以下を別ターミナルから実行する。

## 1. ウィンドウを見つけられるか

```powershell
python poc\probe_window.py
```

期待: `exe=javaw.exe` で、`client=854x480`(現状のウィンドウサイズ)が出る。`class=` に出るクラス名を控えておく(SDL3 なら `SDL_app` のはず)。

## 2. F2 を外から送れるか

```powershell
# Minecraft をクリックして前面にしてから 3 秒以内に実行
python poc\send_f2.py --focus

# 前面でなくても届くかの検証(ターミナルを前面にしたまま実行)
python poc\send_f2.py --method postmessage
python poc\send_f2.py --method postmessage --with-scancode
```

期待: `OK` と新規 PNG 名が出る。`postmessage` が NG なら「F2 を外部から送るには Minecraft が前面にある必要がある」と確定する。

## 3. 窓単位の連続キャプチャができるか

```powershell
python poc\capture_loop.py --interval 5 --duration 60
```

期待: `poc\captures\` に `YYYY-MM-DD_HH.MM.SS.webp` が 12 枚前後でき、中身が Minecraft の画面(タイトルバーなし)になっている。

確認したいこと:

- ゲーム中に黄色い枠が出るか(出る場合は Windows の「設定 > システム > ディスプレイ > グラフィック > 画面キャプチャの境界線」)
- ターミナルなど別ウィンドウを Minecraft の上に重ねても撮れ続けるか
- 1 枚あたりの KB(調査時の見積もりは WebP 品質 80 で 60〜100KB)
- ゲームの FPS が体感で落ちないか

## 4. latest.log からイベントを拾えるか

```powershell
python poc\tail_log.py
```

この状態で、ゲーム内で F2 を押す、T でチャットを開いて何か打って Enter、進捗を達成する、Esc で一時停止する、をそれぞれ試す。

期待: それぞれ `screenshot` `chat` `advancement` `pause` の行が時刻付きで出る。特に `chat` の行が `player=... text=...` で出れば、「チャット欄に打ったコメントを時刻付きで回収できる」ことが確定する。

## 5. typeless の入力先(手動)

typeless をインストールしたうえで:

1. Minecraft で T を押してチャット欄を開き、typeless のショートカット(既定は右 Alt)で日本語を話して確定する。チャット欄に文字が入り、Enter で `tail_log.py` に `chat` として出るか
2. メモ帳など別の窓を前面にして同じことをする(こちらは確実に動くはず)。このとき Minecraft が一時停止メニューになることも確認する(`pauseOnLostFocus:true` のため)

1 が通れば「ゲームを止めずにチャット欄へ口述 → ログから回収」という最小構成が成立する。

## 6. 話すだけでコメントを残せるか(voice_note.py)

typeless の代わりに、ラッパー自身がマイクを録って文字起こしする案の検証。Minecraft を前面にしたまま動き、チャット欄も開かない。マイクが必要。

```powershell
# 初回は large-v3-turbo(約 1.6GB)のダウンロードが走る(この PC には取得済み)。軽く試すなら --model small
python poc\voice_note.py
```

Minecraft をプレイしながら、右 Ctrl を 1 回押して話し、もう 1 回押す(ビープ音で開始/停止が分かる)。数秒後にターミナルに文が出て、`poc\voice_notes\notes.jsonl` と WAV が増える。

確認したいこと:

- 右 Ctrl を押しても Minecraft 側で何も起きないか(起きるなら `--key mouse_x2` や `--key scroll_lock` に変える)
- 実際の声での精度。誤りが多ければ `--beam 5`、それでもだめなら `--model kotoba-tech/kotoba-whisper-v2.0-faster` を試す
- `--mode hold --key mouse_x2`(押している間だけ)と `--mode vad --meter`(ハンズフリー)のどちらが使いやすいか。vad はスピーカーからのゲーム音を拾ったら `--vad-db` を上げる(例 `-30`)
- 文字起こし中にゲームがカクつかないか(カクつくなら `--threads 4`)
- 過去の WAV は `--transcribe-file poc\voice_notes\xxx.wav --model small` のように別モデルで起こし直せる

Minecraft 用語を覚えさせるには `poc\glossary.txt` に 1 行足す(上の行ほど優先)。聞き間違いが固定的なら `フィリッジャー => ピリジャー` のような置換ルールも書ける。効いたかどうかは、その録音の WAV を `--transcribe-file` で起こし直して確かめる。

## 7. 一時停止を除いたプレイ時間(手動確認)

ログには一時停止の解除が出ないので、Minecraft の統計ファイルを使う。ワールドに入る前と出た後で次を実行し、`play_time` の差分が「一時停止を除いた実プレイ時間」になっているかを確かめる。

```powershell
python -c "import json,glob;p=glob.glob(r'%APPDATA%\.minecraft\saves\20260920\players\stats\*.json'.replace('%APPDATA%',__import__('os').environ['APPDATA']))[0];c=json.load(open(p,encoding='utf-8'))['stats']['minecraft:custom'];print('play_time',c['minecraft:play_time']/20/60,'min','total_world_time',c['minecraft:total_world_time']/20/60,'min')"
```

----

## 検証結果
1. 成功。下記を得た。

hwnd=0xaf0b34 pid=14876 exe=javaw.exe class=SDL_app client=854x480 rect=(-1006, 416, -136, 935) foreground=False minimized=False title='Minecraft 26.3 - シングルプレイ'

2. F2キー
- `python poc\send_f2.py --focus`: 成功
- `postmessage`: 失敗、何も起きない


3. 成功。
- 黄色い線は出ない
- 別ウィンドウを上に重ねても撮れ続ける
- サイズは20～80KB
- FPSは落ちていない

4. 拾えている
- F2、チャット、Escは確認できた。進捗は試せていない
- EscでのPauseは得られるが、Pauseが解除されて再開したタイミングは出ていない
  - 時間を計測する際には、Pause～Pause解除の時間を作業時間外としたいので、Pause解除のタイミングも知れるとよい。

5. typeless
1. 成功
2. minecraft操作を止め、メモ帳を前面にして、文字を入力することは避けたい

Minecraftの操作はそのままにして、Tのチャット欄すら開かず、話すだけで入力したい。つまりMinecraftの機能を使わずに別のウィンドウで文字入力を待ち受ける形。
そうすると、typelessは相性が悪いかもしれない。Minecraftが最前面にならざるを得ないから。

typelessに縛られず、いい方法を考えて。

6. ok
ctrlはMinecraftに悪影響を与えていない。
ビープ音でうまく動作している。wavもできている。
精度はよさそうだが、「ピリジャー」など、Minecraft独特の単語を覚えられたらいいなと思う。

7. 一時停止を除いた時間になっていた
