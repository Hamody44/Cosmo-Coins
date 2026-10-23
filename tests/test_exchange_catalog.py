import os
import tempfile
import time
import unittest

import database


class ExchangeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        os.makedirs("guilds_data", exist_ok=True)

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def test_currency_label_is_server_scoped_and_requires_active_premium(self):
        conn, cur = database.get_guild_db(101)
        cur.execute(
            "INSERT INTO premium_info (guild_id, premium_end, custom_currency_name) VALUES (?, ?, ?)",
            (101, int(time.time()) + 3600, "Stars"),
        )
        conn.commit()
        conn.close()

        conn, cur = database.get_guild_db(202)
        cur.execute(
            "INSERT INTO premium_info (guild_id, premium_end, custom_currency_name) VALUES (?, ?, ?)",
            (202, int(time.time()) - 1, "ExpiredName"),
        )
        conn.commit()
        conn.close()

        self.assertEqual(database.get_currency_label(101), "Stars")
        self.assertEqual(database.get_currency_label(202), "Cosmo")
        self.assertEqual(database.get_currency_label(303), "Cosmo")

    def test_exchange_currency_catalog_reads_only_cosmo_guild_database(self):
        conn, cur = database.get_guild_db(database.COSMO_GUILD_ID)
        cur.execute(
            "INSERT INTO exchange_currencies (currency_name, exchange_rate, is_active, min_received) VALUES (?, ?, ?, ?)",
            ("Gems", 2.5, 1, 10),
        )
        conn.commit()
        conn.close()

        conn, cur = database.get_guild_db(303)
        cur.execute(
            "INSERT INTO exchange_currencies (currency_name, exchange_rate, is_active, min_received) VALUES (?, ?, ?, ?)",
            ("LocalOnly", 99, 1, 0),
        )
        conn.commit()
        conn.close()

        self.assertEqual(database.get_exchange_currency("gEmS"), ("Gems", 2.5, 1, 10.0))
        self.assertIsNone(database.get_exchange_currency("LocalOnly"))
        self.assertEqual([row[0] for row in database.get_exchange_currencies(active_only=True)], ["Gems"])


if __name__ == "__main__":
    unittest.main()
