"""Hauptfenster: links eine Spalte je Stream (Pegel/Fader/Ducker), rechts die Videovorschau."""
from __future__ import annotations

import time
from typing import Callable, Dict, List
from urllib.parse import urlparse

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from . import BUILD, __version__
from . import engine as eng
from .config import (VOICE_LEFT, VOICE_NONE, VOICE_RIGHT, AppConfig, StreamConfig,
                     save_config)
from .devices import list_dshow_devices, resolve_ffmpeg
from .procs import EngineProcess, FFProcess

STATUS_COLORS = {"off": "#888", "starting": "#d90", "running": "#2a2", "error": "#d33"}
STRIP_WIDTH = 300


# =========================================================================== widgets

TARGET_DB = -14.0  # Zielpegel, auf allen Pegelanzeigen markiert
TARGET_COLOR = "#3aa0ff"


class LevelMeter(QWidget):
    """Pegelmesser −60…0 dBFS mit dB-Skala (Balken = RMS, Strich = Peak).

    Der Zielpegel (−14 dB) ist als blaue Linie mit Beschriftung markiert.
    """

    MIN_DB = -60.0
    ZONES = ((-60, -10, "#2c2"), (-10, -4, "#dd2"), (-4, 0, "#e33"))
    SCALE = (0, -6, -14, -24, -36, -48, -60)
    SCALE_W = 22   # Breite der Skala (senkrecht)
    BAR_W = 14
    SCALE_H = 14   # Höhe der Skala (waagerecht)

    def __init__(self, label: str = "", vertical: bool = False, parent=None):
        super().__init__(parent)
        self.label = label
        self.vertical = vertical
        self.peak = self.rms = -120.0
        self._t = 0.0
        self.setToolTip(f"Balken = RMS, weißer Strich = Spitze, blaue Linie = Ziel {TARGET_DB:.0f} dB")
        if vertical:
            self.setFixedWidth(self.SCALE_W + self.BAR_W + 2)
            self.setMinimumHeight(140)
        else:
            self.setMinimumHeight(18 + self.SCALE_H)
            self.setMinimumWidth(160)

    def set_level(self, peak: float, rms: float) -> None:
        self.peak, self.rms, self._t = peak, rms, time.monotonic()
        self.update()

    def decay(self) -> None:
        if self.rms > -120 and time.monotonic() - self._t > 0.6:
            self.peak = self.rms = -120.0
            self.update()

    def _frac(self, db: float) -> float:
        return max(0.0, min(1.0, (db - self.MIN_DB) / -self.MIN_DB))

    def paintEvent(self, _ev):
        p = QPainter(self)
        small = QFont(self.font())
        small.setPointSizeF(max(6.5, small.pointSizeF() - 3))
        p.setFont(small)
        pal_text = self.palette().color(self.foregroundRole())

        if self.vertical:
            # Skala links, Balken rechts; 6 px Rand oben/unten, damit Beschriftung passt
            bar = QRectF(self.SCALE_W, 6, self.BAR_W, self.height() - 12)
        else:
            bar = QRectF(0, 0, self.width() - 1, self.height() - self.SCALE_H - 1)
        length = bar.height() if self.vertical else bar.width()
        p.fillRect(bar, QColor("#1b1b1b"))

        def seg(a: float, b: float, col: str):
            # a, b: Anteile 0..1 entlang der Messrichtung
            if self.vertical:
                p.fillRect(QRectF(bar.left(), bar.bottom() - b * length, bar.width(),
                                  (b - a) * length), QColor(col))
            else:
                p.fillRect(QRectF(bar.left() + a * length, bar.top(), (b - a) * length,
                                  bar.height()), QColor(col))

        x = self._frac(self.rms)
        for lo, hi, col in self.ZONES:
            a, b = self._frac(lo), min(x, self._frac(hi))
            if b > a:
                seg(a, b, col)
        if self.peak > self.MIN_DB:
            pk = self._frac(self.peak)
            seg(max(0.0, pk - 2.0 / length), pk, "#fff")

        # ---- Skala
        for db in self.SCALE:
            f = self._frac(db)
            target = db == TARGET_DB
            p.setPen(QColor(TARGET_COLOR) if target else pal_text)
            fb = QFont(small)
            fb.setBold(target)
            p.setFont(fb)
            txt = str(db)
            if self.vertical:
                y = bar.bottom() - f * length
                p.drawLine(int(bar.left()) - 3, int(y), int(bar.left()) - 1, int(y))
                p.drawText(QRectF(0, y - 6, self.SCALE_W - 4, 12), Qt.AlignRight | Qt.AlignVCenter, txt)
            else:
                xx = bar.left() + f * length
                p.drawLine(int(xx), int(bar.bottom()) + 1, int(xx), int(bar.bottom()) + 2)
                align = Qt.AlignRight if db == 0 else (Qt.AlignLeft if db == -60 else Qt.AlignHCenter)
                left = xx - 30 if db == 0 else (xx if db == -60 else xx - 15)
                p.drawText(QRectF(left, bar.bottom() + 3, 30, self.SCALE_H - 2),
                           align | Qt.AlignVCenter, txt)

        # ---- Ziellinie −14 dB quer über den Balken
        f = self._frac(TARGET_DB)
        pen = p.pen()
        pen.setColor(QColor(TARGET_COLOR))
        pen.setWidth(2)
        p.setPen(pen)
        if self.vertical:
            y = int(bar.bottom() - f * length)
            p.drawLine(int(bar.left()), y, int(bar.right()), y)
        else:
            xx = int(bar.left() + f * length)
            p.drawLine(xx, int(bar.top()), xx, int(bar.bottom()))

        if not self.vertical:
            p.setPen(QColor("#ddd"))
            p.setFont(self.font())
            txt = f"{self.label}  {self.rms:5.1f} dB" if self.rms > -120 else f"{self.label}  —"
            p.drawText(bar.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)


class ChannelFader(QWidget):
    """Senkrechter Fader mit Pegelanzeige daneben (wie am Mischpult)."""

    changed = Signal(float)

    def __init__(self, title: str, value: float, lo=-60.0, hi=12.0, parent=None):
        super().__init__(parent)
        self.meter = LevelMeter(vertical=True)
        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(int(lo * 2), int(hi * 2))  # 0,5-dB-Schritte
        self.slider.setTickPosition(QSlider.TicksLeft)
        self.slider.setTickInterval(12)  # alle 6 dB
        self.slider.setMinimumHeight(120)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-weight:bold")
        self.value_label = QLabel()
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setToolTip("Doppelklick: 0 dB")
        self.value_label.mouseDoubleClickEvent = lambda _e: self.set_value(0.0, emit=True)
        self.level_label = QLabel("—")
        self.level_label.setAlignment(Qt.AlignCenter)
        self.level_label.setStyleSheet("color:#888; font-size:10px")
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(4)
        lay.addWidget(self.title, 0, 0, 1, 2)
        lay.addWidget(self.slider, 1, 0, Qt.AlignHCenter)
        lay.addWidget(self.meter, 1, 1)
        lay.addWidget(self.value_label, 2, 0, 1, 2)
        lay.addWidget(self.level_label, 3, 0, 1, 2)
        lay.setRowStretch(1, 1)
        self.slider.valueChanged.connect(self._on_change)
        self.set_value(value)

    def value(self) -> float:
        return self.slider.value() / 2.0

    def set_value(self, v: float, emit: bool = False) -> None:
        self.slider.blockSignals(not emit)
        self.slider.setValue(round(v * 2))
        self.slider.blockSignals(False)
        self._update_label()

    def set_level(self, peak: float, rms: float) -> None:
        self.meter.set_level(peak, rms)
        self.level_label.setText(f"{rms:.0f} dB" if rms > -120 else "—")

    def decay(self) -> None:
        self.meter.decay()
        if self.meter.rms <= -120:
            self.level_label.setText("—")

    def _update_label(self):
        self.value_label.setText(f"{self.value():+.1f} dB")

    def _on_change(self, _):
        self._update_label()
        self.changed.emit(self.value())


class ParamSlider(QWidget):
    """Kompakter waagerechter Regler mit Wertanzeige. Doppelklick auf Wert = Standard."""

    changed = Signal(float)

    def __init__(self, title: str, lo: float, hi: float, step: float, unit: str,
                 value: float, default: float, parent=None):
        super().__init__(parent)
        self._step, self._unit, self._default = step, unit, default
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(lo / step), round(hi / step))
        self.value_label = QLabel()
        self.value_label.setMinimumWidth(58)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setToolTip("Doppelklick: Standardwert")
        self.value_label.mouseDoubleClickEvent = lambda _e: self.set_value(self._default, emit=True)
        title_label = QLabel(title)
        title_label.setMinimumWidth(58)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(title_label)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.value_label)
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


class VideoPreview(QWidget):
    """Zeigt das zuletzt empfangene Vorschaubild (Seitenverhältnis bleibt erhalten)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._t = 0.0
        self.message = "Keine Vorschau – Engine starten"
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_jpeg(self, data: bytes) -> None:
        pix = QPixmap()
        if pix.loadFromData(data, "JPG"):
            self._pix, self._t = pix, time.monotonic()
            self.update()

    def check_stale(self) -> None:
        if self._pix is not None and time.monotonic() - self._t > 2.0:
            self._pix = None
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        if self._pix is None:
            p.setPen(QColor("#777"))
            p.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        scaled = self._pix.size().scaled(self.size(), Qt.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(x, y, scaled.width(), scaled.height(), self._pix)


class PreviewReceiver(QTcpServer):
    """Lokaler TCP-Server, zu dem sich die Engine mit dem MJPEG-Vorschaustream verbindet."""

    frame = Signal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock: QTcpSocket | None = None
        self._parser = eng.MjpegParser()
        self.newConnection.connect(self._on_conn)

    def start(self, port: int) -> bool:
        if self.isListening():
            if self.serverPort() == port:
                return True
            self.close()
        return self.listen(QHostAddress.LocalHost, port)

    def _on_conn(self):
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            if self._sock is not None:
                self._sock.abort()
                self._sock.deleteLater()
            self._sock = sock
            self._parser = eng.MjpegParser()
            sock.readyRead.connect(lambda s=sock: self._on_data(s))

    def _on_data(self, sock: QTcpSocket):
        if sock is not self._sock:
            return
        for jpg in self._parser.feed(bytes(sock.readAll())):
            self.frame.emit(jpg)


# =========================================================================== dialogs

class TargetDialog(QDialog):
    """RTMP-URL und Schlüssel eines Streams bearbeiten."""

    def __init__(self, cfg: StreamConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Ziel – {cfg.name}")
        self.setMinimumWidth(520)
        f = QFormLayout(self)
        self.url = QLineEdit(cfg.url)
        self.url.setPlaceholderText("rtmp://a.rtmp.youtube.com/live2")
        self.key = QLineEdit(cfg.key)
        self.key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.key.setPlaceholderText("Streamschlüssel (wird an die URL angehängt)")
        f.addRow("RTMP-URL", self.url)
        f.addRow("Schlüssel", self.key)
        hint = QLabel("Der Schlüssel wird mit „/“ an die URL angehängt. "
                      "Steht er schon in der URL, Feld leer lassen.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")
        f.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


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
        self.setFixedWidth(STRIP_WIDTH)
        v = QVBoxLayout(self)
        v.setSpacing(6)

        top = QHBoxLayout()
        self.name_edit = QLineEdit(cfg.name)
        self.name_edit.setFont(QFont(self.font().family(), self.font().pointSize() + 2, QFont.Bold))
        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedWidth(26)
        self.remove_btn.setToolTip("Stream entfernen")
        top.addWidget(self.name_edit)
        top.addWidget(self.remove_btn)
        v.addLayout(top)

        self.voice_combo = QComboBox()
        self.voice_combo.setToolTip("Welche Sprache dieser Stream bekommt")
        v.addWidget(self.voice_combo)
        self.set_labels(labels)

        # ---- Mischpult: Saalton | Dolmetscher | Summe
        faders = QHBoxLayout()
        faders.setSpacing(10)
        self.floor_f = ChannelFader("Saal", cfg.floor_db)
        self.voice_f = ChannelFader("Dolm.", cfg.voice_db)
        self.master_f = ChannelFader("Summe", cfg.master_db, lo=-30)
        self.floor_f.setToolTip("Saalton (Pegel nach dem Ducker)")
        self.voice_f.setToolTip("Dolmetscher")
        self.master_f.setToolTip("Gesamtmix, der gesendet wird")
        for w in (self.floor_f, self.voice_f, self.master_f):
            faders.addWidget(w)
        v.addLayout(faders, 1)

        # ---- Ducker
        self.duck_box = QGroupBox("Ducker")
        self.duck_box.setToolTip("Saalton um einen festen Wert absenken, solange der Dolmetscher spricht")
        self.duck_box.setCheckable(True)
        self.duck_box.setChecked(cfg.duck_on)
        dv = QVBoxLayout(self.duck_box)
        dv.setSpacing(2)
        self.duck_led = QLabel()
        self.duck_led.setAlignment(Qt.AlignRight)
        dv.addWidget(self.duck_led)
        self.set_ducking(False)
        self.depth_s = ParamSlider("Absenkung", 0, 40, 1, "dB", cfg.duck_db, 15)
        self.depth_s.setToolTip("Um so viele dB wird der Saalton leiser, wenn der Dolmetscher spricht")
        self.thr_s = ParamSlider("Schwelle", -60, 0, 1, "dB", cfg.threshold_db, -36)
        self.thr_s.setToolTip("Ab diesem Dolmetscher-Pegel wird abgesenkt")
        self.att_s = ParamSlider("Attack", 1, 1000, 1, "ms", cfg.attack_ms, 30)
        self.att_s.setToolTip("Wie schnell abgesenkt wird")
        self.rel_s = ParamSlider("Release", 50, 3000, 10, "ms", cfg.release_ms, 600)
        self.rel_s.setToolTip("Wie schnell der Saalton zurückkommt, wenn der Dolmetscher pausiert")
        for w in (self.depth_s, self.thr_s, self.att_s, self.rel_s):
            dv.addWidget(w)
        v.addWidget(self.duck_box)

        # ---- Ziel + Start
        tgt = QHBoxLayout()
        self.target_label = QLabel()
        self.target_label.setStyleSheet("color:#888")
        self.target_btn = QPushButton("Ziel…")
        self.target_btn.setToolTip("RTMP-URL und Streamschlüssel")
        tgt.addWidget(self.target_label, 1)
        tgt.addWidget(self.target_btn)
        v.addLayout(tgt)
        self._update_target_label()

        self.go_btn = QPushButton("▶ Stream starten")
        self.go_btn.setCheckable(True)
        self.go_btn.setMinimumHeight(34)
        v.addWidget(self.go_btn)
        self.status_label = QLabel("Gestoppt")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(32)
        self.status_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v.addWidget(self.status_label)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#888; font-size:10px")
        v.addWidget(self.stats_label)

        # ---- Verbindungen
        self.name_edit.textChanged.connect(lambda t: self._set("name", t, live=False))
        self.voice_combo.currentIndexChanged.connect(
            lambda _i: self._set("voice", self.voice_combo.currentData()))
        self.floor_f.changed.connect(lambda x: self._set("floor_db", x))
        self.voice_f.changed.connect(lambda x: self._set("voice_db", x))
        self.master_f.changed.connect(lambda x: self._set("master_db", x))
        self.duck_box.toggled.connect(lambda b: self._set("duck_on", b))
        self.thr_s.changed.connect(lambda x: self._set("threshold_db", x))
        self.depth_s.changed.connect(lambda x: self._set("duck_db", x))
        self.att_s.changed.connect(lambda x: self._set("attack_ms", x))
        self.rel_s.changed.connect(lambda x: self._set("release_ms", x))
        self.target_btn.clicked.connect(self._edit_target)
        self.go_btn.clicked.connect(self._on_go)
        self.remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self.index))
        self._update_enabled()

    # Pegel-IDs aus der Engine: f<i> Saal, v<i> Dolmetscher, s<i> Summe
    def meter_for(self, prefix: str) -> ChannelFader | None:
        return {"f": self.floor_f, "v": self.voice_f, "s": self.master_f}.get(prefix)

    def decay(self) -> None:
        for w in (self.floor_f, self.voice_f, self.master_f):
            w.decay()
        if self.voice_f.meter.rms <= -120:
            self.set_ducking(False)

    def set_ducking(self, active: bool) -> None:
        if active:
            self.duck_led.setText(f"<b style='color:#e80'>● senkt um {self.cfg.duck_db:.0f} dB</b>")
        else:
            self.duck_led.setText("<span style='color:#888'>○ aus</span>")

    def update_ducking(self, voice_rms: float) -> None:
        """Anzeige, ob gerade abgesenkt wird (aus dem Dolmetscher-Pegel vor dem Fader)."""
        key = voice_rms - self.cfg.voice_db
        self.set_ducking(self.cfg.duck_on and self.cfg.voice != VOICE_NONE
                         and voice_rms > -120 and key >= self.cfg.threshold_db)

    def set_labels(self, labels: Dict[str, str]) -> None:
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("Nur Saalton (Original)", VOICE_NONE)
        self.voice_combo.addItem(f"{labels['left']} (Dolm. links)", VOICE_LEFT)
        self.voice_combo.addItem(f"{labels['right']} (Dolm. rechts)", VOICE_RIGHT)
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
        self.voice_f.setEnabled(has_voice)
        self.duck_box.setEnabled(has_voice)

    def _update_target_label(self) -> None:
        if not self.cfg.url:
            self.target_label.setText("<i>kein Ziel</i>")
            return
        host = urlparse(self.cfg.url).netloc or self.cfg.url
        key = " 🔑" if self.cfg.key else ""
        self.target_label.setText(host + key)
        self.target_label.setToolTip(self.cfg.url)

    def _edit_target(self) -> None:
        dlg = TargetDialog(self.cfg, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.url = dlg.url.text().strip()
            self.cfg.key = dlg.key.text().strip()
            self._update_target_label()
            self.config_changed.emit()

    def _on_go(self, checked: bool) -> None:
        (self.start_clicked if checked else self.stop_clicked).emit(self.index)

    def set_running_wanted(self, wanted: bool) -> None:
        self.go_btn.setChecked(wanted)
        self.go_btn.setText("■ Stream stoppen" if wanted else "▶ Stream starten")
        self.target_btn.setEnabled(not wanted)
        self.remove_btn.setEnabled(not wanted)

    def set_status(self, state: str, text: str) -> None:
        col = STATUS_COLORS.get(state, "#888")
        self.status_label.setText(f"<b style='color:{col}'>●</b> {text}")
        live = state == "running"
        self.go_btn.setStyleSheet("background:#2a7a2a; color:white" if live else "")
        if not live:
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
        outer.setContentsMargins(0, 0, 0, 0)
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
        self.preview_cb = QCheckBox("Videovorschau anzeigen")
        self.preview_cb.setChecked(e.preview)
        f.addRow(self.preview_cb)
        self.preview_port = self._spin(1024, 65000, e.preview_port, "")
        f.addRow("Vorschau-Port (TCP)", self.preview_port)
        show_cmd = QPushButton("Engine-Befehl im Log anzeigen")
        show_cmd.clicked.connect(self._show_cmd)
        f.addRow(show_cmd)
        lay.addWidget(g)
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
        for w in (self.floor_delay, self.interp_delay, self.vbr, self.abr, self.port_spin,
                  self.preview_port):
            w.valueChanged.connect(self._apply)
        for w in (self.test_cb, self.meters_cb, self.preview_cb):
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
            QMessageBox.information(self, "Geräte", "Keine Geräte gefunden. ffmpeg-Pfad prüfen.")
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
        e.preview = self.preview_cb.isChecked()
        e.preview_port = self.preview_port.value()
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


class SettingsDialog(QDialog):
    def __init__(self, page: SettingsPage, locked: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.resize(640, 720)
        lay = QVBoxLayout(self)
        if locked:
            note = QLabel("Die Engine läuft – Einstellungen können nur bei gestoppter Engine "
                          "geändert werden.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#d90; font-weight:bold")
            lay.addWidget(note)
        else:
            note = QLabel("Änderungen gelten beim nächsten Engine-Start. "
                          "Fader und Ducker wirken dagegen sofort.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#888")
            lay.addWidget(note)
        page.setEnabled(not locked)
        lay.addWidget(page, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


# =========================================================================== main window

class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(f"StreamApp {__version__} ({BUILD}) – Mehrsprachiges RTMP-Streaming")
        self.resize(1600, 900)

        self.engine = EngineProcess(self)
        self.engine.status.connect(self._on_engine_status)
        self.engine.log.connect(self.log)
        self.engine.stats.connect(lambda s: self.engine_stats.setText(s))
        self.engine.meters.connect(self._on_meters)
        self.preview_rx = PreviewReceiver(self)
        self.relays: List[FFProcess] = []
        self.strips: List[StreamStrip] = []

        # Einstellungen leben in einem Dialog, die Seite wird wiederverwendet
        self.log_view = QPlainTextEdit()
        self.settings = SettingsPage(cfg, self.log)
        self.settings.changed.connect(self._schedule_save)
        self.settings.labels_changed.connect(self._update_labels)

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
        self.settings_btn = QPushButton("⚙ Einstellungen")
        self.settings_btn.clicked.connect(self._open_settings)
        self.log_btn = QPushButton("Log")
        self.log_btn.setCheckable(True)
        head.addWidget(self.engine_btn)
        head.addWidget(self.engine_status, 1)
        for b in (self.all_btn, self.none_btn, self.settings_btn, self.log_btn):
            head.addWidget(b)
        root.addLayout(head)

        # ---- Hauptbereich: links Stream-Spalten, rechts Video
        main_split = QSplitter(Qt.Horizontal)

        self.strip_scroll = QScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setFrameShape(QScrollArea.NoFrame)
        self.strip_host = QWidget()
        self.strip_layout = QHBoxLayout(self.strip_host)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setAlignment(Qt.AlignLeft)
        self.add_btn = QPushButton("+\nStream")
        self.add_btn.setFixedWidth(56)
        self.add_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.add_btn.clicked.connect(self._add_stream)
        self.strip_scroll.setWidget(self.strip_host)
        main_split.addWidget(self.strip_scroll)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.preview = VideoPreview()
        rv.addWidget(self.preview, 1)
        self.engine_stats = QLabel()
        self.engine_stats.setStyleSheet("color:#888; font-size:10px")
        rv.addWidget(self.engine_stats)
        inputs = QGroupBox("Eingänge")
        il = QVBoxLayout(inputs)
        self.in_meters = {"in_floor": LevelMeter("Saalton"), "in_il": LevelMeter(""),
                          "in_ir": LevelMeter("")}
        for m in self.in_meters.values():
            il.addWidget(m)
        rv.addWidget(inputs)
        main_split.addWidget(right)
        main_split.setStretchFactor(0, 0)
        main_split.setStretchFactor(1, 1)
        self.main_split = main_split

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(main_split)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        vsplit.addWidget(self.log_view)
        vsplit.setStretchFactor(0, 1)
        self.log_view.setVisible(False)
        self.log_btn.toggled.connect(self.log_view.setVisible)
        root.addWidget(vsplit, 1)

        self.preview_rx.frame.connect(self.preview.set_jpeg)

        self._save_timer = QTimer(self, singleShot=True, interval=800)
        self._save_timer.timeout.connect(lambda: save_config(self.cfg))
        self._decay_timer = QTimer(self, interval=300)
        self._decay_timer.timeout.connect(self._decay)
        self._decay_timer.start()

        self._rebuild_strips()
        self._update_labels()
        self._on_engine_status("off", "Gestoppt")

    # ------------------------------------------------------------------ helpers
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(time.strftime("%H:%M:%S ") + text)
        if "Start: " not in text and ("Fehler" in text or "rror" in text):
            self.log_btn.setStyleSheet("color:#d33; font-weight:bold")

    def _schedule_save(self):
        self._save_timer.start()

    def _labels(self) -> Dict[str, str]:
        e = self.cfg.engine
        return {"left": e.interp_left_label, "right": e.interp_right_label}

    def _update_labels(self):
        lb = self._labels()
        self.in_meters["in_il"].label = f"Dolm. L: {lb['left']}"
        self.in_meters["in_ir"].label = f"Dolm. R: {lb['right']}"
        for m in self.in_meters.values():
            m.update()
        for s in self.strips:
            s.set_labels(lb)

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, locked=self.engine.wanted, parent=self)
        dlg.exec()
        self.settings.setParent(None)  # Seite für das nächste Öffnen behalten
        save_config(self.cfg)

    # ------------------------------------------------------------------ strips
    def _rebuild_strips(self):
        self.strip_layout.removeWidget(self.add_btn)
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
            relay.stats.connect(lambda txt, s=strip: s.stats_label.setText(_short_stats(txt)))
            relay.log.connect(self.log)
            self.relays.append(relay)
        self.strip_layout.addWidget(self.add_btn)
        # Linke Seite so breit wie alle Spalten (soweit das Fenster reicht)
        want = len(self.strips) * (STRIP_WIDTH + 8) + 80
        self.main_split.setSizes([want, max(400, self.width() - want)])
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
        self.add_btn.setToolTip("Engine stoppen, um Streams hinzuzufügen" if running
                                else "Stream hinzufügen")
        for s, r in zip(self.strips, self.relays):
            s.remove_btn.setEnabled(not running and not r.wanted)

    # ------------------------------------------------------------------ engine
    def _toggle_engine(self, checked: bool):
        if checked:
            self.settings._apply()
            save_config(self.cfg)
            e = self.cfg.engine
            if e.preview and not self.preview_rx.start(e.preview_port):
                self.log(f"Vorschau-Port {e.preview_port} ist belegt – Vorschau deaktiviert "
                         "(Port in den Einstellungen ändern).")
            self.preview.message = "Warte auf Bild…" if e.preview else "Vorschau ausgeschaltet"
            self.preview.update()
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
        self.all_btn.setEnabled(wanted)
        if state in ("off", "error"):
            self.engine_stats.setText("")
        if state == "off":
            self.preview.message = "Keine Vorschau – Engine starten"
            self.preview.update()
        self._update_structure_lock()

    def _send_live(self, i: int):
        for target, cmd, value in eng.stream_params(i, self.cfg.streams[i]):
            self.engine.set_param(target, cmd, value)

    def _on_meters(self, levels: dict):
        for key, (peak, rms) in levels.items():
            if key in self.in_meters:
                self.in_meters[key].set_level(peak, rms)
            elif key[:1] in "fvs" and key[1:].isdigit():
                i = int(key[1:])
                if i < len(self.strips):
                    w = self.strips[i].meter_for(key[0])
                    if w is not None:
                        w.set_level(peak, rms)
                    if key[0] == "v":
                        self.strips[i].update_ducking(rms)

    def _decay(self):
        for m in self.in_meters.values():
            m.decay()
        for s in self.strips:
            s.decay()
        self.preview.check_stale()

    # ------------------------------------------------------------------ relays
    def _start_stream(self, i: int):
        strip = self.strips[i]
        if not self.engine.wanted:
            QMessageBox.information(self, "Engine", "Bitte zuerst die Engine starten.")
            strip.set_running_wanted(False)
            return
        if not eng.rtmp_target(self.cfg.streams[i]):
            QMessageBox.warning(self, "Stream", "Bitte über „Ziel…“ eine RTMP-URL eintragen.")
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


def _short_stats(line: str) -> str:
    """'frame= 91 fps= 60 ... size= 2304KiB time=00:00:03.22 bitrate=...' -> 'time · bitrate'."""
    parts = {}
    for key in ("time", "bitrate", "speed"):
        idx = line.find(key + "=")
        if idx >= 0:
            parts[key] = line[idx + len(key) + 1:].strip().split(" ")[0]
    if not parts:
        return line[:60]
    return "  ".join(f"{k} {v}" for k, v in parts.items())
