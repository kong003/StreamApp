"""Hauptfenster."""
from __future__ import annotations

import time
from typing import Callable, Dict, List

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import engine as eng
from .config import (VOICE_LEFT, VOICE_NONE, VOICE_RIGHT, AppConfig, StreamConfig,
                     save_config)
from .devices import list_dshow_devices, resolve_ffmpeg
from .procs import EngineProcess, FFProcess

STATUS_COLORS = {"off": "#888", "starting": "#d90", "running": "#2a2", "error": "#d33"}


# =========================================================================== widgets

class LevelMeter(QWidget):
    """Horizontaler Pegelmesser −60…0 dBFS (Balken = RMS, Strich = Peak)."""

    MIN_DB = -60.0

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self.label = label
        self.peak = self.rms = -120.0
        self._t = 0.0
        self.setMinimumHeight(18)
        self.setMinimumWidth(120)

    def set_level(self, peak: float, rms: float) -> None:
        self.peak, self.rms, self._t = peak, rms, time.monotonic()
        self.update()

    def decay(self) -> None:
        if self.rms > -120 and time.monotonic() - self._t > 0.6:
            self.peak = self.rms = -120.0
            self.update()

    def _x(self, db: float, w: float) -> float:
        return max(0.0, min(1.0, (db - self.MIN_DB) / -self.MIN_DB)) * w

    def paintEvent(self, _ev):
        p = QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, QColor("#222"))
        w = r.width()
        x = self._x(self.rms, w)
        if x > 0:
            for lo, hi, col in ((-60, -18, "#2c2"), (-18, -6, "#dd2"), (-6, 0, "#e33")):
                a, b = self._x(lo, w), min(x, self._x(hi, w))
                if b > a:
                    p.fillRect(QRectF(a, 1, b - a, r.height() - 1), QColor(col))
        if self.peak > self.MIN_DB:
            px = self._x(self.peak, w)
            p.fillRect(QRectF(px - 1, 1, 2, r.height() - 1), QColor("#fff"))
        p.setPen(QColor("#555"))
        for db in (-48, -36, -24, -12, -6):
            gx = self._x(db, w)
            p.drawLine(int(gx), r.bottom() - 3, int(gx), r.bottom())
        p.setPen(QColor("#ddd"))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        txt = f"{self.label}  {self.rms:5.1f} dB" if self.rms > -120 else f"{self.label}  —"
        p.drawText(r.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)


class ParamSlider(QWidget):
    """Schieberegler mit Beschriftung und Wertanzeige. Doppelklick auf Wert = Standard."""

    changed = Signal(float)

    def __init__(self, title: str, lo: float, hi: float, step: float, unit: str,
                 value: float, default: float, parent=None):
        super().__init__(parent)
        self._step, self._unit, self._default = step, unit, default
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(lo / step), round(hi / step))
        self.value_label = QLabel()
        self.value_label.setMinimumWidth(64)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setToolTip("Doppelklick: Standardwert")
        self.value_label.mouseDoubleClickEvent = lambda _e: self.set_value(self._default, emit=True)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setVerticalSpacing(0)
        lay.addWidget(QLabel(title), 0, 0)
        lay.addWidget(self.value_label, 0, 1)
        lay.addWidget(self.slider, 1, 0, 1, 2)
        self.slider.valueChanged.connect(self._on_change)
        self.set_value(value)

    def value(self) -> float:
        return self.slider.value() * self._step

    def set_value(self, v: float, emit: bool = False) -> None:
        self.slider.blockSignals(not emit)
        self.slider.setValue(round(v / self._step))
        self.slider.blockSignals(False)
        self._update_label()

    def _update_label(self):
        v = self.value()
        txt = f"{v:.0f}" if self._step >= 1 else f"{v:.1f}"
        self.value_label.setText(f"{txt} {self._unit}".strip())

    def _on_change(self, _):
        self._update_label()
        self.changed.emit(self.value())


# =========================================================================== stream strip

class StreamStrip(QGroupBox):
    live_changed = Signal(int)       # Index -> Live-Parameter neu senden
    config_changed = Signal()
    start_clicked = Signal(int)
    stop_clicked = Signal(int)
    remove_clicked = Signal(int)

    def __init__(self, index: int, cfg: StreamConfig, labels: Dict[str, str], parent=None):
        super().__init__(parent)
        self.index = index
        self.cfg = cfg
        self.setMinimumWidth(300)
        self.setMaximumWidth(380)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.name_edit = QLineEdit(cfg.name)
        self.name_edit.setFont(QFont(self.font().family(), self.font().pointSize() + 2, QFont.Bold))
        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedWidth(28)
        self.remove_btn.setToolTip("Stream entfernen")
        top.addWidget(self.name_edit)
        top.addWidget(self.remove_btn)
        v.addLayout(top)

        form = QFormLayout()
        self.url_edit = QLineEdit(cfg.url)
        self.url_edit.setPlaceholderText("rtmp://server/app")
        self.key_edit = QLineEdit(cfg.key)
        self.key_edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key_edit.setPlaceholderText("Streamschlüssel")
        self.voice_combo = QComboBox()
        form.addRow("RTMP-URL", self.url_edit)
        form.addRow("Schlüssel", self.key_edit)
        form.addRow("Sprache", self.voice_combo)
        v.addLayout(form)
        self.set_labels(labels)

        mix = QGroupBox("Mix")
        mv = QVBoxLayout(mix)
        self.floor_s = ParamSlider("Saalton", -60, 12, 0.5, "dB", cfg.floor_db, 0)
        self.voice_s = ParamSlider("Dolmetscher", -60, 12, 0.5, "dB", cfg.voice_db, 0)
        self.master_s = ParamSlider("Summe", -30, 12, 0.5, "dB", cfg.master_db, 0)
        for w in (self.floor_s, self.voice_s, self.master_s):
            mv.addWidget(w)
        v.addWidget(mix)

        self.duck_box = QGroupBox("Ducker (Saalton leiser, wenn Dolmetscher spricht)")
        self.duck_box.setCheckable(True)
        self.duck_box.setChecked(cfg.duck_on)
        dv = QVBoxLayout(self.duck_box)
        self.thr_s = ParamSlider("Schwelle", -60, 0, 1, "dB", cfg.threshold_db, -36)
        self.ratio_s = ParamSlider("Stärke (Ratio)", 1, 20, 0.5, ": 1", cfg.ratio, 10)
        self.att_s = ParamSlider("Attack", 1, 500, 1, "ms", cfg.attack_ms, 20)
        self.rel_s = ParamSlider("Release", 50, 3000, 10, "ms", cfg.release_ms, 600)
        for w in (self.thr_s, self.ratio_s, self.att_s, self.rel_s):
            dv.addWidget(w)
        v.addWidget(self.duck_box)

        self.meter = LevelMeter("Ausgang")
        v.addWidget(self.meter)

        bottom = QHBoxLayout()
        self.go_btn = QPushButton("▶ Stream starten")
        self.go_btn.setCheckable(True)
        self.go_btn.setMinimumHeight(32)
        bottom.addWidget(self.go_btn)
        v.addLayout(bottom)
        self.status_label = QLabel("Gestoppt")
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#888; font-size:10px")
        v.addWidget(self.stats_label)
        v.addStretch(1)

        # ---- Verbindungen
        self.name_edit.textChanged.connect(lambda t: self._set("name", t, live=False))
        self.url_edit.textChanged.connect(lambda t: self._set("url", t.strip(), live=False))
        self.key_edit.textChanged.connect(lambda t: self._set("key", t.strip(), live=False))
        self.voice_combo.currentIndexChanged.connect(
            lambda _i: self._set("voice", self.voice_combo.currentData()))
        self.floor_s.changed.connect(lambda x: self._set("floor_db", x))
        self.voice_s.changed.connect(lambda x: self._set("voice_db", x))
        self.master_s.changed.connect(lambda x: self._set("master_db", x))
        self.duck_box.toggled.connect(lambda b: self._set("duck_on", b))
        self.thr_s.changed.connect(lambda x: self._set("threshold_db", x))
        self.ratio_s.changed.connect(lambda x: self._set("ratio", x))
        self.att_s.changed.connect(lambda x: self._set("attack_ms", x))
        self.rel_s.changed.connect(lambda x: self._set("release_ms", x))
        self.go_btn.clicked.connect(self._on_go)
        self.remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self.index))
        self._update_enabled()

    def set_labels(self, labels: Dict[str, str]) -> None:
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("Nur Saalton (Original)", VOICE_NONE)
        self.voice_combo.addItem(f"{labels['left']} (Dolmetscher links)", VOICE_LEFT)
        self.voice_combo.addItem(f"{labels['right']} (Dolmetscher rechts)", VOICE_RIGHT)
        self.voice_combo.setCurrentIndex(max(0, self.voice_combo.findData(self.cfg.voice)))
        self.voice_combo.blockSignals(False)

    def _set(self, attr: str, value, live: bool = True) -> None:
        setattr(self.cfg, attr, value)
        if attr == "voice":
            self._update_enabled()
        self.config_changed.emit()
        if live:
            self.live_changed.emit(self.index)

    def _update_enabled(self) -> None:
        has_voice = self.cfg.voice != VOICE_NONE
        self.voice_s.setEnabled(has_voice)
        self.duck_box.setEnabled(has_voice)

    def _on_go(self, checked: bool) -> None:
        (self.start_clicked if checked else self.stop_clicked).emit(self.index)

    def set_running_wanted(self, wanted: bool) -> None:
        self.go_btn.setChecked(wanted)
        self.go_btn.setText("■ Stream stoppen" if wanted else "▶ Stream starten")
        for w in (self.url_edit, self.key_edit, self.remove_btn):
            w.setEnabled(not wanted)

    def set_status(self, state: str, text: str) -> None:
        self.status_label.setText(f"<b style='color:{STATUS_COLORS.get(state, '#888')}'>●</b> {text}")
        if state != "running":
            self.stats_label.setText("")


# =========================================================================== settings

class SettingsPage(QWidget):
    changed = Signal()
    labels_changed = Signal()

    ENCODERS = [("libx264", "x264 (CPU)"), ("h264_nvenc", "NVIDIA NVENC"),
                ("h264_qsv", "Intel QuickSync"), ("h264_amf", "AMD AMF")]
    PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]

    def __init__(self, cfg: AppConfig, log: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.log = log
        e = cfg.engine
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)

        # ---- ffmpeg
        g = QGroupBox("ffmpeg")
        f = QFormLayout(g)
        self.ff_edit = QLineEdit(e.ffmpeg_path)
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse_ffmpeg)
        row = QHBoxLayout()
        row.addWidget(self.ff_edit)
        row.addWidget(browse)
        f.addRow("Pfad zu ffmpeg.exe", row)
        self.ff_resolved = QLabel()
        self.ff_resolved.setStyleSheet("color:#888")
        f.addRow("", self.ff_resolved)
        lay.addWidget(g)

        # ---- Eingänge
        g = QGroupBox("Eingänge")
        f = QFormLayout(g)
        self.test_cb = QCheckBox("Testmodus (Testbild + Testtöne, keine Hardware nötig)")
        self.test_cb.setChecked(e.test_mode)
        f.addRow(self.test_cb)
        self.video_combo = self._dev_combo(e.video_device)
        self.floor_combo = self._dev_combo(e.floor_device)
        self.interp_combo = self._dev_combo(e.interp_device)
        refresh = QPushButton("Geräte suchen")
        refresh.clicked.connect(self.refresh_devices)
        f.addRow("", refresh)
        f.addRow("Videogerät", self.video_combo)
        self.vsize_edit = QLineEdit(e.video_input_size)
        self.vsize_edit.setPlaceholderText("Gerätestandard (z.B. 1920x1080)")
        self.vfps_edit = QLineEdit(e.video_input_fps)
        self.vfps_edit.setPlaceholderText("Gerätestandard (z.B. 30 oder 25)")
        f.addRow("Video-Eingangsformat", self.vsize_edit)
        f.addRow("Video-Eingangs-FPS", self.vfps_edit)
        f.addRow("Saalton (Stereo)", self.floor_combo)
        f.addRow("Dolmetscher (L / R)", self.interp_combo)
        self.left_edit = QLineEdit(e.interp_left_label)
        self.right_edit = QLineEdit(e.interp_right_label)
        f.addRow("Name Kanal links", self.left_edit)
        f.addRow("Name Kanal rechts", self.right_edit)
        self.floor_delay = self._spin(0, 2000, e.floor_delay_ms, " ms")
        self.interp_delay = self._spin(0, 2000, e.interp_delay_ms, " ms")
        f.addRow("Verzögerung Saalton", self.floor_delay)
        f.addRow("Verzögerung Dolmetscher", self.interp_delay)
        lay.addWidget(g)

        # ---- Ausgabe
        g = QGroupBox("Ausgabe (gilt für alle Streams)")
        f = QFormLayout(g)
        self.res_combo = QComboBox()
        for w, h in ((1920, 1080), (1280, 720), (960, 540), (854, 480)):
            self.res_combo.addItem(f"{w}x{h}", (w, h))
        self.res_combo.setCurrentIndex(max(0, self.res_combo.findData((e.out_width, e.out_height))))
        self.fps_combo = QComboBox()
        for fps in (25, 30, 50, 60):
            self.fps_combo.addItem(str(fps), fps)
        self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(e.out_fps)))
        self.enc_combo = QComboBox()
        for key, label in self.ENCODERS:
            self.enc_combo.addItem(label, key)
        self.enc_combo.setCurrentIndex(max(0, self.enc_combo.findData(e.video_encoder)))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(self.PRESETS)
        self.preset_combo.setCurrentText(e.x264_preset)
        self.vbr = self._spin(500, 50000, e.video_bitrate_k, " kbit/s", 500)
        self.abr = self._spin(64, 320, e.audio_bitrate_k, " kbit/s", 32)
        f.addRow("Auflösung", self.res_combo)
        f.addRow("Bildrate", self.fps_combo)
        f.addRow("Video-Encoder", self.enc_combo)
        f.addRow("x264-Preset", self.preset_combo)
        f.addRow("Video-Bitrate", self.vbr)
        f.addRow("Audio-Bitrate (je Stream)", self.abr)
        lay.addWidget(g)

        g = QGroupBox("Erweitert")
        f = QFormLayout(g)
        self.port_spin = self._spin(1024, 65000, e.base_port, "")
        f.addRow("Lokale UDP-Ports ab", self.port_spin)
        self.meters_cb = QCheckBox("Pegelmeter anzeigen")
        self.meters_cb.setChecked(e.meters)
        f.addRow(self.meters_cb)
        show_cmd = QPushButton("Engine-Befehl im Log anzeigen")
        show_cmd.clicked.connect(self._show_cmd)
        f.addRow(show_cmd)
        lay.addWidget(g)

        note = QLabel("Änderungen auf dieser Seite werden beim nächsten Engine-Start wirksam. "
                      "Pegel und Ducker auf der Mixer-Seite wirken sofort.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888")
        lay.addWidget(note)
        lay.addStretch(1)

        for w in (self.ff_edit, self.vsize_edit, self.vfps_edit):
            w.textChanged.connect(self._apply)
        for w in (self.left_edit, self.right_edit):
            w.textChanged.connect(self._apply_labels)
        for w in (self.video_combo, self.floor_combo, self.interp_combo):
            w.currentTextChanged.connect(self._apply)
        for w in (self.res_combo, self.fps_combo, self.enc_combo):
            w.currentIndexChanged.connect(self._apply)
        self.preset_combo.currentTextChanged.connect(self._apply)
        for w in (self.floor_delay, self.interp_delay, self.vbr, self.abr, self.port_spin):
            w.valueChanged.connect(self._apply)
        for w in (self.test_cb, self.meters_cb):
            w.toggled.connect(self._apply)
        self._update_resolved()

    # ---- helpers
    @staticmethod
    def _spin(lo, hi, val, suffix, step=1) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setValue(int(val))
        s.setSuffix(suffix)
        return s

    @staticmethod
    def _dev_combo(current: str) -> QComboBox:
        c = QComboBox()
        c.setEditable(True)
        c.setMinimumWidth(320)
        if current:
            c.addItem(current, current)
        c.setCurrentText(current)
        return c

    @staticmethod
    def _combo_ident(c: QComboBox) -> str:
        text = c.currentText().strip()
        idx = c.findText(text)
        if idx >= 0 and c.itemData(idx):
            return c.itemData(idx)
        return text

    def ffmpeg(self) -> str:
        return resolve_ffmpeg(self.cfg.engine.ffmpeg_path)

    def _update_resolved(self):
        self.ff_resolved.setText(f"verwendet: {self.ffmpeg()}")

    def _browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, "ffmpeg auswählen", "", "ffmpeg (ffmpeg*)")
        if path:
            self.ff_edit.setText(path)

    def refresh_devices(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            video, audio = list_dshow_devices(self.ffmpeg())
        finally:
            QApplication.restoreOverrideCursor()
        if not video and not audio:
            self.log("Keine DirectShow-Geräte gefunden (nur unter Windows verfügbar, ffmpeg-Pfad prüfen).")
            return
        for combo, devs in ((self.video_combo, video), (self.floor_combo, audio),
                            (self.interp_combo, audio)):
            cur = self._combo_ident(combo)
            combo.blockSignals(True)
            combo.clear()
            for d in devs:
                combo.addItem(d.name, d.ident)
            idx = combo.findData(cur)
            if idx < 0:
                idx = combo.findText(cur)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentText(cur)
            combo.blockSignals(False)
        self.log(f"Geräte: {len(video)} Video, {len(audio)} Audio gefunden.")
        self._apply()

    def _apply(self, *_):
        e = self.cfg.engine
        e.ffmpeg_path = self.ff_edit.text().strip() or "ffmpeg"
        e.test_mode = self.test_cb.isChecked()
        e.meters = self.meters_cb.isChecked()
        e.video_device = self._combo_ident(self.video_combo)
        e.floor_device = self._combo_ident(self.floor_combo)
        e.interp_device = self._combo_ident(self.interp_combo)
        e.video_input_size = self.vsize_edit.text().strip()
        e.video_input_fps = self.vfps_edit.text().strip()
        e.floor_delay_ms = self.floor_delay.value()
        e.interp_delay_ms = self.interp_delay.value()
        e.out_width, e.out_height = self.res_combo.currentData()
        e.out_fps = self.fps_combo.currentData()
        e.video_encoder = self.enc_combo.currentData()
        e.x264_preset = self.preset_combo.currentText()
        e.video_bitrate_k = self.vbr.value()
        e.audio_bitrate_k = self.abr.value()
        e.base_port = self.port_spin.value()
        self.preset_combo.setEnabled(e.video_encoder == "libx264")
        self._update_resolved()
        self.changed.emit()

    def _apply_labels(self, *_):
        self.cfg.engine.interp_left_label = self.left_edit.text().strip() or "Sprache L"
        self.cfg.engine.interp_right_label = self.right_edit.text().strip() or "Sprache R"
        self.changed.emit()
        self.labels_changed.emit()

    def _show_cmd(self):
        try:
            args = eng.build_engine_args(self.cfg)
        except ValueError as exc:
            self.log(str(exc))
            return
        quoted = [f'"{a}"' if any(c in a for c in ' |;[]') else a for a in args]
        self.log(self.ffmpeg() + " " + " ".join(quoted))


# =========================================================================== main window

class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("StreamApp – Mehrsprachiges RTMP-Streaming")
        self.resize(1280, 860)

        self.engine = EngineProcess(self)
        self.engine.status.connect(self._on_engine_status)
        self.engine.log.connect(self.log)
        self.engine.stats.connect(lambda s: self.engine_stats.setText(s))
        self.engine.meters.connect(self._on_meters)
        self.relays: List[FFProcess] = []
        self.strips: List[StreamStrip] = []

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ---- Kopfzeile
        head = QHBoxLayout()
        self.engine_btn = QPushButton("▶ Engine starten")
        self.engine_btn.setCheckable(True)
        self.engine_btn.setMinimumHeight(36)
        self.engine_btn.setToolTip("Startet Bild/Ton-Verarbeitung. Danach Streams einzeln starten.")
        self.engine_btn.clicked.connect(self._toggle_engine)
        self.engine_status = QLabel()
        self.all_btn = QPushButton("Alle Streams starten")
        self.all_btn.clicked.connect(self._start_all)
        self.none_btn = QPushButton("Alle Streams stoppen")
        self.none_btn.clicked.connect(self._stop_all)
        head.addWidget(self.engine_btn)
        head.addWidget(self.engine_status, 1)
        head.addWidget(self.all_btn)
        head.addWidget(self.none_btn)
        root.addLayout(head)
        self.engine_stats = QLabel()
        self.engine_stats.setStyleSheet("color:#888; font-size:10px")
        root.addWidget(self.engine_stats)

        self.tabs = QTabWidget()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        split.addWidget(self.log_view)
        split.setSizes([680, 140])
        root.addWidget(split, 1)

        # ---- Mixer-Seite
        mixer = QWidget()
        mv = QVBoxLayout(mixer)
        inputs = QGroupBox("Eingänge")
        il = QHBoxLayout(inputs)
        self.in_meters = {"in_floor": LevelMeter("Saalton"), "in_il": LevelMeter(""),
                          "in_ir": LevelMeter("")}
        for m in self.in_meters.values():
            il.addWidget(m)
        mv.addWidget(inputs)
        self.strip_scroll = QScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        self.strip_host = QWidget()
        self.strip_layout = QHBoxLayout(self.strip_host)
        self.strip_layout.setAlignment(Qt.AlignLeft)
        self.strip_scroll.setWidget(self.strip_host)
        mv.addWidget(self.strip_scroll, 1)
        self.add_btn = QPushButton("+ Stream hinzufügen")
        self.add_btn.clicked.connect(self._add_stream)
        mv.addWidget(self.add_btn, 0, Qt.AlignLeft)
        self.tabs.addTab(mixer, "Mixer")

        self.settings = SettingsPage(cfg, self.log)
        self.settings.changed.connect(self._schedule_save)
        self.settings.labels_changed.connect(self._update_labels)
        self.tabs.addTab(self.settings, "Einstellungen")

        self._save_timer = QTimer(self, singleShot=True, interval=800)
        self._save_timer.timeout.connect(lambda: save_config(self.cfg))
        self._decay_timer = QTimer(self, interval=300)
        self._decay_timer.timeout.connect(self._decay_meters)
        self._decay_timer.start()

        self._rebuild_strips()
        self._update_labels()
        self._on_engine_status("off", "Gestoppt")

    # ------------------------------------------------------------------ helpers
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(time.strftime("%H:%M:%S ") + text)

    def _schedule_save(self):
        self._save_timer.start()

    def _labels(self) -> Dict[str, str]:
        e = self.cfg.engine
        return {"left": e.interp_left_label, "right": e.interp_right_label}

    def _update_labels(self):
        lb = self._labels()
        self.in_meters["in_il"].label = f"L: {lb['left']}"
        self.in_meters["in_ir"].label = f"R: {lb['right']}"
        for m in self.in_meters.values():
            m.update()
        for s in self.strips:
            s.set_labels(lb)

    # ------------------------------------------------------------------ strips
    def _rebuild_strips(self):
        for s in self.strips:
            s.setParent(None)
            s.deleteLater()
        for r in self.relays:
            r.stop()
            r.deleteLater()
        self.strips, self.relays = [], []
        for i, sc in enumerate(self.cfg.streams):
            strip = StreamStrip(i, sc, self._labels())
            strip.live_changed.connect(self._send_live)
            strip.config_changed.connect(self._schedule_save)
            strip.start_clicked.connect(self._start_stream)
            strip.stop_clicked.connect(self._stop_stream)
            strip.remove_clicked.connect(self._remove_stream)
            self.strip_layout.addWidget(strip)
            self.strips.append(strip)
            relay = FFProcess(sc.name, self)
            relay.status.connect(lambda st, txt, s=strip: s.set_status(st, txt))
            relay.stats.connect(lambda txt, s=strip: s.stats_label.setText(txt))
            relay.log.connect(self.log)
            self.relays.append(relay)
        self._update_structure_lock()

    def _add_stream(self):
        n = len(self.cfg.streams) + 1
        self.cfg.streams.append(StreamConfig(name=f"Stream {n}"))
        self._rebuild_strips()
        self._schedule_save()

    def _remove_stream(self, i: int):
        name = self.cfg.streams[i].name
        if QMessageBox.question(self, "Stream entfernen", f"„{name}“ entfernen?") != QMessageBox.Yes:
            return
        del self.cfg.streams[i]
        self._rebuild_strips()
        self._schedule_save()

    def _update_structure_lock(self):
        running = self.engine.wanted
        self.add_btn.setEnabled(not running)
        self.add_btn.setToolTip("Engine stoppen, um Streams hinzuzufügen" if running else "")
        for s, r in zip(self.strips, self.relays):
            s.remove_btn.setEnabled(not running and not r.wanted)

    # ------------------------------------------------------------------ engine
    def _toggle_engine(self, checked: bool):
        if checked:
            self.cfg.engine.ffmpeg_path = self.settings.ff_edit.text().strip() or "ffmpeg"
            save_config(self.cfg)
            self.engine.start(self.settings.ffmpeg(), self._engine_args)
        else:
            self._stop_all()
            self.engine.stop()
        self._update_structure_lock()

    def _engine_args(self) -> List[str]:
        args = eng.build_engine_args(self.cfg)
        self.engine.seed_params([p for i, s in enumerate(self.cfg.streams)
                                 for p in eng.stream_params(i, s)])
        return args

    def _on_engine_status(self, state: str, text: str):
        col = STATUS_COLORS.get(state, "#888")
        self.engine_status.setText(f"<b style='color:{col}'>●</b> Engine: {text}")
        wanted = self.engine.wanted
        self.engine_btn.setChecked(wanted)
        self.engine_btn.setText("■ Engine stoppen" if wanted else "▶ Engine starten")
        self.settings.setEnabled(not wanted)
        self.all_btn.setEnabled(wanted)
        if state in ("off", "error"):
            self.engine_stats.setText("")
        self._update_structure_lock()

    def _send_live(self, i: int):
        for target, cmd, value in eng.stream_params(i, self.cfg.streams[i]):
            self.engine.set_param(target, cmd, value)

    def _on_meters(self, levels: dict):
        for key, (peak, rms) in levels.items():
            if key in self.in_meters:
                self.in_meters[key].set_level(peak, rms)
            elif key.startswith("s") and key[1:].isdigit():
                i = int(key[1:])
                if i < len(self.strips):
                    self.strips[i].meter.set_level(peak, rms)

    def _decay_meters(self):
        for m in self.in_meters.values():
            m.decay()
        for s in self.strips:
            s.meter.decay()

    # ------------------------------------------------------------------ relays
    def _start_stream(self, i: int):
        strip = self.strips[i]
        if not self.engine.wanted:
            QMessageBox.information(self, "Engine", "Bitte zuerst die Engine starten.")
            strip.set_running_wanted(False)
            return
        if not eng.rtmp_target(self.cfg.streams[i]):
            QMessageBox.warning(self, "Stream", "Bitte RTMP-URL (und ggf. Schlüssel) eintragen.")
            strip.set_running_wanted(False)
            return
        strip.set_running_wanted(True)
        self.relays[i].name = self.cfg.streams[i].name
        self.relays[i].start(self.settings.ffmpeg(), lambda i=i: eng.build_relay_args(self.cfg, i))
        self._update_structure_lock()

    def _stop_stream(self, i: int):
        self.strips[i].set_running_wanted(False)
        self.relays[i].stop()
        self._update_structure_lock()

    def _start_all(self):
        for i in range(len(self.strips)):
            if not self.relays[i].wanted and eng.rtmp_target(self.cfg.streams[i]):
                self._start_stream(i)

    def _stop_all(self):
        for i in range(len(self.strips)):
            if self.relays[i].wanted:
                self._stop_stream(i)

    # ------------------------------------------------------------------ close
    def closeEvent(self, ev):
        live = any(r.wanted for r in self.relays)
        if live and QMessageBox.question(
                self, "Beenden", "Es laufen noch Streams. Wirklich beenden?") != QMessageBox.Yes:
            ev.ignore()
            return
        save_config(self.cfg)
        procs = [r.proc for r in self.relays if r.proc] + ([self.engine.proc] if self.engine.proc else [])
        self._stop_all()
        self.engine.stop()
        for p in procs:
            if not p.waitForFinished(3000):
                p.kill()
        ev.accept()
