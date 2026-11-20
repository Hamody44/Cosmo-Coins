from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_join


class AutoJoinSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(auto_join, "AUTO_JOIN_QUEUE_PATH", root / "queue.json"),
            patch.object(auto_join, "SECOND_BOT_STATE_PATH", root / "state.json"),
            patch.object(auto_join, "AUTO_JOIN_SUPPRESSIONS_PATH", root / "suppressed.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_departed_guild_is_suppressed_and_pending_invite_is_removed(self) -> None:
        auto_join.publish_second_bot_state(user_id=77, guild_ids=[100, 200])
        self.assertTrue(auto_join.enqueue_auto_join(guild_id=100, invite_url="https://discord.gg/example"))

        auto_join.publish_second_bot_state(user_id=77, guild_ids=[200])

        self.assertEqual(auto_join.load_auto_join_suppressions(), {100})
        self.assertNotIn("100", auto_join.load_auto_join_queue())
        self.assertFalse(auto_join.enqueue_auto_join(guild_id=100, invite_url="https://discord.gg/new"))
        self.assertIn(200, auto_join.load_second_bot_state()["guild_ids"])

    def test_new_account_state_does_not_suppress_old_account_guilds(self) -> None:
        auto_join.publish_second_bot_state(user_id=77, guild_ids=[100])

        auto_join.publish_second_bot_state(user_id=88, guild_ids=[])

        self.assertEqual(auto_join.load_auto_join_suppressions(), set())


if __name__ == "__main__":
    unittest.main()
