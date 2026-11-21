"""Baut die ffmpeg-Kommandozeilen.

Aufbau:

    Videogerät ──┐
    Saalton (St) ─┼─► ENGINE (1x ffmpeg) ──UDP/MPEG-TS──► RELAY 1 (ffmpeg -c copy) ──► RTMP 1
    Dolm. L/R ───┘   - Video 1x encodiert    ──────────► RELAY 2                    ──► RTMP 2
                     - pro Stream eigener     ──────────► RELAY n                    ──► RTMP n
                       Tonmix mit Ducker

Die Engine läuft durchgehend; jeder Stream (Relay) lässt sich einzeln starten/stoppen
und wird bei Verbindungsabbruch automatisch neu gestartet.

Regler werden live über stdin an ffmpeg geschickt ("c<ziel> -1 <befehl> <wert>").
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .config import VOICE_LEFT, VOICE_RIGHT, AppConfig, EngineConfig, StreamConfig

SR = 48000
AFMT = f"aformat=sample_fmts=fltp:sample_rates={SR}:channel_layouts=stereo"
METER_INPUT_IDS = ("in_floor", "in_il", "in_ir")


def db_to_lin(db: float) -> float:
    if db <= -90:
        return 0.0
    return 10.0 ** (db / 20.0)


def fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def threshold_lin(db: float) -> float:
    # sidechaincompress erlaubt 0.000976563 .. 1
    return min(1.0, max(0.000977, db_to_lin(db)))


# --------------------------------------------------------------------------- live params

def stream_params(i: int, s: StreamConfig) -> List[Tuple[str, str, str]]:
    """Alle live änderbaren Parameter eines Streams als (ziel, befehl, wert)."""
    sel_l = 1.0 if s.voice == VOICE_LEFT else 0.0
    sel_r = 1.0 if s.voice == VOICE_RIGHT else 0.0
    return [
        (f"volume@selL{i}", "volume", fmt(sel_l)),
        (f"volume@selR{i}", "volume", fmt(sel_r)),
        (f"volume@voice{i}", "volume", fmt(db_to_lin(s.voice_db))),
        (f"volume@floor{i}", "volume", fmt(db_to_lin(s.floor_db))),
        (f"volume@master{i}", "volume", fmt(db_to_lin(s.master_db))),
        (f"sidechaincompress@duck{i}", "threshold", fmt(threshold_lin(s.threshold_db))),
        (f"sidechaincompress@duck{i}", "ratio", fmt(s.ratio if s.duck_on else 1.0)),
        (f"sidechaincompress@duck{i}", "attack", fmt(max(0.01, s.attack_ms))),
        (f"sidechaincompress@duck{i}", "release", fmt(max(0.01, s.release_ms))),
    ]


def command_line(target: str, cmd: str, value: str) -> str:
    """Zeile für ffmpeg-stdin ('c' = Filterbefehl senden, -1 = sofort)."""
    return f"c{target} -1 {cmd} {value}\n"


# --------------------------------------------------------------------------- engine

def _dshow_input(kind: str, device: str, extra: List[str]) -> List[str]:
    return ["-f", "dshow", "-thread_queue_size", "4096", *extra, "-i", f"{kind}={device}"]


def _inputs(e: EngineConfig) -> List[str]:
    if e.test_mode:
        floor = "anoisesrc=c=pink:a=0.12:r=48000,pan=stereo|c0=c0|c1=c0"
        # "Sprecher" im Wechsel: links 0-2 s, rechts 2-4 s
        interp = ("aevalsrc=0.4*sin(2*PI*600*t)*lt(mod(t\\,4)\\,2)"
                  "|0.4*sin(2*PI*900*t)*gte(mod(t\\,4)\\,2):s=48000:c=stereo")
        video = f"testsrc2=size={e.out_width}x{e.out_height}:rate={e.out_fps}"
        return ["-re", "-f", "lavfi", "-i", video,
                "-re", "-f", "lavfi", "-i", floor,
                "-re", "-f", "lavfi", "-i", interp]

    vextra = ["-rtbufsize", "512M"]
    if e.video_input_size.strip():
        vextra += ["-video_size", e.video_input_size.strip()]
    if e.video_input_fps.strip():
        vextra += ["-framerate", e.video_input_fps.strip()]
    aextra = ["-audio_buffer_size", "20"]
    return (_dshow_input("video", e.video_device, vextra)
            + _dshow_input("audio", e.floor_device, aextra)
            + _dshow_input("audio", e.interp_device, aextra))


def _meter_chain(label: str, meter_id: str) -> str:
    return (f"[{label}]asetnsamples=n=4800:p=0,"
            "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level+RMS_level,"
            f"ametadata=mode=add:key=sid:value={meter_id},"
            "ametadata=mode=print:file=-:direct=1,anullsink")


def _split(src: str, outs: List[str]) -> str:
    if len(outs) == 1:
        return f"[{src}]anull[{outs[0]}]"
    return f"[{src}]asplit={len(outs)}" + "".join(f"[{o}]" for o in outs)


def build_filter_graph(cfg: AppConfig) -> str:
    e = cfg.engine
    n = len(cfg.streams)
    W, H, F = e.out_width, e.out_height, e.out_fps
    parts: List[str] = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={F},format=yuv420p[vout]"
    ]

    def prep(idx: int, delay: int) -> str:
        chain = f"[{idx}:a]aresample={SR}:async=1000,{AFMT}"
        if delay > 0:
            chain += f",adelay=delays={int(delay)}:all=1"
        return chain

    m = 1 if e.meters else 0
    parts.append(prep(1, e.floor_delay_ms) + "[floor]")
    parts.append(prep(2, e.interp_delay_ms) + ",channelsplit=channel_layout=stereo[il][ir]")
    parts.append(_split("floor", [f"f{i}" for i in range(n)] + ["fm"] * m))
    parts.append(_split("il", [f"il{i}" for i in range(n)] + ["ilm"] * m))
    parts.append(_split("ir", [f"ir{i}" for i in range(n)] + ["irm"] * m))

    for i, s in enumerate(cfg.streams):
        p = {(t, c): v for t, c, v in stream_params(i, s)}
        duck = f"sidechaincompress@duck{i}"
        parts += [
            f"[il{i}]volume@selL{i}={p[(f'volume@selL{i}', 'volume')]}[sl{i}]",
            f"[ir{i}]volume@selR{i}={p[(f'volume@selR{i}', 'volume')]}[sr{i}]",
            # gewählter Dolmetscherkanal (mono) -> Schlüssel für den Ducker + Sprachweg
            f"[sl{i}][sr{i}]amix=inputs=2:normalize=0:dropout_transition=0,asplit=2[k{i}][v{i}]",
            f"[v{i}]volume@voice{i}={p[(f'volume@voice{i}', 'volume')]},"
            f"pan=stereo|c0=c0|c1=c0,{AFMT}[vo{i}]",
            f"[f{i}][k{i}]{duck}=threshold={p[(duck, 'threshold')]}:ratio={p[(duck, 'ratio')]}"
            f":attack={p[(duck, 'attack')]}:release={p[(duck, 'release')]}"
            f":detection=rms:link=maximum[fd{i}]",
            f"[fd{i}]volume@floor{i}={p[(f'volume@floor{i}', 'volume')]}[fo{i}]",
            f"[fo{i}][vo{i}]amix=inputs=2:normalize=0:dropout_transition=0,"
            f"volume@master{i}={p[(f'volume@master{i}', 'volume')]},"
            f"alimiter=limit=0.95:level=0,{AFMT}" + (f",asplit=2[a{i}][m{i}]" if m else f"[a{i}]"),
        ]
        if m:
            parts.append(_meter_chain(f"m{i}", f"s{i}"))

    if m:
        parts += [_meter_chain("fm", "in_floor"), _meter_chain("ilm", "in_il"),
                  _meter_chain("irm", "in_ir")]
    return ";".join(parts)


def _video_encoder_args(e: EngineConfig) -> List[str]:
    b = f"{e.video_bitrate_k}k"
    gop = str(max(1, e.out_fps * 2))  # Keyframe alle 2 s (von den meisten Plattformen gefordert)
    common = ["-b:v", b, "-maxrate", b, "-bufsize", f"{e.video_bitrate_k * 2}k", "-g", gop]
    enc = e.video_encoder
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p4", "-tune", "ll", "-rc", "cbr", *common]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-preset", "veryfast", *common]
    if enc == "h264_amf":
        return ["-c:v", enc, "-usage", "lowlatency", "-rc", "cbr", *common]
    return ["-c:v", "libx264", "-preset", e.x264_preset, "-tune", "zerolatency",
            "-profile:v", "high", "-keyint_min", gop, "-sc_threshold", "0",
            "-x264-params", "nal-hrd=cbr", *common]


def stream_port(cfg: AppConfig, i: int) -> int:
    return cfg.engine.base_port + i


def build_engine_args(cfg: AppConfig) -> List[str]:
    e = cfg.engine
    n = len(cfg.streams)
    if n == 0:
        raise ValueError("Keine Streams konfiguriert")
    if not e.test_mode:
        missing = [name for name, v in (("Videogerät", e.video_device),
                                         ("Saalton-Gerät", e.floor_device),
                                         ("Dolmetscher-Gerät", e.interp_device)) if not v]
        if missing:
            raise ValueError("Bitte auswählen: " + ", ".join(missing))

    args = ["-hide_banner", "-loglevel", "warning", "-stats", *_inputs(e),
            "-filter_complex", build_filter_graph(cfg), "-map", "[vout]"]
    for i in range(n):
        args += ["-map", f"[a{i}]"]
    args += _video_encoder_args(e)
    args += ["-c:a", "aac", "-b:a", f"{e.audio_bitrate_k}k", "-ar", str(SR), "-ac", "2",
             "-max_muxing_queue_size", "2048", "-f", "tee"]
    slaves = [f"[f=mpegts:select=\\'v:0,a:{i}\\':onfail=ignore]"
              f"udp://127.0.0.1:{stream_port(cfg, i)}?pkt_size=1316"
              for i in range(n)]
    args.append("|".join(slaves))
    return args


# --------------------------------------------------------------------------- relay

def rtmp_target(s: StreamConfig) -> str:
    url = s.url.strip()
    key = s.key.strip()
    if key:
        url = url.rstrip("/") + "/" + key
    return url


def build_relay_args(cfg: AppConfig, i: int) -> List[str]:
    s = cfg.streams[i]
    target = rtmp_target(s)
    if not target:
        raise ValueError(f"{s.name}: keine RTMP-URL eingetragen")
    # timeout (µs): Relay beendet sich, wenn 5 s keine Daten von der Engine kommen
    # -> die App startet es dann automatisch neu.
    src = (f"udp://127.0.0.1:{stream_port(cfg, i)}"
           "?fifo_size=1000000&overrun_nonfatal=1&timeout=5000000")
    return ["-hide_banner", "-loglevel", "error", "-stats",
            "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "2000000",
            "-i", src, "-map", "0:v", "-map", "0:a", "-c", "copy",
            "-f", "flv", "-flvflags", "no_duration_filesize", target]


# --------------------------------------------------------------------------- meters

class MeterParser:
    """Liest die ametadata-Ausgabe (stdout der Engine): liefert {id: (peak_db, rms_db)}."""

    def __init__(self) -> None:
        self._buf = ""
        self._cur: Dict[str, float] = {}

    def feed(self, text: str) -> Dict[str, Tuple[float, float]]:
        out: Dict[str, Tuple[float, float]] = {}
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("frame:"):
                self._cur = {}
            elif line.startswith("lavfi.astats.Overall.Peak_level="):
                self._cur["peak"] = _to_db(line.split("=", 1)[1])
            elif line.startswith("lavfi.astats.Overall.RMS_level="):
                self._cur["rms"] = _to_db(line.split("=", 1)[1])
            elif line.startswith("sid="):
                out[line[4:]] = (self._cur.get("peak", -120.0), self._cur.get("rms", -120.0))
                self._cur = {}
        return out


def _to_db(v: str) -> float:
    try:
        x = float(v)
    except ValueError:
        return -120.0
    return -120.0 if math.isinf(x) or math.isnan(x) else x
