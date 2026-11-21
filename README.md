# StreamApp – mehrsprachiges RTMP-Streaming

Ein Bild, mehrere RTMP-Streams, jeder mit eigenem Tonmix:

| Eingang | Inhalt |
|---|---|
| Videogerät | Kamera / Capture-Karte (DirectShow) |
| Audiogerät 1 (Stereo) | Saalton / Original |
| Audiogerät 2 (Stereo) | **links** = Dolmetscher 1, **rechts** = Dolmetscher 2 |

Für jeden Stream wählst du die Sprache (nur Saalton, Dolmetscher links oder rechts) und mischst
den Saalton darunter. Ein **Ducker** senkt den Saalton automatisch ab, solange der Dolmetscher
spricht. Pegel und Ducker-Einstellungen wirken **sofort im laufenden Stream**.

## So funktioniert es

```
Video ──────┐
Saalton ────┼─► ENGINE (ein ffmpeg-Prozess)  ──UDP──► Relay 1 ──► RTMP (z.B. Original)
Dolm. L/R ──┘   · Video wird nur 1× encodiert ──UDP──► Relay 2 ──► RTMP (z.B. Englisch)
                · je Stream eigener Tonmix    ──UDP──► Relay 3 ──► RTMP (z.B. Französisch)
                  mit Ducker (sidechaincompress)
```

* Die **Engine** läuft durchgehend. Jeder Stream (Relay) wird einzeln gestartet/gestoppt.
* Bricht eine RTMP-Verbindung ab, wird nur dieser Stream nach 3 s automatisch neu verbunden.
* Statt eines OBS-Forks nutzt die App ffmpeg direkt (dieselbe Bibliothek, die OBS intern verwendet):
  Ein OBS-Umbau wäre für genau diesen Zweck sehr aufwändig, OBS selbst kann pro Ausgang keinen
  eigenen Tonmix senden.

## Installation (Windows 10/11)

1. **Python 3.10+** installieren (python.org, Haken bei „Add to PATH“).
2. **ffmpeg** holen: z.B. „ffmpeg-release-essentials.zip“ von <https://www.gyan.dev/ffmpeg/builds/>,
   `bin\ffmpeg.exe` nach `StreamApp\ffmpeg\ffmpeg.exe` kopieren.
3. `run.bat` doppelklicken (richtet beim ersten Mal alles ein und startet die App).

Eigene **.exe** bauen: `build.bat` ausführen → `dist\StreamApp\StreamApp.exe`
(den ganzen Ordner `dist\StreamApp` weitergeben; ffmpeg wird mitkopiert, wenn es unter `ffmpeg\` liegt).

## Bedienung

Aufbau des Fensters: **links eine Spalte je Stream**, **rechts die Videovorschau** mit den
Eingangspegeln darunter. Jede Stream-Spalte hat drei Fader mit Pegelanzeige:

* **Saal** – Saalton *nach* dem Ducker (hier sieht man das Absenken direkt),
* **Dolm.** – Dolmetscherkanal,
* **Summe** – der fertige Mix, der gesendet wird.

1. **⚙ Einstellungen** → „Geräte suchen“ → Videogerät, Saalton-Gerät und Dolmetscher-Gerät wählen.
   Namen für den linken/rechten Dolmetscherkanal eintragen (z.B. „Englisch“, „Französisch“).
   Ohne Hardware ausprobieren: **Testmodus** anhaken (Testbild, Rauschen als Saalton,
   abwechselnde Töne als Dolmetscher).
2. Auflösung, Bildrate, Encoder und Bitrate einstellen. Hast du eine NVIDIA-/Intel-/AMD-Grafikkarte,
   entlastet der Hardware-Encoder die CPU deutlich.
3. Pro Stream über **Ziel…** RTMP-URL + Schlüssel eintragen und oben die Sprache wählen.
4. **Engine starten**, dann einzelne Streams oder „Alle Streams starten“.

### Ducker einstellen

| Regler | Wirkung | Startwert |
|---|---|---|
| Schwelle | Ab diesem Dolmetscher-Pegel wird der Saalton abgesenkt. Niedriger = reagiert schon auf leise Sprache | −36 dB |
| Stärke (Ratio) | Wie stark abgesenkt wird (1 = gar nicht, 20 = sehr stark) | 10 : 1 |
| Attack | Wie schnell abgesenkt wird, wenn der Dolmetscher einsetzt | 20 ms |
| Release | Wie schnell der Saalton zurückkommt, wenn der Dolmetscher pausiert | 600 ms |
| Saalton / Dolmetscher / Summe | Grundpegel der Anteile und des Gesamtmixes | 0 dB |

Tipp: Release eher lang (500–1500 ms), damit der Saalton in kurzen Sprechpausen nicht „pumpt“.
Wenn der Saalton zwischen den Sätzen zu laut zurückkommt: Saalton-Pegel generell etwas senken
(z.B. −10 dB) und den Ducker die restliche Absenkung machen lassen.
Doppelklick auf einen Wert setzt ihn auf den Standard zurück.

Die Pegelmeter zeigen die Eingänge und den fertigen Mix je Stream (Balken = RMS, Strich = Spitze).
Ein Limiter am Ende verhindert Übersteuerung.

### Lippensynchronität

Falls Ton und Bild versetzt sind (Capture-Karten haben oft Verzögerung): unter Einstellungen
„Verzögerung Saalton / Dolmetscher“ in ms erhöhen, bis es passt.

## Hinweise

* Änderungen auf der Seite **Einstellungen** (Geräte, Auflösung, Encoder) und das Hinzufügen/Entfernen
  von Streams gelten beim nächsten Engine-Start. Mixer-Regler wirken live.
* Die Konfiguration liegt unter `%APPDATA%\StreamApp\config.json` (Streamschlüssel im Klartext!).
* Die Streams nutzen lokal die UDP-Ports ab 23000. Meldet ein Stream „UDP-Port belegt“, läuft meist
  noch ein alter ffmpeg-Prozess (Task-Manager) – oder in den Einstellungen andere Ports wählen.
* Alle Streams teilen sich das gleiche Video (eine Encodierung → wenig CPU-Last, gleiche Bitrate).
* RTMPS (z.B. Facebook) funktioniert mit den gängigen Windows-ffmpeg-Builds.

## Entwicklung

```
python -m unittest discover -s tests -t .
```

`streamapp/engine.py` baut die ffmpeg-Kommandozeilen, `procs.py` steuert die Prozesse,
`ui.py` ist die Oberfläche (PySide6).
