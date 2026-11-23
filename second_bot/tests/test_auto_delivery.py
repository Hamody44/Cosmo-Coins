from __future__ import annotations

import unittest

from second_bot.auto_delivery import (
    build_delivery_result_message,
    build_handoff_message,
    is_provider_confirmation,
    verify_delivery_result_message,
    verify_handoff_message,
)


SECRET = "a-test-shared-secret-that-is-long-enough-123456"


class SignedHandoffTests(unittest.TestCase):
    def build(self, **overrides):
        fields = {
            "secret": SECRET,
            "issuer_id": 111,
            "order_id": 81,
            "guild_id": 123,
            "delivery_channel_id": 456,
            "user_id": 789,
            "currency": "ProBot Credits",
            "amount": 250,
        }
        fields.update(overrides)
        return build_handoff_message(**fields)

    def test_signed_handoff_contains_derived_provider_command(self):
        handoff = self.build()
        payload = verify_handoff_message(handoff, SECRET)
        self.assertEqual(payload["issuer_id"], 111)
        self.assertEqual(payload["order_id"], 81)
        self.assertEqual(payload["guild_id"], 123)
        self.assertEqual(payload["delivery_channel_id"], 456)
        self.assertEqual(payload["command"], "#credit 789 250")
        self.assertEqual(payload["confirmation_type"], "type1")

    def test_provider_commands_and_confirmation_types_are_derived(self):
        adamc = verify_handoff_message(self.build(currency="AdamC"), SECRET)
        rob = verify_handoff_message(self.build(currency="Robux"), SECRET)
        self.assertEqual(adamc["command"], "T 789 250")
        self.assertEqual(adamc["confirmation_type"], "type2")
        self.assertEqual(rob["command"], "!transfer 789 250")
        self.assertIsNone(rob["confirmation_type"])

    def test_provider_confirmation_ignores_amount_and_recipient(self):
        self.assertTrue(is_provider_confirmation(
            "**ـ zeko2019., قام بتحويل `$14250000` لـ <@!1319281662533697606> ** |:moneybag:",
            282859044593598464,
            "ProBot",
        ))
        self.assertTrue(is_provider_confirmation(
            "💰 | **zeko2019.**, has transferred **$500,000** to <@1459901905898831878>.",
            1505622388975468684,
            "AdamC",
        ))
        self.assertFalse(is_provider_confirmation("has transferred $500 to <@1>", 123, "AdamC"))

    def test_invalid_signature_or_secret_is_rejected(self):
        handoff = self.build()
        altered_signature = handoff[:-3] + ("0" if handoff[-3] != "0" else "1") + handoff[-2:]
        self.assertIsNone(verify_handoff_message(altered_signature, SECRET))
        self.assertIsNone(verify_handoff_message(handoff, "short"))
        self.assertIsNone(verify_handoff_message(handoff, "z" * 48))

    def test_unsupported_currency_or_fractional_amount_does_not_create_handoff(self):
        self.assertIsNone(self.build(currency="Gems"))
        self.assertIsNone(self.build(amount=250.5))

    def test_invalid_identifiers_are_rejected(self):
        self.assertIsNone(self.build(delivery_channel_id=0))
        self.assertIsNone(self.build(user_id=-1))

    def test_signed_delivery_result_round_trips_all_supported_outcomes(self):
        for outcome in ("processing", "automatic", "failed", "pending", "rejected"):
            with self.subTest(outcome=outcome):
                message = build_delivery_result_message(
                    secret=SECRET, issuer_id=444, order_id=81, outcome=outcome
                )
                self.assertEqual(
                    verify_delivery_result_message(message, SECRET),
                    {"v": 1, "issuer_id": 444, "order_id": 81, "outcome": outcome},
                )

    def test_invalid_delivery_result_signature_and_outcome_are_rejected(self):
        message = build_delivery_result_message(
            secret=SECRET, issuer_id=444, order_id=81, outcome="automatic"
        )
        tampered = message[:-3] + ("0" if message[-3] != "0" else "1") + message[-2:]
        self.assertIsNone(verify_delivery_result_message(tampered, SECRET))
        self.assertIsNone(verify_delivery_result_message(message, "another-secret-that-is-long-enough"))
        self.assertIsNone(
            build_delivery_result_message(secret=SECRET, issuer_id=444, order_id=81, outcome="unknown")
        )


if __name__ == "__main__":
    unittest.main()
