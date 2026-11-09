import time
import unittest
from unittest.mock import patch

from dashboard.pool_game import PoolGame


class PoolGameTests(unittest.TestCase):
    def test_two_clients_share_one_authoritative_game_state(self):
        game = PoolGame.create("tournament:match", [10, 11])
        before = game.public()
        self.assertEqual(before["turn_user_id"], 10)
        game.shoot(10, 0.0, 0.7)
        self.assertTrue(game.moving)
        self.assertEqual(game.public()["shot_number"], 1)
        for _ in range(400):
            game.step(game.last_update + 0.05)
            if not game.moving:
                break
        after = game.public()
        self.assertFalse(after["moving"])
        self.assertEqual(after["turn_user_id"], 11)
        self.assertEqual(len(after["balls"]), 16)

    def test_physics_completes_at_25_hz(self):
        game = PoolGame.create("tournament:match", [10, 11])
        game.shoot(10, 0.0, 0.7)
        for _ in range(600):
            game.step(game.last_update + 0.04)
            if not game.moving:
                break
        self.assertFalse(game.moving)

    def test_wrong_player_cannot_shoot(self):
        game = PoolGame.create("tournament:match", [10, 11])
        with self.assertRaises(ValueError):
            game.shoot(11, 0.0, 0.5)

    def test_turn_timeout_switches_authoritatively(self):
        game = PoolGame.create("tournament:match", [10, 11])
        started = time.monotonic()
        game.start_turn(now=started)
        self.assertEqual(game.public()["turn_user_id"], 10)
        game.step(now=started + 31.0)
        self.assertEqual(game.public()["turn_user_id"], 11)
        self.assertGreater(game.public()["turn_remaining"], 0)

    def test_turn_remaining_property_matches_public_state_without_extra_work(self):
        game = PoolGame.create("tournament:match", [10, 11])
        game.turn_deadline = 112.5
        with patch("dashboard.pool_game.time.monotonic", return_value=100.0):
            self.assertEqual(game.turn_remaining, 13)
            self.assertEqual(game.public()["turn_remaining"], 13)


if __name__ == "__main__":
    unittest.main()
