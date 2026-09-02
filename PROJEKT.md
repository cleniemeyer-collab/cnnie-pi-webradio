# Raspberry-Pi-Webradio

## Überblick

Dieses Projekt betreibt ein Webradio auf einem Raspberry Pi. Es bietet zwei
Oberflächen:

1. eine lokale PyQt5-Vollbildoberfläche für den Touchscreen des Raspberry Pi
2. einen responsiven Web-Player für Desktop- und Mobilbrowser

Die lokale Oberfläche gibt Audio über GStreamer am Raspberry Pi aus. Der
Web-Player verwendet HTML5-Audio und gibt den Stream auf dem jeweiligen Browser
aus.

## Funktionen

### Radio

- Senderkarussell mit lokalen Logos
- Startsender und konfigurierbare Senderreihenfolge
- Play/Pause und Senderwechsel
- GStreamer-Metadaten in der lokalen Oberfläche
- HTML5-Audio im Web-Player
- pausierte Web-Wiedergabe bleibt auch beim Aktualisieren der Senderliste
  pausiert
- lokale MPD-Steuerung in der PyQt5-Oberfläche

### Immich-Diashow

- Bilder ausschließlich aus dem Immich-Album `WEB Radio`
- manueller Start und automatischer Start nach zwei Minuten Inaktivität
- Bildwechsel nach 90 Sekunden
- Navigation, Pause und Metadatenanzeige
- Anzeige von Aufnahmedatum und Ort, sofern vorhanden
- serverseitiger Immich-Proxy für den Web-Player
- der Immich-API-Schlüssel wird niemals an den Browser übertragen

### Web-Authentifizierung

- Anmeldung vor dem Zugriff auf Web-Player und Player-APIs
- initialer Zugang `GAST` mit Passwort `gast`
- Passwörter werden mit `scrypt` und zufälligem Salt gespeichert
- thread-sichere Sitzungen mit Ablaufzeit
- Rate-Limit für fehlgeschlagene Anmeldeversuche
- CSRF-Schutz für authentifizierte Aktionen
- Cookies mit `HttpOnly`, `SameSite=Lax` und bei HTTPS mit `Secure`
- der Web-Button `Ausschalten` stoppt Radio und Diashow, beendet die Sitzung und
  öffnet wieder die Loginseite

Das initiale Passwort muss nach der Inbetriebnahme über die lokale
Admin-Oberfläche geändert werden.

### Administration

Die Admin-Oberfläche verwaltet:

- Sendersuche über Radio Browser und radio.de
- manuell angelegte Sender
- Senderreihenfolge
- Startsender
- Aktivierung oder Deaktivierung des Gastzugangs
- Gastpasswort

Die Auth-Einstellungen sind nur von privaten Client-Adressen oder localhost
erreichbar. Die allgemeine Admin-Oberfläche besitzt derzeit noch keine eigene
Anmeldung und darf deshalb nicht öffentlich über den Reverse-Proxy freigegeben
werden.

## Dienste und Ports

| Port | Dienst | Beschreibung |
|---|---|---|
| `8088` | Admin-Server | Sender- und Gastzugangsverwaltung |
| `8089` | Web-Player | Login, Radio und Immich-Diashow |
| `6600` | MPD | lokale MPD-Verbindung, sofern verwendet |

Der Web-Player kann beispielsweise über Nginx Proxy Manager unter einer
öffentlichen HTTPS-Domain auf den internen Dienst `raspberrypi:8089`
weitergeleitet werden. Port `8088` sollte nur im lokalen Netz erreichbar sein.

## Wichtige Dateien

| Datei | Aufgabe |
|---|---|
| `webradio.py` | PyQt5-Hauptprogramm, lokales Radio und lokale Diashow |
| `web_player_server.py` | Webserver, Login, Player-API und Immich-Proxy |
| `web_player.py` | Standalone-Start des Web-Players ohne Qt |
| `web_auth.py` | Passwort-, Session-, Rate-Limit- und CSRF-Logik |
| `web_admin.py` | Admin-Oberfläche und lokale Auth-Verwaltung |
| `station_store.py` | Senderpersistenz und Migration |
| `radio_browser.py` | Sendersuche und Logo-Download |
| `templates/web_player.html` | responsive Player-Oberfläche |
| `templates/login.html` | responsive Anmeldeseite |
| `immich_config.json` | lokale Immich-Konfiguration, nicht versioniert |
| `web_auth_config.json` | lokale Auth-Konfiguration, nicht versioniert |
| `tests/test_web_auth.py` | Auth- und HTTP-Integrationstests |

## Konfiguration

### Immich

Aus `immich_config.example.json` eine lokale Konfiguration erstellen:

```bash
cp immich_config.example.json immich_config.json
chmod 600 immich_config.json
```

Format:

```json
{
  "immich_url": "http://IMMICH-SERVER:2283",
  "api_key": "API-SCHLUESSEL"
}
```

`immich_config.json` enthält ein Geheimnis und darf nicht versioniert,
protokolliert oder öffentlich bereitgestellt werden.

### Web-Authentifizierung

`web_auth_config.json` wird beim ersten Start automatisch erzeugt. Die Datei
enthält den Passwort-Hash und wird durch `.gitignore` ausgeschlossen.

Initiale Anmeldung:

```text
Benutzer: GAST
Passwort: gast
```

Das Passwort anschließend unter `http://raspberrypi:8088` ändern.

## Start

### Komplettes System

```bash
DISPLAY=:0 python3 webradio.py
```

Dabei starten:

- lokale PyQt5-Oberfläche
- Admin-Server auf Port `8088`
- Web-Player auf Port `8089`

### Nur Web-Player

```bash
python3 web_player.py
```

Optional:

```bash
python3 web_player.py --host 0.0.0.0 --port 8089
```

## Prüfung

Python-Syntax für den Raspberry-Pi-Stand prüfen:

```bash
python3 -m py_compile webradio.py station_store.py radio_browser.py \
  web_admin.py web_player_server.py web_player.py web_auth.py
```

Automatisierte Auth- und HTTP-Tests ausführen:

```bash
python3 -m unittest discover -s tests -v
```

## Sicherheitshinweise

- `immich_config.json` und `web_auth_config.json` niemals committen.
- Das initiale Gastpasswort `gast` sofort ändern.
- Den externen Web-Player ausschließlich über HTTPS bereitstellen.
- Port `8088` nicht über den öffentlichen Reverse-Proxy freigeben.
- Reverse-Proxy-Header nicht ungeprüft für Zugriffsentscheidungen verwenden.
- Nach Änderungen den Dienst vollständig neu starten.

## Bekannte Einschränkungen

- Der Web-Player steuert MPD nicht; MPD ist nur lokal in der Qt-Oberfläche
  implementiert.
- Der Web-Player fährt den Raspberry Pi nicht herunter. `Ausschalten` beendet
  lediglich Wiedergabe, Diashow und Browsersitzung.
- Ein vollständiger Systemtest mit Raspberry Pi, Audiohardware, Immich und
  Reverse-Proxy muss auf der Zielumgebung erfolgen.

Weitere Details stehen in [`README.md`](README.md),
[`WEB_PLAYER.md`](WEB_PLAYER.md) und [`STATUS.md`](STATUS.md).
