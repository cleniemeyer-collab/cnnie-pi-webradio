"""Tests für UserStationStores und AuthConfig.authenticate / is_user_enabled."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import station_store
import web_auth


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_guest_store(tmp_dir: Path) -> station_store.StationStore:
    """Erzeugt eine leere Guest-StationStore in einem Temp-Verzeichnis."""
    json_file = tmp_dir / "radio_station_list.json"
    csv_file = tmp_dir / "radio_station_list.csv"
    logo_dir = tmp_dir / "logos"
    logo_dir.mkdir(exist_ok=True)
    # Leere gültige JSON-Datei
    json_file.write_text(
        json.dumps({
            "version": 1,
            "stations": [
                {
                    "id": "local-001",
                    "name": "Testsender Eins",
                    "audio_url": "http://example.com/stream1",
                    "metadata_url": "http://example.com/stream1",
                    "logo_file": "",
                    "source": "local",
                    "startup": True,
                },
                {
                    "id": "local-002",
                    "name": "Testsender Zwei",
                    "audio_url": "http://example.com/stream2",
                    "metadata_url": "http://example.com/stream2",
                    "logo_file": "",
                    "source": "local",
                    "startup": False,
                },
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return station_store.StationStore(json_file, csv_file, logo_dir)


def _write_auth_config(tmp_dir: Path, **overrides) -> web_auth.AuthConfig:
    """Schreibt eine Auth-Konfig und gibt das AuthConfig-Objekt zurück."""
    config_path = Path(tmp_dir) / "web_auth_config.json"
    pw_hash = web_auth.hash_password("gast")
    config = {
        "schema_version": web_auth.SCHEMA_VERSION,
        "guest_enabled": True,
        "guest_username": "GAST",
        "password_salt": pw_hash["salt"],
        "password_hash": pw_hash["hash"],
        "password_changed_at": "2026-01-01T00:00:00Z",
    }
    config.update(overrides)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return web_auth.AuthConfig(config_path)


# ── AuthConfig.authenticate ─────────────────────────────────────────────────

class AuthAuthenticateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_authenticate_guest_with_correct_password(self):
        config = _write_auth_config(self.temp_dir.name)
        result = config.authenticate("gast", "gast")
        self.assertEqual("GAST", result)

    def test_authenticate_guest_case_insensitive(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertEqual("GAST", config.authenticate("Gast", "gast"))
        self.assertEqual("GAST", config.authenticate("GAST", "gast"))
        self.assertEqual("GAST", config.authenticate("gaSt", "gast"))

    def test_authenticate_guest_wrong_password(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertIsNone(config.authenticate("gast", "wrong"))

    def test_authenticate_guest_disabled(self):
        config = _write_auth_config(self.temp_dir.name, guest_enabled=False)
        self.assertIsNone(config.authenticate("gast", "gast"))

    def test_authenticate_empty_inputs(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertIsNone(config.authenticate("", "gast"))
        self.assertIsNone(config.authenticate("gast", ""))
        self.assertIsNone(config.authenticate("", ""))

    def test_authenticate_configured_user_correct_password(self):
        user_entry = web_auth.create_user_entry("secret123")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        result = config.authenticate("alice", "secret123")
        self.assertEqual("alice", result)

    def test_authenticate_configured_user_case_insensitive(self):
        user_entry = web_auth.create_user_entry("secret")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"bob": user_entry},
        )
        self.assertEqual("bob", config.authenticate("Bob", "secret"))
        self.assertEqual("bob", config.authenticate("BOB", "secret"))
        self.assertEqual("bob", config.authenticate("bOb", "secret"))

    def test_authenticate_configured_user_wrong_password(self):
        user_entry = web_auth.create_user_entry("secret")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        self.assertIsNone(config.authenticate("alice", "wrong"))

    def test_authenticate_disabled_user(self):
        user_entry = web_auth.create_user_entry("secret")
        user_entry["enabled"] = False
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        self.assertIsNone(config.authenticate("alice", "secret"))

    def test_authenticate_unknown_user(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertIsNone(config.authenticate("unknown", "anything"))

    def test_authenticate_user_takes_precedence_over_guest_name(self):
        # Ein konfigurierter Benutzer namens "gast" überschreibt Gast-Login
        user_entry = web_auth.create_user_entry("userpass")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"gast": user_entry},
        )
        # Gast-Passwort funktioniert NICHT (konfigurierter User hat Vorrang)
        self.assertIsNone(config.authenticate("gast", "gast"))
        # User-Passwort funktioniert
        self.assertEqual("gast", config.authenticate("gast", "userpass"))

    def test_authenticate_no_plaintext_in_config(self):
        """Kein Klartext-Passwort darf in der Konfigurationsdatei landen."""
        user_entry = web_auth.create_user_entry("supersecret")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        raw = config._path.read_text("utf-8")
        self.assertNotIn("supersecret", raw)


# ── AuthConfig.is_user_enabled ──────────────────────────────────────────────

class AuthIsUserEnabledTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_enabled_user(self):
        user_entry = web_auth.create_user_entry("pass")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        self.assertTrue(config.is_user_enabled("alice"))

    def test_disabled_user(self):
        user_entry = web_auth.create_user_entry("pass")
        user_entry["enabled"] = False
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        self.assertFalse(config.is_user_enabled("alice"))

    def test_unknown_user(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertFalse(config.is_user_enabled("nobody"))

    def test_case_insensitive(self):
        user_entry = web_auth.create_user_entry("pass")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"bob": user_entry},
        )
        self.assertTrue(config.is_user_enabled("Bob"))
        self.assertTrue(config.is_user_enabled("BOB"))

    def test_empty_username(self):
        config = _write_auth_config(self.temp_dir.name)
        self.assertFalse(config.is_user_enabled(""))


# ── create_user_entry helper ─────────────────────────────────────────────────

class CreateUserEntryTests(unittest.TestCase):
    def test_creates_valid_entry(self):
        entry = web_auth.create_user_entry("mypassword")
        self.assertIn("password_salt", entry)
        self.assertIn("password_hash", entry)
        self.assertTrue(entry["enabled"])
        self.assertTrue(
            web_auth.verify_password(
                "mypassword", entry["password_salt"], entry["password_hash"]
            )
        )

    def test_no_plaintext(self):
        entry = web_auth.create_user_entry("secret")
        self.assertNotIn("secret", json.dumps(entry))


# ── _safe_user_dir_name ──────────────────────────────────────────────────────

class SafeUserDirNameTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            station_store._safe_user_dir_name("Alice"),
            station_store._safe_user_dir_name("alice"),
        )

    def test_safe_prefix(self):
        name = station_store._safe_user_dir_name("test")
        self.assertTrue(name.startswith("usr_"))

    def test_no_path_traversal(self):
        name = station_store._safe_user_dir_name("../etc/passwd")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn("..", name)

    def test_case_insensitive_same_hash(self):
        h1 = station_store._safe_user_dir_name("Alice")
        h2 = station_store._safe_user_dir_name("ALICE")
        self.assertEqual(h1, h2)

    def test_different_users_different_hashes(self):
        h1 = station_store._safe_user_dir_name("alice")
        h2 = station_store._safe_user_dir_name("bob")
        self.assertNotEqual(h1, h2)


# ── UserStationStores ────────────────────────────────────────────────────────

class UserStationStoresTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.guest_store = _make_guest_store(self.base_dir)

    def _make_managers(self) -> station_store.UserStationStores:
        return station_store.UserStationStores(
            self.guest_store, self.base_dir, self.base_dir / "logos"
        )

    # ── Guest returns guest store ─────────────────────────────────────

    def test_guest_returns_guest_store(self):
        mgr = self._make_managers()
        self.assertIs(mgr.for_user("GAST"), self.guest_store)
        self.assertIs(mgr.for_user("gast"), self.guest_store)
        self.assertIs(mgr.for_user("Gast"), self.guest_store)

    def test_empty_username_returns_guest_store(self):
        mgr = self._make_managers()
        self.assertIs(mgr.for_user(""), self.guest_store)
        self.assertIs(mgr.for_user(None), self.guest_store)

    # ── Persistence ───────────────────────────────────────────────────

    def test_writable_creates_private_store(self):
        mgr = self._make_managers()
        user_store = mgr.for_user("alice", writable=True)
        self.assertIsNot(user_store, self.guest_store)
        # Enthält Kopie der Gast-Stationen
        stations = user_store.list_stations()
        self.assertEqual(2, len(stations))

    def test_persistent_store_survives_reload(self):
        mgr = self._make_managers()
        user_store = mgr.for_user("alice", writable=True)
        user_store.add_station({
            "id": "local-999",
            "name": "Alice Special",
            "audio_url": "http://alice.example.com",
            "metadata_url": "http://alice.example.com",
            "logo_file": "",
            "source": "local",
        })
        self.assertEqual(3, len(user_store.list_stations()))

        # Neuer Manager-Instanz (simuliert Neuladen)
        mgr2 = self._make_managers()
        loaded = mgr2.for_user("alice")
        self.assertEqual(3, len(loaded.list_stations()))
        names = {s["name"] for s in loaded.list_stations()}
        self.assertIn("Alice Special", names)

    # ── Fallback ──────────────────────────────────────────────────────

    def test_non_writable_nonexistent_falls_back_to_guest(self):
        mgr = self._make_managers()
        store = mgr.for_user("alice", writable=False)
        self.assertIs(store, self.guest_store)

    def test_non_writable_existing_uses_private(self):
        mgr = self._make_managers()
        # Erst erstellen
        mgr.for_user("bob", writable=True)
        # Dann neu abrufen (ohne writable)
        mgr2 = self._make_managers()
        store = mgr2.for_user("bob", writable=False)
        self.assertIsNot(store, self.guest_store)

    # ── Isolation ─────────────────────────────────────────────────────

    def test_user_stations_isolated(self):
        mgr = self._make_managers()
        alice_store = mgr.for_user("alice", writable=True)
        bob_store = mgr.for_user("bob", writable=True)

        alice_store.add_station({
            "id": "local-100",
            "name": "Alice Only",
            "audio_url": "http://alice.example.com",
            "metadata_url": "http://alice.example.com",
            "logo_file": "",
            "source": "local",
        })

        self.assertEqual(3, len(alice_store.list_stations()))
        self.assertEqual(2, len(bob_store.list_stations()))
        self.assertEqual(2, len(self.guest_store.list_stations()))

    def test_user_changes_dont_affect_guest(self):
        mgr = self._make_managers()
        alice_store = mgr.for_user("alice", writable=True)
        alice_store.add_station({
            "id": "local-101",
            "name": "Private Sender",
            "audio_url": "http://private.example.com",
            "metadata_url": "http://private.example.com",
            "logo_file": "",
            "source": "local",
        })
        guest_names = {s["name"] for s in self.guest_store.list_stations()}
        self.assertNotIn("Private Sender", guest_names)

    # ── Case aliases ──────────────────────────────────────────────────

    def test_case_aliases_share_store(self):
        mgr = self._make_managers()
        s1 = mgr.for_user("Alice", writable=True)
        s2 = mgr.for_user("alice")
        s3 = mgr.for_user("ALICE")
        self.assertIs(s1, s2)
        self.assertIs(s2, s3)

    # ── Safe paths ────────────────────────────────────────────────────

    def test_malicious_username_safe_path(self):
        mgr = self._make_managers()
        store = mgr.for_user("../../etc/passwd", writable=True)
        # Der Store existiert und liegt unter user_stations/
        user_dir = self.base_dir / "user_stations"
        self.assertTrue(user_dir.is_dir())
        # Kein Pfad-Ausbruch
        store_json = store.json_file
        self.assertTrue(str(store_json).startswith(str(user_dir)))

    def test_special_chars_username_safe_path(self):
        mgr = self._make_managers()
        store = mgr.for_user("user!@#$%^&*()", writable=True)
        self.assertIsNot(store, self.guest_store)
        self.assertTrue(store.json_file.is_file())

    # ── Empty guest stub compatibility ────────────────────────────────

    def test_empty_guest_store_writable_creates_empty(self):
        """Wenn Gast-Store leer ist, wird ein leerer privater Store erstellt."""
        # Erstelle einen leeren Guest-Store
        empty_json = self.base_dir / "empty_stations.json"
        empty_json.write_text(
            json.dumps({"version": 1, "stations": []}, indent=2),
            encoding="utf-8",
        )
        empty_store = station_store.StationStore(
            empty_json, self.base_dir / "empty.csv", self.base_dir / "logos"
        )
        mgr = station_store.UserStationStores(
            empty_store, self.base_dir, self.base_dir / "logos"
        )
        user_store = mgr.for_user("alice", writable=True)
        self.assertEqual(0, len(user_store.list_stations()))

    # ── Reload ────────────────────────────────────────────────────────

    def test_reload_reloads_from_disk(self):
        mgr = self._make_managers()
        user_store = mgr.for_user("alice", writable=True)
        user_store.add_station({
            "id": "local-200",
            "name": "Reload Test",
            "audio_url": "http://reload.example.com",
            "metadata_url": "http://reload.example.com",
            "logo_file": "",
            "source": "local",
        })

        # Cache leeren und neu laden
        mgr.clear_cache("alice")
        reloaded = mgr.reload("alice")
        self.assertEqual(3, len(reloaded.list_stations()))

    def test_reload_guest_returns_guest_store(self):
        mgr = self._make_managers()
        self.assertIs(mgr.reload("GAST"), self.guest_store)

    # ── Clear cache ───────────────────────────────────────────────────

    def test_clear_cache_all(self):
        mgr = self._make_managers()
        mgr.for_user("alice", writable=True)
        mgr.for_user("bob", writable=True)
        mgr.clear_cache()
        # Persistente Stores werden nach Cache-Clear von der Platte neu geladen.
        self.assertIsNot(mgr.for_user("alice", writable=False), self.guest_store)

    def test_clear_cache_specific(self):
        mgr = self._make_managers()
        mgr.for_user("alice", writable=True)
        mgr.for_user("bob", writable=True)
        mgr.clear_cache("alice")
        # Bob bleibt gecacht
        bob1 = mgr.for_user("bob")
        bob2 = mgr.for_user("bob")
        self.assertIs(bob1, bob2)


# ── Multi-process freshness simulation ───────────────────────────────────────

class MultiProcessFreshnessTests(unittest.TestCase):
    """Simuliert Multi-Process-Szenarien durch direkte Datei-Manipulation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.guest_store = _make_guest_store(self.base_dir)

    def test_external_write_detected_via_reload(self):
        """Simuliert, dass ein anderer Prozess die User-Datei aktualisiert."""
        mgr = self._make_managers()
        user_store = mgr.for_user("alice", writable=True)
        # Cache den Store
        _ = mgr.for_user("alice")

        # Simuliere externen Schreibzugriff
        user_json = user_store.json_file
        stations = user_store.list_stations()
        stations.append({
            "id": "local-300",
            "name": "External Addition",
            "audio_url": "http://external.example.com",
            "metadata_url": "http://external.example.com",
            "logo_file": "",
            "source": "local",
            "startup": False,
        })
        document = {"version": 1, "stations": stations}
        user_json.write_text(json.dumps(document, indent=2), encoding="utf-8")

        # Ohne reload: alter Cache
        cached = mgr.for_user("alice")
        self.assertEqual(2, len(cached.list_stations()))

        # Mit reload: neue Daten
        reloaded = mgr.reload("alice")
        self.assertEqual(3, len(reloaded.list_stations()))

    def test_guest_store_read_freshness(self):
        """Guest-Store wird direkt verwendet – Änderungen sind sofort sichtbar."""
        mgr = self._make_managers()
        self.guest_store.add_station({
            "id": "local-400",
            "name": "Guest Update",
            "audio_url": "http://guest.example.com",
            "metadata_url": "http://guest.example.com",
            "logo_file": "",
            "source": "local",
        })
        guest_view = mgr.for_user("GAST")
        self.assertEqual(3, len(guest_view.list_stations()))

    def _make_managers(self):
        return station_store.UserStationStores(
            self.guest_store, self.base_dir, self.base_dir / "logos"
        )


# ── Thread safety ────────────────────────────────────────────────────────────

class ThreadSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.guest_store = _make_guest_store(self.base_dir)

    def test_concurrent_writable_creations(self):
        """Mehrere Threads erstellen gleichzeitig private Stores."""
        mgr = station_store.UserStationStores(
            self.guest_store, self.base_dir, self.base_dir / "logos"
        )
        errors = []

        def create_user(username):
            try:
                store = mgr.for_user(username, writable=True)
                # Jeder Store sollte isoliert sein
                self.assertEqual(2, len(store.list_stations()))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=create_user, args=(f"user{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors)

    def test_concurrent_authenticate(self):
        """Mehrere Threads authentifizieren gleichzeitig."""
        user_entry = web_auth.create_user_entry("pass")
        config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": user_entry},
        )
        errors = []
        results = []

        def auth_attempt(i):
            try:
                r = config.authenticate("alice", "pass")
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=auth_attempt, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(20, len(results))
        self.assertTrue(all(r == "alice" for r in results))


# ── Backward compatibility ───────────────────────────────────────────────────

class BackwardCompatibilityTests(unittest.TestCase):
    """Stellt sicher, dass bestehendes Verhalten nicht bricht."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_existing_guest_config_still_works(self):
        """Bestehende Konfig ohne 'users'-Feld funktioniert weiterhin."""
        config = _write_auth_config(self.temp_dir.name)
        self.assertEqual("GAST", config.authenticate("gast", "gast"))
        self.assertTrue(config.toggle_guest(False)["guest_enabled"] == False)
        self.assertIsNone(config.authenticate("gast", "gast"))

    def test_station_store_unchanged(self):
        """StationStore funktioniert unverändert."""
        json_file = Path(self.temp_dir.name) / "stations.json"
        csv_file = Path(self.temp_dir.name) / "stations.csv"
        logo_dir = Path(self.temp_dir.name) / "logos"
        logo_dir.mkdir()
        json_file.write_text(
            json.dumps({
                "version": 1,
                "stations": [
                    {
                        "id": "local-500",
                        "name": "Classic Sender",
                        "audio_url": "http://classic.example.com",
                        "metadata_url": "http://classic.example.com",
                        "logo_file": "",
                        "source": "local",
                        "startup": True,
                    },
                ],
            }, indent=2),
            encoding="utf-8",
        )
        store = station_store.StationStore(json_file, csv_file, logo_dir)
        self.assertEqual(1, len(store.list_stations()))

    def test_change_password_still_works(self):
        """change_password funktioniert nach wie vor."""
        config = _write_auth_config(self.temp_dir.name)
        new_config = config.change_password("newpass")
        self.assertTrue(
            web_auth.verify_password(
                "newpass", new_config["password_salt"], new_config["password_hash"]
            )
        )


# ── Integration: Auth + UserStationStores ────────────────────────────────────

class AuthStationIntegrationTests(unittest.TestCase):
    """Testet den typischen Workflow: Auth → StationStore."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.guest_store = _make_guest_store(self.base_dir)

        # Auth mit konfigurierter User
        alice_entry = web_auth.create_user_entry("alicepass")
        self.auth_config = _write_auth_config(
            self.temp_dir.name,
            users={"alice": alice_entry},
        )
        self.mgr = station_store.UserStationStores(
            self.guest_store, self.base_dir, self.base_dir / "logos"
        )

    def test_guest_login_returns_guest_store(self):
        username = self.auth_config.authenticate("gast", "gast")
        self.assertEqual("GAST", username)
        store = self.mgr.for_user(username, writable=True)
        self.assertIs(store, self.guest_store)

    def test_user_login_returns_private_store(self):
        username = self.auth_config.authenticate("alice", "alicepass")
        self.assertEqual("alice", username)
        store = self.mgr.for_user(username, writable=True)
        self.assertIsNot(store, self.guest_store)

    def test_disabled_user_cannot_get_writable_store(self):
        # User deaktivieren
        config = self.auth_config.load()
        config["users"]["alice"]["enabled"] = False
        self.auth_config.save(config)

        username = self.auth_config.authenticate("alice", "alicepass")
        self.assertIsNone(username)

    def test_is_user_enabled_workflow(self):
        self.assertTrue(self.auth_config.is_user_enabled("alice"))
        self.assertFalse(self.auth_config.is_user_enabled("unknown"))


if __name__ == "__main__":
    unittest.main()
