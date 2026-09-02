#!/usr/bin/python3
"""Web-Player-Server: dient die Radio-GUI via HTTP aus, proxied die Diashow-Bilder von Immich."""

import base64
import json
import logging
import mimetypes
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import web_auth

LOGGER = logging.getLogger(__name__)

# ── Immich-API-Helfer ───────────────────────────────────────────────────────


class ImmichServerError(Exception):
    def __init__(self, status_code, endpoint, response_text):
        super().__init__(response_text)
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_text = response_text

    def __str__(self):
        return f"HTTP {self.status_code} bei {self.endpoint}: {self.response_text}"


def _immich_req(base_url, api_key, path, data=None):
    url = base_url + path
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, data=body)
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", errors="replace")
        raise ImmichServerError(err.code, url, text) from err
    except urllib.error.URLError as err:
        raise ImmichServerError(0, url, f"Netzwerkfehler: {err.reason}") from err


def _immich_load_assets(config_path):
    """Bilder aus Immich-Album 'WEB Radio' laden und mischen."""
    LOGGER.info("[Immich] Lade Assets...")
    config_path = Path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Immich-Konfigurationsdatei nicht gefunden: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = config.get("immich_url", "").strip().rstrip("/")
    api_key = config.get("api_key", "").strip()
    if not base_url or not api_key:
        raise ValueError("Immich-Konfiguration fehlt (URL oder API-Key leer)")

    LOGGER.info("[Immich] Suche Album 'WEB Radio'...")
    albums = json.loads(_immich_req(base_url, api_key, "/api/albums").read())
    album = next((a for a in albums if a.get("albumName") == "WEB Radio"), None)
    if album is None:
        raise ValueError("Immich-Album 'WEB Radio' nicht gefunden")
    album_id = album["id"]
    LOGGER.info("[Immich] Album gefunden: %s (%d Bilder)", album.get("albumName"), album.get("assetCount", 0))

    assets = []
    page = 1
    while True:
        LOGGER.info("[Immich] Lade Seite %d...", page)
        result = json.loads(
            _immich_req(base_url, api_key, "/api/search/metadata",
                         {"albumIds": [album_id], "page": page, "size": 1000, "type": "IMAGE"})
            .read()
        )
        items = result.get("assets", {}).get("items", [])
        for item in items:
            aid = item.get("id")
            if not aid:
                continue
            exif = item.get("exifInfo") or {}
            assets.append({
                "id": aid,
                "localDateTime": item.get("localDateTime"),
                "fileCreatedAt": item.get("fileCreatedAt"),
                "exifInfo": {k: exif.get(k) for k in (
                    "dateTimeOriginal", "city", "state", "country",
                    "latitude", "longitude")},
            })
        LOGGER.info("[Immich] Seite %d: %d Bilder (gesamt: %d)", page, len(items), len(assets))
        if not result.get("assets", {}).get("nextPage") or not items:
            break
        page = int(result.get("assets", {}).get("nextPage", page))
    if not assets:
        raise ValueError("Album enthält keine Bilder")
    random.shuffle(assets)
    LOGGER.info("[Immich] %d Bilder geladen und gemischt", len(assets))
    return base_url, api_key, assets


def _format_date(value):
    import re
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not m:
        ym = re.match(r"^(\d{4})", text)
        return ym.group(1) if ym else ""
    month, day = int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(1)
    return f"{day:02d}.{month:02d}.{int(m.group(1))}"


def _format_location(exif):
    parts, seen = [], set()
    for key in ("city", "state", "country"):
        for part in str(exif.get(key) or "").split(","):
            p = part.strip()
            n = p.casefold()
            if p and n not in seen:
                parts.append(p)
                seen.add(n)
    if parts:
        return ", ".join(parts)
    try:
        lat, lon = float(exif.get("latitude", 0)), float(exif.get("longitude", 0))
    except (TypeError, ValueError):
        return ""
    if lat == 0 and lon == 0:
        return ""
    return (f"{abs(lat):.5f}° {'N' if lat >= 0 else 'S'}, "
            f"{abs(lon):.5f}° {'E' if lon >= 0 else 'W'}")


def _display_metadata(asset):
    exif = asset.get("exifInfo") or {}
    dv = exif.get("dateTimeOriginal") or asset.get("localDateTime") or asset.get("fileCreatedAt")
    return {"date": _format_date(dv), "location": _format_location(exif)}


# ── HTML-Vorlagen ───────────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATE_FILE = _BASE_DIR / "templates" / "web_player.html"
_LOGIN_TEMPLATE_FILE = _BASE_DIR / "templates" / "login.html"

try:
    _PLAYER_HTML = _TEMPLATE_FILE.read_bytes()
except OSError:
    _PLAYER_HTML = b"<h1>Web-Player-Vorlage nicht gefunden</h1>"

try:
    _LOGIN_HTML = _LOGIN_TEMPLATE_FILE.read_bytes().decode("utf-8")
except OSError:
    _LOGIN_HTML = "<h1>Login-Vorlage nicht gefunden</h1>"


# ── Server ──────────────────────────────────────────────────────────────────

class WebPlayerServer:
    """HTTP-Server für den Web-Radio-Player mit Diashow-Proxy."""

    def __init__(self, store, logo_dir, base_dir, immich_config,
                 on_change=None, host="0.0.0.0", port=8089,
                 auth_config_path=None):
        self.store = store
        self.logo_dir = Path(logo_dir)
        self.base_dir = Path(base_dir)
        self.immich_config = Path(immich_config)
        self.on_change = on_change or (lambda: None)
        self.host, self.port = host, port

        self._slide_lock = threading.RLock()
        self._slide_base_url = None
        self._slide_api_key = None
        self._slide_assets = []
        self._slide_index = 0
        self._slide_paused = False
        self._slide_status_state = "idle"  # idle | loading | ready | error
        self._slide_status_msg = ""
        self._slide_loading_thread = None

        # ── Authentifizierung ─────────────────────────────────────
        self._auth_enabled = web_auth is not None
        if self._auth_enabled and auth_config_path is None:
            auth_config_path = self.base_dir / "web_auth_config.json"
        self._auth_config_path = Path(auth_config_path) if auth_config_path else None
        self._auth_config = None
        self._session_store = None
        self._rate_limiter = None
        if self._auth_enabled and web_auth:
            self._auth_config = web_auth.AuthConfig(self._auth_config_path)
            self._session_store = web_auth.SessionStore()
            self._rate_limiter = web_auth.RateLimiter()
            # Konfiguration initial laden (erzeugt Standard falls nötig)
            self._auth_config.load()
        # ── Ende Authentifizierung ────────────────────────────────

        self.httpd = None
        self.thread = None

    def start(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler_class())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="web-player-server", daemon=True)
        self.thread.start()
        LOGGER.info("Web-Player-Server auf Port %d gestartet", self.port)

    def stop(self):
        srv, self.httpd = self.httpd, None
        if srv:
            srv.shutdown()
            srv.server_close()
        t, self.thread = self.thread, None
        if t and t.is_alive():
            t.join(timeout=2)

    # ── Diashow-Status ─────────────────────────────────────────────────

    def _slide_init(self):
        """Startet das asynchrone Laden der Diashow-Assets."""
        with self._slide_lock:
            if self._slide_status_state == "loading":
                return  # Bereits im Gange
            self._slide_status_state = "loading"
            self._slide_status_msg = ""
            self._slide_assets = []
            self._slide_index = 0
            self._slide_paused = False
            # Im Hintergrund-Thread laden
            self._slide_loading_thread = threading.Thread(
                target=self._slide_load_background,
                name="slide-loader",
                daemon=True
            )
            self._slide_loading_thread.start()

    def _slide_load_background(self):
        """Lädt die Immich-Assets im Hintergrund."""
        try:
            base_url, api_key, assets = _immich_load_assets(self.immich_config)
            with self._slide_lock:
                self._slide_base_url = base_url
                self._slide_api_key = api_key
                self._slide_assets = assets
                self._slide_status_state = "ready"
                self._slide_status_msg = f"{len(assets)} Bilder geladen"
            LOGGER.info("[Slideshow] Hintergrund-Laden erfolgreich: %d Bilder", len(assets))
        except Exception as exc:
            with self._slide_lock:
                self._slide_status_state = "error"
                self._slide_status_msg = str(exc)
            LOGGER.exception("[Slideshow] Hintergrund-Laden fehlgeschlagen: %s", exc)

    def _slide_next(self):
        with self._slide_lock:
            if not self._slide_assets:
                return None
            asset = self._slide_assets[self._slide_index]
            self._slide_index = (self._slide_index + 1) % len(self._slide_assets)
            return asset

    def _slide_prev(self):
        with self._slide_lock:
            if not self._slide_assets:
                return None
            self._slide_index = (self._slide_index - 1) % len(self._slide_assets)
            return self._slide_assets[self._slide_index]

    def _slide_toggle_pause(self):
        with self._slide_lock:
            self._slide_paused = not self._slide_paused
            return self._slide_paused

    def _slide_status(self):
        with self._slide_lock:
            return {
                "state": self._slide_status_state,
                "message": self._slide_status_msg,
                "total": len(self._slide_assets),
                "index": self._slide_index,
                "paused": self._slide_paused,
            }

    def _fetch_thumbnail(self, asset_id):
        try:
            with _immich_req(self._slide_base_url, self._slide_api_key,
                             "/api/assets/" + urllib.parse.quote(asset_id) +
                             "/thumbnail?size=preview") as resp:
                return resp.read()
        except Exception as exc:
            LOGGER.warning("Thumbnail-Fehler f%C3%BCr %s: %s", asset_id, exc)
            return None

    # ── Handler ────────────────────────────────────────────────────────

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            log_message = lambda s, f, *a: LOGGER.debug(f, *a)

            # ── Auth-Helfer ────────────────────────────────────────

            def _get_current_user(self):
                """Gibt den Benutzernamen zurück oder None."""
                if not owner._auth_enabled or not owner._session_store:
                    return "anonymous"
                config = owner._auth_config.load()
                guest_enabled = config.get("guest_enabled", True)
                session_id = web_auth.get_session_id_from_cookie(self)
                return owner._session_store.validate(session_id, guest_enabled)

            def _require_auth(self):
                """Prüft die Authentifizierung. Gibt False zurück, wenn abgewiesen."""
                if not owner._auth_enabled:
                    return True
                user = self._get_current_user()
                if user is None:
                    self._redirect_to_login()
                    return False
                return True

            def _require_csrf(self):
                token = self.headers.get("X-CSRF-Token", "")
                expected = web_auth.get_csrf_token_from_cookie(self)
                if not web_auth.verify_csrf_token(token, expected):
                    self._json_error(403, "Ungültiges Anfrage-Token.")
                    return False
                return True

            def _is_https(self):
                return self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"

            def _redirect_to_login(self):
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _read_form(self):
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 16384)
                except ValueError:
                    length = 0
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                return urllib.parse.parse_qs(body)

            # ── GET ────────────────────────────────────────────────

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                p = parsed.path

                # Öffentliche Endpunkte (keine Auth)
                if p == "/login":
                    self._serve_login()
                    return
                if p == "/api/login-status":
                    self._login_status()
                    return

                # Geschützte Endpunkte
                if not self._require_auth():
                    return

                if p == "/":
                    self._html(_PLAYER_HTML)
                elif p == "/api/stations":
                    self._json({"stations": owner.store.list_stations()})
                elif p.startswith("/logos/"):
                    self._serve_logo(urllib.parse.unquote(p[7:]))
                elif p == "/api/slideshow/init":
                    self._sl_init()
                elif p == "/api/slideshow/next":
                    self._sl_next()
                elif p == "/api/slideshow/prev":
                    self._sl_prev()
                elif p == "/api/slideshow/pause":
                    self._sl_pause()
                elif p == "/api/slideshow/status":
                    self._sl_status()
                else:
                    self.send_error(404)

            # ── POST ───────────────────────────────────────────────

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)

                # Login-Endpunkt (öffentlich)
                if parsed.path == "/api/login":
                    self._handle_login()
                    return

                # Geschützte POST-Endpunkte
                if not self._require_auth():
                    return

                if parsed.path in ("/api/logout", "/api/shutdown") and not self._require_csrf():
                    return

                if parsed.path == "/api/logout":
                    self._handle_logout()
                elif parsed.path == "/api/shutdown":
                    self._json({"ok": True, "message": "Shutdown angefordert."})
                else:
                    self.send_error(404)

            # ── Login-Endpunkte ────────────────────────────────────

            def _serve_login(self):
                self._html(_LOGIN_HTML.encode("utf-8"))

            def _login_status(self):
                user = self._get_current_user()
                csrf_token = web_auth.get_csrf_token_from_cookie(self) if user else ""
                self._json({
                    "authenticated": user is not None,
                    "username": user or "",
                    "csrf_token": csrf_token,
                })

            def _handle_login(self):
                if not owner._auth_enabled or not web_auth:
                    self.send_error(501)
                    return

                form = self._read_form()
                username = form.get("username", [""])[0].strip()
                password = form.get("password", [""])[0]
                client_ip = web_auth.get_client_ip(self)

                # Rate-Limiting
                if owner._rate_limiter.is_blocked(client_ip):
                    self._json_error(429, "Zu viele fehlgeschlagene Versuche. Bitte warten Sie.")
                    return

                # Der Login ist öffentlich und verändert keine bestehende Sitzung.
                # Ein frischer CSRF-Token wird erst nach erfolgreicher Anmeldung
                # für geschützte Aktionen ausgegeben.

                # Konfiguration laden
                config = owner._auth_config.load()
                guest_enabled = config.get("guest_enabled", True)
                guest_username = config.get("guest_username", "GAST")
                salt = config.get("password_salt", "")
                pw_hash = config.get("password_hash", "")

                # Gastzugang muss aktiviert sein
                if not guest_enabled:
                    owner._rate_limiter.record_failure(client_ip)
                    self._json_error(403, "Anmeldung nicht möglich.")
                    return

                # Benutzername case-insensitive
                if username.upper() != guest_username.upper():
                    owner._rate_limiter.record_failure(client_ip)
                    self._json_error(401, "Benutzername oder Passwort ist falsch.")
                    return

                # Passwortprüfung
                if not web_auth.verify_password(password, salt, pw_hash):
                    blocked = owner._rate_limiter.record_failure(client_ip)
                    if blocked:
                        self._json_error(429, "Zu viele fehlgeschlagene Versuche. Bitte warten Sie.")
                    else:
                        self._json_error(401, "Benutzername oder Passwort ist falsch.")
                    return

                # Erfolgreich – Rate-Limit zurücksetzen
                owner._rate_limiter.record_success(client_ip)

                # Neue Session erstellen (Session-Fixation-Verhinderung)
                session_id = owner._session_store.create(username)

                csrf_token = web_auth.generate_csrf_token()
                secure_cookie = self._is_https()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                web_auth.set_session_cookie(self, session_id, secure=secure_cookie)
                web_auth.set_csrf_cookie(self, csrf_token, secure=secure_cookie)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                LOGGER.info("Anmeldung erfolgreich: %s von %s", username, client_ip)

            def _handle_logout(self):
                if not owner._auth_enabled or not web_auth:
                    self.send_error(501)
                    return

                session_id = web_auth.get_session_id_from_cookie(self)
                if session_id:
                    owner._session_store.invalidate(session_id)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                web_auth.clear_session_cookie(self)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

            def _json_error(self, status, message):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "message": message}).encode("utf-8"))

            # ── Helfer ───────────────────────────────────────────────

            def _html(self, data):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj, status=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def _img(self, data, ct="image/jpeg"):
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)

            def _serve_logo(self, name):
                c = owner.logo_dir / name
                try:
                    c.relative_to(owner.logo_dir)
                except ValueError:
                    self.send_error(403); return
                if not c.is_file():
                    self.send_error(404); return
                ct, _ = mimetypes.guess_type(str(c)) or ("image/png", None)
                try:
                    self._img(c.read_bytes(), ct)
                except OSError:
                    self.send_error(500)

            # ── Diashow-Endpoints ────────────────────────────────────

            def _sl_init(self):
                try:
                    LOGGER.info("[Slideshow] Init gestartet")
                    owner._slide_init()  # Startet Hintergrund-Thread
                    self._json({"ok": True, "state": "loading", "message": "Laden gestartet"})
                except Exception as exc:
                    LOGGER.exception("[Slideshow] Init fehlgeschlagen: %s", exc)
                    self._json({"ok": False, "error": str(exc)}, status=500)

            def _sl_next(self):
                with owner._slide_lock:
                    if owner._slide_status_state == "loading":
                        self._json({"ok": False, "error": "Noch wird geladen ..."}, 503)
                        return
                    if owner._slide_status_state == "error":
                        self._json({"ok": False, "error": owner._slide_status_msg}, 500)
                        return
                    asset = owner._slide_next()
                if asset is None:
                    self._json({"ok": False, "error": "Slideshow nicht initialisiert"}, 500)
                    return
                thumb = owner._fetch_thumbnail(asset["id"])
                if thumb is None:
                    self._json({"ok": False, "error": "Thumbnail nicht verfügbar"}, 502)
                    return
                self._json({
                    "ok": True,
                    "image": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
                    "meta": _display_metadata(asset),
                })

            def _sl_prev(self):
                with owner._slide_lock:
                    if owner._slide_status_state == "loading":
                        self._json({"ok": False, "error": "Noch wird geladen ..."}, 503)
                        return
                    if owner._slide_status_state == "error":
                        self._json({"ok": False, "error": owner._slide_status_msg}, 500)
                        return
                    asset = owner._slide_prev()
                if asset is None:
                    self._json({"ok": False, "error": "Slideshow nicht initialisiert"}, 500)
                    return
                thumb = owner._fetch_thumbnail(asset["id"])
                if thumb is None:
                    self._json({"ok": False, "error": "Thumbnail nicht verfügbar"}, 502)
                    return
                self._json({
                    "ok": True,
                    "image": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
                    "meta": _display_metadata(asset),
                })

            def _sl_pause(self):
                paused = owner._slide_toggle_pause()
                self._json({"ok": True, "paused": paused})

            def _sl_status(self):
                self._json({"ok": True, **owner._slide_status()})

        return Handler
