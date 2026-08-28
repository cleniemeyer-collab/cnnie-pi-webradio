# Übergabe: Raspberry-Pi-Webradio mit Immich-Diashow

Stand: 28.08.2026

## Aufgabe für den neuen Prompt

Arbeite am vorhandenen Projekt `cnnie-pi-webradio` weiter. Auf dem Raspberry Pi
liegt es unter `/home/pi/cnnie-pi-webradio`.

Lies vor einer Änderung mindestens diese vier aktiven Quelldateien vollständig:

1. `webradio.py`
2. `station_store.py`
3. `radio_browser.py`
4. `web_admin.py`

Die Beschreibung in dieser Datei ist eine Orientierung. Der tatsächlich
vorhandene Quellcode ist maßgeblich. Die aktive Hauptdatei heißt
`webradio.py`.

## Projektziel

Das Projekt ist ein bildschirmfüllendes PyQt5-Webradio für einen Raspberry Pi.
Die Radioausgabe läuft über einen angeschlossenen USB-Audio-DAC. Parallel kann
eine Immich-Diashow als Bildschirmschoner angezeigt werden.

## Tatsächlich vorhandene Projektdateien

- `webradio.py` – Hauptfenster, GStreamer-Wiedergabe, Metadaten, MPD,
  Dunkelmodus und Immich-Diashow
- `station_store.py` – Senderpersistenz, CSV-Migration, Reihenfolge und
  Startsender
- `radio_browser.py` – Radio-Browser- und radio.de-Suche, Streamauswahl und
  Logo-Cache
- `web_admin.py` – Weboberfläche zur Senderverwaltung auf TCP-Port 8088
- `radio_station_list.json` – aktuelle Senderliste
- `radio_station_list.csv` – frühere Liste beziehungsweise Migrationsquelle
- `logos/` – lokal gespeicherte Senderlogos
- `immich_config.json` – lokale Immich-Konfiguration; enthält ein Geheimnis und
  darf nicht in Prompts, Logs oder Archive kopiert werden

Alte Dateien wie `hello.py`, `hello.py.org`, `*.pyc`, `__pycache__`, Logdateien
und Sicherungskopien gehören nicht zum aktiven Programmstand.

## Aktueller, im Quellcode vorhandener Funktionsstand

### Radio

- GStreamer `playbin` spielt den Radiostream.
- Ein Qt-Timer verarbeitet den GStreamer-Bus.
- GStreamer-TAG-Nachrichten liefern Künstler und Titel.
- `source-setup` setzt ICY-Modus und User-Agent.
- Senderwechsel, Pause, MPD-Umschaltung und Programmende setzen die passenden
  GStreamer-Zustände.
- Das Senderlogo steht oben links. Künstler und Titel werden groß rechts
  daneben angezeigt.
- Die Senderauswahl erfolgt über ein Karussell.
- Genau ein Sender besitzt in `radio_station_list.json` die Markierung
  `startup: true`.

Wichtige Methoden in `RadioWindow`:

- `start_selected_station()`
- `toggle_mode()`
- `configure_gstreamer_source()`
- `process_gstreamer_bus()`
- `update_gstreamer_metadata()`
- `reload_stations()`
- `set_logo()`

### Senderverwaltung

Die Verwaltung ist im lokalen Netz erreichbar unter:

```text
http://RASPBERRY-PI-IP:8088/
```

Vorhandene Funktionen:

- Sender suchen, hinzufügen und löschen
- Reihenfolge ändern
- Startsender festlegen
- Sender manuell mit Stream-, Metadaten- und Logo-URL anlegen
- Radio Browser als erste Suchquelle
- radio.de über `https://prod.radio-api.net` als Fallback

Änderungen werden über den Callback `on_change` an das Hauptfenster gemeldet;
das Hauptfenster lädt daraufhin die Senderliste neu.

### Immich-Diashow

Die Diashow ist bereits in der aktiven `webradio.py` implementiert. Die
zentrale Klasse heißt `ImmichSlideshowLoader` und läuft als `QThread`.

Vorhandenes Verhalten:

- manueller Start über die Schaltfläche `Diashow`
- automatischer Start nach zwei Minuten ohne Bedienung
- ausschließlich Bilder aus dem Immich-Album `WEB Radio`
- kein Fallback auf die gesamte Immich-Sammlung
- Bildwechsel nach 90 Sekunden
- Abruf von Preview-Dateien, nicht von Originalbildern
- Vorladen des nächsten Bildes
- Überspringen fehlerhafter Previews
- maximal 800 × 600 Pixel große Bildfläche bei erhaltenem Seitenverhältnis
- schwarze freie Flächen
- Pfeile links und rechts zum zyklischen Blättern
- Klick außerhalb der Pfeile beendet die Diashow
- Radiowiedergabe läuft während der Diashow weiter
- das Overlay wird erst nach erfolgreichem Laden des ersten Bildes sichtbar

Wichtige Klassen und Methoden:

- `ImmichSlideshowLoader`
- `SlideshowOverlay`
- `RadioWindow.start_slideshow()`
- `RadioWindow.navigate_slideshow()`
- `RadioWindow.stop_slideshow()`
- `RadioWindow.show_slideshow_image()`
- `RadioWindow.layout_slideshow_overlay()`
- `RadioWindow.scale_slideshow_image()`

Verwendete Immich-Endpunkte:

```text
GET  /api/albums
GET  /api/albums/{albumId}
POST /api/search/metadata
GET  /api/assets/{assetId}/thumbnail?size=preview
```

Da die installierte Immich-Version im Albumdetail keine Assets liefert, sucht
`load_asset_ids()` die Bilder paginiert über `/api/search/metadata` mit der
Album-ID und `type: IMAGE`.

### Dunkelmodus

- `Dunkel` zeigt ein schwarzes Vollbild-Overlay.
- Eine laufende oder ladende Diashow wird dabei beendet.
- Solange das schwarze Overlay sichtbar ist, startet keine Diashow.
- Ein Klick auf die schwarze Fläche beendet den Dunkelmodus und startet den
  Inaktivitätstimer neu.

## Immich-Konfiguration

Die Datei `/home/pi/cnnie-pi-webradio/immich_config.json` hat dieses Format:

```json
{
  "immich_url": "http://IMMICH-SERVER:2283",
  "api_key": "GEHEIMER-API-SCHLUESSEL"
}
```

Der echte API-Schlüssel darf weder angezeigt noch in Quellcode, Dokumentation,
Logs oder Prompts kopiert werden.

## Zuletzt geprüfter technischer Stand

Am 28.08.2026 wurden diese Dateien gemeinsam mit `python3 -m py_compile`
erfolgreich geprüft:

```text
webradio.py
station_store.py
radio_browser.py
web_admin.py
```

Die aktive `webradio.py` hatte dabei 43.123 Byte und enthielt unter anderem
`ImmichSlideshowLoader`, `SlideshowOverlay` und `DarkOverlay`. Diese Angaben
belegen die vorhandene Codefassung, ersetzen aber keinen praktischen Test auf
dem Raspberry Pi nach weiteren Änderungen.

## Nächste gewünschte Änderung

Die Immich-Diashow soll Bildinformationen als dezente Overlays erhalten:

- Aufnahmedatum oben links, sofern vorhanden
- Ortsangabe am unteren Bildrand, sofern vorhanden
- gut lesbarer halbtransparenter Hintergrund
- dynamisch passende Schriftgröße und Umbrüche
- Positionierung relativ zur tatsächlich sichtbaren Bildfläche, nicht zu den
  schwarzen Rändern
- Bilder ohne Datum oder Ort müssen weiterhin normal angezeigt werden

Diese Metadatenanzeige ist im derzeit gesicherten Stand noch nicht vorhanden.
Vor der Implementierung ist zu prüfen, welche Metadaten die vorhandene
Immich-Version über den sinnvollsten API-Endpunkt tatsächlich liefert. Die
Netzwerkabfragen dürfen die Qt-Ereignisschleife nicht blockieren.

## Start und Prüfung auf dem Raspberry Pi

Syntaxprüfung:

```bash
cd /home/pi/cnnie-pi-webradio
python3 -m py_compile webradio.py station_store.py radio_browser.py web_admin.py
```

Programm neu starten:

```bash
pkill -f "[w]ebradio.py"
DISPLAY=:0 python3 /home/pi/cnnie-pi-webradio/webradio.py
```

Falls EGL-Probleme auftreten:

```bash
DISPLAY=:0 LIBGL_ALWAYS_SOFTWARE=1 QT_XCB_GL_INTEGRATION=none \
python3 /home/pi/cnnie-pi-webradio/webradio.py
```

## Vorgehen bei der nächsten Änderung

1. Die vier aktiven Python-Dateien lesen und den vorhandenen Stand bestätigen.
2. Kurz beschreiben, an welchen vorhandenen Klassen und Methoden die Änderung
   ansetzt.
3. Nur die für die konkrete Aufgabe notwendigen Dateien ändern.
4. Danach die Syntaxprüfung ausführen.
5. Klar trennen zwischen automatischer Prüfung und einem noch ausstehenden
   praktischen Test durch den Nutzer am Display und mit der Audioanlage.
