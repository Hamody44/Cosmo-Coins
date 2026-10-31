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


if __name__ == "__main__":
    unittest.main()
