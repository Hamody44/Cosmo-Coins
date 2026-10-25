import unittest

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

    def test_wrong_player_cannot_shoot(self):
        game = PoolGame.create("tournament:match", [10, 11])
        with self.assertRaises(ValueError):
            game.shoot(11, 0.0, 0.5)


if __name__ == "__main__":
    unittest.main()
