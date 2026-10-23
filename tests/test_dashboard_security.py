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

    def test_exchange_guild_filter_requires_owner_or_manage_permissions(self):
        self.assertTrue(dashboard.guild_can_manage_exchange({"owner": True, "permissions": "0"}))
        self.assertTrue(dashboard.guild_can_manage_exchange({"owner": False, "permissions": "32"}))
        self.assertTrue(dashboard.guild_can_manage_exchange({"owner": False, "permissions": "8"}))
        self.assertFalse(dashboard.guild_can_manage_exchange({"owner": False, "permissions": "0"}))
        self.assertFalse(dashboard.guild_can_manage_exchange({"owner": False, "permissions": "invalid"}))

    def test_profile_exchange_list_includes_manageable_non_premium_guild(self):
        class Response:
            status_code = 200
            def json(self):
                return [{"id": "123", "name": "No Premium Guild", "owner": True, "permissions": "0"}]

        class SettingsCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchone(self):
                return (1, None)

        class EmptyCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchall(self):
                return []

        class Connection:
            def close(self):
                pass

        with self.client.session_transaction() as session:
            session["user"] = {"id": "456", "username": "test", "global_name": "Tester"}
            session["access_token"] = "test-token"

        with patch("dashboard.app.requests.get", return_value=Response()), \
             patch("dashboard.app.get_bot_guild_ids", return_value={123}), \
             patch("dashboard.app.database.get_exchange_currencies", return_value=[("Gems", 2.0, 1, 0.0)]), \
             patch("dashboard.app.database.get_currency_label", return_value="Cosmo"), \
             patch("dashboard.app.database.is_global_exchange_enabled", return_value=True), \
             patch("dashboard.app.get_guild_db", return_value=(Connection(), SettingsCursor())), \
             patch("dashboard.app.get_global_db", return_value=(Connection(), EmptyCursor())), \
             patch("dashboard.app.get_balance", return_value=1000):
            response = self.client.get("/profile")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No Premium Guild", response.data)
        self.assertIn(b"Premium", response.data)

    def test_profile_exchange_post_uses_global_rate_without_premium(self):
        class GuildResponse:
            status_code = 200
            def json(self):
                return [{"id": "123", "name": "No Premium Guild", "owner": True, "permissions": "0"}]

        class OtherResponse:
            status_code = 403
            def json(self):
                return []

        class SendResponse:
            status_code = 200

        class SettingsCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchone(self):
                return (1, None)

        class GlobalCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchall(self):
                return []
            def fetchone(self):
                return ("999",)

        class Connection:
            def close(self):
                pass

        def discord_get(url, **_kwargs):
            if url.endswith("/users/@me/guilds"):
                return GuildResponse()
            return OtherResponse()

        with self.client.session_transaction() as session:
            session["user"] = {"id": "456", "username": "test", "global_name": "Tester"}
            session["access_token"] = "test-token"
            session["csrf_token"] = "valid-csrf"

        with patch("dashboard.app.requests.get", side_effect=discord_get), \
             patch("dashboard.app.requests.post", return_value=SendResponse()), \
             patch("dashboard.app.get_bot_guild_ids", return_value={123}), \
             patch("dashboard.app.database.get_exchange_currencies", return_value=[("Gems", 2.0, 1, 0.0)]), \
             patch("dashboard.app.database.get_exchange_currency", return_value=("Gems", 2.0, 1, 0.0)), \
             patch("dashboard.app.database.get_currency_label", return_value="Cosmo"), \
             patch("dashboard.app.database.is_global_exchange_enabled", return_value=True), \
             patch("dashboard.app.get_guild_db", return_value=(Connection(), SettingsCursor())), \
             patch("dashboard.app.get_global_db", return_value=(Connection(), GlobalCursor())), \
             patch("dashboard.app.get_balance", return_value=1000), \
             patch("dashboard.app.create_exchange_request", return_value=42) as create_request:
            response = self.client.post("/profile", data={
                "csrf_token": "valid-csrf",
                "guild_id": "123",
                "currency_name": "Gems",
                "cosmo_amount": "200",
                "notes": "player id 123",
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(create_request.call_args.args[:5], (123, 456, 200, "Gems", 400.0))

    def test_profile_blocks_exchange_post_when_global_switch_is_closed(self):
        class GuildResponse:
            status_code = 200
            def json(self):
                return [{"id": "123", "name": "No Premium Guild", "owner": True, "permissions": "0"}]

        with self.client.session_transaction() as session:
            session["user"] = {"id": "456", "username": "test"}
            session["access_token"] = "test-token"
            session["csrf_token"] = "valid-csrf"

        with patch("dashboard.app.requests.get", return_value=GuildResponse()), \
             patch("dashboard.app.database.is_global_exchange_enabled", return_value=False), \
             patch("dashboard.app.get_bot_guild_ids", return_value={123}), \
             patch("dashboard.app.get_balance", return_value=1000), \
             patch("dashboard.app.create_exchange_request") as create_request:
            response = self.client.post("/profile", data={
                "csrf_token": "valid-csrf", "guild_id": "123", "currency_name": "Gems",
                "cosmo_amount": "200", "notes": "test",
            })

        self.assertEqual(response.status_code, 302)
        create_request.assert_not_called()

    def test_profile_handles_global_switch_closing_during_request_creation(self):
        class Response:
            status_code = 200
            def json(self):
                return [{"id": "123", "name": "Managed Guild", "owner": True, "permissions": "0"}]

        class SettingsCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchone(self):
                return (None,)

        class GlobalCursor:
            def execute(self, *_args, **_kwargs):
                pass
            def fetchall(self):
                return []
            def fetchone(self):
                return ("999",)

        class Connection:
            def close(self):
                pass

        with self.client.session_transaction() as session:
            session["user"] = {"id": "456", "username": "test"}
            session["access_token"] = "test-token"
            session["csrf_token"] = "valid-csrf"

        with patch("dashboard.app.requests.get", return_value=Response()), \
             patch("dashboard.app.requests.post", return_value=Response()), \
             patch("dashboard.app.get_bot_guild_ids", return_value={123}), \
             patch("dashboard.app.database.get_exchange_currencies", return_value=[("Gems", 2.0, 1, 0.0)]), \
             patch("dashboard.app.database.get_exchange_currency", return_value=("Gems", 2.0, 1, 0.0)), \
             patch("dashboard.app.database.get_currency_label", return_value="Cosmo"), \
             patch("dashboard.app.database.is_global_exchange_enabled", return_value=True), \
             patch("dashboard.app.get_guild_db", return_value=(Connection(), SettingsCursor())), \
             patch("dashboard.app.get_global_db", return_value=(Connection(), GlobalCursor())), \
             patch("dashboard.app.get_balance", return_value=1000), \
             patch("dashboard.app.create_exchange_request", side_effect=dashboard.database.ExchangeSystemClosed):
            response = self.client.post("/profile", data={
                "csrf_token": "valid-csrf", "guild_id": "123", "currency_name": "Gems",
                "cosmo_amount": "200", "notes": "test",
            })

        self.assertEqual(response.status_code, 302)

    def test_session_key_is_not_the_known_weak_default(self):
        self.assertNotEqual(dashboard.app.secret_key, "cosmo_fixed_dashboard_secret_key_2026")
        self.assertGreaterEqual(len(dashboard.app.secret_key), 32)

    def test_redirect_uri_requires_configured_public_https_url(self):
        self.assertEqual(dashboard.get_redirect_uri(), "https://dashboard.example.test/callback")
        with patch.dict(os.environ, {"DASHBOARD_URL": ""}):
            with self.assertRaises(RuntimeError):
                dashboard.get_redirect_uri()

    def test_bot_invite_uses_application_id_not_server_id(self):
        with patch.object(dashboard, "CLIENT_ID", "1550533434362044607"):
            query = parse_qs(urlparse(dashboard.build_bot_invite_url()).query)
        self.assertEqual(query["client_id"], ["1550533434362044607"])
        self.assertEqual(query["scope"], ["bot applications.commands"])
        self.assertNotIn("guild_id", query)

    @patch("dashboard.app.user_can_manage_guild", return_value=True)
    @patch("dashboard.app.get_bot_guild_ids", return_value={123})
    @patch("dashboard.app.get_guild_db")
    def test_manage_saves_ticket_log_for_this_guild(self, get_db, _bot_guilds, _permission_check):
        class Cursor:
            def __init__(self):
                self.statements = []
            def execute(self, sql, params=()):
                self.statements.append((sql, params))
            def fetchone(self):
                return None

        class Connection:
            def __init__(self):
                self.committed = False
                self.closed = False
            def commit(self):
                self.committed = True
            def close(self):
                self.closed = True

        cursor = Cursor()
        connection = Connection()
        get_db.return_value = (connection, cursor)
        with self.client.session_transaction() as session:
            session["user"] = {"id": "456", "username": "test"}
            session["access_token"] = "test-token"
            session["csrf_token"] = "valid-csrf"

        response = self.client.post("/manage/123", data={
            "csrf_token": "valid-csrf",
            "action": "save_settings",
            "ticket_log": "789",
        })

        self.assertEqual(response.status_code, 302)
        save_sql, save_params = next(
            (sql, params) for sql, params in cursor.statements if "INSERT INTO settings" in sql
        )
        self.assertIn("ticket_log", save_sql)
        self.assertIn("ON CONFLICT(guild_id) DO UPDATE", save_sql)
        self.assertNotIn("exchange_enabled", save_sql)
        self.assertEqual(save_params, (123, None, None, None, None, "789"))
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
