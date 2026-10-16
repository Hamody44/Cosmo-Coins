import unittest
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from dashboard import app as dashboard


class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"DASHBOARD_URL": "https://dashboard.example.test"})
        self.env_patcher.start()
        dashboard.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.env_patcher.stop()

    def test_login_sets_oauth_state_and_includes_it_in_redirect(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.location).query)
        self.assertTrue(query.get("state", [""])[0])
        with self.client.session_transaction() as session:
            self.assertEqual(session["oauth_state"], query["state"][0])

    def test_callback_rejects_missing_or_invalid_oauth_state(self):
        with self.client.session_transaction() as session:
            session["oauth_state"] = "expected-state"
        response = self.client.get("/callback?code=unused&state=wrong")
        self.assertEqual(response.status_code, 400)

    def test_post_without_csrf_token_is_rejected_before_route_logic(self):
        response = self.client.post("/profile", data={})
        self.assertEqual(response.status_code, 400)

    @patch("dashboard.app.user_can_manage_guild", return_value=False)
    def test_manage_route_rejects_guild_without_oauth_permission(self, _permission_check):
        with self.client.session_transaction() as session:
            session["user"] = {"id": "123", "username": "test"}
            session["access_token"] = "test-token"
        response = self.client.get("/manage/456")
        self.assertEqual(response.status_code, 403)

    @patch("dashboard.app.requests.get")
    def test_permission_helper_requires_manage_permission_or_ownership(self, request_get):
        class Response:
            status_code = 200
            def json(self):
                return [
                    {"id": "101", "owner": False, "permissions": "32"},
                    {"id": "202", "owner": False, "permissions": "0"},
                    {"id": "303", "owner": True, "permissions": "0"},
                ]
        request_get.return_value = Response()
        self.assertTrue(dashboard.user_can_manage_guild("token", 101))
        self.assertFalse(dashboard.user_can_manage_guild("token", 202))
        self.assertTrue(dashboard.user_can_manage_guild("token", 303))

    def test_session_key_is_not_the_known_weak_default(self):
        self.assertNotEqual(dashboard.app.secret_key, "cosmo_fixed_dashboard_secret_key_2026")
        self.assertGreaterEqual(len(dashboard.app.secret_key), 32)

    def test_redirect_uri_requires_configured_public_https_url(self):
        self.assertEqual(dashboard.get_redirect_uri(), "https://dashboard.example.test/callback")
        with patch.dict(os.environ, {"DASHBOARD_URL": ""}):
            with self.assertRaises(RuntimeError):
                dashboard.get_redirect_uri()


if __name__ == "__main__":
    unittest.main()
