"""Unit tests for the standalone Discord bot; no Discord connection is made."""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
from discord.ext import commands

_BOT_SPEC = spec_from_file_location("second_bot_main", Path(__file__).resolve().parents[1] / "main.py")
assert _BOT_SPEC is not None and _BOT_SPEC.loader is not None
main = module_from_spec(_BOT_SPEC)
_BOT_SPEC.loader.exec_module(main)


ROLE_ID = 123456789012345678


def make_context(*, role_ids: tuple[int, ...] = (ROLE_ID,), permissions: discord.Permissions | None = None, guild_id: int = 777):
    if permissions is None:
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            send_messages_in_threads=True,
        )

    author = MagicMock(spec=discord.Member)
    author.id = 987654321
    author.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]

    bot_member = MagicMock(spec=discord.Member)
    channel = SimpleNamespace(
        id=555,
        permissions_for=Mock(return_value=permissions),
    )
    message = SimpleNamespace(id=444, delete=AsyncMock())
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id, me=bot_member),
        author=author,
        channel=channel,
        message=message,
        send=AsyncMock(),
        command=None,
    )


class BotConfigurationTests(unittest.TestCase):
    def test_load_config_returns_token_and_positive_role_id(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": "",
            "DISCORD_BOT_TOKEN": " test-token ",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            self.assertEqual(main.load_config(), ("test-token", ROLE_ID))

    def test_dedicated_token_takes_precedence_and_bot_prefix_is_removed(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": " Bot second-bot-token ",
            "DISCORD_BOT_TOKEN": "cosmo-coins-token",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            self.assertEqual(main.load_config(), ("second-bot-token", ROLE_ID))

    def test_load_config_rejects_missing_token(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": "",
            "DISCORD_BOT_TOKEN": "",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            with self.assertRaisesRegex(ValueError, "DISCORD_BOT_TOKEN"):
                main.load_config()

    def test_load_config_rejects_invalid_role_id(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "token", "SAY_ALLOWED_ROLE_ID": "not-a-role"}):
            with self.assertRaisesRegex(ValueError, "SAY_ALLOWED_ROLE_ID"):
                main.load_config()


class SharedGuildCommandCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "shared_guilds.json"
        self.path_patch = patch.object(main, "SHARED_GUILD_STATE_PATH", self.state_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def write_state(self, guild_ids, updated_at=None):
        self.state_path.write_text(json.dumps({
            "updated_at": time.time() if updated_at is None else updated_at,
            "guild_ids": guild_ids,
        }), encoding="utf-8")

    async def test_shared_guild_is_allowed(self):
        self.write_state([777, 888])
        self.assertTrue(await main.check_shared_guild(make_context()))

    async def test_non_shared_guild_is_rejected(self):
        self.write_state([888])
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(make_context())

    async def test_missing_or_stale_state_fails_closed(self):
        self.assertEqual(main.load_shared_guild_ids(), set())
        self.write_state([777], updated_at=time.time() - main.SHARED_GUILD_STATE_MAX_AGE_SECONDS - 1)
        self.assertEqual(main.load_shared_guild_ids(), set())
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(make_context())

    async def test_direct_messages_are_rejected(self):
        ctx = make_context()
        ctx.guild = None
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(ctx)

    async def test_rejected_guild_command_is_silent(self):
        ctx = make_context()
        await main.on_command_error(ctx, main.NotInSharedGuild("not shared"))
        ctx.send.assert_not_awaited()


class SayCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.SAY_ALLOWED_ROLE_ID = ROLE_ID
        main.bot.is_owner = AsyncMock(return_value=False)

    async def test_authorized_role_deletes_command_and_sends_text_without_mentions(self):
        ctx = make_context()

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_awaited_once_with()
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.await_args
        self.assertEqual(args, ("hello",))
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        main.bot.is_owner.assert_not_awaited()

    async def test_bot_owner_is_allowed_without_configured_role(self):
        ctx = make_context(role_ids=())
        main.bot.is_owner = AsyncMock(return_value=True)

        await main.say.callback(ctx, text="owner message")

        ctx.message.delete.assert_awaited_once_with()
        self.assertEqual(ctx.send.await_args.args, ("owner message",))
        main.bot.is_owner.assert_awaited_once_with(ctx.author)

    async def test_empty_text_is_rejected(self):
        ctx = make_context()

        await main.say.callback(ctx, text="   \t  ")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("فارغ", ctx.send.await_args.args[0])

    async def test_text_over_2000_characters_is_rejected(self):
        ctx = make_context()

        await main.say.callback(ctx, text="x" * 2001)

        ctx.message.delete.assert_not_awaited()
        self.assertIn("2000", ctx.send.await_args.args[0])

    async def test_text_of_exactly_2000_characters_is_accepted(self):
        ctx = make_context()

        await main.say.callback(ctx, text="x" * 2000)

        ctx.message.delete.assert_awaited_once_with()
        self.assertEqual(ctx.send.await_args.args[0], "x" * 2000)

    async def test_unauthorized_user_is_rejected(self):
        ctx = make_context(role_ids=())

        await main.say.callback(ctx, text="not allowed")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("ليس لديك صلاحية", ctx.send.await_args.args[0])

    async def test_missing_manage_messages_permission_is_reported(self):
        ctx = make_context(
            permissions=discord.Permissions(send_messages=True, manage_messages=False)
        )

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("Manage Messages", ctx.send.await_args.args[0])

    async def test_forbidden_delete_is_reported_without_stopping_handler(self):
        ctx = make_context()
        forbidden = discord.Forbidden(
            response=SimpleNamespace(status=403, reason="Forbidden"),
            message="missing permission",
        )
        ctx.message.delete.side_effect = forbidden

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_awaited_once_with()
        self.assertIn("تعذر حذف", ctx.send.await_args.args[0])

    async def test_missing_say_argument_has_clear_error(self):
        ctx = make_context()
        ctx.command = main.say
        error = commands.MissingRequiredArgument(main.say.clean_params["text"])

        await main.on_command_error(ctx, error)

        self.assertIn("النص فارغ", ctx.send.await_args.args[0])

    async def test_unexpected_command_error_is_reported(self):
        ctx = make_context()

        await main.on_command_error(ctx, commands.CommandError("simulated failure"))

        self.assertIn("خطأ غير متوقع", ctx.send.await_args.args[0])

    async def test_unknown_command_is_ignored(self):
        ctx = make_context()

        await main.on_command_error(ctx, commands.CommandNotFound("!unknown"))

        ctx.send.assert_not_awaited()


class PingCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_reports_online_status_latency_and_disables_mentions(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(latency=0.1234)

        with patch.object(main, "bot", fake_bot):
            await main.ping.callback(ctx)

        response = ctx.send.await_args.args[0]
        self.assertIn("حالة البوت: متصل", response)
        self.assertIn("123 ms", response)
        self.assertEqual(
            ctx.send.await_args.kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
