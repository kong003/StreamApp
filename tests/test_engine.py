import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from streamapp.config import (VOICE_LEFT, VOICE_RIGHT, AppConfig, StreamConfig, load_config,
                              save_config)
from streamapp.devices import parse_dshow_list
from streamapp import engine as eng

FFMPEG = shutil.which("ffmpeg")

NEW_FORMAT = '''
[dshow @ 0000020] "Cam Link 4K" (video)
[dshow @ 0000020]   Alternative name "@device_pnp_\\\\?\\usb#vid_0fd9"
[dshow @ 0000020] "Mikrofon (2- Focusrite USB)" (audio)
[dshow @ 0000020]   Alternative name "@device_cm_{33D9A762}\\wave_{AAA}"
[dshow @ 0000020] "Line: In 3/4" (audio)
[dshow @ 0000020]   Alternative name "@device_cm_{33D9A762}\\wave_{BBB}"
'''

OLD_FORMAT = '''
[dshow @ 01] DirectShow video devices (some may be both video and audio devices)
[dshow @ 01]  "Decklink Video Capture"
[dshow @ 01]     Alternative name "@device_sw_{860BB310}"
[dshow @ 01] DirectShow audio devices
[dshow @ 01]  "Line 1 (Virtual Audio)"
[dshow @ 01]     Alternative name "@device_cm_{X}"
'''


class DeviceParsing(unittest.TestCase):
    def test_new_format(self):
        video, audio = parse_dshow_list(NEW_FORMAT)
        self.assertEqual([d.name for d in video], ["Cam Link 4K"])
        self.assertEqual([d.name for d in audio], ["Mikrofon (2- Focusrite USB)", "Line: In 3/4"])
        self.assertEqual(audio[0].ident, "Mikrofon (2- Focusrite USB)")
        # Doppelpunkt im Namen -> eindeutiger Alternativname wird verwendet
        self.assertTrue(audio[1].ident.startswith("@device_cm_"))

    def test_old_format(self):
        video, audio = parse_dshow_list(OLD_FORMAT)
        self.assertEqual([d.name for d in video], ["Decklink Video Capture"])
        self.assertEqual([d.name for d in audio], ["Line 1 (Virtual Audio)"])


class Params(unittest.TestCase):
    def test_voice_selection(self):
        p = {(t, c): v for t, c, v in eng.stream_params(2, StreamConfig(voice=VOICE_RIGHT))}
        self.assertEqual(p[("volume@selL2", "volume")], "0")
        self.assertEqual(p[("volume@selR2", "volume")], "1")

    def test_ducker_off_means_no_subtraction(self):
        p = {(t, c): v for t, c, v in eng.stream_params(0, StreamConfig(duck_on=False, duck_db=12))}
        self.assertEqual(p[("volume@depth0", "volume")], "0")

    def test_duck_depth(self):
        # Saalton · (1 + g) muss der gewünschten Absenkung entsprechen
        for db in (6, 15, 30):
            self.assertAlmostEqual(1 + eng.duck_depth_gain(db), eng.db_to_lin(-db))

    def test_threshold_clamped(self):
        self.assertAlmostEqual(eng.threshold_lin(-90), 0.000977)
        self.assertAlmostEqual(eng.threshold_lin(0), 1.0)

    def test_command_line(self):
        self.assertEqual(eng.command_line("volume@floor1", "volume", "0.5"),
                         "cvolume@floor1 -1 volume 0.5\n")

    def test_rtmp_target(self):
        self.assertEqual(eng.rtmp_target(StreamConfig(url="rtmp://x/live2/", key="abc")),
                         "rtmp://x/live2/abc")
        self.assertEqual(eng.rtmp_target(StreamConfig(url="rtmp://x/live/abc")), "rtmp://x/live/abc")

    def test_missing_devices(self):
        with self.assertRaises(ValueError):
            eng.build_engine_args(AppConfig())


class Meters(unittest.TestCase):
    def test_parse_split_chunks(self):
        mp = eng.MeterParser()
        text = ("frame:0 pts:0 pts_time:0\nlavfi.astats.Overall.Peak_level=-6.5\n"
                "lavfi.astats.Overall.RMS_level=-12.25\nsid=s1\n"
                "frame:0 pts:0 pts_time:0\nlavfi.astats.Overall.Peak_level=-inf\n"
                "lavfi.astats.Overall.RMS_level=-inf\nsid=in_il\n")
        out = mp.feed(text[:50])
        out.update(mp.feed(text[50:]))
        self.assertEqual(out["s1"], (-6.5, -12.25))
        self.assertEqual(out["in_il"], (-120.0, -120.0))


class Preview(unittest.TestCase):
    def test_mjpeg_parser_chunks(self):
        def part(data):
            return (b"--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n" % len(data)
                    + data + b"\r\n")
        stream = part(b"\xff\xd8AAAA\xff\xd9") + part(b"\xff\xd8BBBBBB\xff\xd9")
        mp = eng.MjpegParser()
        got = []
        for i in range(0, len(stream), 7):
            got += mp.feed(stream[i:i + 7])
        self.assertEqual(got, [b"\xff\xd8AAAA\xff\xd9", b"\xff\xd8BBBBBB\xff\xd9"])

    def test_encoder_options_only_main_video(self):
        cfg = AppConfig()
        cfg.engine.test_mode = True
        args = eng.build_engine_args(cfg)
        self.assertIn("-c:v:0", args)
        self.assertIn("-b:v:0", args)
        self.assertIn("-preset:v:0", args)
        self.assertNotIn("-b:v", args)
        self.assertIn("-c:v:1", args)
        self.assertTrue(args[-1].endswith(f"tcp://127.0.0.1:{cfg.engine.preview_port}"))

    def test_preview_off(self):
        cfg = AppConfig()
        cfg.engine.test_mode = True
        cfg.engine.preview = False
        args = eng.build_engine_args(cfg)
        self.assertNotIn("[vprev]", args)
        self.assertNotIn("tcp://", args[-1])


class ConfigRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        cfg = AppConfig()
        cfg.streams[1].duck_db = 22
        cfg.engine.video_device = "Cam"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            save_config(cfg, p)
            back = load_config(p)
        self.assertEqual(back.streams[1].duck_db, 22)
        self.assertEqual(back.engine.video_device, "Cam")
        self.assertEqual(len(back.streams), 3)

    def test_old_config_with_ratio_still_loads(self):
        cfg = AppConfig.from_dict({"streams": [{"name": "Alt", "ratio": 10, "voice": "left"}]})
        self.assertEqual(cfg.streams[0].name, "Alt")
        self.assertEqual(cfg.streams[0].duck_db, 15.0)


@unittest.skipUnless(FFMPEG, "ffmpeg nicht installiert")
class FfmpegGraph(unittest.TestCase):
    """Lässt den echten Filtergraph 1 s lang mit Testquellen laufen."""

    def test_graph_runs(self):
        cfg = AppConfig()
        cfg.engine.test_mode = True
        cfg.engine.out_width, cfg.engine.out_height = 320, 180
        cfg.engine.floor_delay_ms = 40
        cfg.streams.append(StreamConfig(name="x", voice=VOICE_LEFT, duck_on=False))
        args = eng.build_engine_args(cfg)
        args = [a for a in args if a != "-re"]
        fc = args[args.index("-filter_complex") + 1]
        maps = []
        for i, a in enumerate(args):
            if a == "-map":
                maps += ["-map", args[i + 1]]
        inputs = args[args.index("-stats") + 1: args.index("-filter_complex")]
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", fc,
               *maps, "-t", "1", "-f", "null", "-"]
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr.decode(errors="replace"))
        self.assertIn(b"sid=s3", res.stdout)

    def test_ducker_lowers_by_exact_db(self):
        """Saalton = konstanter Ton, Dolmetscher spricht 0–2 s, schweigt 2–4 s.
        Gemessen wird der Saalton-Pegel (f0) nach dem Ducker."""
        cfg = AppConfig()
        cfg.engine.test_mode = True
        cfg.engine.preview = False
        cfg.streams = [StreamConfig(voice=VOICE_LEFT, duck_db=12, attack_ms=10, release_ms=50),
                       # ohne Dolmetscher (Steuersignal = exakte Stille): nie absenken
                       StreamConfig(voice="none", duck_db=12, attack_ms=10, release_ms=50)]
        fc = eng.build_filter_graph(cfg)
        inputs = ["-f", "lavfi", "-i", "color=s=64x36:r=5:d=4",
                  "-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*300*t)|0.5*sin(2*PI*300*t):s=48000:d=4",
                  "-f", "lavfi", "-i",
                  "aevalsrc=0.3*sin(2*PI*700*t)*lt(t\\,2)|0:s=48000:c=stereo:d=4"]
        res = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *inputs,
                              "-filter_complex", fc, "-map", "[vout]", "-map", "[a0]",
                              "-map", "[a1]", "-f", "null", "-"], capture_output=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr.decode(errors="replace"))
        levels = {"f0": [], "f1": []}
        cur = {}
        for line in res.stdout.decode().splitlines():
            if line.startswith("lavfi.astats.Overall.RMS_level="):
                cur["rms"] = float(line.split("=", 1)[1])
            elif line.startswith("sid=") and line[4:] in levels:
                levels[line[4:]].append(cur["rms"])
        f0, f1 = levels["f0"], levels["f1"]
        # Blöcke à 0,1 s: 0,5–1,5 s = Dolmetscher spricht, 2,5–3,5 s = Stille
        ducked = sum(f0[5:15]) / 10
        normal = sum(f0[25:35]) / 10
        self.assertAlmostEqual(normal - ducked, 12.0, delta=0.3)
        # Stream ohne Dolmetscher bleibt immer auf vollem Saalton-Pegel
        self.assertAlmostEqual(min(f1[2:38]), normal, delta=0.3)
        self.assertAlmostEqual(max(f1[2:38]), normal, delta=0.3)


if __name__ == "__main__":
    unittest.main()
