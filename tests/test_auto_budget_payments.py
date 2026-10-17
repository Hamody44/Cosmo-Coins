import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database
from cogs.admin import (
    ADAMC_ID,
    PRIMARY_OWNER_ID,
    PROBOT_ID,
    parse_budget_payment_confirmation,
)


class AutoBudgetPaymentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.guild_db_path = os.path.join(self.temp_dir.name, "guild.db")
        self.global_db_path = os.path.join(self.temp_dir.name, "global.db")
        database.GLOBAL_DB_PATH = self.global_db_path
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("CREATE TABLE global_settings (key TEXT PRIMARY KEY, value INTEGER)")

        def get_test_guild_db(_guild_id):
            conn = sqlite3.connect(self.guild_db_path, timeout=10)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id INTEGER PRIMARY KEY,
                    spending_balance INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS budget_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT, amount INTEGER, old_balance INTEGER,
                    new_balance INTEGER, admin_id INTEGER, timestamp TEXT
                );
                CREATE TABLE IF NOT EXISTS budget_payment_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    payer_name TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    method TEXT NOT NULL, budget_amount INTEGER NOT NULL,
                    expected_payment INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    payment_message_id INTEGER UNIQUE, payment_author_id INTEGER, paid_at INTEGER
                );
                """
            )
            return conn, conn.cursor()

        self.guild_db_patch = patch.object(database, "get_guild_db", side_effect=get_test_guild_db)
        self.guild_db_patch.start()
        self.addCleanup(self.guild_db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_provider_message_examples_parse_only_for_expected_bot_ids(self):
        probot_text = "**ـ new.3mk, قام بتحويل `$100000000` لـ <@!988840428591808584> ** |:moneybag:"
        adamc_text = "💰 | **x_x_x.1**, has transferred **$1,000,000** to <@988840428591808584>."

        probot = parse_budget_payment_confirmation("credit", PROBOT_ID, probot_text)
        adamc = parse_budget_payment_confirmation("adamc", ADAMC_ID, adamc_text)

        self.assertEqual(probot, {"sender": "new.3mk", "amount": 100_000_000, "recipient_id": PRIMARY_OWNER_ID})
        self.assertEqual(adamc, {"sender": "x_x_x.1", "amount": 1_000_000, "recipient_id": PRIMARY_OWNER_ID})
        self.assertIsNone(parse_budget_payment_confirmation("credit", 123, probot_text))
        self.assertIsNone(parse_budget_payment_confirmation("adamc", PROBOT_ID, adamc_text))

    def test_payment_methods_default_closed_and_owner_can_toggle_each(self):
        for method in ("credit", "adamc", "crypto"):
            self.assertFalse(database.is_auto_budget_payment_enabled(method))
            database.set_auto_budget_payment_enabled(method, True)
            self.assertTrue(database.is_auto_budget_payment_enabled(method))
            database.set_auto_budget_payment_enabled(method, False)
            self.assertFalse(database.is_auto_budget_payment_enabled(method))

    def test_payment_order_rejects_nonmatching_rates_and_fractional_cosmo(self):
        database.set_auto_budget_payment_enabled("credit", True)
        database.set_auto_budget_payment_enabled("adamc", True)
        self.assertIsNone(database.create_budget_payment_order(101, 202, "payer", 303, "credit", 5, 7, 1000, 1300))
        self.assertIsNone(database.create_budget_payment_order(101, 202, "payer", 303, "adamc", 10, 3, 1000, 1300))
        self.assertIsNone(database.create_budget_payment_order(101, 202, "payer", 303, "credit", 6, 7, 1000, 1300))

    def test_payment_is_bound_to_sender_guild_channel_amount_and_five_minute_window(self):
        database.set_auto_budget_payment_enabled("credit", True)
        order_id = database.create_budget_payment_order(
            guild_id=101, user_id=202, payer_name="Player.One", channel_id=303,
            method="credit", budget_amount=5, expected_payment=6,
            created_at=1000, expires_at=1300,
        )
        self.assertIsNotNone(order_id)

        self.assertIsNone(database.settle_budget_payment(101, "credit", 304, "player.one", 6, 1, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "someone.else", 6, 2, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 7, 3, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 4, PROBOT_ID, 1301, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 5, 123, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 6, PROBOT_ID, 1100, 123))

        result = database.settle_budget_payment(101, "credit", 303, "PLAYER.ONE", 6, 7, PROBOT_ID, 1300, PRIMARY_OWNER_ID)
        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], order_id)
        self.assertEqual(result["new_balance"], 5)
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 7, PROBOT_ID, 1300, PRIMARY_OWNER_ID))

        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM budget_payment_orders WHERE id = ?", (order_id,)).fetchone()[0], "paid")

    def test_only_one_pending_order_per_user_and_guild(self):
        database.set_auto_budget_payment_enabled("adamc", True)
        database.set_auto_budget_payment_enabled("credit", True)
        first = database.create_budget_payment_order(101, 202, "player.one", 303, "adamc", 10, 2, 1000, 1300)
        second = database.create_budget_payment_order(101, 202, "player.one", 303, "credit", 10, 12, 1010, 1310)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_disabling_method_cancels_pending_orders_even_after_reenable(self):
        database.set_auto_budget_payment_enabled("credit", True)
        order_id = database.create_budget_payment_order(101, 202, "player.one", 303, "credit", 5, 6, 1000, 1300)
        self.assertIsNotNone(order_id)

        cancelled = database.set_auto_budget_payment_enabled("credit", False, [101])
        self.assertEqual(cancelled, [{"guild_id": 101, "order_id": order_id, "user_id": 202, "channel_id": 303}])
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 10, PROBOT_ID, 1100, PRIMARY_OWNER_ID))

        database.set_auto_budget_payment_enabled("credit", True, [101])
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 10, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM budget_payment_orders WHERE id = ?", (order_id,)).fetchone()[0], "cancelled")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
