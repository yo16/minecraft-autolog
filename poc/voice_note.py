"""話すだけでコメントを残す。Minecraft を前面にしたまま、フォーカスを奪わずに動く。

マイクを常時開いておき、「録音する区間」だけをキーか声で区切る。区切った音声は WAV で残しつつ、
裏のスレッドで faster-whisper(ローカル・オフライン)が文字起こしして notes.jsonl に追記する。
キーはグローバルフック(pynput)で拾うので、Minecraft からフォーカスは移らない。

モード:
  --mode toggle  (既定) キーを 1 回押すと録音開始、もう 1 回押すと停止 → 文字起こし
  --mode hold    キーを押している間だけ録音
  --mode vad     キー不要。声が出たら自動で録音を始め、無音が続いたら区切る(ハンズフリー)

キーは Minecraft が割り当てていないものを選ぶ。既定は右 Ctrl(Minecraft のダッシュは左 Ctrl のみ)。
  --key right_ctrl | right_shift | right_alt | scroll_lock | pause | caps_lock | f13〜f24 | mouse_x1 | mouse_x2

用語集(poc/glossary.txt):
  Minecraft 用語を Whisper にヒントとして渡し、「誤 => 正」の置換ルールも適用する。
  新しい単語を覚えさせたいときはこのファイルに 1 行足す。

使い方:
  python poc\\voice_note.py                              # 右 Ctrl でトグル録音、large-v3-turbo で文字起こし
  python poc\\voice_note.py --mode hold --key mouse_x2   # マウスのサイドボタンを押している間だけ
  python poc\\voice_note.py --mode vad --meter           # ハンズフリー。--meter で入力レベルを表示してしきい値を決める
  python poc\\voice_note.py --list-devices               # マイク一覧
  python poc\\voice_note.py --transcribe-file a.wav      # 既存の音声ファイルを文字起こしして速度と精度を見る

出力: poc/voice_notes/YYYY-MM-DD_HH.MM.SS.wav と poc/voice_notes/notes.jsonl
      (start = 話し始めの時刻。capture_loop.py の画像とはこの時刻で突き合わせる)
初回はモデルのダウンロードが走る(large-v3-turbo で約 1.6GB、small で約 0.5GB)。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy

from common import setup_console_and_dpi

SAMPLE_RATE = 16000
BLOCK = 320  # 20 ms
PRE_ROLL_S = 0.6  # キーを押す直前・声の出だしを取りこぼさないための先読み
PROMPT_TOKEN_BUDGET = 220  # Whisper がヒントとして受け取れる上限(224)より少し手前
PROMPT_LEAD = "Minecraft のプレイ記録。"

KEY_ALIASES = {
    "right_ctrl": "ctrl_r",
    "right_shift": "shift_r",
    "right_alt": "alt_r",
    "scroll_lock": "scroll_lock",
    "pause": "pause",
    "caps_lock": "caps_lock",
    **{f"f{i}": f"f{i}" for i in range(13, 25)},
}
MOUSE_ALIASES = {"mouse_x1": "x1", "mouse_x2": "x2", "mouse_middle": "middle"}

# 無音や環境音に対して Whisper が出しがちな定型句。出たら要注意として印を付ける
HALLUCINATIONS = ("ご視聴ありがとうございました", "チャンネル登録", "字幕", "おやすみなさい", "ありがとうございました")


def load_glossary(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """用語集を読む。戻り値は (ヒントに渡す語の一覧, 置換ルール [(誤, 正), ...])。"""
    terms: list[str] = []
    corrections: list[tuple[str, str]] = []
    if not path.exists():
        return terms, corrections
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, right = (s.strip() for s in line.split("=>", 1))
            if wrong and right:
                corrections.append((wrong, right))
        else:
            terms.append(line)
    return terms, corrections


def apply_corrections(text: str, corrections: list[tuple[str, str]]) -> str:
    for wrong, right in corrections:
        text = text.replace(wrong, right)
    return text


def beep(freq: int) -> None:
    try:
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, 70), daemon=True).start()
    except Exception:
        pass


def write_wav(path: Path, audio: numpy.ndarray) -> None:
    pcm = numpy.clip(audio * 32767, -32768, 32767).astype(numpy.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


class Transcriber(threading.Thread):
    """録音区間を受け取って WAV 保存 → 文字起こし → notes.jsonl 追記、を裏で順に処理する。"""

    def __init__(
        self,
        model_name: str,
        out_dir: Path,
        terms: list[str],
        corrections: list[tuple[str, str]],
        threads: int,
        beam: int,
        mode: str,
    ):
        super().__init__(daemon=True)
        self.model_name = model_name
        self.out_dir = out_dir
        self.terms = terms
        self.corrections = corrections
        self.threads = threads
        self.beam = beam
        self.mode = mode
        self.jobs: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.model = None
        self.prompt: str | None = None

    def _count_tokens(self, text: str) -> int:
        assert self.model is not None
        return len(self.model.hf_tokenizer.encode(text, add_special_tokens=False).ids)

    def build_prompt(self) -> None:
        """用語集からヒント文を作る。トークン上限に収まるところまで、上の行から採用する。"""
        if not self.terms:
            self.prompt = None
            return
        used: list[str] = []
        for term in self.terms:
            candidate = PROMPT_LEAD + "、".join(used + [term]) + "。"
            if self._count_tokens(candidate) > PROMPT_TOKEN_BUDGET:
                break
            used.append(term)
        self.prompt = PROMPT_LEAD + "、".join(used) + "。" if used else None
        dropped = len(self.terms) - len(used)
        note = f"(上限のため下の {dropped} 語は渡せませんでした)" if dropped else ""
        print(f"用語集: {len(used)} 語をヒントとして使用{note}、置換ルール {len(self.corrections)} 件", flush=True)

    def load(self) -> None:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        print(f"モデル {self.model_name} を読み込み中(初回はダウンロードあり)…", flush=True)
        self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8", cpu_threads=self.threads)
        print(f"モデル読み込み完了 {time.perf_counter() - t0:.1f} 秒", flush=True)
        self.build_prompt()
        self.ready.set()

    def run(self) -> None:
        self.load()
        while True:
            job = self.jobs.get()
            if job is None:
                break
            audio, started, ended = job
            self.handle(audio, started, ended)

    def transcribe(self, audio: numpy.ndarray) -> tuple[str, str, float, list[dict]]:
        """戻り値: (置換後の本文, 置換前の本文, 所要秒, 区間ごとの詳細)。"""
        assert self.model is not None
        t0 = time.perf_counter()
        segments, _info = self.model.transcribe(
            audio,
            language="ja",
            beam_size=self.beam,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=self.prompt,
        )
        parts: list[dict] = []
        for s in segments:
            text = s.text.strip()
            if not text or s.no_speech_prob > 0.6:
                continue
            parts.append({"text": text, "no_speech_prob": round(s.no_speech_prob, 3), "avg_logprob": round(s.avg_logprob, 3)})
        raw = "".join(p["text"] for p in parts)
        return apply_corrections(raw, self.corrections), raw, time.perf_counter() - t0, parts

    def handle(self, audio: numpy.ndarray, started: float, ended: float) -> None:
        start_dt = datetime.fromtimestamp(started)
        name = start_dt.strftime("%Y-%m-%d_%H.%M.%S")
        wav_path = self.out_dir / f"{name}.wav"
        write_wav(wav_path, audio)

        text, raw, elapsed, parts = self.transcribe(audio)
        suspicious = any(h in text for h in HALLUCINATIONS)
        record = {
            "start": start_dt.isoformat(timespec="milliseconds"),
            "end": datetime.fromtimestamp(ended).isoformat(timespec="milliseconds"),
            "duration_s": round(ended - started, 2),
            "text": text,
            "audio": wav_path.name,
            "model": self.model_name,
            "mode": self.mode,
            "transcribe_s": round(elapsed, 2),
            "suspicious": suspicious,
            "segments": parts,
        }
        if raw != text:
            record["text_raw"] = raw
        with (self.out_dir / "notes.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        flag = " ⚠ 定型句(無音の誤認識かも)" if suspicious else ""
        shown = text if text else "(無音・聞き取れず)"
        print(f"{start_dt:%H:%M:%S} 「{shown}」 [{ended - started:.1f}秒の音声を {elapsed:.1f}秒で処理]{flag}", flush=True)


class Recorder:
    """マイクを常時開き、録音区間の開始/終了だけを制御する。"""

    def __init__(self, device, on_segment):
        import sounddevice

        self.blocks: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.pre_roll: deque = deque(maxlen=int(PRE_ROLL_S * SAMPLE_RATE / BLOCK))
        self.active = False
        self.chunks: list[numpy.ndarray] = []
        self.started_at = 0.0
        self.on_segment = on_segment
        self.stream = sounddevice.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK, device=device, callback=self._callback
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"音声入力: {status}", file=sys.stderr, flush=True)
        self.blocks.put(indata[:, 0].copy())

    # フックのスレッドからはコマンドを積むだけにして、状態変更はメインループで行う
    def request(self, command: str) -> None:
        self.commands.put(command)

    def _start(self) -> None:
        if self.active:
            return
        self.active = True
        self.chunks = list(self.pre_roll)
        self.started_at = time.time() - len(self.chunks) * BLOCK / SAMPLE_RATE
        beep(1000)
        print("● 録音中…", flush=True)

    def _stop(self) -> None:
        if not self.active:
            return
        self.active = False
        beep(600)
        audio = numpy.concatenate(self.chunks) if self.chunks else numpy.zeros(0, dtype=numpy.float32)
        ended = time.time()
        print(f"■ 停止({ended - self.started_at:.1f} 秒)。文字起こしへ", flush=True)
        self.on_segment(audio, self.started_at, ended)

    def apply_commands(self) -> None:
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                return
            if cmd == "start":
                self._start()
            elif cmd == "stop":
                self._stop()
            elif cmd == "toggle":
                self._stop() if self.active else self._start()

    def pump(self, timeout: float = 0.1) -> numpy.ndarray | None:
        try:
            block = self.blocks.get(timeout=timeout)
        except queue.Empty:
            return None
        self.pre_roll.append(block)
        if self.active:
            self.chunks.append(block)
        return block

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.active else 0.0


def install_hooks(key_name: str, mode: str, recorder: Recorder) -> list:
    """pynput でキーボード/マウスのグローバルフックを張り、押下をコマンドに変換する。"""
    from pynput import keyboard, mouse

    pressed = {"down": False}

    def on_down() -> None:
        if pressed["down"]:
            return  # キーリピート
        pressed["down"] = True
        recorder.request("toggle" if mode == "toggle" else "start")

    def on_up() -> None:
        pressed["down"] = False
        if mode == "hold":
            recorder.request("stop")

    listeners = []
    if key_name in MOUSE_ALIASES:
        target = getattr(mouse.Button, MOUSE_ALIASES[key_name])

        def on_click(x, y, button, is_pressed):
            if button == target:
                on_down() if is_pressed else on_up()

        listeners.append(mouse.Listener(on_click=on_click))
    else:
        alias = KEY_ALIASES.get(key_name, key_name)
        target = getattr(keyboard.Key, alias, None)
        if target is None:
            if len(key_name) == 1:
                target = keyboard.KeyCode.from_char(key_name)
            else:
                raise SystemExit(f"不明なキー名: {key_name}")

        def matches(key) -> bool:
            if key == target:
                return True
            # 右 Alt が alt_gr として届く環境への保険
            return alias == "alt_r" and key == getattr(keyboard.Key, "alt_gr", None)

        listeners.append(
            keyboard.Listener(
                on_press=lambda k: on_down() if matches(k) else None,
                on_release=lambda k: on_up() if matches(k) else None,
            )
        )
    for listener in listeners:
        listener.start()
    return listeners


def resolve_device(spec: str | None):
    import sounddevice

    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    for idx, dev in enumerate(sounddevice.query_devices()):
        if dev["max_input_channels"] > 0 and spec.lower() in dev["name"].lower():
            return idx
    raise SystemExit(f"入力デバイスが見つかりません: {spec}(--list-devices で確認)")


def db_of(block: numpy.ndarray) -> float:
    rms = float(numpy.sqrt(numpy.mean(block * block))) if block.size else 0.0
    return 20 * math.log10(rms + 1e-9)


def main() -> int:
    setup_console_and_dpi()
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["toggle", "hold", "vad"], default="toggle")
    parser.add_argument("--key", default="right_ctrl", help="toggle/hold で使うキー(既定 right_ctrl)")
    parser.add_argument("--device", help="マイクの番号か名前の一部(既定: システム既定)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--model", default="large-v3-turbo", help="faster-whisper のモデル名(small / medium / large-v3-turbo / kotoba-tech/kotoba-whisper-v2.0-faster など)")
    parser.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 8) // 2))
    parser.add_argument("--beam", type=int, default=2, help="ビーム幅。大きいほど精度は上がるが遅い")
    parser.add_argument("--glossary", default=str(here / "glossary.txt"), help="用語集ファイル(ヒント語と置換ルール)")
    parser.add_argument("--no-prompt", action="store_true", help="用語集のヒントを渡さない(置換ルールは使う)")
    parser.add_argument("--out", default=str(here / "voice_notes"))
    parser.add_argument("--vad-db", type=float, default=-38.0, help="vad モードで声とみなす音量(dBFS)")
    parser.add_argument("--vad-silence", type=float, default=1.2, help="vad モードで区切りとみなす無音秒数")
    parser.add_argument("--max-seconds", type=float, default=90.0, help="1 区間の最長秒数")
    parser.add_argument("--meter", action="store_true", help="入力レベルを 0.5 秒ごとに表示する")
    parser.add_argument("--transcribe-file", help="音声ファイルを文字起こしして終了(速度と精度の確認用)")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice

        print(sounddevice.query_devices())
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    terms, corrections = load_glossary(Path(args.glossary))
    if args.no_prompt:
        terms = []
    transcriber = Transcriber(args.model, out, terms, corrections, args.threads, args.beam, args.mode)

    if args.transcribe_file:
        from faster_whisper import decode_audio

        transcriber.load()
        audio = decode_audio(args.transcribe_file, sampling_rate=SAMPLE_RATE)
        text, raw, elapsed, parts = transcriber.transcribe(audio)
        duration = len(audio) / SAMPLE_RATE
        print(f"音声 {duration:.1f} 秒 → 処理 {elapsed:.1f} 秒(実時間の {elapsed / duration:.2f} 倍)")
        print(f"「{text}」")
        if raw != text:
            print(f"  置換前: 「{raw}」")
        for p in parts:
            print(f"  - {p}")
        return 0

    transcriber.start()
    recorder = Recorder(resolve_device(args.device), lambda a, s, e: transcriber.jobs.put((a, s, e)))
    listeners = install_hooks(args.key, args.mode, recorder) if args.mode != "vad" else []

    import sounddevice

    dev_name = sounddevice.query_devices(recorder.stream.device)["name"]
    how = {
        "toggle": f"{args.key} を押すと録音開始、もう一度押すと停止",
        "hold": f"{args.key} を押している間だけ録音",
        "vad": f"声を出すと自動で録音、{args.vad_silence:.1f} 秒の無音で区切り(しきい値 {args.vad_db:.0f} dBFS)",
    }[args.mode]
    print(f"マイク: {dev_name}\nモード: {args.mode} — {how}\n出力: {out}\nCtrl+C で終了", flush=True)

    onset_blocks = int(0.25 * SAMPLE_RATE / BLOCK)
    silence_blocks = int(args.vad_silence * SAMPLE_RATE / BLOCK)
    voiced = quiet = 0
    last_meter = 0.0
    level = -100.0

    with recorder.stream:
        try:
            while True:
                recorder.apply_commands()
                block = recorder.pump()
                if block is None:
                    continue
                if args.mode == "vad" or args.meter:
                    level = db_of(block)
                if args.meter and time.time() - last_meter > 0.5:
                    last_meter = time.time()
                    bar = "#" * max(0, int((level + 60) / 2))
                    print(f"\r{level:6.1f} dBFS {bar:<30}", end="", flush=True)
                if args.mode == "vad":
                    if not recorder.active:
                        voiced = voiced + 1 if level > args.vad_db else 0
                        if voiced >= onset_blocks:
                            voiced = 0
                            recorder.request("start")
                    else:
                        quiet = 0 if level > args.vad_db else quiet + 1
                        if quiet >= silence_blocks:
                            quiet = 0
                            recorder.request("stop")
                if recorder.active and recorder.elapsed > args.max_seconds:
                    print(f"{args.max_seconds:.0f} 秒を超えたので区切ります", flush=True)
                    recorder.request("stop")
        except KeyboardInterrupt:
            pass
        finally:
            recorder.request("stop")
            recorder.apply_commands()

    for listener in listeners:
        listener.stop()
    pending = transcriber.jobs.qsize()
    if pending:
        print(f"文字起こし待ち {pending} 件を処理してから終了します", flush=True)
    transcriber.jobs.put(None)
    transcriber.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
