import os
import tempfile
import unittest
from datetime import datetime

import database
from leveling_store import (
    RIYADH,
    ensure_voice_config,
    get_voice_leaderboard,
    reset_voice_sessions,
    set_voice_leveling_enabled,
    update_voice_sessions,
)


class VoiceLevelingStoreTests(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.original_global_db = database.GLOBAL_DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        os.makedirs("guilds_data", exist_ok=True)

    def tearDown(self):
        database.GLOBAL_DB_PATH = self.original_global_db
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def test_voice_toggle_is_disabled_by_default_and_independent(self):
        guild_id = 80101
        config = ensure_voice_config(guild_id)
        self.assertFalse(config["enabled"])
        set_voice_leveling_enabled(guild_id, True)
        self.assertTrue(ensure_voice_config(guild_id)["enabled"])

    def test_session_time_splits_at_riyadh_midnight_and_points_equal_minutes(self):
        guild_id, user_id, channel_id = 80102, 42, 7002
        start = datetime(2026, 10, 23, 23, 59, 30, tzinfo=RIYADH).timestamp()
        reset_voice_sessions(guild_id, {user_id: channel_id}, start)
        update_voice_sessions(guild_id, {user_id: channel_id}, start + 60)
        update_voice_sessions(guild_id, {}, start + 90)

        all_time = get_voice_leaderboard(guild_id)
        self.assertEqual(all_time, [{"user_id": user_id, "seconds": 90, "points": 1}])
        previous_day = get_voice_leaderboard(guild_id, "2026-10-23", "2026-10-24")
        current_day = get_voice_leaderboard(guild_id, "2026-10-24", "2026-10-25")
        self.assertEqual(previous_day, [{"user_id": user_id, "seconds": 30, "points": 0}])
        self.assertEqual(current_day, [{"user_id": user_id, "seconds": 60, "points": 1}])

    def test_repeated_checkpoint_is_idempotent_and_restart_does_not_award_downtime(self):
        guild_id, user_id, channel_id = 80103, 43, 7003
        start = datetime(2026, 10, 24, 12, 0, tzinfo=RIYADH).timestamp()
        reset_voice_sessions(guild_id, {user_id: channel_id}, start)
        update_voice_sessions(guild_id, {user_id: channel_id}, start + 80)
        update_voice_sessions(guild_id, {user_id: channel_id}, start + 80)

        restart_time = start + 10_000
        reset_voice_sessions(guild_id, {user_id: channel_id}, restart_time)
        update_voice_sessions(guild_id, {}, restart_time + 60)
        self.assertEqual(
            get_voice_leaderboard(guild_id),
            [{"user_id": user_id, "seconds": 140, "points": 2}],
        )

    def test_channel_change_closes_old_session_and_starts_new_session(self):
        guild_id, user_id = 80104, 44
        start = datetime(2026, 10, 24, 13, 0, tzinfo=RIYADH).timestamp()
        reset_voice_sessions(guild_id, {user_id: 7004}, start)
        update_voice_sessions(guild_id, {user_id: 7005}, start + 30)
        update_voice_sessions(guild_id, {}, start + 60)
        self.assertEqual(
            get_voice_leaderboard(guild_id),
            [{"user_id": user_id, "seconds": 60, "points": 1}],
        )


if __name__ == "__main__":
    unittest.main()
