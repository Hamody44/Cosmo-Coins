import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from cogs.admin import (
    ADAMC_ID,
    AutoBudgetCommandButton,
    PRIMARY_OWNER_ID,
    PROBOT_ID,
    available_budget_payment_methods,
    budget_history_action_label,
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
            conn.execute("CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)")

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
                    payment_message_id INTEGER UNIQUE, payment_author_id INTEGER, paid_at INTEGER,
                    provider_order_id TEXT, provider_payment_id TEXT, quote_usd TEXT,
                    pay_address TEXT, pay_currency TEXT, provider_status TEXT
                );
                """
            )
            return conn, conn.cursor()

        self.guild_db_patch = patch.object(database, "get_guild_db", side_effect=get_test_guild_db)
        self.guild_db_patch.start()
        self.addCleanup(self.guild_db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_crypto_is_hidden_from_budget_checkout_methods(self):
        with patch("cogs.admin.is_auto_budget_payment_enabled", return_value=True):
            manager_methods = available_budget_payment_methods(True, 0, 100)
            member_methods = available_budget_payment_methods(False, 100, 100)
            insufficient_member_methods = available_budget_payment_methods(False, 99, 100)

        self.assertEqual(manager_methods, ["credit", "adamc"])
        self.assertEqual(member_methods, ["cosmo"])
        self.assertEqual(insufficient_member_methods, [])
        self.assertNotIn("crypto", manager_methods)

    def test_provider_message_examples_parse_only_for_expected_bot_ids(self):
        probot_text = "**ـ new.3mk, قام بتحويل `$100000000` لـ <@!988840428591808584> ** |:moneybag:"
        probot_english_text = "**:moneybag: | .o4rv, has transferred `$24000000` to <@!988840428591808584> **"
        adamc_text = "💰 | **x_x_x.1**, has transferred **$1,000,000** to <@988840428591808584>."

        probot = parse_budget_payment_confirmation("credit", PROBOT_ID, probot_text)
        probot_english = parse_budget_payment_confirmation("credit", PROBOT_ID, probot_english_text)
        adamc = parse_budget_payment_confirmation("adamc", ADAMC_ID, adamc_text)

        self.assertEqual(probot, {"sender": "new.3mk", "amount": 100_000_000, "recipient_id": PRIMARY_OWNER_ID})
        self.assertEqual(probot_english, {"sender": ".o4rv", "amount": 24_000_000, "recipient_id": PRIMARY_OWNER_ID})
        self.assertEqual(adamc, {"sender": "x_x_x.1", "amount": 1_000_000, "recipient_id": PRIMARY_OWNER_ID})
        self.assertIsNone(parse_budget_payment_confirmation("credit", 123, probot_text))
        self.assertIsNone(parse_budget_payment_confirmation("credit", ADAMC_ID, probot_english_text))
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

    def test_payment_is_bound_to_sender_guild_amount_and_five_minute_window_not_request_channel(self):
        database.set_auto_budget_payment_enabled("credit", True)
        order_id = database.create_budget_payment_order(
            guild_id=101, user_id=202, payer_name="Player.One", channel_id=303,
            method="credit", budget_amount=5, expected_payment=6,
            created_at=1000, expires_at=1300,
        )
        self.assertIsNotNone(order_id)

        self.assertIsNone(database.settle_budget_payment(102, "credit", 303, "player.one", 6, 1, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "someone.else", 6, 2, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 7, 3, PROBOT_ID, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 4, PROBOT_ID, 1301, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 5, 123, 1100, PRIMARY_OWNER_ID))
        self.assertIsNone(database.settle_budget_payment(101, "credit", 303, "player.one", 6, 6, PROBOT_ID, 1100, 123))

        result = database.settle_budget_payment(101, "credit", 304, "PLAYER.ONE", 6, 7, PROBOT_ID, 1300, PRIMARY_OWNER_ID)
        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], order_id)
        self.assertEqual(result["new_balance"], 5)
        self.assertIsNone(database.settle_budget_payment(101, "credit", 304, "player.one", 6, 7, PROBOT_ID, 1300, PRIMARY_OWNER_ID))

        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT action FROM budget_logs").fetchone()[0],
                "إضافة تلقائية (credit)",
            )
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

    def test_personal_cosmo_transfer_is_atomic_and_written_to_budget_history(self):
        guild_conn, _ = database.get_guild_db(101)
        guild_conn.close()
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("INSERT INTO balances (user_id, balance) VALUES (202, 100)")
        with sqlite3.connect(self.guild_db_path) as conn:
            conn.execute("INSERT INTO settings (guild_id, spending_balance) VALUES (101, 20)")

        result = database.transfer_balance_to_server_budget(202, 101, 35, "2026-10-19 12:00:00")

        self.assertEqual(result["new_personal_balance"], 65)
        self.assertEqual(result["old_budget"], 20)
        self.assertEqual(result["new_budget"], 55)
        with sqlite3.connect(self.global_db_path) as conn:
            self.assertEqual(conn.execute("SELECT balance FROM balances WHERE user_id = 202").fetchone()[0], 65)
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 55)
            self.assertEqual(
                conn.execute("SELECT action, amount, admin_id FROM budget_logs").fetchone(),
                ("إضافة من رصيد Cosmo", 35, 202),
            )

    def test_insufficient_personal_cosmo_does_not_change_either_balance(self):
        guild_conn, _ = database.get_guild_db(101)
        guild_conn.close()
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("INSERT INTO balances (user_id, balance) VALUES (202, 10)")
        with sqlite3.connect(self.guild_db_path) as conn:
            conn.execute("INSERT INTO settings (guild_id, spending_balance) VALUES (101, 20)")

        self.assertIsNone(database.transfer_balance_to_server_budget(202, 101, 11))
        with sqlite3.connect(self.global_db_path) as conn:
            self.assertEqual(conn.execute("SELECT balance FROM balances WHERE user_id = 202").fetchone()[0], 10)
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 20)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)

    def test_concurrent_personal_transfers_cannot_double_spend(self):
        guild_conn, _ = database.get_guild_db(101)
        guild_conn.close()
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("INSERT INTO balances (user_id, balance) VALUES (202, 100)")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: database.transfer_balance_to_server_budget(202, 101, 80), range(2)))

        self.assertEqual(sum(result is not None for result in results), 1)
        with sqlite3.connect(self.global_db_path) as conn:
            self.assertEqual(conn.execute("SELECT balance FROM balances WHERE user_id = 202").fetchone()[0], 20)
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 80)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 1)

    def test_personal_transfer_fails_closed_if_either_database_uses_wal(self):
        guild_conn, _ = database.get_guild_db(101)
        guild_conn.close()
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("INSERT INTO balances (user_id, balance) VALUES (202, 100)")
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")

        with self.assertRaises(RuntimeError):
            database.transfer_balance_to_server_budget(202, 101, 40)
        with sqlite3.connect(self.global_db_path) as conn:
            self.assertEqual(conn.execute("SELECT balance FROM balances WHERE user_id = 202").fetchone()[0], 100)
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)


class AutoBudgetCommandButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_returns_exact_command_ephemerally_to_requester(self):
        command = "#credit 988840428591808584 120 with tax"
        button = AutoBudgetCommandButton(user_id=42, command=command)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            command, ephemeral=True,
        )

    async def test_button_does_not_reveal_command_to_another_user(self):
        command = "!transfer 988840428591808584 25"
        button = AutoBudgetCommandButton(user_id=42, command=command)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "هذا الزر مخصص لصاحب طلب الشحن فقط.", ephemeral=True
        )
        self.assertNotIn(command, interaction.response.send_message.await_args.args[0])


class BudgetHistoryLabelTests(unittest.TestCase):
    def test_automatic_topups_are_presented_as_additions_with_provider(self):
        self.assertEqual(
            budget_history_action_label("إضافة تلقائية (credit)"),
            "💳 شحن تلقائي عبر ProBot Credits",
        )
        self.assertEqual(
            budget_history_action_label("إضافة تلقائية (adamc)"),
            "💳 شحن تلقائي عبر AdamC",
        )
        self.assertIn("NOWPayments", budget_history_action_label("إضافة تلقائية (crypto)"))
        self.assertIn("الشخصي", budget_history_action_label("إضافة من رصيد Cosmo"))

    def test_manual_additions_deductions_and_direct_set_are_distinguished(self):
        self.assertEqual(budget_history_action_label("إضافة"), "🟢 إضافة يدوية")
        self.assertEqual(budget_history_action_label("خصم"), "🔴 خصم")
        self.assertEqual(budget_history_action_label("تعيين مباشر"), "⚙️ تعيين مباشر")


if __name__ == "__main__":
    unittest.main()
