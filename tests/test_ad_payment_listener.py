import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.advertisements import AdContentSubmitView, Advertisements
from database import ADAMC_PAYMENT_BOT_ID, PROBOT_PAYMENT_BOT_ID


class AdvertisementPaymentListenerTests(unittest.IsolatedAsyncioTestCase):
    async def assert_provider_receipt_is_settled(self, method, bot_id):
        guild_id = 101
        order_id = 303
        advertiser_id = 404
        owner_id = 505
        channel_id = 606
        message_id = 707

        cursor = MagicMock()
        cursor.execute.return_value.fetchone.return_value = (
            order_id,
            advertiser_id,
            owner_id,
            "draft",
            None,
            None,
        )
        connection = MagicMock()
        receipt = {"sender": "payer", "amount": 100, "recipient_id": owner_id}
        payment = {"status": "pending", "method": method, "expected_amount": 100}
        channel = SimpleNamespace(id=channel_id, send=AsyncMock())
        message = SimpleNamespace(
            id=message_id,
            guild=SimpleNamespace(id=guild_id),
            author=SimpleNamespace(id=bot_id, bot=True),
            channel=channel,
            content="provider receipt",
            embeds=[],
        )

        bot = SimpleNamespace(add_view=MagicMock())
        with (
            patch("cogs.advertisements.ensure_ad_schema") as ensure_schema,
            patch("cogs.advertisements.get_guild_db", return_value=(connection, cursor)),
            patch("cogs.advertisements.get_ad_payment", return_value=payment),
            patch("cogs.advertisements.parse_budget_payment_confirmation", return_value=receipt) as parse_receipt,
            patch("cogs.advertisements.settle_ad_payment", return_value={"status": "paid"}) as settle_payment,
        ):
            await Advertisements(bot).on_message(message)

        ensure_schema.assert_called_once_with(guild_id)
        parse_receipt.assert_called_once_with(method, bot_id, "provider receipt")
        settle_payment.assert_called_once_with(
            guild_id,
            order_id,
            message_id,
            bot_id,
            100,
            recipient_id=owner_id,
            channel_id=channel_id,
        )
        channel.send.assert_awaited_once()
        sent_message = channel.send.await_args
        self.assertIn(f"تم تأكيد دفع الإعلان #{order_id}", sent_message.args[0])
        self.assertIn("اضغط الزر لإدخال محتوى الإعلان", sent_message.args[0])
        self.assertIsInstance(sent_message.kwargs["view"], AdContentSubmitView)
        bot.add_view.assert_called_once_with(sent_message.kwargs["view"])

    async def test_probot_bot_receipt_is_not_discarded(self):
        await self.assert_provider_receipt_is_settled("credit", PROBOT_PAYMENT_BOT_ID)

    async def test_adamc_bot_receipt_is_not_discarded(self):
        await self.assert_provider_receipt_is_settled("adamc", ADAMC_PAYMENT_BOT_ID)

    async def test_unrelated_bot_messages_are_ignored(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(id=101),
            author=SimpleNamespace(id=999, bot=True),
        )
        with patch("cogs.advertisements.ensure_ad_schema") as ensure_schema:
            await Advertisements(SimpleNamespace()).on_message(message)
        ensure_schema.assert_not_called()


if __name__ == "__main__":
    unittest.main()
