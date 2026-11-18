import os
import importlib.util
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


class _FakeChat:
    async def send_message(self, prompt):
        return SimpleNamespace(text=prompt)


class _FakeChats:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        chat = _FakeChat()
        self.created.append((kwargs, chat))
        return chat


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
        chats = _FakeChats()
        cog = object.__new__(GeminiAI)
        cog.client = SimpleNamespace(aio=SimpleNamespace(chats=chats))
        cog.backup_client = None
        cog.user_chats = {}

        first = await cog.generate_response(7, "أول", 100, 200)
        second = await cog.generate_response(7, "ثاني", 100, 201)
        third = await cog.generate_response(7, "ثالث", 101, 200)
        repeated = await cog.generate_response(7, "متابعة", 100, 200)

        self.assertEqual((first, second, third, repeated), ("أول", "ثاني", "ثالث", "متابعة"))
        self.assertEqual(len(chats.created), 3)
        self.assertEqual(set(cog.user_chats), {(100, 200, 7), (100, 201, 7), (101, 200, 7)})

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
