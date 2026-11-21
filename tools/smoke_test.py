"""End-to-End-Test ohne Hardware: Engine (Testmodus) -> UDP -> Relays -> FLV-Dateien.

Prüft außerdem, dass Live-Befehle über die stdin-Pipe ankommen (wichtig unter Windows).
Aufruf:  python tools/smoke_test.py [pfad/zu/ffmpeg]
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from streamapp.config import AppConfig  # noqa: E402
from streamapp.engine import (MeterParser, build_engine_args, build_relay_args,  # noqa: E402
                              command_line, stream_params)

FF = sys.argv[1] if len(sys.argv) > 1 else "ffmpeg"


def main() -> int:
    out = tempfile.mkdtemp()
    cfg = AppConfig()
    cfg.engine.test_mode = True
    cfg.engine.out_width, cfg.engine.out_height = 640, 360
    for i, s in enumerate(cfg.streams):
        s.url = os.path.join(out, f"s{i}.flv")

    eng = subprocess.Popen([FF, *build_engine_args(cfg)], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    levels, errs, mp = {}, [], MeterParser()
    threading.Thread(target=lambda: [levels.update(mp.feed(l.decode(errors="replace")))
                                     for l in iter(eng.stdout.readline, b"")], daemon=True).start()
    threading.Thread(target=lambda: [errs.append(l.decode(errors="replace"))
                                     for l in iter(eng.stderr.readline, b"")], daemon=True).start()
    time.sleep(1.5)
    relays = [subprocess.Popen([FF, "-y", *build_relay_args(cfg, i)], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
              for i in range(len(cfg.streams))]
    time.sleep(4)

    cfg.streams[1].voice = "right"
    cfg.streams[1].duck_db = 20
    sent = 0
    for t, c, v in stream_params(1, cfg.streams[1]):
        eng.stdin.write(command_line(t, c, v).encode())
        eng.stdin.flush()
        sent += 1
        time.sleep(0.2)
    time.sleep(1.5)

    for r in relays:
        r.stdin.write(b"q")
        r.stdin.flush()
    for r in relays:
        try:
            r.wait(10)
        except subprocess.TimeoutExpired:
            r.kill()
    eng.stdin.write(b"q")
    eng.stdin.flush()
    try:
        eng.wait(10)
    except subprocess.TimeoutExpired:
        eng.kill()
    time.sleep(0.5)

    text = "".join(errs).replace("\r", "\n")
    ok_replies = sum(1 for l in text.split("\n") if "reply for stream 0: ret:0" in l)
    print("Pegel:", {k: round(v[1], 1) for k, v in levels.items()})
    print(f"Live-Befehle bestätigt: {ok_replies}/{sent}")
    failed = False
    for i in range(len(cfg.streams)):
        path = cfg.streams[i].url
        size = os.path.getsize(path) if os.path.exists(path) else 0
        probe = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True)
        streams = [l.strip() for l in probe.stderr.decode(errors="replace").splitlines()
                   if "Stream #" in l]
        print(f"Stream {i}: {size} Bytes, {streams}")
        if size < 50_000 or not any("Video" in s for s in streams) \
                or not any("Audio" in s for s in streams):
            failed = True
    if ok_replies < sent:
        failed = True
    if not {"in_floor", "in_il", "in_ir", "s0", "s1", "s2"} <= set(levels):
        print("Pegelmeter fehlen")
        failed = True
    if failed:
        print("---- Engine-Log ----\n" + text[-3000:])
        print("SMOKE TEST FEHLGESCHLAGEN")
        return 1
    print("SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
