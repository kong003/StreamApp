"""DirectShow-Geräte über ffmpeg auflisten und ffmpeg finden."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

CREATE_NO_WINDOW = 0x08000000


@dataclass
class Device:
    name: str          # Anzeigename
    alt_name: str = ""  # "@device_..." – eindeutig, falls der Name Sonderzeichen enthält

    @property
    def ident(self) -> str:
        """Was an ffmpeg übergeben wird (dshow trennt video=..:audio=.. am Doppelpunkt)."""
        if ":" in self.name and self.alt_name:
            return self.alt_name
        return self.name


_LINE = re.compile(r'"(?P<name>.+)"(?:\s+\((?P<kind>video|audio|none)\))?\s*$')
_ALT = re.compile(r'Alternative name\s+"(?P<alt>.+)"')


def parse_dshow_list(text: str) -> Tuple[List[Device], List[Device]]:
    """Versteht das alte (Überschriften) und neue Format ("Name" (video))."""
    video: List[Device] = []
    audio: List[Device] = []
    section = None
    last = None
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        m = _ALT.search(line)
        if m:
            if last is not None:
                last.alt_name = m.group("alt")
            continue
        m = _LINE.search(line)
        if not m:
            continue
        kind = m.group("kind") or section
        dev = Device(m.group("name"))
        if kind == "video":
            video.append(dev)
        elif kind == "audio":
            audio.append(dev)
        last = dev if kind in ("video", "audio") else None
    return video, audio


def list_dshow_devices(ffmpeg: str) -> Tuple[List[Device], List[Device]]:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        res = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow",
                              "-i", "dummy"], capture_output=True, timeout=15, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return [], []
    return parse_dshow_list(res.stderr.decode("utf-8", errors="replace"))


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_ffmpeg(configured: str) -> str:
    """Konfigurierter Pfad > ffmpeg(.exe) neben der App > ffmpeg/ Unterordner > PATH."""
    if configured and configured != "ffmpeg" and os.path.isfile(configured):
        return configured
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for cand in (app_dir() / exe, app_dir() / "ffmpeg" / exe, app_dir() / "ffmpeg" / "bin" / exe):
        if cand.is_file():
            return str(cand)
    return shutil.which(configured or "ffmpeg") or shutil.which("ffmpeg") or (configured or "ffmpeg")
