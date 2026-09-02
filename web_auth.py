#!/usr/bin/python3
"""
web_auth – Authentifizierung für den Web-Player.

Verantwortlichkeiten:
  • Passwort-Hash (scrypt, Python-Standardbibliothek)
  • Session-Verwaltung (thread-safe, konfigurierbare TTL)
  • Konfigurationsdatei (atomares Schreiben, Gastzugang-Status)
  • Rate-Limiting fehlgeschlagener Loginversuche pro Client-IP
  • CSRF-Token-Generierung und -Prüfung

Keine externen Abhängigkeiten.
"""

import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

# ── Standardkonstanten ──────────────────────────────────────────────────────

DEFAULT_GUEST_USERNAME = "GAST"
DEFAULT_GUEST_PASSWORD = "gast"
SCHEMA_VERSION = 1
SESSION_TTL_SECONDS = 8 * 3600  # 8 Stunden – konfigurierbar
MAX_LOGIN_ATTEMPTS = 10          # pro IP innerhalb der Rate-Limit-Fenster
RATE_LIMIT_WINDOW = 300          # 5 Minuten

# scrypt-Parameter (ausreichend für embedded, keine bcrypt-Abhängigkeit)
SCRYPT_N = 2 ** 14               # CPU/Memory-Kosten
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 1 << 27          # 128 MiB
SCRYPT_KEY_LEN = 64
SALT_LEN = 16
SESSION_ID_LEN = 32              # 32 Bytes → 43 chars URL-safe
CSRF_TOKEN_LEN = 32


# ── Passwort-Hash ───────────────────────────────────────────────────────────

def hash_password(password: str) -> dict:
    """Erzeugt einen scrypt-Hash mit zufälligem Salt.

    Rückgabe: dict mit keys 'salt', 'hash' (beide hex-encoded).
    """
    salt = secrets.token_bytes(SALT_LEN)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LEN,
    )
    return {"salt": salt.hex(), "hash": dk.hex()}


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Prüft ein Passwort gegen einen gespeicherten scrypt-Hash.

    Verwendet den gleichen Algorithmus; Timing-Angriffe werden durch
    scrypt selbst erschwert.
    """
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LEN,
    )
    # Konstanter Zeitvergleich
    return secrets.compare_digest(dk.hex(), expected.hex())


# ── Konfigurationsdatei ─────────────────────────────────────────────────────

class AuthConfig:
    """Liest/schreibt web_auth_config.json atomar."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()

    def _default_config(self) -> dict:
        pw_hash = hash_password(DEFAULT_GUEST_PASSWORD)
        return {
            "schema_version": SCHEMA_VERSION,
            "guest_enabled": True,
            "guest_username": DEFAULT_GUEST_USERNAME,
            "password_salt": pw_hash["salt"],
            "password_hash": pw_hash["hash"],
            "password_changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def load(self) -> dict:
        """Lädt die Konfiguration; erzeugt Standard bei fehlender/defekter Datei."""
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict:
        """Liest die Konfiguration ohne Lock (muss innerhalb von _lock aufgerufen werden)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            required = ("schema_version", "guest_enabled", "guest_username",
                        "password_salt", "password_hash")
            if not all(k in data for k in required):
                raise ValueError
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            return data
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            LOGGER.warning("Auth-Konfiguration fehlt oder ist defekt (%s) – Standard wird verwendet", exc)
            config = self._default_config()
            self._save_locked(config)
            return config

    def save(self, config: dict):
        """Speichert die Konfiguration atomar."""
        with self._lock:
            self._save_locked(config)

    def _save_locked(self, config: dict):
        """Atomares Schreiben: temp-Datei → rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".web_auth_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            # Restriktive Rechte (nur Owner) – nur auf Unix
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, str(self._path))
            LOGGER.info("Auth-Konfiguration gespeichert")
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def change_password(self, new_password: str) -> dict:
        """Ändert das Gastpasswort und gibt die aktualisierte Konfiguration zurück."""
        with self._lock:
            config = self._load_unlocked()
            pw_hash = hash_password(new_password)
            config["password_salt"] = pw_hash["salt"]
            config["password_hash"] = pw_hash["hash"]
            config["password_changed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save_locked(config)
            return config

    def toggle_guest(self, enabled: bool) -> dict:
        """Aktiviert/deaktiviert den Gastzugang."""
        with self._lock:
            config = self._load_unlocked()
            config["guest_enabled"] = enabled
            self._save_locked(config)
            return config


# ── Session-Verwaltung ──────────────────────────────────────────────────────

class SessionStore:
    """Thread-sichere Session-Verwaltung mit TTL."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._sessions: dict[str, dict] = {}  # session_id → {username, created}
        self._lock = threading.RLock()

    def create(self, username: str) -> str:
        """Erzeugt eine neue Session und gibt die Session-ID zurück."""
        session_id = secrets.token_urlsafe(SESSION_ID_LEN)
        with self._lock:
            self._sessions[session_id] = {
                "username": username,
                "created": time.monotonic(),
            }
        return session_id

    def validate(self, session_id: str, guest_enabled: bool) -> Optional[str]:
        """Prüft eine Session. Gibt den Benutzernamen zurück oder None."""
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            # TTL-Prüfung
            if time.monotonic() - session["created"] > self._ttl:
                del self._sessions[session_id]
                return None
            # Wenn Gastzugang deaktiviert wurde und der Session-Benutzer
            # der Gast ist → Session ungültig
            username = session["username"]
            if not guest_enabled and username.upper() == DEFAULT_GUEST_USERNAME.upper():
                del self._sessions[session_id]
                return None
            return username

    def invalidate(self, session_id: str):
        """Löscht eine Session (Logout)."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def invalidate_guest_sessions(self):
        """Löscht alle Gast-Sessions (wenn Gastzugang deaktiviert wird)."""
        with self._lock:
            to_remove = [
                sid for sid, s in self._sessions.items()
                if s["username"].upper() == DEFAULT_GUEST_USERNAME.upper()
            ]
            for sid in to_remove:
                del self._sessions[sid]
            if to_remove:
                LOGGER.info("%d Gast-Session(s) invalidiert", len(to_remove))

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


# ── Rate-Limiting ───────────────────────────────────────────────────────────

class RateLimiter:
    """Begrenzt fehlgeschlagene Loginversuche pro Client-IP."""

    def __init__(self, max_attempts: int = MAX_LOGIN_ATTEMPTS,
                 window_seconds: int = RATE_LIMIT_WINDOW):
        self._max = max_attempts
        self._window = window_seconds
        self._failures: dict[str, list] = {}  # ip → [timestamps]
        self._lock = threading.Lock()

    def record_failure(self, client_ip: str) -> bool:
        """Meldest einen fehlgeschlagenen Versuch.

        Gibt True zurück, wenn der Client gesperrt ist.
        """
        now = time.monotonic()
        with self._lock:
            timestamps = self._failures.setdefault(client_ip, [])
            # Alte Einträge entfernen
            cutoff = now - self._window
            timestamps[:] = [t for t in timestamps if t > cutoff]
            timestamps.append(now)
            return len(timestamps) > self._max

    def is_blocked(self, client_ip: str) -> bool:
        with self._lock:
            timestamps = self._failures.get(client_ip, [])
            now = time.monotonic()
            cutoff = now - self._window
            recent = [t for t in timestamps if t > cutoff]
            return len(recent) > self._max

    def record_success(self, client_ip: str):
        """Löscht die Failure-Historie nach erfolgreichem Login."""
        with self._lock:
            self._failures.pop(client_ip, None)


# ── CSRF-Token ──────────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LEN)


def verify_csrf_token(token: str, expected: str) -> bool:
    if not token or not expected:
        return False
    return secrets.compare_digest(token, expected)


# ── Hilfsfunktionen für HTTP-Handler ────────────────────────────────────────

def set_session_cookie(handler, session_id: str, secure: bool = False):
    """Setzt das Session-Cookie mit HttpOnly und SameSite=Lax."""
    parts = [
        f"SID={session_id}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_TTL_SECONDS}",
    ]
    if secure:
        parts.append("Secure")
    handler.send_header("Set-Cookie", "; ".join(parts))


def clear_session_cookie(handler):
    handler.send_header(
        "Set-Cookie",
        "SID=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
    )


def get_session_id_from_cookie(handler) -> str:
    """Extrahiert die SID aus dem Cookie-Header."""
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("SID="):
            return part[4:].strip()
    return ""


def get_csrf_token_from_cookie(handler) -> str:
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("CSRF="):
            return part[5:].strip()
    return ""


def set_csrf_cookie(handler, token: str, secure: bool = False):
    parts = [
        f"CSRF={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    handler.send_header("Set-Cookie", "; ".join(parts))


def get_client_ip(handler) -> str:
    """Bestimmt die Client-IP.

    Vertraut X-Forwarded-For NICHT ungeprüft; verwendet
    handler.client_address[0] als Fallback.
    """
    return handler.client_address[0]
