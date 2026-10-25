import unittest
from unittest.mock import patch

from dashboard.tournament import TournamentError, TournamentManager


class EightBallTournamentTests(unittest.TestCase):
    def setUp(self):
        self.manager = TournamentManager()
        self.now = 1_000_000.0
        self.state = self.manager.create(
            guild_id=10,
            channel_id=20,
            created_by=99,
            prize_amount=500,
            registration_seconds=30,
            now=self.now,
        )
        self.tournament_id = self.state["tournament_id"]

    def join(self, user_id):
        return self.manager.join(self.tournament_id, user_id, f"Player {user_id}", now=self.now + 1)

    def test_registration_is_open_for_30_seconds_and_duplicate_join_is_idempotent(self):
        self.join(1)
        self.join(1)
        state = self.manager.state(self.tournament_id, now=self.now + 29)
        self.assertEqual(state["player_count"], 1)
        with self.assertRaises(TournamentError):
            self.manager.join(self.tournament_id, 2, "Player 2", now=self.now + 31)

    def test_four_players_are_paired_then_advanced_to_final(self):
        for user_id in (1, 2, 3, 4):
            self.join(user_id)
        state = self.manager.state(self.tournament_id, now=self.now + 30)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["round"], 1)
        self.assertEqual(len(state["matches"]), 2)

        first_round = [match["match_id"] for match in state["matches"]]
        for match_id in first_round:
            match = next(item for item in state["matches"] if item["match_id"] == match_id)
            self.manager.report_match_result(self.tournament_id, match_id, match["players"][0]["user_id"], referee_id=99, now=self.now + 31)

        state = self.manager.state(self.tournament_id, now=self.now + 32)
        self.assertEqual(state["round"], 2)
        self.assertEqual(len(state["matches"]), 1)
        final = state["matches"][0]
        winner = final["players"][0]["user_id"]
        completed = self.manager.report_match_result(self.tournament_id, final["match_id"], winner, referee_id=99, now=self.now + 33)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["winner_id"], winner)
        completion = self.manager.completion(self.tournament_id)
        self.assertEqual(completion["winner_id"], winner)
        self.assertEqual(completion["completion_nonce"], completed["completion_nonce"])

    def test_odd_player_count_gets_a_bye_without_creating_a_phantom_player(self):
        for user_id in (1, 2, 3):
            self.join(user_id)
        state = self.manager.state(self.tournament_id, now=self.now + 30)
        self.assertEqual(state["round"], 1)
        self.assertEqual(sum(len(match["players"]) for match in state["matches"]), 3)
        byes = [match for match in state["matches"] if len(match["players"]) == 1]
        self.assertEqual(len(byes), 1)
        self.assertEqual(byes[0]["status"], "completed")

    def test_only_referee_can_report_result_and_replay_is_idempotent(self):
        self.join(1)
        self.join(2)
        state = self.manager.state(self.tournament_id, now=self.now + 30)
        match = state["matches"][0]
        with self.assertRaises(TournamentError):
            self.manager.report_match_result(self.tournament_id, match["match_id"], 1, referee_id=123)
        first = self.manager.report_match_result(self.tournament_id, match["match_id"], 1, referee_id=99)
        replay = self.manager.report_match_result(self.tournament_id, match["match_id"], 1, referee_id=99)
        self.assertEqual(first["completion_nonce"], replay["completion_nonce"])

    def test_result_cannot_name_a_player_from_another_match(self):
        for user_id in (1, 2, 3, 4):
            self.join(user_id)
        state = self.manager.state(self.tournament_id, now=self.now + 30)
        with self.assertRaises(TournamentError):
            self.manager.report_match_result(self.tournament_id, state["matches"][0]["match_id"], 999, referee_id=99)


if __name__ == "__main__":
    unittest.main()


class MatchEntryWindowTests(unittest.TestCase):
    def test_both_players_must_enter_before_match_becomes_ready(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, registration_seconds=1, now=100)
        tid = created["tournament_id"]
        manager.join(tid, 10, "A", now=100.1)
        manager.join(tid, 11, "B", now=100.1)
        state = manager.state(tid, now=101)
        match_id = state["matches"][0]["match_id"]
        waiting = manager.enter_match(tid, match_id, 10, now=101.1)
        self.assertEqual(waiting["matches"][0]["status"], "waiting_entry")
        ready = manager.enter_match(tid, match_id, 11, now=101.2)
        self.assertEqual(ready["matches"][0]["status"], "ready")
        self.assertEqual(ready["matches"][0]["entered_count"], 2)

    def test_no_player_entry_cancels_and_marks_tournament_for_refund(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, registration_seconds=1, now=100)
        tid = created["tournament_id"]
        manager.join(tid, 10, "A", now=100.1)
        manager.join(tid, 11, "B", now=100.1)
        state = manager.state(tid, now=101)
        self.assertEqual(state["status"], "active")
        manager.tick(now=112)
        state = manager.state(tid, now=112)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(len(manager.pending_refunds()), 1)

    def test_manual_cancel_marks_active_tournament_for_refund(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, registration_seconds=30, now=100)
        tid = created["tournament_id"]
        state = manager.cancel(tid, reason="administrator stop")
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["cancelled_reason"], "administrator stop")
        self.assertEqual(len(manager.pending_refunds()), 1)

    def test_cancel_all_only_affects_non_terminal_tournaments_in_guild(self):
        manager = TournamentManager()
        first = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, now=100)
        second = manager.create(guild_id=2, channel_id=3, created_by=9, prize_amount=100, now=100)
        states = manager.cancel_all(1, reason="stop all")
        self.assertEqual([item["tournament_id"] for item in states], [first["tournament_id"]])
        self.assertEqual(manager.state(second["tournament_id"], now=100)["status"], "registration")

    def test_one_player_entry_forfeits_the_absent_player_without_starting_physics(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, registration_seconds=1, now=100)
        tid = created["tournament_id"]
        manager.join(tid, 10, "A", now=100.1)
        manager.join(tid, 11, "B", now=100.1)
        match_id = manager.state(tid, now=101)["matches"][0]["match_id"]
        manager.enter_match(tid, match_id, 10, now=101.1)
        manager.tick(now=112)
        completed = manager.completion(tid)
        self.assertIsNotNone(completed)
        self.assertEqual(completed["winner_id"], 10)


class SpectatorGuessTests(unittest.TestCase):
    def test_guess_can_be_changed_for_ten_seconds_without_a_stake(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, guess_reward=25, registration_seconds=1, now=100)
        tid = created["tournament_id"]
        manager.join(tid, 10, "A", now=100.1)
        manager.join(tid, 11, "B", now=100.1)
        match_id = manager.state(tid, now=101)["matches"][0]["match_id"]
        manager.enter_match(tid, match_id, 10, now=101.1)
        manager.enter_match(tid, match_id, 11, now=101.2)
        manager.submit_guess(tid, match_id, 50, 10, now=102)
        manager.submit_guess(tid, match_id, 50, 11, now=103)
        with self.assertRaises(TournamentError):
            manager.submit_guess(tid, match_id, 50, 10, now=112)
        manager.report_match_result(tid, match_id, 11, referee_id=9, now=104)
        rewards = manager.pending_guess_rewards(now=112)
        self.assertEqual(len(rewards), 1)
        self.assertEqual(rewards[0]["user_id"], 50)
        self.assertEqual(rewards[0]["amount"], 25)
        self.assertEqual(rewards[0]["channel_id"], 2)
        self.assertEqual(set(rewards[0]["player_names"]), {"A", "B"})
        self.assertEqual(rewards[0]["guessed_winner_name"], "B")

    def test_spectator_lobby_lists_active_matches(self):
        manager = TournamentManager()
        created = manager.create(guild_id=1, channel_id=2, created_by=9, prize_amount=100, registration_seconds=1, now=100)
        tid = created["tournament_id"]
        for user_id in (10, 11, 12, 13):
            manager.join(tid, user_id, str(user_id), now=100.1)
        manager.state(tid, now=101)
        lobbies = manager.pending_spectator_lobbies()
        self.assertEqual(len(lobbies), 1)
        self.assertEqual(len(lobbies[0]["matches"]), 2)
        manager.mark_spectator_lobby_sent(tid, 1)
        self.assertEqual(manager.pending_spectator_lobbies(), [])
