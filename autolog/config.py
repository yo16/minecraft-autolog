"""config.toml の読み込みと既定値、シーズン名の決定。"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_DIR / "config.toml"

DEFAULTS: dict[str, Any] = {
    "data_dir": "%LOCALAPPDATA%/minecraft-autolog",
    "minecraft_dir": "%APPDATA%/.minecraft",
    "minecraft_log_dir": "",
    "season": "",
    "capture": {
        "interval_s": 5,
        "format": "webp",
        "quality": 80,
        "static_threshold": 0.01,
        "keep": "all",
        "near_events_window_s": 120,
    },
    "voice": {
        "enabled": True,
        "mode": "toggle",
        "key": "right_ctrl",
        "device": "",
        "model": "large-v3-turbo",
        "threads": 8,
        "beam": 2,
        "glossary": "glossary.txt",
        "vad_db": -38,
        "vad_silence_s": 1.2,
        "max_seconds": 90,
    },
    "export": {
        "frames_per_note": 1,
        "long_note_s": 15,
        "include_advancements": True,
        "include_deaths": True,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def expand_path(value: str) -> Path:
    """%APPDATA% や ~ を展開したパス。"""
    return Path(os.path.expanduser(os.path.expandvars(value)))


class Config:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.data_dir = expand_path(raw["data_dir"])
        self.minecraft_dir = expand_path(raw["minecraft_dir"])
        self.minecraft_log_dir = expand_path(raw["minecraft_log_dir"]) if raw["minecraft_log_dir"] else None
        self.season = raw["season"]
        self.capture = raw["capture"]
        self.voice = raw["voice"]
        self.export = raw["export"]

    @property
    def latest_log(self) -> Path:
        return self.minecraft_dir / "logs" / "latest.log"

    @property
    def saves_dir(self) -> Path:
        return self.minecraft_dir / "saves"

    @property
    def screenshots_dir(self) -> Path:
        return self.minecraft_dir / "screenshots"


def load_config(path: Path | None = None) -> Config:
    """config.toml を読んで既定値に重ねる。ファイルがなければ既定値だけで動く。"""
    path = path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)
    return Config(_merge(DEFAULTS, raw))


def resolve_season(season: str, minecraft_log_dir: Path | None, world: str | None) -> str:
    """シーズン名を決める(設計 §3.8): 明示 > minecraft-log の current_season > ワールド名。"""
    if season:
        return season
    if minecraft_log_dir is not None:
        state_path = minecraft_log_dir / "log" / "import_state.json"
        try:
            current = json.loads(state_path.read_text(encoding="utf-8")).get("current_season")
        except (OSError, ValueError):
            current = None
        if current:
            return str(current)
    if world:
        return f"world-{world}"
    return "unknown"
