"""Prozesssteuerung für Engine und Relays (QProcess, Auto-Neustart, Live-Befehle)."""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable, List, Optional

import shiboken6
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .engine import MeterParser, command_line

RESTART_DELAY_MS = 3000


class FFProcess(QObject):
    """Ein ffmpeg-Prozess, der (solange gewünscht) automatisch neu gestartet wird."""

    status = Signal(str, str)    # (zustand, text) zustand: off|starting|running|error
    log = Signal(str)
    stats = Signal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.wanted = False
        self.program = "ffmpeg"
        self.args_factory: Optional[Callable[[], List[str]]] = None
        self.proc: Optional[QProcess] = None
        self._err_buf = ""
        self._last_error = ""
        self._restart = QTimer(self, singleShot=True, interval=RESTART_DELAY_MS)
        self._restart.timeout.connect(self._launch)

    # ------------------------------------------------------------------ public
    def start(self, program: str, args_factory: Callable[[], List[str]]) -> None:
        self.program = program
        self.args_factory = args_factory
        self.wanted = True
        if self.proc is None:
            self._launch()

    def stop(self) -> None:
        self.wanted = False
        self._restart.stop()
        if self.proc is not None:
            p = self.proc
            if p.state() != QProcess.NotRunning:
                p.write(b"q")
                QTimer.singleShot(4000, lambda: p.kill() if p.state() != QProcess.NotRunning else None)
        else:
            self.status.emit("off", "Gestoppt")

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def write(self, data: bytes) -> None:
        if self.is_running():
            self.proc.write(data)

    # ------------------------------------------------------------------ intern
    def _launch(self) -> None:
        if not self.wanted or self.args_factory is None:
            return
        try:
            args = self.args_factory()
        except ValueError as exc:
            self.wanted = False
            self.status.emit("error", str(exc))
            self.log.emit(f"[{self.name}] {exc}")
            return
        p = QProcess(self)
        p.setProgram(self.program)
        p.setArguments(args)
        p.readyReadStandardError.connect(lambda: self._on_stderr(p))
        p.readyReadStandardOutput.connect(lambda: self._on_stdout(p))
        p.finished.connect(lambda code, _st: self._on_finished(p, code))
        p.errorOccurred.connect(lambda err: self._on_error(p, err))
        self.proc = p
        self._last_error = ""
        self.status.emit("starting", "Startet…")
        self.log.emit(f"[{self.name}] Start: {self.program} {' '.join(args)}")
        p.start()

    def _on_stdout(self, p: QProcess) -> None:
        p.readAllStandardOutput()  # in Unterklasse genutzt

    def _on_stderr(self, p: QProcess) -> None:
        self._err_buf += bytes(p.readAllStandardError()).decode("utf-8", errors="replace")
        self._err_buf = self._err_buf.replace("\r", "\n")
        *lines, self._err_buf = self._err_buf.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("frame=") or line.startswith("size="):
                self.stats.emit(line)
                self.status.emit("running", "Läuft")
                continue
            if self._ignore_line(line):
                continue
            if "bind failed" in line:
                line += ("  → UDP-Port belegt (läuft noch ein alter ffmpeg-Prozess?). "
                         "Ggf. im Task-Manager beenden oder in den Einstellungen andere Ports wählen.")
            self._last_error = line
            self.log.emit(f"[{self.name}] {line}")

    # Harmlos: Relay steigt mitten im Datenstrom ein und wartet auf das nächste Keyframe.
    NOISE = ("non-existing PPS", "no frame!", "Last message repeated",
             "decode_slice_header error", "Resumed reading at pts",
             "deprecated pixel format used")

    def _ignore_line(self, line: str) -> bool:
        return any(n in line for n in self.NOISE)

    def _on_error(self, p: QProcess, err) -> None:
        if err == QProcess.FailedToStart:
            self.log.emit(f"[{self.name}] ffmpeg konnte nicht gestartet werden: {self.program}")
            self._last_error = "ffmpeg nicht gefunden (Einstellungen prüfen)"
            self._on_finished(p, -1)

    def _on_finished(self, p: QProcess, code: int) -> None:
        if p is not self.proc or not shiboken6.isValid(self):
            return
        self.proc = None
        if shiboken6.isValid(p):
            p.deleteLater()
        if self.wanted:
            msg = self._last_error or f"beendet (Code {code})"
            self.status.emit("error", f"{msg} – Neustart in {RESTART_DELAY_MS // 1000} s")
            self.log.emit(f"[{self.name}] beendet (Code {code}), Neustart folgt")
            self._restart.start()
        else:
            self.status.emit("off", "Gestoppt")


class EngineProcess(FFProcess):
    """Engine: liefert Pegelwerte und nimmt Live-Befehle entgegen."""

    meters = Signal(dict)

    # ffmpeg liest Tasten nur ca. alle 100 ms -> Befehle zusammenfassen und takten
    SEND_INTERVAL_MS = 120

    def __init__(self, parent=None):
        super().__init__("Engine", parent)
        self._parser = MeterParser()
        self._pending: "OrderedDict[tuple, str]" = OrderedDict()
        self._sent: dict = {}
        self._sender = QTimer(self, interval=self.SEND_INTERVAL_MS)
        self._sender.timeout.connect(self._send_next)
        self._sender.start()

    def _launch(self) -> None:
        # Neuer Prozess bekommt die aktuellen Werte über die Kommandozeile.
        self._pending.clear()
        self._sent: dict = {}
        self._parser = MeterParser()
        super()._launch()

    def set_param(self, target: str, cmd: str, value: str) -> None:
        if not self.is_running():
            return
        key = (target, cmd)
        if self._sent.get(key) == value:
            self._pending.pop(key, None)
            return
        self._pending.pop(key, None)
        self._pending[key] = value

    def seed_params(self, params) -> None:
        """Werte, mit denen der Prozess gestartet wurde (nicht erneut senden)."""
        self._sent = {(t, c): v for t, c, v in params}

    def _send_next(self) -> None:
        if not self._pending or not self.is_running():
            return
        (target, cmd), value = self._pending.popitem(last=False)
        self._sent[(target, cmd)] = value
        self.write(command_line(target, cmd, value).encode())

    def _on_stdout(self, p: QProcess) -> None:
        data = bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace")
        levels = self._parser.feed(data.replace("\r", ""))
        if levels:
            self.meters.emit(levels)

    def _ignore_line(self, line: str) -> bool:
        if super()._ignore_line(line):
            return True
        if line.startswith("Enter command:") or line.startswith("Parse error"):
            return True
        if line.startswith("Command reply for stream"):
            # -1 = Antwort des Gesamtgraphen (immer ENOSYS), nur echte Fehler zeigen
            return "stream -1" in line or "ret:0 " in line + " "
        return False
