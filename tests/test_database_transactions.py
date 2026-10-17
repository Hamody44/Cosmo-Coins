import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

import database


class FinancialTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.GLOBAL_DB_PATH = os.path.join(self.temp_dir.name, "global.db")
        with database.sqlite3.connect(database.GLOBAL_DB_PATH) as conn:
            conn.executescript(
                """
                CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0);
                CREATE TABLE transfer_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER, receiver_id INTEGER, amount INTEGER, timestamp TEXT
                );
                CREATE TABLE exchange_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER, user_id INTEGER, cosmo_amount INTEGER,
                    target_currency TEXT, received_amount REAL, note TEXT,
                    status TEXT, timestamp TEXT
                );
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def balance(self, user_id):
        with database.sqlite3.connect(database.GLOBAL_DB_PATH) as conn:
            row = conn.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,)).fetchone()
            return row[0] if row else 0

    def test_transfer_is_atomic_and_never_overdraws(self):
        database.update_balance(10, 100)
        self.assertTrue(database.transfer_balance(10, 20, 70, "now"))
        self.assertEqual(self.balance(10), 30)
        self.assertEqual(self.balance(20), 70)
        self.assertFalse(database.transfer_balance(10, 20, 40, "later"))
        self.assertEqual(self.balance(10), 30)
        with database.sqlite3.connect(database.GLOBAL_DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transfer_logs").fetchone()[0], 1)

    def test_exchange_creation_debits_and_records_in_one_operation(self):
        database.update_balance(11, 250)
        request_id = database.create_exchange_request(99, 11, 200, "Gems", 20.0, "test", "now")
        self.assertIsNotNone(request_id)
        self.assertTrue(database.has_pending_exchange_request(11))
        self.assertEqual(self.balance(11), 50)
        self.assertIsNone(database.create_exchange_request(99, 11, 20, "Gems", 2.0, "test", "later"))
        self.assertEqual(self.balance(11), 50)
        with database.sqlite3.connect(database.GLOBAL_DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM exchange_logs").fetchone()[0], 1)

    def test_exchange_rejection_refunds_exactly_once(self):
        database.update_balance(12, 300)
        request_id = database.create_exchange_request(99, 12, 120, "Gems", 12.0, "test", "now")
        self.assertEqual(self.balance(12), 180)
        self.assertEqual(database.deny_exchange_request(request_id), (12, 120))
        self.assertIsNone(database.deny_exchange_request(request_id))
        self.assertFalse(database.has_pending_exchange_request(12))
        self.assertEqual(self.balance(12), 300)

    def test_acceptance_is_idempotent_and_rejection_cannot_follow(self):
        database.update_balance(13, 100)
        request_id = database.create_exchange_request(99, 13, 10, "Gems", 1.0, "test", "now")
        self.assertTrue(database.accept_exchange_request(request_id))
        self.assertFalse(database.accept_exchange_request(request_id))
        self.assertIsNone(database.deny_exchange_request(request_id))
        self.assertEqual(self.balance(13), 90)

    def test_direct_negative_balance_update_is_rejected(self):
        database.update_balance(14, 5)
        with self.assertRaises(ValueError):
            database.update_balance(14, -6)
        self.assertEqual(self.balance(14), 5)

    def test_concurrent_transfers_cannot_overdraw(self):
        database.update_balance(15, 100)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(
                lambda index: database.transfer_balance(15, 100 + index, 20, str(index)),
                range(10),
            ))
        self.assertEqual(sum(results), 5)
        self.assertEqual(self.balance(15), 0)
        with database.sqlite3.connect(database.GLOBAL_DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transfer_logs").fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()
