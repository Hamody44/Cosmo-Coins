import os
import tempfile
import unittest
from types import SimpleNamespace

import database
from cogs.tickets import can_manage_ticket_member


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


if __name__ == "__main__":
    unittest.main()
