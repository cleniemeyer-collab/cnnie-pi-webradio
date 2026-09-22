# LMS vom Raspberry Pi nach ct102 verlagern

## Zielbild

- **ct102:** Lyrion Music Server in Docker, Qobuz-Plugin, Datenbank, Cache und Scan der Musiksammlung
- **NAS:** Musikfreigabe, auf ct102 nur lesend eingebunden
- **Raspberry Pi:** Squeezelite als Player, USB-DAC und cnnie-pi-webradio
- **Handy:** LMS-Steuerung über Material Skin oder eine kompatible App

Der lokale LMS auf dem Raspberry Pi bleibt bis zum erfolgreichen Abschlusstest erhalten.

## Aktueller Stand

- Der lokale Dienst **lyrionmusicserver.service** läuft auf dem Raspberry Pi.
- Squeezelite ist installiert und heißt **WebRadio-LMS**.
- Der USB-DAC wird über **plughw:CARD=Device,DEV=0** angesprochen.
- Squeezelite verbindet sich derzeit mit **127.0.0.1**.
- Qobuz wurde mit diesem Aufbau erfolgreich getestet.
- Die Webradio-Oberfläche startet Squeezelite im LMS-Modus und stoppt es bei den anderen Quellen.

## 1. ct102 prüfen

Nach Herstellung des Zugriffs zunächst ausführen:

~~~bash
hostnamectl
ip addr
docker --version
docker compose version
df -h
free -h
~~~

Die feste LAN-IP von ct102 feststellen. Der Raspberry Pi muss ct102 auf TCP-Port 3483 erreichen können.

## 2. NAS auf ct102 einhängen

Die genaue NAS-Freigabe und Adresse vor Ort prüfen. Vorgesehener Mountpunkt:

~~~text
/mnt/nas542-music
~~~

Vorgaben:

- CIFS-Credentials in einer nur für root lesbaren Datei speichern
- Mount nur lesend mit der Option **ro**
- bei diesem alten NAS zusätzlich **vers=1.0**
- SMB1 nur im vertrauenswürdigen lokalen Netz verwenden
- keine Zugangsdaten in Compose- oder Markdown-Dateien schreiben

Prüfung:

~~~bash
findmnt /mnt/nas542-music
ls -la /mnt/nas542-music
~~~

## 3. Verzeichnisse auf ct102 anlegen

~~~bash
sudo mkdir -p /opt/lyrion/config
sudo mkdir -p /opt/lyrion/playlists
~~~

Konfiguration und Playlists bleiben dadurch außerhalb des Containers persistent.

## 4. Docker Compose vorbereiten

Datei **/opt/lyrion/compose.yaml**:

~~~yaml
services:
  lyrion:
    image: lmscommunity/logitechmediaserver:stable
    container_name: lyrion
    network_mode: host
    restart: unless-stopped
    environment:
      TZ: Europe/Berlin
    volumes:
      - /opt/lyrion/config:/config
      - /opt/lyrion/playlists:/playlist
      - /mnt/nas542-music:/music:ro
~~~

Vor dem Start die aktuelle Dokumentation des Images und dessen Volume-Pfade prüfen. Das Host-Netzwerk vereinfacht die Player-Erkennung. Ohne Host-Netzwerk wären mindestens 9000/tcp, 3483/tcp und 3483/udp zu veröffentlichen.

## 5. Container starten

~~~bash
cd /opt/lyrion
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 lyrion
~~~

Weboberfläche: **http://ct102:9000** oder über die LAN-IP von ct102.

## 6. LMS konfigurieren

1. **/music** als Musikordner einstellen.
2. **/playlist** als Wiedergabelistenordner einstellen.
3. Musikbibliothek scannen lassen.
4. Qobuz-Plugin installieren oder aktivieren.
5. Qobuz anmelden und Katalogzugriff testen.
6. Material Skin und benötigte weitere Plugins prüfen.

Zunächst ist eine saubere Einrichtung vorzuziehen. Die bestehende lokale LMS-Konfiguration nur migrieren, wenn dafür ein konkreter Bedarf besteht; alte Cache- oder Plugin-Probleme könnten sonst übernommen werden.

## 7. Verbindung vom Raspberry Pi prüfen

~~~bash
getent hosts ct102
nc -vz ct102 3483
curl -I http://ct102:9000/
~~~

Falls die Namensauflösung nicht zuverlässig funktioniert, wird für Squeezelite die feste LAN-IP verwendet.

## 8. Squeezelite umstellen

Auf dem Raspberry Pi liegt die Unit unter **/etc/systemd/system/squeezelite.service**. In der Zeile **ExecStart** nur den Server ändern:

~~~ini
ExecStart=/usr/bin/squeezelite -n WebRadio-LMS -o plughw:CARD=Device,DEV=0 -s ct102
~~~

Danach:

~~~bash
sudo systemctl daemon-reload
sudo systemctl restart squeezelite
sudo systemctl status squeezelite --no-pager
~~~

In LMS auf ct102 muss anschließend **WebRadio-LMS** als verbundener Player erscheinen.

## 9. Vollständiger Funktionstest

1. In web_radio die Quelle LMS wählen.
2. In der Handy-App WebRadio-LMS auswählen.
3. Einen Qobuz-Titel abspielen und USB-DAC prüfen.
4. Einen Titel aus der NAS-Bibliothek abspielen.
5. Zu Radio wechseln; Squeezelite muss stoppen.
6. Wieder zu LMS wechseln; Squeezelite muss starten und ct102 erreichen.
7. MPD und Bluetooth ebenfalls auf konfliktfreie Umschaltung prüfen.
8. Raspberry Pi und ct102 neu starten und alle wichtigen Schritte wiederholen.

Hilfreiche Prüfungen auf dem Raspberry Pi:

~~~bash
systemctl is-active squeezelite
ps -ef | grep squeezelite
sudo fuser -v /dev/snd/pcmC2D0p
~~~

## 10. Lokalen LMS deaktivieren

Erst nach dem vollständigen erfolgreichen Test:

~~~bash
sudo systemctl disable --now lyrionmusicserver.service
systemctl is-enabled lyrionmusicserver.service
systemctl is-active lyrionmusicserver.service
~~~

Den lokalen LMS vorerst nicht deinstallieren, damit ein einfacher Rückweg bleibt.

## 11. Rückfallplan

Lokalen LMS wieder aktivieren:

~~~bash
sudo systemctl enable --now lyrionmusicserver.service
~~~

In der Squeezelite-Unit den Server wieder auf **127.0.0.1** stellen:

~~~ini
ExecStart=/usr/bin/squeezelite -n WebRadio-LMS -o plughw:CARD=Device,DEV=0 -s 127.0.0.1
~~~

Anschließend:

~~~bash
sudo systemctl daemon-reload
sudo systemctl restart squeezelite
~~~

## Wichtig für die nächste Sitzung

Heute wird nur diese Anleitung erstellt. Bis der Zugriff auf ct102 besteht und der externe LMS getestet wurde, werden weder der lokale LMS deaktiviert noch die aktuelle Squeezelite-Adresse verändert.
