import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.advertisements import Advertisements


class AdvertisementCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_restores_oldest_name_once_for_multiple_settled_ads(self):
        channel = SimpleNamespace(edit=AsyncMock())
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: channel if channel_id == 55 else None)
        cog = Advertisements(SimpleNamespace())

        with (
            patch("cogs.advertisements.list_ad_cleanup_orders", return_value=[
                (10, 55, "giveaway"),
                (20, 55, "custom-ad-name"),
            ]),
            patch("cogs.advertisements.list_busy_ad_channels", return_value=set()),
            patch("cogs.advertisements.cleanup_ad_order", return_value=True) as mark_cleaned,
        ):
            await cog._cleanup_settled_ads(guild)

        channel.edit.assert_awaited_once_with(
            name="giveaway",
            reason="Automatic cleanup for settled ad #10",
        )
        self.assertEqual(mark_cleaned.call_count, 2)

    async def test_cleanup_does_not_rename_a_channel_with_a_live_ad(self):
        channel = SimpleNamespace(edit=AsyncMock())
        guild = SimpleNamespace(id=1, get_channel=lambda channel_id: channel if channel_id == 55 else None)
        cog = Advertisements(SimpleNamespace())

        with (
            patch("cogs.advertisements.list_ad_cleanup_orders", return_value=[(10, 55, "giveaway")]),
            patch("cogs.advertisements.list_busy_ad_channels", return_value={55}),
            patch("cogs.advertisements.cleanup_ad_order") as mark_cleaned,
        ):
            await cog._cleanup_settled_ads(guild)

        channel.edit.assert_not_awaited()
        mark_cleaned.assert_not_called()


if __name__ == "__main__":
    unittest.main()
