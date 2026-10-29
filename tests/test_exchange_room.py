import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from cogs.economy import Economy, manual_probot_transfer_command


class ExchangeRoomPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        os.makedirs("guilds_data", exist_ok=True)

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def test_exchange_room_setting_migrates_existing_settings_table(self):
        guild_id = 123456
        db_path = os.path.join("guilds_data", f"guild_{guild_id}.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TABLE settings (
                    guild_id INTEGER PRIMARY KEY,
                    transfer_log INTEGER,
                    exchange_request_log INTEGER,
                    exchange_success_log INTEGER,
                    delivery_log INTEGER,
                    balance_log INTEGER,
                    ticket_log INTEGER,
                    invite_log INTEGER,
                    admin_role_id INTEGER,
                    owner_role_id INTEGER,
                    blacklist_role_id INTEGER,
                    exchange_enabled INTEGER DEFAULT 1,
                    double_event_end INTEGER DEFAULT 0,
                    spending_balance INTEGER DEFAULT 0,
                    eightball_guess_reward INTEGER NOT NULL DEFAULT 0
                )"""
            )

        self.assertIsNone(database.get_exchange_room_id(guild_id))
        self.assertEqual(database.set_exchange_room_id(guild_id, 987654), 987654)
        self.assertEqual(database.get_exchange_room_id(guild_id), 987654)
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        self.assertIn("exchange_room", columns)


class ExchangeRoomCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, channel_id):
        return SimpleNamespace(
            guild=SimpleNamespace(id=123, name="Test server"),
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=456),
            send=AsyncMock(),
        )

    async def test_exchange_fails_closed_when_room_has_not_been_configured(self):
        ctx = self.make_context(200)
        with patch("cogs.economy.get_exchange_room_id", return_value=None):
            await Economy.exchange.callback(Economy(None), ctx, "Gems", "100")
        ctx.send.assert_awaited_once()
        self.assertIn("set_exchange_room", ctx.send.await_args.args[0])

    async def test_exchange_is_rejected_outside_configured_room(self):
        ctx = self.make_context(200)
        with patch("cogs.economy.get_exchange_room_id", return_value=300), patch(
            "cogs.economy.is_global_exchange_enabled"
        ) as global_switch:
            await Economy.exchange.callback(Economy(None), ctx, "Gems", "100")
        ctx.send.assert_awaited_once_with("⛔ استخدم أمر `exchange` داخل <#300> فقط.")
        global_switch.assert_not_called()


class ManualProBotInstructionTests(unittest.TestCase):
    def test_instruction_uses_exchange_recipient_id_without_affecting_other_currencies(self):
        self.assertEqual(
            manual_probot_transfer_command("ProBot Credits", 987654, 250.0),
            "!transfer 987654 250",
        )
        self.assertIsNone(manual_probot_transfer_command("AdamC", 987654, 250.0))
        self.assertIsNone(manual_probot_transfer_command("Robux", 987654, 250.0))
        self.assertIsNone(manual_probot_transfer_command("ProBot Credits", 987654, 250.5))


if __name__ == "__main__":
    unittest.main()
