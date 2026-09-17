import http.client
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web_auth
from web_player_server import WebPlayerServer, _parse_icy_title


class IcyMetadataTests(unittest.TestCase):
    def test_extracts_stream_title(self):
        metadata = b"StreamTitle='Artist - Titel';StreamUrl='';" + b"\0" * 8
        self.assertEqual("Artist - Titel", _parse_icy_title(metadata))

    def test_rejects_empty_or_url_title(self):
        self.assertEqual("", _parse_icy_title(b"StreamTitle='';"))
        self.assertEqual("", _parse_icy_title(b"StreamTitle='https://example.org/live';"))
        self.assertEqual("", _parse_icy_title(b"StreamUrl='';"))


class AuthConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "web_auth_config.json"
        self.config = web_auth.AuthConfig(self.config_path)

    def test_default_guest_credentials_are_hashed(self):
        config = self.config.load()

        self.assertEqual("GAST", config["guest_username"])
        self.assertTrue(config["guest_enabled"])
        self.assertNotIn("password", config)
        self.assertNotEqual("gast", config["password_hash"])
        self.assertTrue(
            web_auth.verify_password(
                "gast", config["password_salt"], config["password_hash"]
            )
        )
        self.assertNotIn('"password": "gast"', self.config_path.read_text("utf-8"))

    def test_password_verification_accepts_correct_and_rejects_wrong_password(self):
        config = self.config.load()

        self.assertTrue(
            web_auth.verify_password(
                "gast", config["password_salt"], config["password_hash"]
            )
        )
        self.assertFalse(
            web_auth.verify_password(
                "falsch", config["password_salt"], config["password_hash"]
            )
        )

    def test_change_password_replaces_old_password(self):
        old_config = self.config.load()
        changed = self.config.change_password("neues-passwort")

        self.assertNotEqual(old_config["password_hash"], changed["password_hash"])
        self.assertTrue(
            web_auth.verify_password(
                "neues-passwort", changed["password_salt"], changed["password_hash"]
            )
        )
        self.assertFalse(
            web_auth.verify_password(
                "gast", changed["password_salt"], changed["password_hash"]
            )
        )
        self.assertEqual(changed, self.config.load())

    def test_guest_toggle_is_persisted(self):
        self.assertFalse(self.config.toggle_guest(False)["guest_enabled"])
        self.assertFalse(self.config.load()["guest_enabled"])
        self.assertTrue(self.config.toggle_guest(True)["guest_enabled"])
        self.assertTrue(self.config.load()["guest_enabled"])


class SessionStoreTests(unittest.TestCase):
    def test_session_expires_after_ttl(self):
        store = web_auth.SessionStore(ttl_seconds=5)
        with mock.patch.object(web_auth.time, "monotonic", side_effect=[100.0, 106.0]):
            session_id = store.create("GAST")
            self.assertIsNone(store.validate(session_id, guest_enabled=True))

        self.assertEqual(0, store.count())

    def test_disabling_guest_invalidates_session_during_validation(self):
        store = web_auth.SessionStore()
        session_id = store.create("gast")

        self.assertIsNone(store.validate(session_id, guest_enabled=False))
        self.assertIsNone(store.validate(session_id, guest_enabled=True))

    def test_invalidate_logs_session_out(self):
        store = web_auth.SessionStore()
        session_id = store.create("GAST")
        self.assertEqual("GAST", store.validate(session_id, guest_enabled=True))

        store.invalidate(session_id)

        self.assertIsNone(store.validate(session_id, guest_enabled=True))
        store.invalidate("unbekannt")

    def test_invalidate_guest_sessions_preserves_other_users(self):
        store = web_auth.SessionStore()
        guest_id = store.create("Gast")
        other_id = store.create("admin")

        store.invalidate_guest_sessions()

        self.assertIsNone(store.validate(guest_id, guest_enabled=True))
        self.assertEqual("admin", store.validate(other_id, guest_enabled=True))


class RateLimiterTests(unittest.TestCase):
    def test_blocks_only_after_maximum_failures_and_resets_on_success(self):
        limiter = web_auth.RateLimiter(max_attempts=2, window_seconds=60)

        self.assertFalse(limiter.record_failure("192.0.2.1"))
        self.assertFalse(limiter.record_failure("192.0.2.1"))
        self.assertTrue(limiter.record_failure("192.0.2.1"))
        self.assertTrue(limiter.is_blocked("192.0.2.1"))
        self.assertFalse(limiter.is_blocked("192.0.2.2"))

        limiter.record_success("192.0.2.1")
        self.assertFalse(limiter.is_blocked("192.0.2.1"))

    def test_old_failures_expire(self):
        limiter = web_auth.RateLimiter(max_attempts=1, window_seconds=10)
        with mock.patch.object(
            web_auth.time, "monotonic", side_effect=[0.0, 1.0, 20.0]
        ):
            self.assertFalse(limiter.record_failure("192.0.2.1"))
            self.assertTrue(limiter.record_failure("192.0.2.1"))
            self.assertFalse(limiter.is_blocked("192.0.2.1"))


class _StoreStub:
    def list_stations(self):
        return []


class WebPlayerServerAuthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base_dir = Path(self.temp_dir.name)
        self.server = WebPlayerServer(
            store=_StoreStub(),
            logo_dir=base_dir / "logos",
            base_dir=base_dir,
            immich_config=base_dir / "immich.json",
            host="127.0.0.1",
            port=0,
            auth_config_path=base_dir / "auth.json",
        )
        self.server.start()
        self.addCleanup(self.server.stop)
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.httpd.server_port, timeout=5
        )
        self.addCleanup(self.connection.close)

    def test_anonymous_root_redirects_and_login_with_csrf_cookie_succeeds(self):
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(303, response.status)
        self.assertEqual("/login", response.getheader("Location"))

        self.connection.request("GET", "/login")
        response = self.connection.getresponse()
        login_html = response.read().decode("utf-8")
        self.assertEqual(200, response.status)
        self.assertNotIn("csrf_token", login_html)

        form = urllib.parse.urlencode(
            {"username": "gast", "password": "gast"}
        )
        self.connection.request(
            "POST",
            "/api/login",
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(200, response.status)
        self.assertEqual({"ok": True}, payload)
        set_cookies = response.getheaders()
        cookie_values = [value.split(";", 1)[0] for name, value in set_cookies if name.lower() == "set-cookie"]
        session_cookie = next(value for value in cookie_values if value.startswith("SID="))
        csrf_cookie = next(value for value in cookie_values if value.startswith("CSRF="))
        csrf_token = csrf_cookie.split("=", 1)[1]

        cookies = session_cookie + "; " + csrf_cookie
        self.connection.request("GET", "/", headers={"Cookie": cookies})
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(200, response.status)

        self.connection.request("POST", "/api/logout", headers={"Cookie": cookies})
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(403, response.status)

        self.connection.request(
            "POST",
            "/api/logout",
            headers={"Cookie": cookies, "X-CSRF-Token": csrf_token},
        )
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(200, response.status)

        self.connection.request("GET", "/", headers={"Cookie": cookies})
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(303, response.status)

    def test_user_can_manage_and_load_personal_stations(self):
        self.server._auth_config.add_user("alice", "geheim")
        form = urllib.parse.urlencode({"username": "Alice", "password": "geheim"})
        self.connection.request(
            "POST", "/api/login", body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(200, response.status)
        cookie_values = [
            value.split(";", 1)[0]
            for name, value in response.getheaders()
            if name.lower() == "set-cookie"
        ]
        session_cookie = next(value for value in cookie_values if value.startswith("SID="))
        csrf_cookie = next(value for value in cookie_values if value.startswith("CSRF="))
        csrf_token = csrf_cookie.split("=", 1)[1]
        cookies = session_cookie + "; " + csrf_cookie

        self.connection.request("GET", "/admin", headers={"Cookie": cookies})
        response = self.connection.getresponse()
        admin_html = response.read().decode("utf-8")
        self.assertEqual(200, response.status)
        self.assertIn("Webradio-Sender", admin_html)

        station_form = urllib.parse.urlencode({
            "name": "Alice Radio",
            "audio_url": "https://example.com/alice.mp3",
            "metadata_url": "https://example.com/alice.mp3",
        })
        with mock.patch("web_player_server.select_and_cache_logo", return_value=""):
            self.connection.request(
                "POST", "/admin/api/add-manual", body=station_form,
                headers={
                    "Cookie": cookies,
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = self.connection.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        self.assertEqual(200, response.status)
        self.assertTrue(result["personal_created"])

        self.connection.request("GET", "/api/stations", headers={"Cookie": cookies})
        response = self.connection.getresponse()
        stations = json.loads(response.read().decode("utf-8"))["stations"]
        self.assertEqual(200, response.status)
        self.assertEqual(["Alice Radio"], [station["name"] for station in stations])


if __name__ == "__main__":
    unittest.main()
