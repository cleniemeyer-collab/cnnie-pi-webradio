# Webradio – Web-Player

## Überblick

Dieses Projekt erweitert das bestehende PyQt5-Webradio um einen **Web-Player**, der die gleiche GUI über einen Browser zugänglich macht. Audio-Wiedergabe und Senderauswahl laufen im Browser; die Immich-Diashow wird über einen serverseitigen Proxy bereitgestellt. Der Player verlangt eine Anmeldung.

---

## Architektur

```
┌─────────────────────────────────────────────────┐
│  Raspberry Pi (Server)                           │
│                                                  │
│  webradio.py                                     │
│  ├── Qt-GUI (Touchscreen am Pi)                  │
│  │   └── GStreamer-Audio (lokal)                 │
│  ├── Admin-Server  :8088  (Sender verwalten)     │
│  └── Web-Player-Server :8089                     │
│       ├── GET  /                  → HTML-GUI     │
│       ├── GET  /api/stations      → Sender JSON  │
│       ├── GET  /logos/<datei>     → Logo-Bilder  │
│       └── GET  /api/slideshow/*   → Immich-Proxy │
└──────────────────────┬──────────────────────────┘
                       │ HTTP
                       ▼
┌─────────────────────────────────────────────────┐
│  Browser (Client)                                │
│                                                  │
│  http://<pi-ip>:8089                             │
│  ├── HTML5 <audio> (Ton im Browser)              │
│  ├── Carousel-Senderauswahl                      │
│  ├── Immich-Diashow (auto nach 2 Min Idle)       │
│  ├── Dark Mode                                   │
│  └── Touch & Tastatur-Steuerung                  │
└─────────────────────────────────────────────────┘
```

---

## Dateien

| Datei | Beschreibung |
|---|---|
| `webradio.py` | Hauptprogramm (Qt-GUI + Admin-Server + Web-Player-Server) |
| `web_player_server.py` | HTTP-Server mit API-Endpunkten & Immich-Diashow-Proxy |
| `templates/web_player.html` | Komplette Player-GUI als HTML/CSS/JS |
| `web_player.py` | Standalone-Startskript (nur Web, ohne Qt) |
| `web_admin.py` | Admin-Oberfläche zum Sender-Management (Port 8088) |
| `station_store.py` | Sender-Speicherung (JSON/CSV) |

---

## Ports

| Port | Dienst | URL |
|---|---|---|
| **8088** | Admin-Oberfläche (Sender verwalten) | `http://<pi-ip>:8088` |
| **8089** | Web-Player (Radio abspielen) | `http://<pi-ip>:8089` |

---

## Starten

### Qt-GUI + Web parallel (Standard)

```bash
python3 webradio.py
```

Startet alles gleichzeitig:
- Qt-GUI auf dem Pi-Touchscreen
- Web-Player unter `http://<pi-ip>:8089`
- Admin-Oberfläche unter `http://<pi-ip>:8088`

### Nur Web-Modus (headless)

```bash
python3 web_player.py
python3 web_player.py --port 8080
```

Nützlich, wenn der Pi ohne Display läuft.

---

## Bedienung im Browser

### Anmeldung

- initialer Benutzer: `GAST`
- initiales Passwort: `gast`
- das Passwort sollte sofort über die lokale Admin-Oberfläche auf Port `8088` geändert werden
- der Gastzugang kann dort aktiviert oder deaktiviert werden
- **Ausschalten** stoppt Radio und Diashow, beendet die Browsersitzung und öffnet die Loginseite

### Sender-Auswahl

- **Mittlere Station** klicken → Play / Pause
- **Seitliche Station** klicken → Sender wechseln & abspielen
- **‹ / ›** Buttons → Sender durchblättern
- **Swipe** (Touch) → Links/Rechts wischen

### Diashow

- **Automatisch** nach 2 Minuten Inaktivität
- **Manuell** über den „Diashow"-Button
- **Swipe** oder **‹ / ›** → Bilder wechseln
- **⏸ / ▶** → Pause / Fortsetzen
- **✕** oder **Escape** → Schließen

### Tastatur

| Taste | Wirkung |
|---|---|
| `←` `→` | Sender / Bilder wechseln |
| `Leertaste` | Play/Pause oder Diashow-Pause |
| `Escape` | Diashow/Dark-Mode schließen |

### Dark Mode

- **„Dunkel"** Button → Bildschirm schwarz (Touchscreen aus)
- **Klick/Touch** auf schwarzen Bildschirm → zurück zur GUI

---

## API-Endpunkte (Port 8089)

### Sender

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/` | Player-GUI (HTML) |
| `GET` | `/api/stations` | Senderliste als JSON |
| `GET` | `/logos/<datei>` | Sender-Logo-Bild |

### Anmeldung

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/login` | Anmeldeseite |
| `GET` | `/api/login-status` | Sitzung und CSRF-Token abfragen |
| `POST` | `/api/login` | Anmeldung |
| `POST` | `/api/logout` | Abmeldung |

Player, Sender, Logos, Diashow und Shutdown verlangen eine gültige Sitzung.

### Diashow

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/slideshow/init` | Album laden & mischen |
| `GET` | `/api/slideshow/next` | Nächstes Bild (base64 + EXIF) |
| `GET` | `/api/slideshow/prev` | Vorheriges Bild |
| `GET` | `/api/slideshow/pause` | Pause umschalten |
| `GET` | `/api/slideshow/status` | Status (Index, Total, Pausiert) |

---

## Unterschiede Qt vs. Web

| Feature | Qt (Pi-Touchscreen) | Web (Browser) |
|---|---|---|
| **Audio** | GStreamer (lokal am Pi) | HTML5 `<audio>` (im Browser) |
| **Diashow** | PyQt-Thread + Immich | Server-Proxy + JS-Timer |
| **Titel-Metadaten** | GStreamer ICY-Tags | Station-Name (ICY im Browser nicht verfügbar) |
| **Ausschalten** | `sudo shutdown` via subprocess | stoppt Audio/Diashow und meldet den Browser ab |
| **Display** | Frameless Fullscreen | Browser-Fenster/Tab |
| **MPD** | echte MPD-Steuerung | nur Anzeige; keine MPD-Steuerung |

---

## Voraussetzungen

- Python 3
- Immich-Server mit Album „WEB Radio" (für Diashow)
- `immich_config.json` mit `immich_url` und `api_key`
- Sender in `radio_station_list.json` oder `radio_station_list.csv`

---

## Projektstruktur

```
cnnie-pi-webradio/
├── webradio.py                  # Hauptprogramm (Qt + Server)
├── web_player_server.py         # Web-Player HTTP-Server
├── web_player.py                # Standalone Web-Start
├── web_admin.py                 # Admin-Oberfläche
├── web_auth.py                  # Passwort-, Session- und CSRF-Logik
├── web_auth_config.json         # lokal erzeugte Auth-Konfiguration
├── station_store.py             # Sender-Speicherung
├── radio_station_list.json      # Sender-Datenbank
├── radio_station_list.csv       # CSV-Quelle (Legacy)
├── immich_config.json           # Immich-Anbindung
├── templates/
│   ├── web_player.html          # Web-Player GUI
│   └── login.html               # Anmeldeseite
└── logos/
    └── *.png / *.jpg            # Sender-Logos
```
