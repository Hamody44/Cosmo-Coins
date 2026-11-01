import unittest
from types import SimpleNamespace

from cogs.advertisements import (
    AdContentModal,
    AdContentSubmitView,
    AdDurationView,
    AdGiveawayModal,
    AdMentionChoiceView,
    AdPaymentView,
    has_manual_mention,
    validate_ad_content,
)


class AdvertisementWizardValidationTests(unittest.TestCase):
    def test_accepts_content_with_discord_invite_and_no_manual_mentions(self):
        valid, cleaned = validate_ad_content("إعلان رائع للمجتمع https://discord.gg/example")
        self.assertTrue(valid)
        self.assertIn("discord.gg/example", cleaned)

    def test_rejects_content_without_discord_invite(self):
        valid, error = validate_ad_content("إعلان رائع للمجتمع بدون رابط دعوة")
        self.assertFalse(valid)
        self.assertIn("رابط دعوة Discord", error)

    def test_rejects_here_everyone_user_and_role_mentions(self):
        for mention in ("@here", "@everyone", "<@123456789>", "<@&123456789>", "<#123456789>"):
            with self.subTest(mention=mention):
                valid, error = validate_ad_content(f"إعلان رائع {mention} https://discord.gg/example")
                self.assertFalse(valid)
                self.assertIn("منشن يدوي", error)
        self.assertTrue(has_manual_mention("عنوان @everyone"))

    def test_interactive_views_and_modals_are_constructed_with_expected_controls(self):
        bot = SimpleNamespace()
        durations = AdDurationView(bot, 1, 2, 3, [("1d", 10_000_000), ("3d", 20_000_000)])
        mention = AdMentionChoiceView(bot, 1, 2, 3, "1d", 10_000_000, 5_000_000, 1, 2_000_000)
        payment = AdPaymentView(bot, 1, 4, 3, 2)
        content = AdContentSubmitView(bot, 1, 4)
        giveaway_modal = AdGiveawayModal(bot, 1, 2, 3, "1d", 10_000_000)
        content_modal = AdContentModal(1, 4)

        self.assertEqual(len(durations.children), 2)
        self.assertTrue(durations.is_persistent())
        self.assertEqual(len(mention.children), 2)
        self.assertEqual(len(payment.children), 3)
        self.assertTrue(payment.is_persistent())
        self.assertEqual(len(content.children), 1)
        self.assertTrue(content.is_persistent())
        self.assertEqual(len(giveaway_modal.children), 2)
        self.assertEqual(len(content_modal.children), 2)


if __name__ == "__main__":
    unittest.main()
