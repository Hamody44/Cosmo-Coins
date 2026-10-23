import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.admin import Admin


class StaffRoleCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.guild_db_path = os.path.join(self.temp_dir.name, "guild.db")
        with sqlite3.connect(self.guild_db_path) as conn:
            conn.execute(
                "CREATE TABLE settings ("
                "guild_id INTEGER PRIMARY KEY, owner_role_id INTEGER, admin_role_id INTEGER)"
            )
            conn.execute(
                "INSERT INTO settings (guild_id, owner_role_id, admin_role_id) VALUES (111, 501, 502)"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def guild_db(self, _guild_id):
        conn = sqlite3.connect(self.guild_db_path)
        return conn, conn.cursor()

    def context(self, *, owner=True, administrator=False, manage_guild=False):
        guild = SimpleNamespace(
            id=111,
            owner_id=200 if owner else 999,
            get_role=lambda _role_id: None,
        )
        author = SimpleNamespace(
            id=200,
            roles=[],
            guild_permissions=SimpleNamespace(
                administrator=administrator,
                manage_guild=manage_guild,
            ),
        )
        return SimpleNamespace(guild=guild, author=author, send=AsyncMock())

    async def test_success_saves_staff_role_syncs_tickets_and_replies_with_mention(self):
        ctx = self.context(owner=False, administrator=True)
        role = SimpleNamespace(id=503, mention="<@&503>")
        cog = object.__new__(Admin)
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(501, 502)), \
             patch("cogs.admin.get_guild_db", side_effect=self.guild_db), \
             patch("cogs.admin.sync_ticket_staff_roles", new_callable=AsyncMock) as sync_roles:
            await Admin.set_staff_role.callback(cog, ctx, role)

        with sqlite3.connect(self.guild_db_path) as conn:
            saved = conn.execute(
                "SELECT owner_role_id, admin_role_id FROM settings WHERE guild_id = 111"
            ).fetchone()
        self.assertEqual(saved, (501, 503))
        sync_roles.assert_awaited_once_with(
            ctx.guild,
            previous_role_ids=(502,),
            active_role_ids=(501, 503),
        )
        ctx.send.assert_awaited_once()
        response = ctx.send.await_args.args[0]
        self.assertIn("✅", response)
        self.assertIn("<@&503>", response)
        self.assertIn("التذاكر", response)
        self.assertIn("الراتب", response)

    async def test_owner_role_cannot_be_reused_as_limited_staff_role(self):
        ctx = self.context()
        owner_role = SimpleNamespace(id=501, mention="<@&501>")
        cog = object.__new__(Admin)
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(501, 502)), \
             patch("cogs.admin.get_guild_db") as get_guild_db, \
             patch("cogs.admin.sync_ticket_staff_roles", new_callable=AsyncMock) as sync_roles:
            await Admin.set_staff_role.callback(cog, ctx, owner_role)

        get_guild_db.assert_not_called()
        sync_roles.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertIn("مختلفة عن رتبة owner", ctx.send.await_args.args[0])
        with sqlite3.connect(self.guild_db_path) as conn:
            saved = conn.execute(
                "SELECT admin_role_id FROM settings WHERE guild_id = 111"
            ).fetchone()[0]
        self.assertEqual(saved, 502)

    async def test_unauthorized_member_gets_denial_without_database_change(self):
        ctx = self.context(owner=False)
        cog = object.__new__(Admin)
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(501, 502)), \
             patch("cogs.admin.is_bot_admin", return_value=False), \
             patch("cogs.admin.get_guild_db") as get_guild_db, \
             patch("cogs.admin.sync_ticket_staff_roles", new_callable=AsyncMock) as sync_roles:
            await Admin.set_staff_role.callback(
                cog,
                ctx,
                SimpleNamespace(id=503, mention="<@&503>"),
            )

        get_guild_db.assert_not_called()
        sync_roles.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertIn("Administrator/Manage Server", ctx.send.await_args.args[0])
        with sqlite3.connect(self.guild_db_path) as conn:
            saved = conn.execute(
                "SELECT admin_role_id FROM settings WHERE guild_id = 111"
            ).fetchone()[0]
        self.assertEqual(saved, 502)


if __name__ == "__main__":
    unittest.main()
