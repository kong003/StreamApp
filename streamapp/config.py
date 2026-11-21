"""Konfiguration (wird als JSON gespeichert)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List

# Mögliche Werte für StreamConfig.voice
VOICE_NONE = "none"    # nur Saalton
VOICE_LEFT = "left"    # Dolmetscher-Kanal links
VOICE_RIGHT = "right"  # Dolmetscher-Kanal rechts


@dataclass
class StreamConfig:
    name: str = "Stream"
    url: str = ""            # z.B. rtmp://a.rtmp.youtube.com/live2
    key: str = ""            # Streamschlüssel (wird an die URL angehängt)
    voice: str = VOICE_NONE  # welcher Dolmetscherkanal in diesen Stream gemischt wird
    floor_db: float = 0.0    # Pegel Saalton
    voice_db: float = 0.0    # Pegel Dolmetscher
    master_db: float = 0.0   # Summenpegel
    duck_on: bool = True
    threshold_db: float = -36.0  # ab hier duckt der Saalton
    ratio: float = 10.0          # 1..20 (höher = stärker absenken)
    attack_ms: float = 20.0
    release_ms: float = 600.0


@dataclass
class EngineConfig:
    ffmpeg_path: str = "ffmpeg"
    test_mode: bool = False  # Testbild + Testtöne statt Hardware
    meters: bool = True

    video_device: str = ""
    video_input_size: str = ""   # z.B. "1920x1080"; leer = Gerätestandard
    video_input_fps: str = ""    # leer = Gerätestandard
    floor_device: str = ""       # Stereo-Saalton
    interp_device: str = ""      # L = Dolmetscher 1, R = Dolmetscher 2
    floor_delay_ms: int = 0
    interp_delay_ms: int = 0
    interp_left_label: str = "Sprache L"
    interp_right_label: str = "Sprache R"

    out_width: int = 1920
    out_height: int = 1080
    out_fps: int = 30
    video_encoder: str = "libx264"  # libx264 | h264_nvenc | h264_qsv | h264_amf
    video_bitrate_k: int = 6000
    x264_preset: str = "veryfast"
    audio_bitrate_k: int = 160

    base_port: int = 23000  # lokale UDP-Ports zwischen Engine und Stream-Relays


def default_streams() -> List[StreamConfig]:
    return [
        StreamConfig(name="Original", voice=VOICE_NONE),
        StreamConfig(name="Sprache 1", voice=VOICE_LEFT),
        StreamConfig(name="Sprache 2", voice=VOICE_RIGHT),
    ]


@dataclass
class AppConfig:
    engine: EngineConfig = field(default_factory=EngineConfig)
    streams: List[StreamConfig] = field(default_factory=default_streams)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "AppConfig":
        def build(cls, data):
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in (data or {}).items() if k in names})

        cfg = AppConfig(engine=build(EngineConfig, d.get("engine")))
        if isinstance(d.get("streams"), list):
            cfg.streams = [build(StreamConfig, s) for s in d["streams"]]
        return cfg


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "StreamApp" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    try:
        return AppConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
