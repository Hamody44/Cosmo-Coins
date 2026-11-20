import os
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

import database

try:
    has_google_genai = importlib.util.find_spec("google.genai") is not None
except (ImportError, ModuleNotFoundError):
    has_google_genai = False

if not has_google_genai:
    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    genai_types_module = types.ModuleType("google.genai.types")
    genai_types_module.GenerateContentConfig = lambda **kwargs: kwargs
    genai_module.types = genai_types_module
    google_module.genai = genai_module
    sys.modules.setdefault("google", google_module)
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = genai_types_module

from cogs.ai import GeminiAI


class AISafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_member_cannot_invoke_admin_command_through_ai(self):
        invoked = False

        async def restricted_command(ctx):
            nonlocal invoked
            invoked = True

        command = commands.Command(
            restricted_command,
            name="set_exchange_room",
            checks=[lambda _ctx: False],
        )

        class FakeBot:
            def get_command(self, name):
                return command if name == "set_exchange_room" else None

            async def get_context(self, message):
                return SimpleNamespace(command=command, bot=self)

            async def can_run(self, _ctx):
                return True

            async def invoke(self, _ctx):
                nonlocal invoked
                invoked = True

        cog = object.__new__(GeminiAI)
        cog.bot = FakeBot()
        ctx = SimpleNamespace(
            message=SimpleNamespace(content="!ask غيّر روم الطلبات"),
            author=SimpleNamespace(id=123456),
            send=AsyncMock(),
        )

        result = await cog.invoke_existing_command(ctx, "!set_exchange_room <#987654>")

        self.assertTrue(result)
        self.assertFalse(invoked)
        ctx.send.assert_awaited_once_with("⛔ لا تملك الصلاحية المطلوبة لتنفيذ هذا الأمر.")

    async def test_ai_toggle_persists_when_settings_row_does_not_exist(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                os.makedirs("guilds_data", exist_ok=True)
                guild_id = 99001
                conn, cur = database.get_guild_db(guild_id)
                cur.execute("DELETE FROM settings WHERE guild_id = ?", (guild_id,))
                conn.commit()
                conn.close()

                ctx = SimpleNamespace(guild=SimpleNamespace(id=guild_id), send=AsyncMock())
                cog = object.__new__(GeminiAI)
                await GeminiAI.ai_toggle.callback(
                    cog, ctx, discord.app_commands.Choice(name="تعطيل", value="disable")
                )

                self.assertFalse(cog.is_ai_enabled(guild_id))
                ctx.send.assert_awaited_once()
            finally:
                os.chdir(original_cwd)

    async def test_chat_context_is_separated_by_guild_channel_and_user(self):
        cog = object.__new__(GeminiAI)
        cog.client = object()
        cog.cerebras_client = None
        cog.backup_client = None
        cog.ask_gemini_ai = AsyncMock(side_effect=("أول", "ثاني", "ثالث", "متابعة"))
        cog.user_chats = {}

        first = await cog.generate_response(7, "أول", 100, 200)
        second = await cog.generate_response(7, "ثاني", 100, 201)
        third = await cog.generate_response(7, "ثالث", 101, 200)
        repeated = await cog.generate_response(7, "متابعة", 100, 200)

        self.assertEqual((first, second, third, repeated), ("أول", "ثاني", "ثالث", "متابعة"))
        self.assertEqual(cog.ask_gemini_ai.await_count, 4)
        self.assertEqual(set(cog.user_chats), {(100, 200, 7), (100, 201, 7), (101, 200, 7)})

    def test_project_read_access_includes_all_files_except_env_and_venv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            included = {
                "second_bot/settings.yaml": "needle second bot settings\nإعداد روم التسليمات",
                "guilds_data/example.sqlite": "needle database contents",
                "assets/blob.bin": "needle arbitrary extension",
                "cogs/example.py": "needle source code",
            }
            excluded = {
                ".env": "secret",
                "config/.env.production": "secret",
                ".venv/lib/module.py": "private virtual env",
                "tools/venv/lib/module.py": "private virtual env",
            }
            for relative, contents in {**included, **excluded}.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")

            cog = object.__new__(GeminiAI)
            with patch("cogs.ai.PRIMARY_REPO_ROOT", root):
                for relative in included:
                    self.assertTrue(GeminiAI.is_readable_project_file(root / relative))
                for relative in excluded:
                    self.assertFalse(GeminiAI.is_readable_project_file(root / relative))
                inventory = cog.project_file_inventory()
                context = cog.project_file_context("needle")
                arabic_context = cog.project_file_context("روم التسليمات")
                self.assertFalse(GeminiAI.is_primary_code_path(root / "second_bot/settings.yaml"))
                self.assertFalse(GeminiAI.is_primary_code_path(root / "guilds_data/example.sqlite"))

            for relative in included:
                self.assertIn(relative, inventory)
            for relative in excluded:
                self.assertNotIn(relative, inventory)
            for marker in ("secret", "private virtual env"):
                self.assertNotIn(marker, context)
            self.assertIn("needle arbitrary extension", context)
            self.assertIn("إعداد روم التسليمات", arabic_context)

    async def test_server_context_hides_configured_channels_without_view_permission(self):
        visible = SimpleNamespace(
            name="general", id=1001,
            permissions_for=lambda _member: SimpleNamespace(view_channel=True),
        )
        hidden = SimpleNamespace(
            name="private-deliveries", id=987654321,
            permissions_for=lambda _member: SimpleNamespace(view_channel=False),
        )
        channels = {visible.id: visible, hidden.id: hidden}
        guild = SimpleNamespace(
            id=123, name="Example Guild", text_channels=[visible, hidden],
            get_channel=lambda channel_id: channels.get(channel_id),
        )
        settings = {key: None for key in (
            "transfer_log", "exchange_request_log", "exchange_success_log", "delivery_log",
            "exchange_room", "balance_log", "ticket_log", "invite_log",
            "boost_log_channel", "announcement_channel_id",
        )}
        settings["delivery_log"] = hidden.id

        with patch.object(GeminiAI, "_read_server_configuration", return_value=(settings, [])):
            context = await object.__new__(GeminiAI).get_server_data_context(guild, object())

        self.assertIn("#general", context)
        self.assertIn("روم التسليمات وإثباتات التسليم", context)
        self.assertIn("مضبوط على قناة لا يملك صاحب السؤال صلاحية رؤيتها", context)
        self.assertNotIn("private-deliveries", context)
        self.assertNotIn(str(hidden.id), context)

    async def test_reset_chat_clears_only_current_conversation(self):
        cog = object.__new__(GeminiAI)
        cog.user_chats = {
            (100, 200, 7): object(),
            (100, 201, 7): object(),
        }
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=7),
            send=AsyncMock(),
        )

        await GeminiAI.reset_chat.callback(cog, ctx)

        self.assertNotIn((100, 200, 7), cog.user_chats)
        self.assertIn((100, 201, 7), cog.user_chats)
        ctx.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
