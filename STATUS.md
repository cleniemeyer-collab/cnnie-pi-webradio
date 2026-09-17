# Projektstatus

Stand: 2. September 2026

## Projektziel

Das Projekt stellt ein Webradio für einen Raspberry Pi bereit. Es besteht aus
einer lokalen PyQt5-Oberfläche und einem Web-Player, der über einen Browser
verwendet werden kann. Beide Oberflächen bieten eine Immich-Diashow aus dem
Album `WEB Radio`.

## Aktueller Funktionsstand

### Lokale Raspberry-Pi-Oberfläche

- PyQt5-Vollbildoberfläche für den Touchscreen
- Radiowiedergabe über GStreamer und den Audioausgang des Raspberry Pi
- Audio-Buffer-Watchdog mit automatischem Stream-Neustart nach 25 Sekunden Stillstand
- Senderkarussell mit lokalen Senderlogos
- Webradio- und MPD-Modus
- Dunkelmodus
- Immich-Diashow mit automatischem Start nach zwei Minuten Inaktivität
- manuelle Navigation, Pause und Metadatenanzeige in der Diashow

### Web-Player

- erreichbar über den HTTP-Server auf Port `8089`
- Browser-Wiedergabe über HTML5-Audio
- Senderauswahl, Play/Pause, Dunkelmodus und Immich-Diashow
- Immich-Bilder werden vom Raspberry Pi geladen und an den Browser
  weitergereicht; der API-Schlüssel gelangt nicht in den Browser
- mobile Darstellung mit dynamischer Viewport-Höhe
- auf schmalen Displays werden drei Senderkarten und die Bedienelemente in
  einer platzsparenden Anordnung dargestellt
- eine manuelle Radiopause bleibt beim automatischen Aktualisieren der
  Senderliste erhalten

### Immich-Anbindung

- Konfiguration über `immich_config.json`
- verwendete Felder: `immich_url` und `api_key`
- das Album muss `WEB Radio` heißen
- Albuminhalte werden über `/api/search/metadata` geladen
- Vorschaubilder werden über `/api/assets/<id>/thumbnail?size=preview` geladen
- der Web-Player verwendet einen serverseitigen Immich-Proxy
- die zuvor untersuchte automatische Umschaltung zwischen lokaler und externer
  Immich-Adresse wurde wieder entfernt

### Web-Administration

- Senderverwaltung über den Admin-Server auf Port `8088`
- Sender können über die vorhandene Admin-Oberfläche gepflegt werden
- Gastzugang kann aus privaten Netzen aktiviert oder deaktiviert werden
- Gastpasswort kann aus privaten Netzen geändert werden

### Web-Authentifizierung

- serverseitige Anmeldung vor Zugriff auf Player und Player-APIs
- initialer Zugang `GAST` / `gast`
- Passwortspeicherung als `scrypt`-Hash mit zufälligem Salt
- thread-sichere Sitzungen mit Ablaufzeit
- Rate-Limit für fehlgeschlagene Anmeldeversuche
- CSRF-Schutz für authentifizierte Aktionen wie Logout und Shutdown-Anfrage
- Session-Cookies mit `HttpOnly`, `SameSite=Lax` und bei Proxy-HTTPS mit `Secure`
- `Ausschalten` stoppt im Web-Player Radio und Diashow und meldet die Sitzung ab
- lokale Konfiguration in `web_auth_config.json`; durch `.gitignore` ausgeschlossen

## Wichtige Dateien

| Datei | Aufgabe |
|---|---|
| `webradio.py` | PyQt5-Hauptprogramm und lokale Immich-Diashow |
| `web_player_server.py` | Web-Player-Server und Immich-Proxy |
| `templates/web_player.html` | Web-Player-Oberfläche mit HTML, CSS und JavaScript |
| `web_player.py` | eigenständiger Start des Web-Players ohne Qt |
| `web_admin.py` | Weboberfläche zur Sender- und Gastzugangsverwaltung |
| `web_auth.py` | Passwort-, Session-, Rate-Limit- und CSRF-Logik |
| `templates/login.html` | responsive Anmeldeseite |
| `station_store.py` | Speicherung und Laden der Senderdaten |
| `immich_config.json` | lokale, nicht versionierte Immich-Zugangsdaten |
| `web-auth/AUFGABE.md` | Spezifikation der geplanten Web-Authentifizierung |

## Zuletzt behobene Probleme

1. Ein verschachteltes Lock im Web-Diashow-Endpunkt blockierte die erste
   Bildantwort. Der Diashow-Status verwendet nun ein reentrantes Lock.
2. Immich-Fehlertexte werden vollständig im Fehlerobjekt gespeichert und
   können dadurch korrekt angezeigt werden.
3. Die Weboberfläche wurde für Mobiltelefone angepasst.
4. Der 30-sekündliche Senderlisten-Refresh startet ein pausiertes Radio nicht
   mehr selbstständig neu.

## Validierung

Folgende Prüfungen wurden während der Änderungen erfolgreich ausgeführt:

- Python-Syntaxprüfung mit `python -m py_compile`
- isolierter HTTP-Test für `init -> status -> next` der Web-Diashow
- HTML-Parser-Prüfung für `templates/web_player.html`
- Editor-Diagnose für die Web-Player-Vorlage ohne Fehler oder Warnungen
- 11 automatisierte Auth- und HTTP-Integrationstests mit `unittest`

Ein vollständiger Systemtest mit Raspberry Pi, Reverse-Proxy, Immich und
externem Mobilgerät muss auf der Zielumgebung erfolgen.

## Boot und Dienste

- Der Web-Player kann mit `deploy/cnnie-webradio-web.service` unabhängig von
  X11 als systemd-Dienst gestartet werden.
- Die lokale GUI bleibt im X11-/Desktop-Autostart.
- `WEBRADIO_START_WEB_PLAYER=0` verhindert dabei eine zweite Web-Player-Instanz
  aus `webradio.py`.
- Einzelheiten stehen in [`BOOT_AUTOSTART.md`](BOOT_AUTOSTART.md).

## Offene Punkte

- Der intern noch vorhandene Web-Shutdown-Endpunkt fährt das System nicht
  tatsächlich herunter; die Weboberfläche verwendet stattdessen Logout.
- MPD wird ausschließlich durch die lokale Qt-Oberfläche gesteuert; der
  Web-Player zeigt den Modus nur an.
- Die Admin-Oberfläche insgesamt besitzt noch keine eigene Anmeldung. Die
  Auth-Einstellungen sind deshalb zusätzlich auf private Client-Adressen und
  localhost begrenzt.

Die ursprüngliche Auth-Spezifikation und ihr Umsetzungsstand stehen in
[`web-auth/AUFGABE.md`](web-auth/AUFGABE.md).
