import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import database
import premium_payments as pp


class PremiumPaymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.global_db = os.path.join(self.tmp.name, "global.db")
        self.guild_db = os.path.join(self.tmp.name, "guild.db")
        self.old_path = database.GLOBAL_DB_PATH
        database.GLOBAL_DB_PATH = self.global_db
        with sqlite3.connect(self.global_db) as c:
            c.execute("CREATE TABLE global_settings(key TEXT PRIMARY KEY, value INTEGER)")
            c.execute("CREATE TABLE balances(user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)")
        with sqlite3.connect(self.guild_db) as c:
            c.executescript("""
                CREATE TABLE premium_info(guild_id INTEGER PRIMARY KEY, activated_at INTEGER DEFAULT 0, premium_end INTEGER DEFAULT 0);
                CREATE TABLE budget_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, amount INTEGER, old_balance INTEGER, new_balance INTEGER, admin_id INTEGER, timestamp TEXT);
            """)
        self.patch = patch.object(database, "get_guild_db", side_effect=lambda _gid: (sqlite3.connect(self.guild_db, timeout=10), None))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.cleanup)
        pp.set_purchase_enabled(True)
        for method in pp.PREMIUM_METHODS:
            pp.set_method_enabled(method, True)
        pp.set_price("30d", 30 * 86400, {"credit": 100, "adamc": 90, "cosmo": 1000, "crypto_usd": "2.50"})

    def cleanup(self):
        database.GLOBAL_DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_price_and_global_method_settings(self):
        price = pp.get_price("30d")
        self.assertEqual(price["duration_seconds"], 30 * 86400)
        self.assertEqual(price["crypto_usd"], pp.Decimal("2.50"))
        pp.set_method_enabled("adamc", False)
        self.assertFalse(pp.method_enabled("adamc"))
        self.assertIsNone(pp.create_order(1, 2, 3, "adamc", "30d", 30 * 86400, 90, created_at=100, expires_at=200))

    def test_credit_settlement_activates_once_and_rejects_wrong_identity(self):
        order = pp.create_order(1, 2, 3, "credit", "30d", 30 * 86400, 100, created_at=100, expires_at=200, payer_name="payer")
        self.assertIsNotNone(order)
        self.assertIsNone(pp.settle_receipt(1, "credit", 99, "payer", 100, 50, database.PROBOT_PAYMENT_BOT_ID, 150, database.PRIMARY_OWNER_ID))
        self.assertIsNone(pp.settle_receipt(1, "credit", 3, "another-user", 100, 49, database.PROBOT_PAYMENT_BOT_ID, 150, database.PRIMARY_OWNER_ID))
        result = pp.settle_receipt(1, "credit", 3, "payer", 100, 50, database.PROBOT_PAYMENT_BOT_ID, 150, database.PRIMARY_OWNER_ID)
        self.assertEqual(result["status"], "paid")
        duplicate = pp.settle_receipt(1, "credit", 3, "payer", 100, 50, database.PROBOT_PAYMENT_BOT_ID, 150, database.PRIMARY_OWNER_ID)
        self.assertIsNone(duplicate)
        with sqlite3.connect(self.guild_db) as c:
            self.assertEqual(c.execute("SELECT status FROM premium_payment_orders").fetchone()[0], "paid")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM premium_info").fetchone()[0], 1)

    def test_cosmo_is_atomic_on_insufficient_balance_and_extends_existing_end(self):
        with sqlite3.connect(self.global_db) as c:
            c.execute("INSERT INTO balances VALUES(2, 500)")
        now = int(time.time())
        order = pp.create_order(1, 2, 3, "cosmo", "30d", 30 * 86400, 1000, created_at=now, expires_at=now + 600)
        self.assertIsNone(pp.settle_cosmo(1, 2, 3, order["order_id"]))
        with sqlite3.connect(self.global_db) as c:
            self.assertEqual(c.execute("SELECT balance FROM balances WHERE user_id=2").fetchone()[0], 500)
        with sqlite3.connect(self.guild_db) as c:
            c.execute("INSERT INTO premium_info VALUES(1, 100, 200)")
        with sqlite3.connect(self.global_db) as c:
            c.execute("UPDATE balances SET balance=2000 WHERE user_id=2")
        result = pp.settle_cosmo(1, 2, 3, order["order_id"])
        self.assertEqual(result["status"], "paid")
        self.assertGreater(result["premium_end"], 200)

    def test_crypto_webhook_full_payment_and_duplicate(self):
        order = pp.create_order(1, 2, 3, "crypto", "30d", 30 * 86400, quote_usd="2.50", created_at=100, expires_at=200)
        provider = order["provider_order_id"]
        self.assertTrue(pp.attach_invoice(1, order["order_id"], {"payment_id":"inv1", "expires_at":200}))
        event = {"type":"InvoiceSettled", "invoiceId":"inv1", "storeId":"store"}
        invoice = {"id":"inv1", "storeId":"store", "status":"Settled", "amount":"2.50", "paidAmount":"2.50", "metadata":{"orderId":provider}}
        result = pp.process_webhook(event, invoice)
        self.assertEqual(result["status"], "paid")
        self.assertEqual(pp.process_webhook(event, invoice)["status"], "duplicate")


if __name__ == "__main__":
    unittest.main()
