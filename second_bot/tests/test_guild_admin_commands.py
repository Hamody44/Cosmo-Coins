"""Unit tests for second-bot guild administration commands."""
from __future__ import annotations

import asyncio
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord


_BOT_SPEC = spec_from_file_location("second_bot_main_admin_tests", Path(__file__).resolve().parents[1] / "main.py")
assert _BOT_SPEC is not None and _BOT_SPEC.loader is not None
main = module_from_spec(_BOT_SPEC)
_BOT_SPEC.loader.exec_module(main)


def make_context(*, guild=True):
    return SimpleNamespace(
        guild=SimpleNamespace(id=100, me=SimpleNamespace()) if guild else None,
        author=SimpleNamespace(id=42),
        channel=SimpleNamespace(id=200),
        message=SimpleNamespace(content="", attachments=[]),
        send=AsyncMock(),
    )


class GuildAdministrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_owner_check_rejects_non_owner_and_direct_messages(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(user=SimpleNamespace(id=999), is_owner=AsyncMock(return_value=False))
        with patch.object(main, "bot", fake_bot):
            self.assertFalse(await main.check_admin_owner(ctx))
            ctx.guild = None
            with self.assertRaises(main.NotInSharedGuild):
                await main.check_admin_owner(ctx)

    async def test_admin_owner_check_accepts_only_account_owner(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(user=SimpleNamespace(id=999), owner_id=42, owner_ids=set(), is_owner=AsyncMock(return_value=True))
        with patch.object(main, "bot", fake_bot):
            self.assertTrue(await main.check_admin_owner(ctx))

    async def test_admin_owner_check_accepts_logged_in_user_without_owner_settings(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(user=SimpleNamespace(id=42), is_owner=AsyncMock(side_effect=AttributeError("Owners aren't set")))
        with patch.object(main, "bot", fake_bot):
            self.assertTrue(await main.check_admin_owner(ctx))
        fake_bot.is_owner.assert_not_awaited()

    async def test_admin_owner_check_accepts_primary_bot_owner_id(self):
        ctx = make_context()
        ctx.author.id = main.PRIMARY_OWNER_ID
        fake_bot = SimpleNamespace(user=None, is_owner=AsyncMock(side_effect=AttributeError("Owners aren't set")))
        with patch.object(main, "bot", fake_bot):
            self.assertTrue(await main.check_admin_owner(ctx))
        fake_bot.is_owner.assert_not_awaited()

    async def test_guilds_reports_sorted_guilds_and_unavailable_invite(self):
        first = SimpleNamespace(
            id=2,
            name="Alpha",
            text_channels=[],
            me=SimpleNamespace(),
            member_count=12,
            owner=SimpleNamespace(name="Owner", display_name="Owner"),
            owner_id=7,
        )
        second = SimpleNamespace(
            id=1,
            name="Zulu",
            text_channels=[],
            me=SimpleNamespace(),
            member_count=None,
            owner=None,
            owner_id=8,
        )
        ctx = make_context()
        fake_bot = SimpleNamespace(guilds=[second, first])
        with patch.object(main, "bot", fake_bot):
            await main.guilds.callback(ctx)

        sent = "\n".join(call.args[0] for call in ctx.send.await_args_list)
        self.assertLess(sent.index("Alpha"), sent.index("Zulu"))
        self.assertIn("`2`", sent)
        self.assertIn("Owner", sent)
        self.assertIn("غير متاح", sent)

    async def test_guilds_reports_empty_account(self):
        ctx = make_context()
        with patch.object(main, "bot", SimpleNamespace(guilds=[])):
            await main.guilds.callback(ctx)
        self.assertIn("غير موجود", ctx.send.await_args.args[0])

    async def test_join_guild_rejects_invalid_url_without_api_call(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(fetch_invite=AsyncMock())
        with patch.object(main, "bot", fake_bot):
            await main.join_guild.callback(ctx, invite_url="https://example.com/invite/nope")
        fake_bot.fetch_invite.assert_not_awaited()
        self.assertIn("غير صالح", ctx.send.await_args.args[0])

    async def test_join_guild_previews_and_joins_after_confirmation(self):
        invite_guild = SimpleNamespace(id=555, name="New Guild")
        invite = SimpleNamespace(
            guild=invite_guild,
            approximate_member_count=23,
            expires_at=None,
        )
        confirmation = SimpleNamespace(author=SimpleNamespace(id=42), channel=SimpleNamespace(id=200), content="نعم")
        fake_bot = SimpleNamespace(
            fetch_invite=AsyncMock(return_value=invite),
            get_guild=Mock(return_value=None),
            wait_for=AsyncMock(return_value=confirmation),
            accept_invite=AsyncMock(return_value=invite),
        )
        ctx = make_context()
        with patch.object(main, "bot", fake_bot):
            await main.join_guild.callback(ctx, invite_url="https://discord.gg/example")

        fake_bot.fetch_invite.assert_awaited_once()
        fake_bot.accept_invite.assert_awaited_once_with(invite)
        self.assertTrue(any("معاينة" in call.args[0] for call in ctx.send.await_args_list))
        self.assertTrue(any("تم إرسال طلب الانضمام" in call.args[0] for call in ctx.send.await_args_list))

    async def test_join_guild_handles_expired_invite(self):
        ctx = make_context()
        response = SimpleNamespace(status=404, reason="Not Found")
        fake_bot = SimpleNamespace(
            fetch_invite=AsyncMock(side_effect=discord.NotFound(response, "expired")),
        )
        with patch.object(main, "bot", fake_bot):
            await main.join_guild.callback(ctx, invite_url="https://discord.com/invite/expired")
        self.assertIn("غير صالحة أو منتهية", ctx.send.await_args.args[0])

    async def test_join_guild_handles_rate_limit(self):
        ctx = make_context()
        response = SimpleNamespace(status=429, reason="Too Many Requests")
        fake_bot = SimpleNamespace(
            fetch_invite=AsyncMock(side_effect=discord.HTTPException(response, "rate limited")),
        )
        with patch.object(main, "bot", fake_bot):
            await main.join_guild.callback(ctx, invite_url="https://discord.gg/limited")
        self.assertIn("حد الطلبات", ctx.send.await_args.args[0])

    async def test_leave_guild_accepts_id_and_leaves_cached_guild(self):
        ctx = make_context()
        guild = SimpleNamespace(id=123, name="Leaving Guild")
        fake_bot = SimpleNamespace(get_guild=Mock(return_value=guild), leave_guild=AsyncMock())
        with patch.object(main, "bot", fake_bot):
            await main.leave_guild.callback(ctx, target="123")
        fake_bot.leave_guild.assert_awaited_once_with(guild)
        self.assertIn("غادر البوت", ctx.send.await_args.args[0])

    async def test_leave_guild_rejects_invalid_target(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(get_guild=Mock())
        with patch.object(main, "bot", fake_bot):
            await main.leave_guild.callback(ctx, target="not-a-guild")
        fake_bot.get_guild.assert_not_called()
        self.assertIn("Guild ID", ctx.send.await_args.args[0])

    async def test_leave_guild_reports_missing_cached_guild(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(get_guild=Mock(return_value=None))
        with patch.object(main, "bot", fake_bot):
            await main.leave_guild.callback(ctx, target="123")
        self.assertIn("غير موجود", ctx.send.await_args.args[0])


class InviteParsingTests(unittest.TestCase):
    def test_accepts_supported_discord_invite_forms(self):
        self.assertEqual(main.parse_discord_invite("https://discord.gg/example"), "example")
        self.assertEqual(main.parse_discord_invite("https://discord.com/invite/example"), "example")

    def test_rejects_non_discord_and_non_https_links(self):
        self.assertIsNone(main.parse_discord_invite("http://discord.gg/example"))
        self.assertIsNone(main.parse_discord_invite("https://example.com/invite/example"))
        self.assertIsNone(main.parse_discord_invite("example"))


if __name__ == "__main__":
    unittest.main()
