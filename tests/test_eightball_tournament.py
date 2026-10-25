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
