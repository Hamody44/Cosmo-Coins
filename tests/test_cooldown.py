"""Regression tests for command cooldown consumption."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import main


class CooldownRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.user_command_cooldowns.clear()

    def tearDown(self):
        main.user_command_cooldowns.clear()

    async def test_prefix_check_does_not_consume_cooldown_before_success(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=123), guild=SimpleNamespace(id=456))

        self.assertTrue(await main.check_user_command_cooldown(ctx))
        self.assertEqual(main.user_command_cooldowns, {})

        await main.after_prefix_command(ctx)
        self.assertIn((123, 456), main.user_command_cooldowns)

        with self.assertRaises(main.UserCommandCooldown):
            await main.check_user_command_cooldown(ctx)

    async def test_slash_check_does_not_consume_until_completion(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            guild_id=456,
            command=object(),
        )

        self.assertTrue(await main.bot.tree.interaction_check(interaction))
        self.assertEqual(main.user_command_cooldowns, {})

        await main.on_app_command_completion(interaction, object())
        self.assertIn((123, 456), main.user_command_cooldowns)

        with self.assertRaises(main.AppUserCommandCooldown):
            await main.bot.tree.interaction_check(interaction)


if __name__ == "__main__":
    unittest.main()
