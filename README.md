# cnnie-pi-webradio

PyQt5-Webradio für einen Raspberry Pi mit GStreamer-Audio, ICY-Metadaten,
Web-Senderverwaltung und Immich-Diashow.

## Funktionen

- Radiowiedergabe und Metadaten über GStreamer
- Senderkarussell mit lokalen Logos
- Webverwaltung auf Port 8088
- Radio-Browser- und radio.de-Suche
- Umschaltung zwischen Webradio und MPD
- Dunkelmodus
- Immich-Diashow aus dem Album `WEB Radio`
- automatischer Diashow-Start nach zwei Minuten Inaktivität
- geschützter Web-Player mit Gastanmeldung (`GAST` / initial `gast`)
- lokale Verwaltung des Gastzugangs über die Admin-Oberfläche

## Installation

Benötigte Systempakete:

```bash
sudo apt update
sudo apt install python3-pyqt5 python3-gi python3-musicpd \
  gir1.2-gstreamer-1.0 gstreamer1.0-tools \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav
```

Immich-Konfiguration anlegen:

```bash
cp immich_config.example.json immich_config.json
chmod 600 immich_config.json
```

Danach in `immich_config.json` die eigene Immich-Adresse und den API-Schlüssel
eintragen. Diese Datei wird von Git ignoriert.

## Prüfung und Start

```bash
python3 -m py_compile webradio.py station_store.py radio_browser.py web_admin.py \
  web_player_server.py web_player.py web_auth.py
DISPLAY=:0 python3 webradio.py
```

Beim ersten Start wird `web_auth_config.json` mit dem Gastzugang `GAST` und dem
initialen Passwort `gast` erzeugt. Das Passwort sollte anschließend in der nur
lokal erreichbaren Admin-Oberfläche auf Port `8088` geändert werden. Die Datei
wird von Git ignoriert.

Der aktuelle Stand steht in [`STATUS.md`](STATUS.md); die ausführliche technische
Übergabe befindet sich in [`PROJEKTSTAND.md`](PROJEKTSTAND.md).

