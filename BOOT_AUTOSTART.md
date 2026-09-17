# Boot und Autostart auf dem Raspberry Pi

## Aktuelle Situation

Der Benutzer `pi` wird automatisch an X11 angemeldet. Die X11-Autostart-
Konfiguration führt anschließend `/home/pi/start_radio.sh` aus. Das ist für die
lokale PyQt5-Oberfläche grundsätzlich sinnvoll, weil sie einen laufenden
X-Server und die Benutzersitzung benötigt.

`webradio.py` startet derzeit zusätzlich:

- die Admin-Oberfläche auf Port `8088`
- den Web-Player auf Port `8089`

Dadurch hängt der Web-Player unnötig vom erfolgreichen X11-Login ab. Wenn X11
oder das Autologin ausfällt, ist auch der externe Webdienst nicht erreichbar.

## Empfohlene Aufteilung

### Web-Player als systemd-Systemdienst

Der Web-Player benötigt kein Display. Er sollte unabhängig von X11 durch
systemd gestartet werden. Eine Vorlage liegt unter:

```text
deploy/cnnie-webradio-web.service
```

Installation auf dem Raspberry Pi:

```bash
sudo cp /home/pi/cnnie-pi-webradio/deploy/cnnie-webradio-web.service \
  /etc/systemd/system/cnnie-webradio-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now cnnie-webradio-web.service
```

Status und Protokoll prüfen:

```bash
systemctl status cnnie-webradio-web.service
journalctl -u cnnie-webradio-web.service -n 100 --no-pager
```

Der Dienst startet nur `web_player.py` auf Port `8089`. Er benötigt weder
`DISPLAY` noch ein X11-Autologin.

### Lokale GUI weiter über die X11-Sitzung

Die Touch-GUI kann weiterhin nach dem Autologin gestartet werden. Ein mögliches
`/home/pi/start_radio.sh` ist:

```bash
#!/bin/sh
cd /home/pi/cnnie-pi-webradio || exit 1
export WEBRADIO_START_WEB_PLAYER=0
exec /usr/bin/python3 /home/pi/cnnie-pi-webradio/webradio.py
```

Die Datei ausführbar machen:

```bash
chmod 755 /home/pi/start_radio.sh
```

`WEBRADIO_START_WEB_PLAYER=0` verhindert, dass die GUI eine zweite Instanz auf
Port `8089` startet. Ohne diese Variable bleibt das bisherige Verhalten erhalten:
`webradio.py` startet GUI, Admin-Server und Web-Player gemeinsam.

## Warum nicht die GUI direkt als Systemdienst starten?

Eine grafische Qt-Anwendung als systemweiter Dienst benötigt mindestens:

- einen bereits gestarteten X-Server
- die korrekte Variable `DISPLAY`
- Zugriff auf die passende Xauthority-Datei
- richtige Audio- und Sitzungsrechte

Das ist fehleranfälliger als ein Desktop-/X11-Autostart. Deshalb ist die
sinnvolle Trennung:

- displayunabhängiger Web-Player: systemd
- lokale Touch-GUI: X11-/Desktop-Autostart

## Verhalten bei Neustarts

Der systemd-Dienst verwendet:

```ini
Restart=on-failure
RestartSec=5
```

Damit wird der Web-Player nach einem Prozessfehler automatisch neu gestartet.
Ein reguläres Stoppen mit `systemctl stop` startet ihn nicht sofort wieder.

## Wichtiger Hinweis

Vor der Aktivierung prüfen, dass Port `8089` nicht bereits durch eine andere
Instanz belegt ist:

```bash
ss -ltnp | grep ':8089'
```

Nach der Umstellung sollte nur eine dauerhaft zuständige Web-Player-Instanz
laufen.
