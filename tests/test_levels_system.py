import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
import discord
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import database
from cogs.levels import (
    Leveling,
    RIYADH,
    can_manage_level_settings,
    daily_report_dates_due,
    eligible_voice_users,
    format_voice_duration,
    leaderboard_embed,
    period_bounds,
    profile_embed,
)
from leveling_store import (
    add_level_channel,
    ensure_level_config,
    get_level_channels,
    get_level_leaderboard,
    get_level_profile,
    get_report_status,
    get_retryable_payouts,
    get_salary_history,
    get_salary_tiers,
    mature_pending_messages,
    mark_daily_report_send_attempt,
    pending_reports,
    queue_payout,
    record_report_failure,
    record_pending_message,
    set_milestone_reward,
    set_leveling_enabled,
    set_salary_tier,
    set_announcement_channel,
    start_daily_report,
)


class IsolatedGuildDatabase(unittest.TestCase):
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


class LevelMessageStoreTests(IsolatedGuildDatabase):
    def test_five_messages_in_ten_seconds_discard_the_burst_and_rate_limit_dm(self):
        guild_id, user_id, first = 9101, 42, 1_800_000_000
        outcomes = [
            record_pending_message(guild_id, user_id, first + i, 71, first + i)
            for i in range(5)
        ]
        self.assertEqual(outcomes[-1]["status"], "spam")
        self.assertTrue(outcomes[-1]["warn"])

        second_burst = [
            record_pending_message(guild_id, user_id, first + 20 + i, 71, first + 20 + i)
            for i in range(5)
        ]
        self.assertEqual(second_burst[-1]["status"], "spam")
        self.assertFalse(second_burst[-1]["warn"])

        mature_pending_messages(guild_id, now=first + 60)
        profile = get_level_profile(guild_id, user_id)
        self.assertEqual(profile["points"], 0)
        self.assertEqual(profile["messages"], 0)
        self.assertEqual(profile["rank"], 1)
        self.assertEqual(get_level_leaderboard(guild_id, None, None), [])
        self.assertEqual(get_level_leaderboard(guild_id, first, first + 90), [])

    def test_four_messages_in_ten_seconds_are_accepted_but_xp_is_capped(self):
        guild_id, user_id, first = 9102, 43, 1_800_100_000
        outcomes = [
            record_pending_message(guild_id, user_id, first + i, 72, first + i)
            for i in range(4)
        ]
        self.assertTrue(all(result["status"] == "pending" for result in outcomes))
        mature_pending_messages(guild_id, now=first + 15)
        profile = get_level_profile(guild_id, user_id)
        self.assertEqual(profile["points"], 1)
        self.assertEqual(profile["messages"], 4)
        self.assertEqual(profile["level"], 1)

    def test_subsecond_messages_are_counted_in_spam_burst(self):
        guild_id, user_id, first = 9106, 46, 1_800_150_000.0
        outcomes = [
            record_pending_message(guild_id, user_id, first + i, 76, first + i * 0.15)
            for i in range(5)
        ]
        self.assertEqual(outcomes[-1]["status"], "spam")
        self.assertEqual(outcomes[-1]["burst_count"], 5)
        mature_pending_messages(guild_id, now=first + 30)
        profile = get_level_profile(guild_id, user_id)
        self.assertEqual(profile["points"], 0)
        self.assertEqual(profile["messages"], 0)

    def test_xp_cooldown_is_per_member_and_per_guild(self):
        guild_id, user_id, first = 9103, 44, 1_800_200_000
        record_pending_message(guild_id, user_id, 1, 73, first)
        mature_pending_messages(guild_id, now=first + 11)
        record_pending_message(guild_id, user_id, 2, 73, first + 12)
        mature_pending_messages(guild_id, now=first + 23)
        self.assertEqual(get_level_profile(guild_id, user_id)["points"], 1)

        record_pending_message(guild_id, user_id, 3, 73, first + 31)
        mature_pending_messages(guild_id, now=first + 42)
        self.assertEqual(get_level_profile(guild_id, user_id)["points"], 2)
        self.assertEqual(get_level_profile(9104, user_id)["points"], 0)

    def test_crossing_level_ten_queues_the_configured_budget_reward_once(self):
        guild_id, user_id, now = 9105, 45, 1_800_400_000
        set_milestone_reward(guild_id, 250)
        conn, cur = database.get_guild_db(guild_id)
        cur.execute(
            "UPDATE leveling_users SET total_points = 899, last_point_at = ? WHERE user_id = ?",
            (now - 30, user_id),
        )
        cur.execute(
            "INSERT OR IGNORE INTO leveling_users (user_id, total_points, last_point_at) VALUES (?, 899, ?)",
            (user_id, now - 30),
        )
        conn.commit()
        conn.close()

        record_pending_message(guild_id, user_id, 900, 75, now)
        payouts = mature_pending_messages(guild_id, now=now + 11)
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0]["milestone"], 10)
        self.assertEqual(payouts[0]["amount"], 250)
        self.assertEqual(get_level_profile(guild_id, user_id)["level"], 10)
        self.assertEqual(len(get_retryable_payouts(guild_id)), 1)

    def test_guild_channels_and_salary_tiers_are_isolated(self):
        self.assertTrue(add_level_channel(9201, 101))
        self.assertFalse(add_level_channel(9201, 101))
        self.assertEqual(get_level_channels(9201), [101])
        self.assertEqual(get_level_channels(9202), [])

        set_salary_tier(9201, 500, 100)
        set_salary_tier(9201, 1000, 250)
        self.assertEqual(get_salary_tiers(9201), [(500, 100), (1000, 250)])
        self.assertEqual(get_salary_tiers(9202), [])

    def test_enable_toggle_records_only_active_period_for_daily_reports(self):
        set_leveling_enabled(9204, True, now=1_800_300_000)
        self.assertEqual(ensure_level_config(9204)["enabled_since"], 1_800_300_000)
        set_leveling_enabled(9204, True, now=1_800_300_100)
        self.assertEqual(ensure_level_config(9204)["enabled_since"], 1_800_300_000)
        set_leveling_enabled(9204, False, now=1_800_300_200)
        self.assertIsNone(ensure_level_config(9204)["enabled_since"])
        set_leveling_enabled(9204, True, now=1_800_300_300)
        self.assertEqual(ensure_level_config(9204)["enabled_since"], 1_800_300_300)

    def test_queued_payout_amount_is_immutable_for_its_idempotency_key(self):
        first = queue_payout(
            9203, "salary:9203:2026-10-23:47", 47, "salary", 100, now=1_800_300_000,
            local_date="2026-10-23", message_count=777, tier_threshold=500,
        )
        repeated = queue_payout(
            9203, "salary:9203:2026-10-23:47", 47, "salary", 200, now=1_800_300_100,
            local_date="2026-10-23", message_count=900, tier_threshold=1000,
        )
        self.assertEqual(first["amount"], 100)
        self.assertEqual(repeated["amount"], 100)
        self.assertEqual(repeated["message_count"], 777)
        self.assertEqual(repeated["tier_threshold"], 500)
        history = get_salary_history(9203, user_id=47)
        self.assertEqual(history[0]["local_date"], "2026-10-23")
        self.assertEqual(history[0]["message_count"], 777)
        self.assertEqual(history[0]["tier_threshold"], 500)


class LevelPeriodAndEmbedTests(unittest.TestCase):
    def test_daily_report_backfill_starts_when_enabled_and_skips_sent_days(self):
        enabled = datetime(2026, 10, 20, 8, 0, tzinfo=RIYADH).timestamp()
        now = datetime(2026, 10, 25, 23, 55, tzinfo=RIYADH)
        self.assertEqual(
            daily_report_dates_due(enabled, "2026-10-21", now),
            ["2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"],
        )
        self.assertEqual(daily_report_dates_due(None, None, now), [])

    def test_daily_report_catchup_after_midnight_targets_yesterday_not_future_today(self):
        enabled = datetime(2026, 10, 25, 12, 0, tzinfo=RIYADH).timestamp()
        now = datetime(2026, 10, 26, 0, 5, tzinfo=RIYADH)
        self.assertEqual(daily_report_dates_due(enabled, None, now), ["2026-10-25"])
        self.assertEqual(daily_report_dates_due(enabled, "2026-10-25", now), [])

    def test_period_ranges_follow_riyadh_calendar_monday_week_and_month_start(self):
        now = datetime(2026, 10, 25, 23, 30, tzinfo=RIYADH)
        day_start, day_end = period_bounds("daily", now)
        self.assertEqual(datetime.fromtimestamp(day_start, RIYADH), datetime(2026, 10, 25, 0, 0, tzinfo=RIYADH))
        self.assertEqual(datetime.fromtimestamp(day_end, RIYADH), datetime(2026, 10, 26, 0, 0, tzinfo=RIYADH))

        week_start, week_end = period_bounds("weekly", now)
        self.assertEqual(datetime.fromtimestamp(week_start, RIYADH), datetime(2026, 10, 19, 0, 0, tzinfo=RIYADH))
        self.assertEqual(datetime.fromtimestamp(week_end, RIYADH), datetime(2026, 10, 26, 0, 0, tzinfo=RIYADH))

        month_start, month_end = period_bounds("monthly", now)
        self.assertEqual(datetime.fromtimestamp(month_start, RIYADH), datetime(2026, 10, 1, 0, 0, tzinfo=RIYADH))
        self.assertEqual(datetime.fromtimestamp(month_end, RIYADH), datetime(2026, 11, 1, 0, 0, tzinfo=RIYADH))

    def test_profile_and_leaderboard_embeds_preserve_guild_stats_and_progress(self):
        self.assertEqual(Leveling.top.name, "top")
        self.assertEqual(period_bounds("all"), (None, None))
        self.assertEqual(period_bounds(None), (None, None))
        profile = profile_embed("سيرفر كوزمو", None, "عضو تجريبي", None, {
            "rank": 2, "messages": 505, "level": 10, "points": 900,
            "next_level_points": 100, "progress": 0,
        })
        self.assertEqual(profile.author.name, "سيرفر كوزمو")
        self.assertIn("عضو تجريبي", profile.title)
        self.assertTrue(any(field.name == "النقاط" and "900" in field.value for field in profile.fields))
        self.assertTrue(any(field.name == "الرسائل المحتسبة" and "505" in field.value for field in profile.fields))
        self.assertIn("توقيت الرياض", profile.footer.text)

        board = leaderboard_embed(
            "سيرفر كوزمو",
            None,
            "الترتيب الأسبوعي",
            [{"user_id": 123, "points": 900, "messages": 505}],
            [{"user_id": 456, "points": 30, "seconds": 1800}],
        )
        self.assertIn("الترتيب الأسبوعي", board.title)
        self.assertIn("<@123>", board.description)
        self.assertIn("900", board.description)
        self.assertIn("<@456>", board.description)
        self.assertIn("30", board.description)

    def test_primary_bot_owner_can_manage_level_commands(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=55, owner_id=777),
            author=SimpleNamespace(id=database.PRIMARY_OWNER_ID, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False)),
        )
        self.assertTrue(can_manage_level_settings(ctx))


class TopCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_top_has_only_an_optional_period_argument(self):
        command = Leveling.top.app_command
        self.assertEqual([parameter.name for parameter in command.parameters], ["period"])
        period = next(parameter for parameter in command.parameters if parameter.name == "period")
        self.assertFalse(period.required)
        self.assertEqual(
            {choice.value for choice in period.choices},
            {"daily", "weekly", "monthly"},
        )
        self.assertEqual(Leveling.toggle_voice_level_system.name, "toggle_voice_level_system")
        self.assertFalse(hasattr(Leveling, "add_voice_level_channel"))
        self.assertFalse(hasattr(Leveling, "remove_voice_level_channel"))
        self.assertFalse(hasattr(Leveling, "list_voice_level_channels"))

    async def test_top_sends_mentions_inside_embed_without_separate_content(self):
        member = SimpleNamespace(display_name="المتصدر")
        guild = SimpleNamespace(
            id=551,
            name="سيرفر الاختبار",
            icon=None,
            get_member=lambda user_id: member if user_id == 123 else None,
        )
        ctx = SimpleNamespace(guild=guild, send=AsyncMock())
        cog = object.__new__(Leveling)
        cog.refresh_voice_sessions = AsyncMock()

        with patch("cogs.levels.get_level_leaderboard", return_value=[{
            "user_id": 123,
            "points": 900,
            "messages": 505,
        }]) as get_text, patch("cogs.levels.get_voice_leaderboard", return_value=[{
            "user_id": 456,
            "points": 61,
            "seconds": 3660,
        }]) as get_voice:
            await Leveling.top.callback(cog, ctx, "weekly")

        get_text.assert_called_once_with(551, *period_bounds("weekly"), 5)
        get_voice.assert_called_once_with(551, "2026-10-19", "2026-10-26", 5)
        cog.refresh_voice_sessions.assert_awaited_once_with(guild)
        ctx.send.assert_awaited_once()
        self.assertNotIn("content", ctx.send.await_args.kwargs)
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertIn("<@123>", embed.description)
        self.assertIn("505", embed.description)
        self.assertIn("<@456>", embed.description)
        self.assertIn("1س 1د", embed.description)
        self.assertLess(embed.description.index("TOP 5 TEXT"), embed.description.index("TOP 5 VOICE"))

    async def test_top_without_period_shows_overall_board_without_period_label(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=552, name="سيرفر الاختبار", icon=None, get_member=lambda _user_id: None),
            send=AsyncMock(),
        )
        cog = object.__new__(Leveling)
        cog.refresh_voice_sessions = AsyncMock()
        entries = [{"user_id": 123, "points": 900, "messages": 505}]
        voice_entries = [{"user_id": 456, "points": 20, "seconds": 1200}]

        with patch("cogs.levels.get_level_leaderboard", return_value=entries) as get_text, \
             patch("cogs.levels.get_voice_leaderboard", return_value=voice_entries) as get_voice:
            await Leveling.top.callback(cog, ctx)

        get_text.assert_called_once_with(552, None, None, 5)
        get_voice.assert_called_once_with(552, None, None, 5)
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "🏆 المتصدرون")
        self.assertNotIn("الأبدي", embed.title)
        self.assertIn("<@123>", embed.description)
        self.assertIn("<@456>", embed.description)

    def test_combined_embed_shows_text_first_then_voice_with_points_and_duration(self):
        self.assertEqual(format_voice_duration(0), "0ث")
        self.assertEqual(format_voice_duration(45), "45ث")
        self.assertEqual(format_voice_duration(90061), "1ي 1س 1د")
        board = leaderboard_embed(
            "سيرفر كوزمو",
            None,
            None,
            [{"user_id": 123, "points": 900, "messages": 505}],
            [{"user_id": 321, "points": 120, "seconds": 7200}],
        )
        self.assertEqual(board.title, "🏆 المتصدرون")
        self.assertLess(board.description.index("TOP 5 TEXT"), board.description.index("TOP 5 VOICE"))
        self.assertIn("<@123>", board.description)
        self.assertIn("<@321>", board.description)
        self.assertIn("120", board.description)
        self.assertIn("2س 0د", board.description)

    def test_voice_eligibility_auto_tracks_temporary_rooms_but_excludes_afk_and_alone(self):
        def member(user_id, *, bot=False, deaf=False, self_deaf=False, self_mute=False):
            return SimpleNamespace(
                id=user_id,
                bot=bot,
                voice=SimpleNamespace(deaf=deaf, self_deaf=self_deaf, self_mute=self_mute),
            )

        tracked = SimpleNamespace(id=10, members=[member(1, self_mute=True), member(2)])
        one_person = SimpleNamespace(id=11, members=[member(3)])
        bot_room = SimpleNamespace(id=12, members=[member(4), member(5, bot=True)])
        afk_room = SimpleNamespace(id=99, members=[member(6), member(7)])
        temporary_room = SimpleNamespace(id=13, members=[member(8), member(9)])
        guild = SimpleNamespace(
            afk_channel=SimpleNamespace(id=99),
            voice_channels=[tracked, one_person, bot_room, afk_room, temporary_room],
            stage_channels=[],
        )
        self.assertEqual(
            eligible_voice_users(guild),
            {1: 10, 2: 10, 8: 13, 9: 13},
        )


class LevelDailyReportStoreTests(IsolatedGuildDatabase):
    def test_report_backoff_does_not_block_other_days_and_failed_reports_can_be_requeued(self):
        guild_id = 9401
        start_daily_report(guild_id, "2026-10-22", 1, now=100)
        start_daily_report(guild_id, "2026-10-23", 2, now=100)
        mark_daily_report_send_attempt(guild_id, "2026-10-22", 1, now=100)
        self.assertEqual(get_report_status(guild_id, "2026-10-22")[7:9], (100.0, 1))

        first_failure = record_report_failure(guild_id, "2026-10-22", "missing channel", now=100)
        self.assertEqual(first_failure["status"], "pending")
        self.assertEqual(pending_reports(guild_id, now=114), [("2026-10-23", 2)])
        self.assertEqual(pending_reports(guild_id, now=115)[0], ("2026-10-22", 1))

        now = 115
        result = first_failure
        while result["status"] != "failed":
            now = result.get("next_attempt_at", now) or now + 1
            result = record_report_failure(guild_id, "2026-10-22", "missing channel", now=now)
        self.assertEqual(result["attempt_count"], 8)
        self.assertEqual(pending_reports(guild_id, now=now), [("2026-10-23", 2)])
        self.assertEqual(get_report_status(guild_id, "2026-10-22")[2], "failed")

        set_announcement_channel(guild_id, 3)
        self.assertEqual(pending_reports(guild_id, now=now), [("2026-10-22", 3), ("2026-10-23", 3)])
        reopened = get_report_status(guild_id, "2026-10-22")
        self.assertEqual(reopened[2], "pending")
        self.assertIsNone(reopened[7])
        self.assertIsNone(reopened[8])


class LevelBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_salary_error_does_not_suppress_daily_top_five_report(self):
        cog = object.__new__(Leveling)
        channel = SimpleNamespace(id=77)

        async def send(*, embed, **_kwargs):
            channel.sent_embed = embed
            return SimpleNamespace(id=9001)

        channel.send = send

        async def fetch_member(_user_id):
            raise RuntimeError("transient member lookup failure")

        guild = SimpleNamespace(
            id=9502,
            name="اختبار يومي",
            icon=None,
            get_channel=lambda _channel_id: channel,
            get_member=lambda _user_id: None,
            fetch_member=fetch_member,
        )
        cog.bot = SimpleNamespace()
        status = (77, None, "pending", 1_800_000_000, 0, 0, None, None, None)

        with patch("cogs.levels.get_report_status", return_value=status), patch(
            "cogs.levels.get_level_leaderboard",
            return_value=[{"user_id": 42, "points": 600, "messages": 600}],
        ), patch("cogs.levels.get_guild_management_role_ids", return_value=(None, 1234)), patch(
            "cogs.levels.get_salary_tiers", return_value=[(500, 20)]
        ), patch("cogs.levels.mark_daily_report_send_attempt"
        ), patch("cogs.levels.save_daily_report_message") as save_message, patch(
            "cogs.levels.record_report_failure"
        ) as record_failure, patch("cogs.levels.finish_daily_report") as finish_report:
            await cog.publish_daily_report(guild, "2026-10-23", 77)

        self.assertEqual([field.name for field in channel.sent_embed.fields], ["🏆 التوب 5", "💰 الرواتب اليومية"])
        self.assertIn("تعذر التحقق أو تجهيز الراتب مؤقتاً", channel.sent_embed.fields[1].value)
        save_message.assert_called_once_with(9502, "2026-10-23", 9001)
        record_failure.assert_called_once()
        finish_report.assert_not_called()

    async def test_one_payout_error_does_not_abort_later_retryable_payouts(self):
        cog = object.__new__(Leveling)
        guild = SimpleNamespace(id=9501, name="اختبار", get_member=lambda _user_id: None)
        payouts = [
            {"payout_key": "milestone:9501:1:10", "user_id": 1, "amount": 5, "payout_type": "milestone", "status": "pending"},
            {"payout_key": "milestone:9501:2:10", "user_id": 2, "amount": 5, "payout_type": "milestone", "status": "pending"},
        ]
        with patch("cogs.levels.get_retryable_payouts", return_value=payouts), patch(
            "cogs.levels.payout_from_server_budget",
            side_effect=[RuntimeError("simulated database failure"), {"status": "paid"}],
        ) as payout:
            await cog.process_retryable_payouts(guild)
        self.assertEqual(payout.call_count, 2)

    async def test_report_reconciliation_searches_after_queue_time(self):
        cog = object.__new__(Leveling)
        marker = "level-daily:9501:2026-10-23"
        embed = discord.Embed(title="daily report")
        embed.set_footer(text=marker)
        message = SimpleNamespace(embeds=[embed])

        class HistoryChannel:
            async def history(self, *, limit, after):
                self.limit = limit
                self.after = after
                yield message

        channel = HistoryChannel()
        found = await cog.find_report_message(channel, marker, 1_800_000_000)
        self.assertIs(found, message)
        self.assertIsNone(channel.limit)
        self.assertEqual(int(channel.after.timestamp()), 1_800_000_000)


class RoleMigrationAndPayoutTests(IsolatedGuildDatabase):
    def test_existing_admin_role_is_migrated_to_owner_role_once(self):
        guild_id = 9301
        conn, cur = database.get_guild_db(guild_id)
        cur.execute("INSERT INTO settings (guild_id, admin_role_id) VALUES (?, ?)", (guild_id, 909))
        conn.commit()
        cur.execute("ALTER TABLE settings DROP COLUMN owner_role_id")
        conn.commit()
        conn.close()
        database._guild_schema_initialized.discard(os.path.abspath(f"guilds_data/guild_{guild_id}.db"))

        conn, cur = database.get_guild_db(guild_id)
        cur.execute("SELECT owner_role_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
        self.assertEqual(cur.fetchone(), (909, None))
        conn.close()

        conn, cur = database.get_guild_db(guild_id)
        cur.execute("UPDATE settings SET admin_role_id = 707 WHERE guild_id = ?", (guild_id,))
        conn.commit()
        conn.close()
        conn, cur = database.get_guild_db(guild_id)
        cur.execute("SELECT owner_role_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
        self.assertEqual(cur.fetchone(), (909, 707))
        conn.close()

    def test_legacy_salary_ledger_gets_history_columns_on_open(self):
        guild_id = 9302
        conn, cur = database.get_guild_db(guild_id)
        cur.execute(
            "INSERT INTO leveling_daily_reports (local_date, channel_id, status, created_at) "
            "VALUES ('2026-10-23', 88, 'pending', 1800000000)"
        )
        cur.execute("DROP INDEX idx_level_salary_history")
        for column in ("tier_threshold", "message_count", "local_date"):
            cur.execute(f"ALTER TABLE leveling_payouts DROP COLUMN {column}")
        for column in ("last_send_channel_id", "last_send_attempt_at", "last_error", "next_attempt_at", "attempt_count"):
            cur.execute(f"ALTER TABLE leveling_daily_reports DROP COLUMN {column}")
        cur.execute("ALTER TABLE settings DROP COLUMN owner_role_id")
        conn.commit()
        conn.close()
        database._guild_schema_initialized.discard(os.path.abspath(f"guilds_data/guild_{guild_id}.db"))

        barrier = threading.Barrier(8)

        def open_and_close(_):
            barrier.wait()
            migrated_conn, _cur = database.get_guild_db(guild_id)
            migrated_conn.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(open_and_close, range(8)))

        self.assertEqual(get_salary_history(guild_id), [])
        conn, cur = database.get_guild_db(guild_id)
        columns = {row[1] for row in cur.execute("PRAGMA table_info(leveling_payouts)").fetchall()}
        self.assertTrue({"local_date", "message_count", "tier_threshold"}.issubset(columns))
        report_columns = {row[1] for row in cur.execute("PRAGMA table_info(leveling_daily_reports)").fetchall()}
        self.assertTrue({"last_send_attempt_at", "last_send_channel_id"}.issubset(report_columns))
        settings_columns = {row[1] for row in cur.execute("PRAGMA table_info(settings)").fetchall()}
        self.assertIn("owner_role_id", settings_columns)
        conn.close()
        report = get_report_status(guild_id, "2026-10-23")
        self.assertEqual(report[7], 1_800_000_000)
        self.assertEqual(report[8], 88)

    def test_guild_budget_payout_is_atomic_idempotent_and_retryable(self):
        guild_id, user_id = 9401, 88
        database.GLOBAL_DB_PATH = os.path.join(self.temp_dir.name, "wallet.db")
        wallet = sqlite3.connect(database.GLOBAL_DB_PATH)
        wallet.execute("CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)")
        wallet.commit()
        wallet.close()

        conn, cur = database.get_guild_db(guild_id)
        cur.execute("INSERT INTO settings (guild_id, spending_balance) VALUES (?, 500)", (guild_id,))
        conn.commit()
        conn.close()

        first = database.payout_from_server_budget(guild_id, user_id, 100, "reward:one", "milestone", 1_800_500_000)
        self.assertEqual(first["status"], "paid")
        self.assertEqual(database.get_server_spending_balance(guild_id), 400)
        self.assertEqual(database.get_balance(user_id), 100)

        repeated = database.payout_from_server_budget(guild_id, user_id, 100, "reward:one", "milestone", 1_800_500_100)
        self.assertEqual(repeated["status"], "already_paid")
        self.assertEqual(database.get_balance(user_id), 100)

        insufficient = database.payout_from_server_budget(guild_id, user_id, 600, "salary:one", "salary", 1_800_500_200)
        self.assertEqual(insufficient["status"], "insufficient")
        self.assertEqual(database.get_balance(user_id), 100)
        conn, cur = database.get_guild_db(guild_id)
        cur.execute("UPDATE settings SET spending_balance = 700 WHERE guild_id = ?", (guild_id,))
        conn.commit()
        conn.close()

        retried = database.payout_from_server_budget(guild_id, user_id, 600, "salary:one", "salary", 1_800_500_500)
        self.assertEqual(retried["status"], "paid")
        self.assertEqual(database.get_server_spending_balance(guild_id), 100)
        self.assertEqual(database.get_balance(user_id), 700)

    def test_payout_fails_closed_when_either_database_uses_wal(self):
        guild_id, user_id = 9402, 89
        database.GLOBAL_DB_PATH = os.path.join(self.temp_dir.name, "wallet-wal.db")
        wallet = sqlite3.connect(database.GLOBAL_DB_PATH)
        wallet.execute("CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)")
        wallet.execute("PRAGMA journal_mode=WAL")
        wallet.commit()
        wallet.close()

        conn, cur = database.get_guild_db(guild_id)
        cur.execute("INSERT INTO settings (guild_id, spending_balance) VALUES (?, 500)", (guild_id,))
        conn.commit()
        conn.close()

        result = database.payout_from_server_budget(guild_id, user_id, 100, "reward:wal", "milestone", 1_800_600_000)
        self.assertEqual(result["status"], "blocked_wal")
        self.assertEqual(database.get_server_spending_balance(guild_id), 500)
        self.assertEqual(database.get_balance(user_id), 0)

        wallet = sqlite3.connect(database.GLOBAL_DB_PATH)
        wallet.execute("PRAGMA journal_mode=DELETE")
        wallet.close()
        paid = database.payout_from_server_budget(guild_id, user_id, 100, "reward:once", "milestone", 1_800_600_100)
        self.assertEqual(paid["status"], "paid")

        wallet = sqlite3.connect(database.GLOBAL_DB_PATH)
        wallet.execute("PRAGMA journal_mode=WAL")
        wallet.close()
        replay = database.payout_from_server_budget(guild_id, user_id, 100, "reward:once", "milestone", 1_800_600_200)
        self.assertEqual(replay["status"], "already_paid")
        self.assertEqual(database.get_server_spending_balance(guild_id), 400)
        self.assertEqual(database.get_balance(user_id), 100)


if __name__ == "__main__":
    unittest.main()
