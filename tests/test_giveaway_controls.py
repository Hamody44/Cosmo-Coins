import os
import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from cogs import games
from cogs.games import Games


class GiveawayControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.guild_path = os.path.join(self.temp_dir.name, "guild.db")
        self.global_path = os.path.join(self.temp_dir.name, "global.db")
        self.old_global_path = database.GLOBAL_DB_PATH
        database.GLOBAL_DB_PATH = self.global_path
        with sqlite3.connect(self.global_path) as conn:
            conn.execute("CREATE TABLE balances (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)")
        with sqlite3.connect(self.guild_path) as conn:
            conn.executescript(
                """
                CREATE TABLE settings (guild_id INTEGER PRIMARY KEY, spending_balance INTEGER DEFAULT 0);
                CREATE TABLE active_giveaways (
                    message_id INTEGER PRIMARY KEY, channel_id INTEGER, amount INTEGER, end_timestamp INTEGER,
                    winners_count INTEGER, condition TEXT, participants_json TEXT, is_finished INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active', generation INTEGER DEFAULT 0, managed_version INTEGER DEFAULT 1
                );
                CREATE TABLE giveaway_winners (
                    message_id INTEGER NOT NULL, generation INTEGER NOT NULL, prize_slot INTEGER NOT NULL,
                    user_id INTEGER, state TEXT NOT NULL, PRIMARY KEY(message_id, generation, prize_slot)
                );
                """
            )
            conn.execute("INSERT INTO settings (guild_id, spending_balance) VALUES (111, 25)")

        def guild_db(_guild_id):
            conn = sqlite3.connect(self.guild_path, timeout=10)
            return conn, conn.cursor()

        self.guild_db_patch = patch("cogs.games.get_guild_db", side_effect=guild_db)
        self.guild_db_patch.start()
        self.addCleanup(self.guild_db_patch.stop)
        self.addCleanup(setattr, database, "GLOBAL_DB_PATH", self.old_global_path)
        self.addCleanup(self.temp_dir.cleanup)

    def add_giveaway(self, *, message_id=700, status="active", generation=0, managed_version=1,
                     amount=10, winners_count=2, channel_id=222):
        with sqlite3.connect(self.guild_path) as conn:
            conn.execute(
                "INSERT INTO active_giveaways "
                "(message_id, channel_id, amount, end_timestamp, winners_count, condition, participants_json, "
                "is_finished, status, generation, managed_version) "
                "VALUES (?, ?, ?, 0, ?, 'join group', '[]', ?, ?, ?, ?)",
                (message_id, channel_id, amount, winners_count, int(status != "active"), status, generation, managed_version),
            )

    def context(self):
        guild = SimpleNamespace(id=111, owner_id=999, get_channel=lambda _channel_id: object())
        author = SimpleNamespace(id=999)
        return SimpleNamespace(guild=guild, author=author, send=AsyncMock())

    def budget(self):
        with sqlite3.connect(self.guild_path) as conn:
            return conn.execute("SELECT spending_balance FROM settings WHERE guild_id = 111").fetchone()[0]

    async def test_stop_refunds_full_reservation_once_and_closes_active_giveaway(self):
        self.add_giveaway(amount=10, winners_count=2)
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        ctx = self.context()
        with patch("cogs.games.can_manage_server_games", return_value=True), \
             patch.object(cog, "_giveaway_channel", new=AsyncMock(return_value=None)):
            await Games.stop_giveaway.callback(cog, ctx, 700)
            await Games.stop_giveaway.callback(cog, ctx, 700)

        with sqlite3.connect(self.guild_path) as conn:
            status = conn.execute("SELECT status FROM active_giveaways WHERE message_id = 700").fetchone()[0]
        self.assertEqual(status, "stopped")
        self.assertEqual(self.budget(), 45)
        self.assertEqual(ctx.send.await_count, 2)

    async def test_reroll_reselects_all_winners_and_recharges_only_previously_refunded_slots(self):
        self.add_giveaway(status="ended", generation=0, amount=10, winners_count=3)
        with sqlite3.connect(self.guild_path) as conn:
            conn.executemany(
                "INSERT INTO giveaway_winners (message_id, generation, prize_slot, user_id, state) VALUES (700, 0, ?, ?, ?)",
                [(0, 31, "pending"), (1, 32, "refunded"), (2, None, "unfilled")],
            )
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        cog._finish_giveaway_locked = AsyncMock(return_value=True)
        ctx = self.context()
        channel = object()
        with patch("cogs.games.can_manage_server_games", return_value=True), \
             patch.object(cog, "_giveaway_channel", new=AsyncMock(return_value=channel)):
            await Games.reroll_giveaway.callback(cog, ctx, 700)

        with sqlite3.connect(self.guild_path) as conn:
            row = conn.execute(
                "SELECT status, is_finished, generation FROM active_giveaways WHERE message_id = 700"
            ).fetchone()
            old_states = conn.execute(
                "SELECT state FROM giveaway_winners WHERE message_id = 700 AND generation = 0 ORDER BY prize_slot"
            ).fetchall()
        self.assertEqual(row, ("active", 0, 1))
        self.assertEqual([state[0] for state in old_states], ["void", "void", "void"])
        self.assertEqual(self.budget(), 5)
        cog._finish_giveaway_locked.assert_awaited_once_with(ctx.guild, channel, 700)

    async def test_reroll_is_refused_after_any_prize_was_credited(self):
        self.add_giveaway(status="ended")
        database.credit_giveaway_prize(111, 700, 0, 0, 31, 10)
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        ctx = self.context()
        with patch("cogs.games.can_manage_server_games", return_value=True):
            await Games.reroll_giveaway.callback(cog, ctx, 700)

        with sqlite3.connect(self.guild_path) as conn:
            state = conn.execute(
                "SELECT status, generation FROM active_giveaways WHERE message_id = 700"
            ).fetchone()
        self.assertEqual(state, ("ended", 0))
        self.assertEqual(self.budget(), 25)
        self.assertIn("لا يمكن إعادة السحب", ctx.send.await_args.args[0])

    async def test_new_generation_excludes_all_voided_winners(self):
        self.add_giveaway(status="active", generation=1, amount=10, winners_count=1)
        with sqlite3.connect(self.guild_path) as conn:
            conn.execute(
                "UPDATE active_giveaways SET participants_json = ?, condition = NULL WHERE message_id = 700",
                (json.dumps([31, 32]),),
            )
            conn.execute(
                "INSERT INTO giveaway_winners (message_id, generation, prize_slot, user_id, state) "
                "VALUES (700, 0, 0, 31, 'void')"
            )

        members = {
            31: SimpleNamespace(id=31, mention="<@31>", send=AsyncMock()),
            32: SimpleNamespace(id=32, mention="<@32>", send=AsyncMock()),
        }
        guild = SimpleNamespace(id=111, get_member=members.get)
        channel = SimpleNamespace(send=AsyncMock())
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        cog._update_giveaway_post = AsyncMock()
        with patch("cogs.games.is_blacklisted", return_value=False), \
             patch("cogs.games.get_currency_label", return_value="Cosmo"), \
             patch("cogs.games.random.sample", side_effect=lambda population, count: list(population)[:count]) as sample:
            await Games.finish_giveaway(cog, guild, channel, 700)

        sample.assert_called_once_with([32], 1)
        with sqlite3.connect(self.guild_path) as conn:
            winner = conn.execute(
                "SELECT user_id, state FROM giveaway_winners WHERE message_id = 700 AND generation = 1"
            ).fetchone()
        self.assertEqual(winner, (32, "paid"))

    async def test_reroll_rejects_legacy_finished_giveaways_without_payout_ledger(self):
        self.add_giveaway(status="ended", managed_version=0)
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        ctx = self.context()
        with patch("cogs.games.can_manage_server_games", return_value=True):
            await Games.reroll_giveaway.callback(cog, ctx, 700)
        self.assertIn("سجل صرف موثوقاً", ctx.send.await_args.args[0])
        self.assertEqual(self.budget(), 25)

    async def test_conditional_prize_claim_and_denial_are_single_use(self):
        self.add_giveaway(status="ended", amount=12, winners_count=1)
        with sqlite3.connect(self.guild_path) as conn:
            conn.execute(
                "INSERT INTO giveaway_winners (message_id, generation, prize_slot, user_id, state) "
                "VALUES (700, 0, 0, 41, 'pending')"
            )
        self.assertEqual(games._set_giveaway_prize_state(111, 700, 0, 0, 41, 12, "claim"), "ready")
        database.credit_giveaway_prize(111, 700, 0, 0, 41, 12)
        games._mark_giveaway_prize_paid(111, 700, 0, 0)
        self.assertEqual(games._set_giveaway_prize_state(111, 700, 0, 0, 41, 12, "claim"), "already_paid")
        self.assertEqual(database.get_balance(41), 12)
        self.assertEqual(self.budget(), 25)

        self.add_giveaway(message_id=701, status="ended", amount=12, winners_count=1)
        with sqlite3.connect(self.guild_path) as conn:
            conn.execute(
                "INSERT INTO giveaway_winners (message_id, generation, prize_slot, user_id, state) "
                "VALUES (701, 0, 0, 42, 'pending')"
            )
        self.assertEqual(games._set_giveaway_prize_state(111, 701, 0, 0, 42, 12, "deny"), "refunded")
        self.assertEqual(games._set_giveaway_prize_state(111, 701, 0, 0, 42, 12, "deny"), "already_refunded")
        self.assertEqual(self.budget(), 37)

    async def test_finish_draw_pays_selected_winner_once_and_refunds_unfilled_slot(self):
        winner = SimpleNamespace(id=31, mention="<@31>", send=AsyncMock())
        self.add_giveaway(message_id=702, amount=10, winners_count=2)
        with sqlite3.connect(self.guild_path) as conn:
            conn.execute(
                "UPDATE active_giveaways SET participants_json = ?, condition = NULL WHERE message_id = 702",
                (json.dumps([31, 31]),),
            )
        guild = SimpleNamespace(id=111, get_member=lambda user_id: winner if user_id == 31 else None)
        channel = SimpleNamespace(send=AsyncMock())
        cog = Games.__new__(Games)
        cog.bot = SimpleNamespace()
        cog._update_giveaway_post = AsyncMock()
        with patch("cogs.games.is_blacklisted", return_value=False), \
             patch("cogs.games.get_currency_label", return_value="Cosmo"):
            first = await Games.finish_giveaway(cog, guild, channel, 702)
            second = await Games.finish_giveaway(cog, guild, channel, 702)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(database.get_balance(31), 10)
        self.assertEqual(self.budget(), 35)
        with sqlite3.connect(self.guild_path) as conn:
            status = conn.execute("SELECT status FROM active_giveaways WHERE message_id = 702").fetchone()[0]
            states = conn.execute(
                "SELECT state FROM giveaway_winners WHERE message_id = 702 ORDER BY prize_slot"
            ).fetchall()
        self.assertEqual(status, "ended")
        self.assertEqual([state[0] for state in states], ["paid", "unfilled"])


if __name__ == "__main__":
    unittest.main()
