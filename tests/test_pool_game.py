import math
import time
import unittest
from unittest.mock import patch

from dashboard.pool_game import BALL_RADIUS, TABLE_ASPECT, PoolGame


class PoolGameTests(unittest.TestCase):
    def make_game(self):
        return PoolGame.create("tournament:match", [10, 11])

    def finish_test_shot(
        self,
        game,
        *,
        first_contact=1,
        pocketed=None,
        rail_or_pocket=True,
        cue_scratch=False,
        is_break=False,
    ):
        game._shot_was_break = is_break
        game._shot_first_contact = first_contact
        game._shot_pocketed = list(pocketed or [])
        game._shot_rail_or_pocket = rail_or_pocket
        game._shot_cue_scratched = cue_scratch
        game.moving = False
        game._resolve_shot(time.monotonic())

    def test_rack_has_cue_ball_and_all_fifteen_object_balls(self):
        game = self.make_game()
        state = game.public()
        self.assertEqual(len(state["balls"]), 16)
        self.assertEqual({ball["number"] for ball in state["balls"]}, set(range(16)))
        eight = next(ball for ball in state["balls"] if ball["number"] == 8)
        self.assertAlmostEqual(eight["y"], 0.5)
        self.assertEqual(state["phase"], "break")
        self.assertEqual(state["groups"], {"10": None, "11": None})
        object_balls = [ball for ball in game.balls if ball.number]
        for index, first in enumerate(object_balls):
            for second in object_balls[index + 1:]:
                distance = math.hypot((first.x - second.x) * TABLE_ASPECT, first.y - second.y)
                self.assertGreaterEqual(distance, BALL_RADIUS * 2 - 1e-6)

    def test_shot_moves_balls_and_server_completes_the_physics(self):
        game = self.make_game()
        game.shoot(10, 0.0, 0.7)
        self.assertTrue(game.moving)
        self.assertEqual(game.public()["shot_number"], 1)
        for _ in range(600):
            game.step(game.last_update + 0.04)
            if not game.moving:
                break
        state = game.public()
        self.assertFalse(state["moving"])
        self.assertEqual(state["phase"], "open")
        self.assertIsNotNone(state["last_shot"])
        self.assertEqual(state["last_shot"]["first_contact"], 1)
        self.assertEqual(len(state["balls"]), 16)

    def test_physics_completes_at_25_hz(self):
        game = self.make_game()
        game.shoot(10, 0.0, 0.7)
        for _ in range(600):
            game.step(game.last_update + 0.04)
            if not game.moving:
                break
        self.assertFalse(game.moving)

    def test_first_contact_uses_earliest_collision_not_ball_list_order(self):
        game = self.make_game()
        cue = next(ball for ball in game.balls if ball.number == 0)
        later_ball = next(ball for ball in game.balls if ball.number == 1)
        earlier_ball = next(ball for ball in game.balls if ball.number == 15)
        for ball in game.balls:
            if ball.number not in (0, 1, 15):
                ball.pocketed = True
        cue.x, cue.y, cue.vx, cue.vy = 0.2, 0.5, 1.6, 0.0
        later_ball.x, later_ball.y = 0.251, 0.476
        earlier_ball.x, earlier_ball.y = 0.25, 0.524
        game.phase = "open"
        game.moving = True
        game.last_update = time.monotonic()

        for _ in range(600):
            game.step(game.last_update + 0.04)
            if not game.moving:
                break

        self.assertEqual(game.last_shot["first_contact"], 15)

    def test_wrong_player_cannot_shoot(self):
        game = self.make_game()
        with self.assertRaisesRegex(ValueError, "not your turn"):
            game.shoot(11, 0.0, 0.5)

    def test_shot_is_rejected_while_balls_are_moving(self):
        game = self.make_game()
        game.shoot(10, 0.0, 0.7)
        with self.assertRaisesRegex(ValueError, "still moving"):
            game.shoot(10, 0.5, 0.5)

    def test_non_finite_shot_values_are_rejected(self):
        game = self.make_game()
        with self.assertRaisesRegex(ValueError, "finite"):
            game.shoot(10, float("nan"), 0.5)

    def test_legal_break_opens_table_and_pocketed_eight_is_respotted(self):
        game = self.make_game()
        self.finish_test_shot(game, first_contact=1, pocketed=[1, 8], is_break=True)
        self.assertEqual(game.phase, "open")
        self.assertEqual(game.current_player_id, 10)
        self.assertIsNone(game.winner_id)
        eight = next(ball for ball in game.balls if ball.number == 8)
        self.assertFalse(eight.pocketed)
        self.assertEqual(game.groups, {10: None, 11: None})

    def test_respotted_eight_avoids_a_ball_occupying_the_foot_spot(self):
        game = self.make_game()
        blocking_ball = next(ball for ball in game.balls if ball.number == 1)
        blocking_ball.x, blocking_ball.y = 0.78, 0.5
        self.finish_test_shot(game, first_contact=1, pocketed=[1, 8], is_break=True)
        eight = next(ball for ball in game.balls if ball.number == 8)
        self.assertFalse(eight.pocketed)
        for ball in game.balls:
            if ball.number != 8 and not ball.pocketed:
                self.assertGreaterEqual(
                    math.hypot((eight.x - ball.x) * TABLE_ASPECT, eight.y - ball.y),
                    BALL_RADIUS * 2 + 0.001,
                )

    def test_illegal_break_awards_opponent_ball_in_hand(self):
        game = self.make_game()
        self.finish_test_shot(game, first_contact=1, pocketed=[], rail_or_pocket=False, is_break=True)
        self.assertEqual(game.current_player_id, 11)
        self.assertEqual(game.ball_in_hand_user_id, 11)
        self.assertTrue(game.last_shot["foul"])

    def test_open_table_assigns_groups_only_after_a_legal_pot(self):
        game = self.make_game()
        game.phase = "open"
        self.finish_test_shot(game, first_contact=3, pocketed=[3])
        self.assertEqual(game.groups, {10: "solids", 11: "stripes"})
        self.assertEqual(game.phase, "playing")
        self.assertEqual(game.current_player_id, 10)

    def test_open_table_foul_does_not_assign_groups(self):
        game = self.make_game()
        game.phase = "open"
        self.finish_test_shot(game, first_contact=8, pocketed=[3])
        self.assertEqual(game.groups, {10: None, 11: None})
        self.assertEqual(game.current_player_id, 11)
        self.assertEqual(game.ball_in_hand_user_id, 11)

    def test_wrong_group_first_contact_is_a_foul(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        self.finish_test_shot(game, first_contact=9, pocketed=[1])
        self.assertEqual(game.current_player_id, 11)
        self.assertEqual(game.ball_in_hand_user_id, 11)
        self.assertTrue(game.last_shot["foul"])

    def test_no_rail_or_pocket_after_contact_is_a_foul(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        self.finish_test_shot(game, first_contact=1, pocketed=[], rail_or_pocket=False)
        self.assertEqual(game.current_player_id, 11)
        self.assertTrue(game.last_shot["foul"])

    def test_legal_own_group_pot_retains_turn(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        self.finish_test_shot(game, first_contact=1, pocketed=[1])
        self.assertEqual(game.current_player_id, 10)
        self.assertIsNone(game.ball_in_hand_user_id)

    def test_cue_ball_scratch_gives_opponent_ball_in_hand(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        self.finish_test_shot(game, first_contact=1, pocketed=[1, 0], cue_scratch=True)
        self.assertEqual(game.current_player_id, 11)
        self.assertEqual(game.ball_in_hand_user_id, 11)
        self.assertTrue(next(ball for ball in game.balls if ball.number == 0).pocketed)

    def test_ball_in_hand_placement_is_validated_and_restores_cue_ball(self):
        game = self.make_game()
        game.turn_index = 1
        game.ball_in_hand_user_id = 11
        cue = next(ball for ball in game.balls if ball.number == 0)
        cue.pocketed = True
        with self.assertRaisesRegex(ValueError, "not your turn"):
            game.place_cue_ball(10, 0.3, 0.5)
        with self.assertRaisesRegex(ValueError, "overlap"):
            game.place_cue_ball(11, 0.72, 0.5)
        game.place_cue_ball(11, 0.32, 0.48)
        self.assertFalse(cue.pocketed)
        self.assertAlmostEqual(cue.x, 0.32)
        self.assertAlmostEqual(cue.y, 0.48)
        self.assertIsNone(game.ball_in_hand_user_id)

    def test_shooting_is_blocked_until_cue_ball_is_placed(self):
        game = self.make_game()
        game.ball_in_hand_user_id = 10
        with self.assertRaisesRegex(ValueError, "place the cue ball"):
            game.shoot(10, 0.0, 0.5)

    def test_early_eight_ball_loses_the_match(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        self.finish_test_shot(game, first_contact=1, pocketed=[8])
        self.assertEqual(game.phase, "finished")
        self.assertEqual(game.winner_id, 11)
        self.assertEqual(game.loser_id, 10)

    def test_eight_ball_after_clearing_group_wins(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        for ball in game.balls:
            if 1 <= ball.number <= 7:
                ball.pocketed = True
        self.finish_test_shot(game, first_contact=8, pocketed=[8])
        self.assertEqual(game.phase, "finished")
        self.assertEqual(game.winner_id, 10)
        self.assertEqual(game.public()["winner_id"], 10)

    def test_scratch_while_pocketing_eight_loses(self):
        game = self.make_game()
        game.phase = "playing"
        game.groups = {10: "solids", 11: "stripes"}
        for ball in game.balls:
            if 1 <= ball.number <= 7:
                ball.pocketed = True
        self.finish_test_shot(game, first_contact=8, pocketed=[0, 8], cue_scratch=True)
        self.assertEqual(game.winner_id, 11)
        self.assertEqual(game.loser_id, 10)

    def test_turn_timeout_switches_authoritatively(self):
        game = self.make_game()
        started = time.monotonic()
        game.start_turn(now=started)
        self.assertEqual(game.public()["turn_user_id"], 10)
        game.step(now=started + 31.0)
        self.assertEqual(game.public()["turn_user_id"], 11)
        self.assertGreater(game.public()["turn_remaining"], 0)

    def test_turn_remaining_property_matches_public_state(self):
        game = self.make_game()
        game.turn_deadline = 112.5
        with patch("dashboard.pool_game.time.monotonic", return_value=100.0):
            self.assertEqual(game.turn_remaining, 13)
            self.assertEqual(game.public()["turn_remaining"], 13)


if __name__ == "__main__":
    unittest.main()
