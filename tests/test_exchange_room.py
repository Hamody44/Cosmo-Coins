import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from cogs.economy import (
    Economy,
    ManualExchangeDeliveryView,
    manual_exchange_transfer_command,
    manual_probot_transfer_command,
)
from second_bot.auto_delivery import build_delivery_result_message
from cogs.photo_contest import select_invite_for_join, select_used_invite


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

    async def test_exchange_is_rejected_outside_configured_room(self):
        ctx = self.make_context(200)
        with patch("cogs.economy.get_exchange_room_id", return_value=300), patch(
            "cogs.economy.is_global_exchange_enabled"
        ) as global_switch:
            await Economy.exchange.callback(Economy(None), ctx, "Gems", "100")
        ctx.send.assert_awaited_once_with("⛔ استخدم أمر `exchange` داخل <#300> فقط.")
        global_switch.assert_not_called()

    async def test_exchange_is_allowed_in_any_room_when_no_room_is_configured(self):
        ctx = self.make_context(200)
        with patch("cogs.economy.get_exchange_room_id", return_value=None), patch(
            "cogs.economy.is_global_exchange_enabled", return_value=False
        ):
            await Economy.exchange.callback(Economy(None), ctx, "Gems", "100")
        ctx.send.assert_awaited_once_with("⛔ نظام التبديل مغلق عالميًا من سيرفر Cosmo حاليًا.")


class InviteAttributionTests(unittest.TestCase):
    def invite(self, code, uses, inviter):
        return SimpleNamespace(code=code, uses=uses, inviter=SimpleNamespace(id=inviter))

    def test_selects_invite_with_largest_increase(self):
        current = [self.invite("a", 4, 11), self.invite("b", 7, 22)]
        previous = {"a": (3, 11), "b": (5, 22)}
        self.assertEqual(select_used_invite(current, previous).code, "b")

    def test_does_not_assign_random_inviter_when_increases_tie(self):
        current = [self.invite("a", 4, 11), self.invite("b", 6, 22)]
        previous = {"a": (3, 11), "b": (5, 22)}
        self.assertIsNone(select_used_invite(current, previous))

    def test_concurrent_invite_selection_is_deterministic(self):
        current = [self.invite("b", 6, 22), self.invite("a", 6, 11)]
        previous = {"a": (5, 11), "b": (5, 22)}
        self.assertEqual(select_invite_for_join(current, previous).code, "a")

    def test_concurrent_invite_selection_preserves_remaining_uses(self):
        current = [self.invite("a", 7, 11)]
        previous = {"a": (5, 11)}
        first = select_invite_for_join(current, previous)
        self.assertEqual(first.code, "a")
        after_first = {"a": (6, 11)}
        self.assertEqual(select_invite_for_join(current, after_first).code, "a")


class ManualExchangeInstructionTests(unittest.TestCase):
    def test_instruction_uses_provider_specific_commands_for_supported_currencies(self):
        self.assertEqual(
            manual_exchange_transfer_command("ProBot Credits", 987654, 250.0),
            ("ProBot", "#credit 987654 250"),
        )
        self.assertEqual(
            manual_exchange_transfer_command("AdamC", 987654, 250.0),
            ("AdamC", "T 987654 250"),
        )
        self.assertEqual(
            manual_exchange_transfer_command("Rob", 987654, 250.0),
            ("Rob", "!transfer 987654 250"),
        )
        self.assertEqual(
            manual_exchange_transfer_command("Robux", 987654, 250.0),
            ("Rob", "!transfer 987654 250"),
        )
        self.assertIsNone(manual_exchange_transfer_command("Gems", 987654, 250.0))
        self.assertIsNone(manual_exchange_transfer_command("ProBot Credits", 987654, 250.5))
        self.assertEqual(
            manual_probot_transfer_command("ProBot Credits", 987654, 250.0),
            "#credit 987654 250",
        )

    def test_delivery_view_shows_the_button_for_the_selected_provider(self):
        view = ManualExchangeDeliveryView(123, "AdamC", "T 987654 250")
        self.assertIsNone(view.timeout)
        self.assertEqual(len(view.children), 2)
        self.assertEqual(
            {button.label for button in view.children},
            {"قبول الطلب يدويًا", "رفض الطلب"},
        )

        manually_accepted = ManualExchangeDeliveryView(
            123, "AdamC", "T 987654 250", status="تسليم يدوي"
        )
        self.assertEqual(len(manually_accepted.children), 1)
        self.assertEqual(manually_accepted.children[0].label, "نسخ أمر AdamC")

        processing = ManualExchangeDeliveryView(
            123, "AdamC", "T 987654 250", status="قيد التسليم التلقائي"
        )
        self.assertTrue(all(button.disabled for button in processing.children))

    def test_delivery_status_labels_match_the_exchange_states(self):
        labels = {
            "قيد الانتظار": "⏳ قيد الانتظار",
            "تسليم تلقائي": "🤖 تسليم تلقائي",
            "تسليم يدوي": "🧑‍💼 تسليم يدوي",
            "مرفوض": "❌ مرفوض",
        }
        for status, expected in labels.items():
            with self.subTest(status=status):
                self.assertEqual(Economy._delivery_status_label(status), expected)


class ExchangeDeliveryCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_server_admin_can_use_central_request_button(self):
        member = SimpleNamespace(
            id=55,
            guild_permissions=SimpleNamespace(administrator=True, manage_guild=False),
        )
        origin_guild = SimpleNamespace(
            id=123,
            owner_id=1,
            get_member=lambda user_id: member if user_id == member.id else None,
        )
        cog = Economy(SimpleNamespace(get_guild=lambda guild_id: origin_guild))

        self.assertTrue(await cog._can_manage_request_user(SimpleNamespace(id=55), 123))

    async def test_primary_accepts_only_signed_result_from_the_stated_bot(self):
        secret = "exchange-result-test-secret-that-is-long-enough"
        content = build_delivery_result_message(
            secret=secret, issuer_id=444, order_id=81, outcome="automatic"
        )
        message = SimpleNamespace(content=content, author=SimpleNamespace(id=444))
        cog = Economy(SimpleNamespace())
        with patch.dict(os.environ, {"COSMO_AUTO_DELIVERY_SECRET": secret}), patch(
            "cogs.economy.complete_auto_exchange_delivery", return_value=True
        ) as complete, patch.object(cog, "_update_exchange_request_embed", new=AsyncMock()) as update:
            await cog.on_message(message)

        complete.assert_called_once_with(81)
        update.assert_awaited_once_with(81)

    async def test_primary_ignores_tampered_or_wrong_author_result(self):
        secret = "exchange-result-test-secret-that-is-long-enough"
        content = build_delivery_result_message(
            secret=secret, issuer_id=444, order_id=81, outcome="automatic"
        )
        message = SimpleNamespace(content=content, author=SimpleNamespace(id=445))
        cog = Economy(SimpleNamespace())
        with patch.dict(os.environ, {"COSMO_AUTO_DELIVERY_SECRET": secret}), patch(
            "cogs.economy.complete_auto_exchange_delivery"
        ) as complete, patch.object(cog, "_update_exchange_request_embed", new=AsyncMock()) as update:
            await cog.on_message(message)

        complete.assert_not_called()
        update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
