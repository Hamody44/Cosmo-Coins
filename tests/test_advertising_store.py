import os
import tempfile
import unittest

import advertising_store as store


class AdvertisingStoreTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        os.makedirs("guilds_data", exist_ok=True)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_prices_channels_and_config_are_persisted(self):
        store.set_ad_price(1, "3d", 10_000_000)
        store.set_ad_config(1, daily_limit=3, everyone_fee=2_000_000)
        store.add_ad_channel(1, 55)
        config = store.get_ad_config(1)
        self.assertEqual(config["prices"]["3d"], 10_000_000)
        self.assertEqual(config["daily_limit"], 3)
        self.assertEqual(config["everyone_fee"], 2_000_000)
        self.assertEqual(config["channels"], [55])

    def test_queued_order_gets_rank_and_keeps_giveaway_separate(self):
        order_id = store.create_ad_order(
            1, 20, 30, "5d", 12_000_000, 100_000_000, 2, "here", queued=True
        )
        order = store.get_ad_order(1, order_id)
        self.assertEqual(order["status"], "queued")
        self.assertEqual(order["queue_rank"], 1)
        self.assertEqual(order["ad_price"], 12_000_000)
        self.assertEqual(order["giveaway_total"], 100_000_000)

    def test_rejects_giveaway_below_minimum(self):
        with self.assertRaises(ValueError):
            store.create_ad_order(1, 20, 30, "1d", 1_000_000, 4_999_999)

    def test_content_submission_creates_payment_and_settles_once(self):
        order_id = store.create_ad_order(1, 20, 30, "1d", 8_000_000, 5_000_000)
        self.assertTrue(store.submit_ad_content(1, order_id, 20, "عنوان", "شاهد https://discord.gg/example", None, 99))
        order = store.get_ad_order(1, order_id)
        self.assertEqual(order["status"], "awaiting_payment")
        store.create_ad_payment(1, order_id, "adamc", 13_000_000)
        self.assertEqual(store.settle_ad_payment(1, order_id, 700, 1234, 13_000_000)["status"], "paid")
        self.assertEqual(store.get_ad_order(1, order_id)["status"], "paid")
        self.assertEqual(store.settle_ad_payment(1, order_id, 700, 1234, 13_000_000)["status"], "already_paid")

    def test_total_payment_and_amount_per_winner_are_snapshotted(self):
        order_id = store.create_ad_order(1, 20, 30, "1d", 8_000_000, 100_000_000, 2, "everyone", everyone_fee=2_000_000)
        order = store.get_ad_order(1, order_id)
        self.assertEqual(order["everyone_fee"], 2_000_000)
        self.assertEqual(order["total_payment"], 110_000_000)
        self.assertEqual(order["amount_per_winner"], 50_000_000)

    def test_multiple_winners_require_exact_safe_split(self):
        with self.assertRaises(ValueError):
            store.create_ad_order(1, 20, 30, "1d", 1, 100_000_001, 2)
        with self.assertRaises(ValueError):
            store.create_ad_order(1, 20, 30, "1d", 1, 99_999_999, 2)

    def test_queue_claim_respects_daily_limit(self):
        first = store.create_ad_order(1, 20, 30, "1d", 1, 5_000_000, queued=True, server_owner_id=20)
        second = store.create_ad_order(1, 21, 31, "1d", 1, 5_000_000, queued=True)
        store.submit_ad_content(1, first, 20, "عنوان", "إعلان صالح https://discord.gg/example", None, 20)
        self.assertEqual(store.claim_next_queued_order(1, 0, 1), first)
        self.assertIsNone(store.claim_next_queued_order(1, 0, 1))
        self.assertEqual(store.get_ad_order(1, second)["status"], "queued")

    def test_server_budget_reservation_and_refund_are_single_use(self):
        from database import get_guild_db
        conn, cur = get_guild_db(1)
        cur.execute("INSERT INTO settings(guild_id, spending_balance) VALUES(?, ?) ON CONFLICT(guild_id) DO UPDATE SET spending_balance=excluded.spending_balance", (1, 5_000_000))
        conn.commit()
        conn.close()
        order_id = store.create_ad_order(1, 20, 30, "1d", 1, 5_000_000)
        store.update_ad_order(1, order_id, status="paid")
        self.assertEqual(store.reserve_ad_giveaway(1, order_id)["status"], "reserved")
        self.assertEqual(store.reserve_ad_giveaway(1, order_id)["status"], "already_reserved")
        self.assertEqual(store.reject_ad_delivery(1, order_id)["status"], "refunded")
        self.assertEqual(store.reject_ad_delivery(1, order_id)["status"], "already_refunded")

    def test_expired_giveaway_is_claimed_once_for_worker(self):
        order_id = store.create_ad_order(1, 20, 30, "1d", 1, 5_000_000)
        store.update_ad_order(1, order_id, status="paid")
        store.reserve_ad_giveaway(1, order_id, "customer_payment")
        store.record_ad_published(1, order_id, 55, 66, 0, "old-room")
        claimed = store.claim_expired_ad_giveaways(1, now=10**12)
        self.assertEqual([row["order_id"] for row in claimed], [order_id])
        self.assertEqual(store.claim_expired_ad_giveaways(1, now=10**12), [])
        self.assertEqual(store.finish_ad_giveaway(1, order_id, [77])["status"], "awaiting_owner_decision")


if __name__ == "__main__":
    unittest.main()
