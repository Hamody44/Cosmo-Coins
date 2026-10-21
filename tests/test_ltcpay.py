import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import database
import ltcpay


class LTCPayClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "LTCPAY_URL": "https://pay.example.test",
                "LTCPAY_STORE_ID": "Store123",
                "LTCPAY_API_KEY": "test-api-key",
                "LTCPAY_WEBHOOK_SECRET": "test-webhook-secret",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_budget_rate_and_minimum_are_exact(self):
        self.assertEqual(ltcpay.usd_for_budget(195_000_000), Decimal("0.75000000"))
        self.assertEqual(ltcpay.usd_for_budget(26_000_000), Decimal("0.10000000"))
        self.assertEqual(ltcpay.MIN_CRYPTO_BUDGET, 26_000_000)
        with self.assertRaises(ltcpay.LTCPayError):
            ltcpay.usd_for_budget(25_999_999)

    def test_configuration_requires_store_credentials_and_webhook_secret(self):
        self.assertTrue(ltcpay.is_configured())
        with patch.dict(os.environ, {"LTCPAY_WEBHOOK_SECRET": ""}):
            self.assertFalse(ltcpay.is_configured())
        with patch.dict(os.environ, {"LTCPAY_URL": "http://public.example.test"}):
            self.assertFalse(ltcpay.is_configured())

    def test_create_ltc_invoice_uses_usd_and_ltc_only(self):
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": "Invoice123", "storeId": "Store123", "currency": "USD",
                "amount": "0.75", "checkoutLink": "https://pay.example.test/i/Invoice123",
                "expirationTime": 1_900_000_000, "status": "New",
                "metadata": {"orderId": "cosmo:101:" + "a" * 32, "discord_user_id": "202"},
                "checkout": {"paymentMethods": ["LTC"]},
            },
        )
        with patch("ltcpay.requests.request", return_value=response) as request:
            invoice = ltcpay.create_ltc_invoice(
                price_usd=Decimal("0.75"), provider_order_id="cosmo:101:" + "a" * 32, discord_user_id=202,
            )
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["amount"], "0.75000000")
        self.assertEqual(body["checkout"]["paymentMethods"], ["LTC"])
        self.assertEqual(body["checkout"]["speedPolicy"], "MediumSpeed")
        self.assertEqual(body["metadata"]["discord_user_id"], "202")
        self.assertNotIn("defaultPaymentMethod", body["checkout"])
        self.assertEqual(invoice["payment_id"], "Invoice123")
        self.assertEqual(invoice["pay_address"], "https://pay.example.test/i/Invoice123")
        self.assertEqual(invoice["metadata_order_id"], "cosmo:101:" + "a" * 32)
        self.assertEqual(invoice["metadata_user_id"], "202")
        self.assertTrue(request.call_args.kwargs["headers"]["Authorization"].startswith("token "))

    def test_provider_422_includes_short_validation_reason(self):
        response = SimpleNamespace(
            status_code=422,
            json=lambda: {"message": "No enabled payment method for this store"},
        )
        with patch("ltcpay.requests.request", return_value=response):
            with self.assertRaisesRegex(ltcpay.LTCPayError, "No enabled payment method"):
                ltcpay._api_request("POST", "/api/v1/stores/Store123/invoices", json_body={})

    def test_create_invoice_fails_closed_on_store_amount_metadata_or_checkout_link_mismatch(self):
        wrong_store = {
            "id": "Invoice123", "storeId": "OtherStore", "currency": "USD", "amount": "0.75",
            "checkoutLink": "https://pay.example.test/i/Invoice123", "expirationTime": 1_900_000_000,
            "metadata": {"orderId": "cosmo:101:" + "a" * 32, "discord_user_id": "202"},
            "checkout": {"paymentMethods": ["LTC"]},
        }
        with patch("ltcpay.requests.request", return_value=SimpleNamespace(status_code=200, json=lambda: wrong_store)):
            with self.assertRaises(ltcpay.LTCPayError):
                ltcpay.create_ltc_invoice(price_usd=Decimal("0.75"), provider_order_id="cosmo:101:" + "a" * 32, discord_user_id=202)

        external_link = dict(wrong_store, storeId="Store123", checkoutLink="https://evil.example/i")
        with patch("ltcpay.requests.request", return_value=SimpleNamespace(status_code=200, json=lambda: external_link)):
            with self.assertRaises(ltcpay.LTCPayError):
                ltcpay.create_ltc_invoice(price_usd=Decimal("0.75"), provider_order_id="cosmo:101:" + "a" * 32, discord_user_id=202)

    def test_webhook_signature_verifies_exact_raw_body(self):
        raw = b'{"type":"InvoiceSettled","invoiceId":"Invoice123"}'
        signature = "sha256=" + hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
        self.assertTrue(ltcpay.verify_webhook_signature(raw, signature))
        self.assertFalse(ltcpay.verify_webhook_signature(raw + b" ", signature))
        self.assertFalse(ltcpay.verify_webhook_signature(raw, "sha256=" + "0" * 64))


class LTCPaySettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.guild_db_path = os.path.join(self.temp_dir.name, "guild.db")
        self.global_db_path = os.path.join(self.temp_dir.name, "global.db")
        self.old_global_path = database.GLOBAL_DB_PATH
        database.GLOBAL_DB_PATH = self.global_db_path
        with sqlite3.connect(self.global_db_path) as conn:
            conn.execute("CREATE TABLE global_settings (key TEXT PRIMARY KEY, value INTEGER)")
        with sqlite3.connect(self.guild_db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE settings (guild_id INTEGER PRIMARY KEY, spending_balance INTEGER DEFAULT 0);
                CREATE TABLE budget_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, amount INTEGER,
                    old_balance INTEGER, new_balance INTEGER, admin_id INTEGER, timestamp TEXT
                );
                CREATE TABLE budget_payment_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, payer_name TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    method TEXT NOT NULL, budget_amount INTEGER NOT NULL, expected_payment INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', payment_message_id INTEGER UNIQUE,
                    payment_author_id INTEGER, paid_at INTEGER, provider_order_id TEXT,
                    provider_payment_id TEXT, quote_usd TEXT, pay_address TEXT,
                    pay_currency TEXT, provider_status TEXT
                );
                CREATE UNIQUE INDEX idx_test_provider_order ON budget_payment_orders(provider_order_id);
                CREATE UNIQUE INDEX idx_test_provider_payment ON budget_payment_orders(provider_payment_id);
                """
            )

        def get_test_guild_db(_guild_id):
            conn = sqlite3.connect(self.guild_db_path, timeout=10)
            return conn, conn.cursor()

        self.guild_db_patch = patch.object(database, "get_guild_db", side_effect=get_test_guild_db)
        self.guild_db_patch.start()
        self.addCleanup(self.guild_db_patch.stop)
        self.addCleanup(self._cleanup)
        database.set_auto_budget_payment_enabled("crypto", True)
        self.provider_order_id = "cosmo:101:" + "b" * 32
        self.invoice_id = "Invoice123"
        self.order_id = database.create_crypto_budget_payment_order(
            guild_id=101, user_id=202, payer_name="payer", channel_id=303,
            budget_amount=195_000_000, quote_usd="0.75000000",
            provider_order_id=self.provider_order_id, created_at=1000, expires_at=1600,
        )
        self.assertIsNotNone(self.order_id)
        self.created_invoice = {
            "payment_id": self.invoice_id, "pay_address": "https://pay.example.test/i/Invoice123",
            "pay_currency": "ltc", "provider_status": "New", "expires_at": 1600,
            "amount_usd": Decimal("0.75000000"), "store_id": "Store123",
            "metadata_order_id": self.provider_order_id,
        }
        self.assertTrue(database.update_crypto_budget_payment_order(101, self.order_id, self.provider_order_id, self.created_invoice))

    def _cleanup(self):
        database.GLOBAL_DB_PATH = self.old_global_path
        self.temp_dir.cleanup()

    def event(self, **overrides):
        event = {"type": "InvoiceSettled", "invoiceId": self.invoice_id, "storeId": "Store123"}
        event.update(overrides)
        return event

    def invoice(self, **overrides):
        invoice = {
            "id": self.invoice_id, "storeId": "Store123", "amount": "0.75", "paidAmount": "0.75",
            "currency": "USD", "status": "Settled", "additionalStatus": "None",
            "metadata": {"orderId": self.provider_order_id, "discord_user_id": "202"},
            "checkout": {"paymentMethods": ["LTC"]},
        }
        invoice.update(overrides)
        return invoice

    def test_settled_ltc_invoice_credits_budget_and_history_exactly_once(self):
        processing = database.process_ltcpay_webhook(
            self.event(type="InvoiceProcessing"), self.invoice(status="Processing", paidAmount="0.75")
        )
        self.assertEqual(processing["status"], "processing")
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)

        result = database.process_ltcpay_webhook(self.event(), self.invoice())
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["new_balance"], 195_000_000)
        duplicate = database.process_ltcpay_webhook(self.event(), self.invoice())
        self.assertEqual(duplicate["status"], "duplicate")
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 195_000_000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM budget_payment_orders WHERE id = ?", (self.order_id,)).fetchone()[0], "paid")

    def test_greenfield_ltc_chain_alias_is_accepted_as_the_single_ltc_method(self):
        result = database.process_ltcpay_webhook(
            self.event(), self.invoice(checkout={"paymentMethods": ["LTC-CHAIN"]})
        )
        self.assertEqual(result["status"], "paid")

    def test_nonexact_unexpected_store_or_non_ltc_funded_invoices_go_to_review(self):
        cases = (
            (self.event(), self.invoice(paidAmount="0.70")),
            (self.event(), self.invoice(additionalStatus="PaidOver")),
            (self.event(), self.invoice(checkout={"paymentMethods": ["BTC-CHAIN"]})),
            (self.event(storeId="OtherStore"), self.invoice()),
            (self.event(), self.invoice(metadata={"orderId": "cosmo:102:" + "c" * 32, "discord_user_id": "202"})),
            (self.event(), self.invoice(metadata={"orderId": self.provider_order_id, "discord_user_id": "999"})),
        )
        for event, invoice in cases:
            result = database.process_ltcpay_webhook(event, invoice)
            self.assertTrue(result is None or result["status"] == "needs_review")

        with sqlite3.connect(self.guild_db_path) as conn:
            row = conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()
            self.assertEqual(row[0] if row else 0, 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)

    def test_invoices_below_minimum_and_wrong_usd_quote_are_rejected(self):
        below = database.create_crypto_budget_payment_order(
            101, 204, "payer", 304, 25_999_999, "0.10000000", "cosmo:101:" + "c" * 32, 1000, 1600,
        )
        wrong_quote = database.create_crypto_budget_payment_order(
            101, 204, "payer", 304, 26_000_000, "0.11000000", "cosmo:101:" + "c" * 32, 1000, 1600,
        )
        self.assertIsNone(below)
        self.assertIsNone(wrong_quote)

    def test_expired_invoice_is_never_credited(self):
        result = database.process_ltcpay_webhook(
            self.event(type="InvoiceExpired"), self.invoice(status="Expired", additionalStatus="None")
        )
        self.assertEqual(result["status"], "expired")
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)

    def test_disabling_ltc_blocks_new_orders_but_existing_invoice_can_settle(self):
        database.set_auto_budget_payment_enabled("crypto", False, [101])
        self.assertEqual(database.process_ltcpay_webhook(self.event(), self.invoice())["status"], "paid")
        self.assertIsNone(
            database.create_crypto_budget_payment_order(
                101, 203, "other", 304, 100_000_000, "0.38461539",
                "cosmo:101:" + "c" * 32, 1001, 1601,
            )
        )


class LTCPayWebhookRouteTests(unittest.TestCase):
    def test_valid_signed_event_fetches_canonical_invoice_and_bad_signature_is_rejected(self):
        import dashboard.app as dashboard_app

        raw = json.dumps(
            {"type": "InvoiceSettled", "invoiceId": "Invoice123", "storeId": "Store123"},
            separators=(",", ":"),
        ).encode()
        signature = "sha256=" + hmac.new(b"route-secret", raw, hashlib.sha256).hexdigest()
        with patch.dict(
            os.environ,
            {"LTCPAY_WEBHOOK_SECRET": "route-secret", "LTCPAY_API_KEY": "test", "LTCPAY_STORE_ID": "Store123"},
        ), patch.object(dashboard_app, "active_store_id", return_value="Store123"), patch.object(
            dashboard_app, "get_invoice", return_value={"id": "Invoice123", "storeId": "Store123"}
        ) as fetch_invoice, patch.object(
            dashboard_app.database, "process_ltcpay_webhook", return_value={"status": "processing"}
        ) as process:
            client = dashboard_app.app.test_client()
            response = client.post(
                "/ltcpay/webhook", data=raw, content_type="application/json", headers={"BTCPAY-SIG": signature}
            )
            self.assertEqual(response.status_code, 200)
            fetch_invoice.assert_called_once_with("Invoice123")
            process.assert_called_once()
            bad = client.post(
                "/ltcpay/webhook", data=raw, content_type="application/json", headers={"BTCPAY-SIG": "sha256=" + "0" * 64}
            )
            self.assertEqual(bad.status_code, 401)


if __name__ == "__main__":
    unittest.main()
