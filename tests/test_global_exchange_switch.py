import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import database
from cogs.admin import can_manage_global_exchange


class GlobalExchangeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "guild.db")

        def get_test_guild_db(_guild_id):
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER PRIMARY KEY, exchange_enabled INTEGER DEFAULT 1)"
            )
            conn.commit()
            return conn, conn.cursor()

        self.patch_db = patch.object(database, "get_guild_db", side_effect=get_test_guild_db)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_global_switch_defaults_open_and_ignores_other_guild_local_values(self):
        self.assertTrue(database.is_global_exchange_enabled())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO settings (guild_id, exchange_enabled) VALUES (123, 0)")
        self.assertTrue(database.is_global_exchange_enabled())
        database.set_global_exchange_enabled(False)
        self.assertFalse(database.is_global_exchange_enabled())
        with self.assertRaises(database.ExchangeSystemClosed):
            database.create_exchange_request(123, 456, 10, "Gems", 20.0, "test", "now")
        database.set_global_exchange_enabled(True)
        self.assertTrue(database.is_global_exchange_enabled())

    def test_global_toggle_permission_is_limited_to_cosmo_guild(self):
        permissions = SimpleNamespace(administrator=True, manage_guild=True)
        author = SimpleNamespace(id=555, guild_permissions=permissions)
        guild_admin = SimpleNamespace(id=123, owner_id=777)
        cosmo_admin = SimpleNamespace(id=database.COSMO_GUILD_ID, owner_id=777)
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(None, None)):
            self.assertFalse(can_manage_global_exchange(SimpleNamespace(guild=guild_admin, author=author)))
            self.assertTrue(can_manage_global_exchange(SimpleNamespace(guild=cosmo_admin, author=author)))


if __name__ == "__main__":
    unittest.main()
