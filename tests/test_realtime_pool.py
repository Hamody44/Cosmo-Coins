import unittest
from unittest.mock import AsyncMock, patch

from dashboard.pool_game import PoolGame
from dashboard.realtime import _report_pool_winner
from dashboard.tournament import TournamentManager


class PoolResultIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_winner_advances_tournament_once(self):
        manager = TournamentManager()
        now = 1_700_000_000.0
        created = manager.create(
            guild_id=1,
            channel_id=2,
            created_by=99,
            prize_amount=100,
            registration_seconds=1,
            now=now,
        )
        tournament_id = created["tournament_id"]
        manager.join(tournament_id, 10, "A", now=now + 0.1)
        manager.join(tournament_id, 11, "B", now=now + 0.1)
        state = manager.state(tournament_id, now=now + 1)
        match = state["matches"][0]
        match_key = f"{tournament_id}:{match['match_id']}"
        game = PoolGame.create(match_key, [10, 11])
        game.phase = "finished"
        game.winner_id = 10
        game.loser_id = 11
        game.shot_number = 7

        with (
            patch("dashboard.realtime.tournament_manager", manager),
            patch("dashboard.realtime._broadcast_tournament_state", new_callable=AsyncMock) as broadcast,
        ):
            await _report_pool_winner(match_key, game)
            await _report_pool_winner(match_key, game)

        completed = manager.state(tournament_id)
        self.assertEqual(completed["matches"][0]["status"], "completed")
        self.assertEqual(completed["matches"][0]["winner_id"], 10)
        self.assertTrue(game.result_reported)
        broadcast.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
