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
import nowpayments


class NowPaymentsHelpersTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "NOWPAYMENTS_API_KEY": "test-api-key",
                "NOWPAYMENTS_IPN_SECRET": "test-ipn-secret",
                "NOWPAYMENTS_PAYOUT_ADDRESS": "ltc1qrn9vg3dcz7wuq6auf5xw4swzmuuzuu3wleu23m",
                "NOWPAYMENTS_PAYOUT_CURRENCY": "ltc",
                "NOWPAYMENTS_FEE_PAID_BY_USER": "false",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_budget_rate_and_ltc_atomic_formatting(self):
        self.assertEqual(nowpayments.usd_for_budget(195_000_000), Decimal("0.75000000"))
        self.assertEqual(nowpayments.amount_to_litoshi("0.0125"), 1_250_000)
        self.assertEqual(nowpayments.format_ltc(1_250_000), "0.0125")
        self.assertEqual(nowpayments.format_usd(Decimal("0.75")), "0.75")

    def test_ltc_configuration_requires_api_secrets_and_litecoin_payout(self):
        self.assertTrue(nowpayments.is_configured())
        with patch.dict(os.environ, {"NOWPAYMENTS_PAYOUT_ADDRESS": "bc1-not-litecoin"}):
            self.assertFalse(nowpayments.is_configured())
        with patch.dict(os.environ, {"NOWPAYMENTS_API_KEY": ""}):
            self.assertFalse(nowpayments.is_configured())

    def test_live_minimum_and_estimate_parameters_for_ltc(self):
        responses = [
            SimpleNamespace(status_code=200, json=lambda: {"min_amount": "0.001", "fiat_equivalent": "0.50"}),
            SimpleNamespace(status_code=200, json=lambda: {"estimated_amount": "0.0125"}),
        ]
        with patch("nowpayments.requests.request", side_effect=responses) as request:
            amount = nowpayments.quote_ltc_amount(Decimal("0.75"))

        self.assertEqual(amount, Decimal("0.0125"))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["params"]["currency_from"], "ltc")
        self.assertEqual(request.call_args_list[0].kwargs["params"]["currency_to"], "ltc")
        self.assertEqual(request.call_args_list[1].kwargs["params"]["currency_from"], "usd")
        self.assertEqual(request.call_args_list[1].kwargs["params"]["currency_to"], "ltc")

    def test_below_provider_minimum_fails_before_estimate_or_payment(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"min_amount": "0.01", "fiat_equivalent": "2.50"})
        with patch("nowpayments.requests.request", return_value=response) as request:
            with self.assertRaises(nowpayments.UnderMinimumPayment):
                nowpayments.quote_ltc_amount(Decimal("0.75"))
        request.assert_called_once()

    def test_payment_creation_uses_fixed_rate_and_validates_ltc_network(self):
        response = SimpleNamespace(
            status_code=201,
            json=lambda: {
                "payment_id": "np-1", "payment_status": "waiting", "pay_address": "ltc-address",
                "pay_amount": "0.01250000", "pay_currency": "ltc", "network": "ltc",
                "order_id": "cosmo:101:" + "a" * 32, "valid_until": "2099-10-19T19:10:00Z",
            },
        )
        with patch("nowpayments.requests.request", return_value=response) as request:
            payment = nowpayments.create_ltc_payment(
                price_usd=Decimal("0.75"), estimated_ltc=Decimal("0.0125"),
                provider_order_id="cosmo:101:" + "a" * 32,
                callback_url="https://example.test/nowpayments/ipn",
            )
        body = request.call_args.kwargs["json"]
        self.assertTrue(body["is_fixed_rate"])
        self.assertFalse(body["is_fee_paid_by_user"])
        self.assertEqual(body["pay_currency"], "ltc")
        self.assertEqual(body["payout_currency"], "ltc")
        self.assertEqual(body["payout_address"], "ltc1qrn9vg3dcz7wuq6auf5xw4swzmuuzuu3wleu23m")
        self.assertEqual(payment["pay_amount_litoshi"], 1_250_000)
        self.assertEqual(payment["pay_address"], "ltc-address")

    def test_ipn_hmac_signature_is_verified(self):
        payload = {"payment_id": 7, "payment_status": "finished", "order_id": "cosmo:101:" + "a" * 32}
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        signature = hmac.new(b"test-ipn-secret", canonical.encode(), hashlib.sha512).hexdigest()
        self.assertTrue(nowpayments.verify_ipn_signature(payload, signature))
        self.assertFalse(nowpayments.verify_ipn_signature(payload, "0" * 128))


class NowPaymentsSettlementTests(unittest.TestCase):
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
        self.order_id = database.create_crypto_budget_payment_order(
            guild_id=101, user_id=202, payer_name="payer", channel_id=303,
            budget_amount=195_000_000, expected_litoshi=1_250_000,
            quote_usd="0.75000000", provider_order_id=self.provider_order_id,
            created_at=1000, expires_at=1600,
        )
        self.assertIsNotNone(self.order_id)
        self.payment = {
            "payment_id": "np-123", "pay_address": "ltc-address",
            "pay_amount_litoshi": 1_250_000, "pay_currency": "ltc",
            "payment_status": "waiting", "expires_at": 1600,
        }
        self.assertTrue(database.update_crypto_budget_payment_order(101, self.order_id, self.provider_order_id, self.payment))

    def _cleanup(self):
        database.GLOBAL_DB_PATH = self.old_global_path
        self.temp_dir.cleanup()

    def payload(self, **overrides):
        payload = {
            "payment_id": "np-123", "order_id": self.provider_order_id,
            "payment_status": "finished", "pay_currency": "ltc", "network": "ltc",
            "price_amount": "0.75", "price_currency": "usd", "pay_amount": "0.0125",
            "actually_paid": "0.0125", "pay_address": "ltc-address", "parent_payment_id": None,
            "outcome_currency": "ltc", "outcome_amount": "0.0123",
        }
        payload.update(overrides)
        return payload

    def test_confirmation_does_not_credit_until_finished_then_is_idempotent(self):
        processing = database.process_nowpayments_ipn(self.payload(payment_status="confirming"))
        self.assertEqual(processing["status"], "processing")
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)

        result = database.process_nowpayments_ipn(self.payload())
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["new_balance"], 195_000_000)
        duplicate = database.process_nowpayments_ipn(self.payload())
        self.assertEqual(duplicate["status"], "duplicate")
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()[0], 195_000_000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT action FROM budget_logs").fetchone()[0], "إضافة تلقائية (crypto)")

    def test_underpayment_wrong_network_and_redeposit_go_to_review_without_credit(self):
        for payload in (
            self.payload(actually_paid="0.0100"),
            self.payload(actually_paid="0.0200"),
            self.payload(network="doge"),
            self.payload(outcome_currency="btc"),
            self.payload(parent_payment_id="np-parent"),
        ):
            result = database.process_nowpayments_ipn(payload)
            self.assertEqual(result["status"], "needs_review")
        with sqlite3.connect(self.guild_db_path) as conn:
            row = conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 101").fetchone()
            self.assertEqual(row[0] if row else 0, 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM budget_payment_orders WHERE id = ?", (self.order_id,)).fetchone()[0], "needs_review")

    def test_waiting_invoice_expires_after_fixed_rate_window_and_allows_new_order(self):
        result = database.process_nowpayments_ipn(self.payload(payment_status="waiting"))
        self.assertEqual(result["status"], "pending")
        second_order = database.create_crypto_budget_payment_order(
            101, 202, "payer", 303, 195_000_000, 1_250_000,
            "0.75000000", "cosmo:101:" + "c" * 32, 1601, 2201,
        )
        self.assertIsNotNone(second_order)
        with sqlite3.connect(self.guild_db_path) as conn:
            old_status = conn.execute("SELECT status FROM budget_payment_orders WHERE id = ?", (self.order_id,)).fetchone()[0]
        self.assertEqual(old_status, "expired")

    def test_crypto_order_rejects_usd_quote_that_does_not_match_budget_rate(self):
        result = database.create_crypto_budget_payment_order(
            101, 203, "payer", 304, 195_000_000, 1_250_000,
            "1.00000000", "cosmo:101:" + "c" * 32, 1001, 1601,
        )
        self.assertIsNone(result)

    def test_disabling_ltc_blocks_new_orders_but_existing_invoice_still_settles(self):
        cancelled = database.set_auto_budget_payment_enabled("crypto", False, [101])
        self.assertEqual(cancelled, [])
        self.assertEqual(database.process_nowpayments_ipn(self.payload())["status"], "paid")
        self.assertIsNone(
            database.create_crypto_budget_payment_order(
                101, 203, "other", 304, 100, 100, "0.00000100",
                "cosmo:101:" + "c" * 32, 1001, 1601,
            )
        )

    def test_unmatched_provider_order_is_never_credited(self):
        result = database.process_nowpayments_ipn(self.payload(order_id="cosmo:102:" + "d" * 32))
        self.assertIsNone(result)
        with sqlite3.connect(self.guild_db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_logs").fetchone()[0], 0)


class NowPaymentsWebhookRouteTests(unittest.TestCase):
    def test_signed_ipn_bypasses_form_csrf_and_bad_signature_is_rejected(self):
        import dashboard.app as dashboard_app

        payload = {"payment_id": "np-123", "order_id": "cosmo:101:" + "a" * 32, "payment_status": "confirming"}
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        signature = hmac.new(b"route-secret", canonical.encode(), hashlib.sha512).hexdigest()
        with patch.dict(os.environ, {"NOWPAYMENTS_IPN_SECRET": "route-secret"}):
            with patch.object(dashboard_app.database, "process_nowpayments_ipn", return_value={"status": "processing"}) as process:
                response = dashboard_app.app.test_client().post(
                    "/nowpayments/ipn", json=payload, headers={"x-nowpayments-sig": signature}
                )
            self.assertEqual(response.status_code, 200)
            process.assert_called_once_with(payload)
            bad = dashboard_app.app.test_client().post(
                "/nowpayments/ipn", json=payload, headers={"x-nowpayments-sig": "bad"}
            )
            self.assertEqual(bad.status_code, 401)


if __name__ == "__main__":
    unittest.main()
