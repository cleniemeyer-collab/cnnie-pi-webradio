# Aufgabe: Authentifizierung für den Web-Player

## Umsetzungsstand

Die Kernaufgabe ist umgesetzt:

- Login mit initial `GAST` / `gast`
- `scrypt`-Passwort-Hash, Sessions, Rate-Limit und CSRF
- Schutz der Web-Player-Routen
- sichtbare Abmeldung
- Gaststatus und Passwortverwaltung über die Admin-Oberfläche
- Beschränkung der Auth-Verwaltung auf private Client-Adressen und localhost
- automatisierte Tests in `tests/test_web_auth.py`

Noch offen ist eine separate Anmeldung für die gesamte Admin-Oberfläche. Die
aktuelle Netzbeschränkung schützt gezielt die Auth-Einstellungen, nicht die
allgemeine Senderverwaltung.

## Kontext

Das Hauptprojekt `cnnie-pi-webradio` stellt den Web-Player über
`web_player_server.py` auf Port `8089` bereit. Die Oberfläche liegt in
`templates/web_player.html`. Aktuell kann jeder, der die öffentliche Adresse
erreicht, den Web-Player öffnen und Radio abspielen.

Die vorhandene Admin-Oberfläche wird durch `web_admin.py` auf Port `8088`
bereitgestellt.

## Ziel

Implementiere eine serverseitige Benutzeranmeldung für den Web-Player. Nicht
angemeldete Besucher dürfen weder die Player-Oberfläche noch die geschützten
Player-API-Endpunkte verwenden.

Es muss einen vorkonfigurierten Gastzugang geben:

- Benutzername: `GAST`
- initiales Passwort: `gast`

Der Benutzername soll ohne Beachtung der Groß-/Kleinschreibung akzeptiert
werden. Passwörter bleiben immer case-sensitive.

## Funktionsanforderungen

### Anmeldung

1. Nicht angemeldete Aufrufe des Web-Players zeigen eine responsive
   Anmeldeseite.
2. Nach erfolgreicher Anmeldung wird der Web-Player geöffnet.
3. Fehlgeschlagene Anmeldungen zeigen eine neutrale Fehlermeldung, aus der
   nicht hervorgeht, ob Benutzername oder Passwort falsch war.
4. Der Web-Player erhält eine sichtbare Abmeldefunktion.
5. Nach dem Abmelden sind die geschützten Endpunkte nicht mehr verwendbar.
6. Eine Sitzung soll einen Browser-Neustart nicht unbegrenzt überleben. Lege
   eine sinnvolle, konfigurierbare Gültigkeitsdauer fest.

### Gastzugang

1. Beim ersten Start existiert der Benutzer `GAST` mit dem Passwort `gast`.
2. Der Gastzugang ist standardmäßig aktiviert.
3. Das Gastpasswort kann in der Admin-Oberfläche geändert werden.
4. Der Gastzugang kann in der Admin-Oberfläche vollständig deaktiviert und
   wieder aktiviert werden.
5. Ist der Gastzugang deaktiviert, muss eine bestehende Gastsitzung spätestens
   beim nächsten Request ungültig werden.
6. Das initiale Passwort `gast` ist nur als ausdrücklich gewünschter
   Startwert zu behandeln. Die Admin-Oberfläche soll darauf hinweisen, dass es
   für einen öffentlich erreichbaren Dienst geändert werden sollte.

### Admin-Oberfläche

Erweitere `web_admin.py` um einen klar abgegrenzten Bereich
`Web-Player-Zugang` mit:

- Statusanzeige `Gastzugang aktiviert/deaktiviert`
- Schalter zum Aktivieren oder Deaktivieren
- Eingabefeldern für neues Passwort und Passwortbestätigung
- verständlichen Erfolgs- und Fehlermeldungen

Die Änderung des Zugangs darf nicht über einen ungeschützten öffentlichen
Endpunkt möglich sein. Prüfe die vorhandene Absicherung der Admin-Oberfläche.
Falls sie bisher nicht geschützt ist, dokumentiere dieses Risiko und schütze
die Zugangseinstellungen mindestens so, dass sie nur aus dem lokalen Netz oder
über eine separate Admin-Anmeldung geändert werden können. Eine frei aus dem
Internet erreichbare Passwortänderung ist nicht akzeptabel.

## Zu schützende Bereiche

Mindestens folgende Ressourcen des Web-Players müssen eine gültige Sitzung
verlangen:

- `GET /`
- `GET /api/stations`
- `GET /logos/*`
- alle Endpunkte unter `GET /api/slideshow/*`
- `POST /api/shutdown`

Öffentlich bleiben dürfen nur die für Anmeldung und statische Darstellung der
Loginseite zwingend erforderlichen Endpunkte.

Die Anmeldung schützt den Web-Player. Sie darf den lokalen PyQt5-Betrieb und
dessen Immich-Diashow nicht beeinträchtigen.

## Sicherheitsanforderungen

1. Passwörter niemals im Klartext speichern oder protokollieren.
2. Verwende einen Passwort-Hash mit Salt, vorzugsweise `scrypt`, `bcrypt`,
   `Argon2` oder `PBKDF2-HMAC`. Bevorzuge vorhandene Python-Standardbibliothek
   oder bereits installierte Abhängigkeiten.
3. Verwende zufällige, kryptografisch sichere Session-IDs.
4. Session-Cookies müssen mindestens `HttpOnly` und `SameSite=Lax` verwenden.
5. Setze `Secure`, wenn der öffentliche Zugriff über HTTPS erfolgt. Beachte,
   dass der interne Reverse-Proxy den Raspberry Pi per HTTP ansprechen kann.
6. Erneuere die Session-ID nach erfolgreicher Anmeldung.
7. Zustandsändernde Requests benötigen CSRF-Schutz oder eine gleichwertige
   Absicherung.
8. Begrenze wiederholte fehlgeschlagene Loginversuche pro Client, ohne den
   Server dauerhaft sperrbar zu machen.
9. Schreibe weder Passwörter noch Session-IDs oder API-Schlüssel in Logs.
10. Verwende konstante Passwortvergleiche, soweit die gewählte Hashfunktion
    dies nicht bereits sicher übernimmt.

## Speicherung

Lege die Authentifizierungsdaten in einer eigenen, nicht öffentlich
bereitgestellten Datei im Projektverzeichnis ab, zum Beispiel
`web_auth_config.json`.

Die Datei soll mindestens enthalten:

- Schema-/Formatversion
- Aktivierungsstatus des Gastzugangs
- Passwort-Hash mit allen für die Prüfung benötigten Parametern
- Zeitpunkt der letzten Passwortänderung, sofern sinnvoll

Anforderungen an die Speicherung:

- atomisches Schreiben über temporäre Datei und anschließendes Ersetzen
- restriktive Dateirechte auf Linux, soweit möglich
- Aufnahme der echten Konfigurationsdatei in `.gitignore`
- versionierbare Beispieldatei nur ohne echten Passwort-Hash oder Geheimnisse
- robuste Behandlung einer fehlenden oder beschädigten Datei

## Architekturhinweise

- Halte Authentifizierungslogik, Passwortspeicherung und Sessionverwaltung in
  einem eigenen Modul. Vermeide umfangreiche Authentifizierungslogik direkt im
  HTTP-Handler.
- Verwende dieselbe Auth-Konfiguration im kombinierten Start über
  `webradio.py` und im Standalone-Start über `web_player.py`.
- Der bestehende Webserver basiert auf `BaseHTTPRequestHandler` und
  `ThreadingHTTPServer`. Gemeinsame veränderliche Zustände müssen thread-safe
  sein.
- Der Web-Player kann hinter einem Reverse-Proxy unter
  `radio-cnnie.duckdns.org` laufen. Vertraue Proxy-Headern wie
  `X-Forwarded-For` oder `X-Forwarded-Proto` nicht ungeprüft.
- Füge möglichst keine neue externe Abhängigkeit hinzu. Falls eine Abhängigkeit
  erforderlich ist, begründe und dokumentiere sie.

## Erwartete Tests

Implementiere automatisierte Tests mindestens für:

1. erfolgreiche Anmeldung mit `GAST`/`gast`
2. Ablehnung eines falschen Passworts
3. Zugriff auf geschützte Endpunkte ohne Sitzung
4. Zugriff mit gültiger Sitzung
5. Abmeldung und anschließende Ablehnung der alten Sitzung
6. Änderung des Gastpassworts
7. Ablehnung des alten und Annahme des neuen Passworts
8. Deaktivierung des Gastzugangs einschließlich bestehender Sitzung
9. erneute Aktivierung des Gastzugangs
10. Ablauf einer Sitzung
11. parallele Requests ohne beschädigten Sessionzustand
12. Speicherung ohne Klartextpasswort

## Dokumentation

Aktualisiere nach der Implementierung:

- `README.md` mit Einrichtung und Bedienung
- `WEB_PLAYER.md` mit Login, Logout und geschützten API-Endpunkten
- `STATUS.md` mit dem neuen Funktionsstand
- `.gitignore` für die lokale Auth-Konfiguration

Beschreibe außerdem, wie das initiale Gastpasswort geändert und der Gastzugang
im Notfall wiederhergestellt werden kann.

## Abnahmekriterien

Die Aufgabe ist abgeschlossen, wenn:

- ein anonymer Internetnutzer den Player und seine APIs nicht verwenden kann,
- `GAST` sich initial mit `gast` anmelden kann,
- Gastzugang und Passwort über die abgesicherte Admin-Oberfläche verwaltet
  werden können,
- Passwörter ausschließlich sicher gehasht gespeichert werden,
- bestehende lokale Radio- und Immich-Funktionen unverändert weiterlaufen,
- die beschriebenen Tests erfolgreich ausgeführt wurden und
- die Dokumentation den tatsächlichen Stand wiedergibt.
