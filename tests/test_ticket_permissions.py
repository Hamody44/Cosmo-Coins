import os
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import database
from cogs.tickets import TicketControlView, Tickets, can_manage_ticket_member, serialize_ticket_creation


class TicketPermissionTests(unittest.TestCase):
    def make_guild_member(self, user_id, *, owner_id=100, admin=False, manage_guild=False, roles=(), role_map=None):
        guild = SimpleNamespace(
            owner_id=owner_id,
            get_role=lambda role_id: (role_map or {}).get(role_id),
        )
        member = SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
            roles=list(roles),
        )
        return guild, member

    def test_server_owner_can_manage_tickets_without_premium(self):
        guild, owner = self.make_guild_member(100)
        self.assertTrue(can_manage_ticket_member(guild, owner))

    def test_server_administrator_can_manage_tickets_without_premium(self):
        guild, admin = self.make_guild_member(200, admin=True)
        self.assertTrue(can_manage_ticket_member(guild, admin))

    def test_configured_ticket_admin_role_works_without_premium(self):
        role = object()
        guild, staff = self.make_guild_member(200, roles=[role], role_map={55: role})
        self.assertTrue(can_manage_ticket_member(guild, staff, admin_role_id=55))

    def test_unprivileged_member_cannot_manage_tickets(self):
        guild, member = self.make_guild_member(200)
        self.assertFalse(can_manage_ticket_member(guild, member))

    def test_ticket_log_setting_is_isolated_per_guild_database(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                os.mkdir("guilds_data")
                conn_a, cur_a = database.get_guild_db(111)
                cur_a.execute("INSERT INTO settings (guild_id, ticket_log) VALUES (?, ?)", (111, 1001))
                conn_a.commit()
                conn_a.close()

                conn_b, cur_b = database.get_guild_db(222)
                cur_b.execute("INSERT INTO settings (guild_id, ticket_log) VALUES (?, ?)", (222, 2002))
                conn_b.commit()
                conn_b.close()

                conn_c, cur_c = database.get_guild_db(333)
                cur_c.execute(
                    "INSERT INTO settings (guild_id, admin_role_id) VALUES (?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET admin_role_id = excluded.admin_role_id",
                    (333, 3003),
                )
                conn_c.commit()
                cur_c.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (333,))
                self.assertEqual(cur_c.fetchone()[0], 3003)
                conn_c.close()

                conn_a, cur_a = database.get_guild_db(111)
                cur_a.execute("SELECT ticket_log FROM settings WHERE guild_id = ?", (111,))
                self.assertEqual(cur_a.fetchone()[0], 1001)
                conn_a.close()

                conn_b, cur_b = database.get_guild_db(222)
                cur_b.execute("SELECT ticket_log FROM settings WHERE guild_id = ?", (222,))
                self.assertEqual(cur_b.fetchone()[0], 2002)
                conn_b.close()
            finally:
                os.chdir(original_cwd)


class TicketRenamePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_owner_without_admin_cannot_rename(self):
        cursor = SimpleNamespace(execute=lambda *_args: None, fetchone=lambda: (333,))
        conn = SimpleNamespace(close=lambda: None)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=111),
            channel=SimpleNamespace(id=222, edit=AsyncMock()),
            author=SimpleNamespace(id=333),
            send=AsyncMock(),
        )
        with patch("cogs.tickets.get_guild_db", return_value=(conn, cursor)), patch(
            "cogs.tickets.can_manage_ticket_settings", return_value=False
        ):
            await Tickets.rename_ticket.callback(Tickets(None), ctx, "renamed-ticket")

        ctx.channel.edit.assert_not_awaited()
        ctx.send.assert_awaited_once_with("⛔ إعادة تسمية التذكرة متاحة لمالك السيرفر أو owner-role فقط.")

    async def test_limited_admin_role_cannot_rename(self):
        cursor = SimpleNamespace(execute=lambda *_args: None, fetchone=lambda: (1,))
        conn = SimpleNamespace(close=lambda: None)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=111),
            channel=SimpleNamespace(id=222, edit=AsyncMock()),
            author=SimpleNamespace(id=444),
            send=AsyncMock(),
        )
        with patch("cogs.tickets.get_guild_db", return_value=(conn, cursor)), patch(
            "cogs.tickets.can_manage_ticket_settings", return_value=False
        ):
            await Tickets.rename_ticket.callback(Tickets(None), ctx, "renamed ticket")

        ctx.channel.edit.assert_not_awaited()
        ctx.send.assert_awaited_once_with("⛔ إعادة تسمية التذكرة متاحة لمالك السيرفر أو owner-role فقط.")

    async def test_owner_role_can_rename(self):
        cursor = SimpleNamespace(execute=lambda *_args: None, fetchone=lambda: (1,))
        conn = SimpleNamespace(close=lambda: None)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=111),
            channel=SimpleNamespace(id=222, edit=AsyncMock()),
            author=SimpleNamespace(id=444),
            send=AsyncMock(),
        )
        with patch("cogs.tickets.get_guild_db", return_value=(conn, cursor)), patch(
            "cogs.tickets.can_manage_ticket_settings", return_value=True
        ):
            await Tickets.rename_ticket.callback(Tickets(None), ctx, "renamed ticket")

        ctx.channel.edit.assert_awaited_once_with(name="renamed-ticket")
        ctx.send.assert_awaited_once()


class TicketAddPersonTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, member_id_or_mention="123"):
        added_member = SimpleNamespace(id=123, mention="<@123>")
        guild = SimpleNamespace(
            id=111,
            get_member=Mock(return_value=None),
            fetch_member=AsyncMock(return_value=added_member),
        )
        channel = SimpleNamespace(
            id=222,
            overwrites_for=Mock(return_value=discord.PermissionOverwrite()),
            set_permissions=AsyncMock(),
        )
        ctx = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=SimpleNamespace(id=444, name="Admin"),
            send=AsyncMock(),
        )
        conn = SimpleNamespace(close=Mock())
        cursor = SimpleNamespace(execute=Mock(), fetchone=Mock(return_value=(0,)))
        return ctx, added_member, conn, cursor

    async def test_non_admin_cannot_add_person(self):
        ctx, _member, conn, cursor = self.make_context()
        with patch("cogs.tickets.can_manage_tickets", return_value=False), patch(
            "cogs.tickets.get_guild_db", return_value=(conn, cursor)
        ) as get_db:
            await Tickets.add_person.callback(Tickets(None), ctx, "123")

        get_db.assert_not_called()
        ctx.channel.set_permissions.assert_not_awaited()
        ctx.send.assert_awaited_once_with("⛔ هذا الأمر متاح لمالك السيرفر أو مسؤولي التذاكر فقط.")

    async def test_ticket_admin_adds_member_by_id_and_fetches_uncached_member(self):
        ctx, member, conn, cursor = self.make_context()
        with patch("cogs.tickets.can_manage_tickets", return_value=True), patch(
            "cogs.tickets.get_guild_db", return_value=(conn, cursor)
        ):
            await Tickets.add_person.callback(Tickets(None), ctx, "123")

        cursor.execute.assert_called_once_with(
            "SELECT is_closed FROM tickets WHERE channel_id = ?", (222,)
        )
        ctx.guild.fetch_member.assert_awaited_once_with(123)
        ctx.channel.set_permissions.assert_awaited_once()
        permissions = ctx.channel.set_permissions.await_args.kwargs["overwrite"]
        self.assertTrue(permissions.view_channel)
        self.assertTrue(permissions.send_messages)
        ctx.send.assert_awaited_once_with("✅ تمت إضافة <@123> إلى التذكرة.")

    async def test_ticket_admin_can_add_member_using_discord_mention(self):
        ctx, member, conn, cursor = self.make_context("<@!123>")
        with patch("cogs.tickets.can_manage_tickets", return_value=True), patch(
            "cogs.tickets.get_guild_db", return_value=(conn, cursor)
        ):
            await Tickets.add_person.callback(Tickets(None), ctx, "<@!123>")

        ctx.guild.fetch_member.assert_awaited_once_with(123)
        ctx.channel.set_permissions.assert_awaited_once()

    async def test_invalid_id_and_closed_ticket_do_not_change_permissions(self):
        ctx, _member, conn, cursor = self.make_context()
        with patch("cogs.tickets.can_manage_tickets", return_value=True), patch(
            "cogs.tickets.get_guild_db", return_value=(conn, cursor)
        ):
            await Tickets.add_person.callback(Tickets(None), ctx, "<@&123>")
            self.assertEqual(ctx.send.await_args.args[0], "❌ أدخل ID عضو صالحاً أو منشنه داخل السيرفر.")
            cursor.fetchone.return_value = (1,)
            ctx.send.reset_mock()
            await Tickets.add_person.callback(Tickets(None), ctx, "123")

        self.assertEqual(ctx.send.await_args.args[0], "⚠️ لا يمكن إضافة عضو إلى تذكرة مغلقة.")
        ctx.channel.set_permissions.assert_not_awaited()
        ctx.guild.fetch_member.assert_not_awaited()


class TicketReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_creation_lock_serializes_same_user(self):
        active = 0
        max_active = 0

        @serialize_ticket_creation
        async def callback(_view, _interaction, _button):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=70001),
            user=SimpleNamespace(id=80001),
        )
        await asyncio.gather(
            callback(None, interaction, None),
            callback(None, interaction, None),
        )
        self.assertEqual(max_active, 1)

    async def test_delete_button_preserves_database_row_if_discord_delete_fails(self):
        cursor = SimpleNamespace(
            execute=Mock(),
            fetchone=Mock(side_effect=[(None, None), (123, "opened", None), (None,)]),
        )
        conn = SimpleNamespace(close=Mock())

        async def empty_history(**_kwargs):
            if False:
                yield None

        channel = SimpleNamespace(
            id=456,
            name="ticket-test",
            history=empty_history,
            delete=AsyncMock(side_effect=RuntimeError("Discord delete failed")),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=70002, name="Guild", get_channel=Mock(return_value=None)),
            channel=channel,
            user=SimpleNamespace(id=999, name="Staff", mention="<@999>"),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with patch("cogs.tickets.get_guild_db", return_value=(conn, cursor)), \
             patch("cogs.tickets.can_manage_ticket_member", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Discord delete failed"):
                await TicketControlView.btn_delete(TicketControlView(), interaction, SimpleNamespace())

        self.assertFalse(any(
            "DELETE FROM tickets" in call.args[0]
            for call in cursor.execute.call_args_list
        ))
        conn.close.assert_called_once()
        channel.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
