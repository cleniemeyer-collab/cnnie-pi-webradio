#!/usr/bin/python3
"""Authentifizierung für den Web-Player mit lokalen Benutzerkonten."""

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

DEFAULT_GUEST_USERNAME = "GAST"
DEFAULT_GUEST_PASSWORD = "gast"
SCHEMA_VERSION = 1
SESSION_TTL_SECONDS = 8 * 3600
MAX_LOGIN_ATTEMPTS = 10
RATE_LIMIT_WINDOW = 300
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LEN = 64
SALT_LEN = 16
SESSION_ID_LEN = 32
CSRF_TOKEN_LEN = 32


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(SALT_LEN)
    derived_key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
        p=SCRYPT_P, dklen=SCRYPT_KEY_LEN,
    )
    return {"salt": salt.hex(), "hash": derived_key.hex()}


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        derived_key = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
            p=SCRYPT_P, dklen=SCRYPT_KEY_LEN,
        )
    except (TypeError, ValueError):
        return False
    return secrets.compare_digest(derived_key, expected)


def create_user_entry(password: str) -> dict:
    password_hash = hash_password(password)
    return {
        "password_salt": password_hash["salt"],
        "password_hash": password_hash["hash"],
        "enabled": True,
    }


class AuthConfig:
    """Liest und schreibt die lokale Auth-Konfiguration atomar."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()

    def _default_config(self) -> dict:
        password_hash = hash_password(DEFAULT_GUEST_PASSWORD)
        return {
            "schema_version": SCHEMA_VERSION,
            "guest_enabled": True,
            "guest_username": DEFAULT_GUEST_USERNAME,
            "password_salt": password_hash["salt"],
            "password_hash": password_hash["hash"],
            "password_changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "users": {},
        }

    def load(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            required = (
                "schema_version", "guest_enabled", "guest_username",
                "password_salt", "password_hash",
            )
            if not isinstance(data, dict) or not all(key in data for key in required):
                raise ValueError
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            if not isinstance(data.get("users", {}), dict):
                raise ValueError
            data.setdefault("users", {})
            return data
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
            LOGGER.warning(
                "Auth-Konfiguration fehlt oder ist defekt (%s) – Standard wird verwendet",
                error,
            )
            config = self._default_config()
            self._save_locked(config)
            return config

    def save(self, config: dict):
        with self._lock:
            self._save_locked(config)

    def _save_locked(self, config: dict):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".web_auth_", suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(config, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, str(self._path))
        except BaseException:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    def authenticate(self, username: str, password: str) -> Optional[str]:
        if not username or not password:
            return None
        config = self.load()
        normalized = username.casefold()
        for canonical_name, entry in config.get("users", {}).items():
            if canonical_name.casefold() != normalized:
                continue
            if not entry.get("enabled", True):
                return None
            if verify_password(
                password, entry.get("password_salt", ""), entry.get("password_hash", "")
            ):
                return canonical_name
            return None
        guest = str(config.get("guest_username", DEFAULT_GUEST_USERNAME))
        if guest.casefold() != normalized or not config.get("guest_enabled", True):
            return None
        if verify_password(password, config.get("password_salt", ""), config.get("password_hash", "")):
            return guest
        return None

    def is_user_enabled(self, username: str) -> bool:
        if not username:
            return False
        normalized = username.casefold()
        return any(
            canonical_name.casefold() == normalized and entry.get("enabled", True)
            for canonical_name, entry in self.load().get("users", {}).items()
        )

    def change_password(self, new_password: str) -> dict:
        with self._lock:
            config = self._load_unlocked()
            password_hash = hash_password(new_password)
            config["password_salt"] = password_hash["salt"]
            config["password_hash"] = password_hash["hash"]
            config["password_changed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save_locked(config)
            return config

    def set_password(self, password: str):
        return self.change_password(password)

    def toggle_guest(self, enabled: bool) -> dict:
        with self._lock:
            config = self._load_unlocked()
            config["guest_enabled"] = bool(enabled)
            self._save_locked(config)
            return config

    def add_user(self, username: str, password: str):
        username = str(username).strip()
        if not username:
            raise ValueError("Der Benutzername darf nicht leer sein.")
        with self._lock:
            config = self._load_unlocked()
            users = config.setdefault("users", {})
            existing = next((key for key in users if key.casefold() == username.casefold()), username)
            users[existing] = create_user_entry(password)
            self._save_locked(config)

    def remove_user(self, username: str) -> bool:
        with self._lock:
            config = self._load_unlocked()
            users = config.setdefault("users", {})
            key = next((key for key in users if key.casefold() == username.casefold()), None)
            if key is None:
                return False
            del users[key]
            self._save_locked(config)
            return True

    def toggle_user(self, username: str, enabled: bool) -> bool:
        with self._lock:
            config = self._load_unlocked()
            users = config.setdefault("users", {})
            key = next((key for key in users if key.casefold() == username.casefold()), None)
            if key is None:
                return False
            users[key]["enabled"] = bool(enabled)
            self._save_locked(config)
            return True


class SessionStore:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._sessions = {}
        self._lock = threading.RLock()

    def create(self, username: str) -> str:
        session_id = secrets.token_urlsafe(SESSION_ID_LEN)
        with self._lock:
            self._sessions[session_id] = {"username": username, "created": time.monotonic()}
        return session_id

    def validate(self, session_id: str, guest_enabled: bool) -> Optional[str]:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if time.monotonic() - session["created"] > self._ttl:
                del self._sessions[session_id]
                return None
            username = session["username"]
            if not guest_enabled and username.casefold() == DEFAULT_GUEST_USERNAME.casefold():
                del self._sessions[session_id]
                return None
            return username

    def invalidate(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)

    def invalidate_guest_sessions(self):
        with self._lock:
            for session_id in [
                key for key, value in self._sessions.items()
                if value["username"].casefold() == DEFAULT_GUEST_USERNAME.casefold()
            ]:
                del self._sessions[session_id]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


class RateLimiter:
    def __init__(self, max_attempts: int = MAX_LOGIN_ATTEMPTS,
                 window_seconds: int = RATE_LIMIT_WINDOW):
        self._max = max_attempts
        self._window = window_seconds
        self._failures = {}
        self._lock = threading.Lock()

    def record_failure(self, client_ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            timestamps = self._failures.setdefault(client_ip, [])
            timestamps[:] = [timestamp for timestamp in timestamps if timestamp > now - self._window]
            timestamps.append(now)
            return len(timestamps) > self._max

    def is_blocked(self, client_ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            timestamps = self._failures.get(client_ip, [])
            timestamps[:] = [timestamp for timestamp in timestamps if timestamp > now - self._window]
            return len(timestamps) > self._max

    def record_success(self, client_ip: str):
        with self._lock:
            self._failures.pop(client_ip, None)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LEN)


def verify_csrf_token(token: str, expected: str) -> bool:
    return bool(token and expected and secrets.compare_digest(token, expected))


def set_session_cookie(handler, session_id: str, secure: bool = False):
    parts = [
        f"SID={session_id}", "Path=/", "HttpOnly", "SameSite=Lax",
        f"Max-Age={SESSION_TTL_SECONDS}",
    ]
    if secure:
        parts.append("Secure")
    handler.send_header("Set-Cookie", "; ".join(parts))


def clear_session_cookie(handler):
    handler.send_header("Set-Cookie", "SID=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")


def get_session_id_from_cookie(handler) -> str:
    return _cookie_value(handler, "SID")


def get_csrf_token_from_cookie(handler) -> str:
    return _cookie_value(handler, "CSRF")


def _cookie_value(handler, name: str) -> str:
    for part in handler.headers.get("Cookie", "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip()
    return ""


def set_csrf_cookie(handler, token: str, secure: bool = False):
    parts = [f"CSRF={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    handler.send_header("Set-Cookie", "; ".join(parts))


def get_client_ip(handler) -> str:
    return handler.client_address[0]
