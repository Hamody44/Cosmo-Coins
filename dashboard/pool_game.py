"""Small authoritative 8Ball table simulation for one-process hosting.

Coordinates are normalized to the cloth (0..1). The browser only renders this
state and sends a shot request; it never advances the shared physics.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


POCKETS = ((0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (0.0, 1.0), (0.5, 1.0), (1.0, 1.0))
BALL_RADIUS = 0.026
TURN_SECONDS = 30


@dataclass
class Ball:
    number: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    pocketed: bool = False


@dataclass
class PoolGame:
    match_key: str
    player_ids: tuple[int, int]
    balls: list[Ball] = field(default_factory=list)
    turn_index: int = 0
    moving: bool = False
    last_update: float = field(default_factory=time.monotonic)
    shot_number: int = 0
    started_at: float = field(default_factory=time.time)
    turn_deadline: float | None = None

    @classmethod
    def create(cls, match_key: str, player_ids: list[int]) -> "PoolGame":
        if len(player_ids) != 2:
            raise ValueError("a pool game needs exactly two players")
        game = cls(match_key=match_key, player_ids=(int(player_ids[0]), int(player_ids[1])))
        game.balls.append(Ball(0, 0.25, 0.5))
        index = 1
        start_x, start_y = 0.68, 0.5
        for row in range(5):
            for col in range(row + 1):
                game.balls.append(Ball(index, start_x + row * BALL_RADIUS * 1.82, start_y + (col - row / 2) * BALL_RADIUS * 2.05))
                index += 1
        return game

    @property
    def current_player_id(self) -> int | None:
        return self.player_ids[self.turn_index] if not self.moving else self.player_ids[self.turn_index]

    def shoot(self, user_id: int, angle: float, power: float) -> None:
        self._expire_turn_if_needed()
        if int(user_id) != self.current_player_id:
            raise ValueError("it is not your turn")
        if self.moving:
            raise ValueError("the table is still moving")
        cue = next(ball for ball in self.balls if ball.number == 0)
        if cue.pocketed:
            cue.pocketed = False
            cue.x, cue.y = 0.25, 0.5
        power = max(0.08, min(float(power), 1.0))
        angle = float(angle)
        speed = 1.65 * power
        cue.vx = math.cos(angle) * speed
        cue.vy = math.sin(angle) * speed
        self.moving = True
        self.turn_deadline = None
        self.shot_number += 1
        self.last_update = time.monotonic()

    def step(self, now: float | None = None) -> bool:
        now = float(now if now is not None else time.monotonic())
        if not self.moving:
            if self.turn_deadline is not None and now >= self.turn_deadline:
                self.turn_index = (self.turn_index + 1) % 2
                self.turn_deadline = now + TURN_SECONDS
                return True
            return False
        dt = max(0.0, min(now - self.last_update, 0.05))
        self.last_update = now
        if dt <= 0:
            return False
        active = [ball for ball in self.balls if not ball.pocketed]
        for ball in active:
            ball.x += ball.vx * dt
            ball.y += ball.vy * dt
            ball.vx *= 0.985 ** (dt * 60)
            ball.vy *= 0.985 ** (dt * 60)
            if ball.x < BALL_RADIUS or ball.x > 1.0 - BALL_RADIUS:
                ball.x = max(BALL_RADIUS, min(1.0 - BALL_RADIUS, ball.x)); ball.vx *= -0.88
            if ball.y < BALL_RADIUS or ball.y > 1.0 - BALL_RADIUS:
                ball.y = max(BALL_RADIUS, min(1.0 - BALL_RADIUS, ball.y)); ball.vy *= -0.88
            if any(math.hypot(ball.x - px, ball.y - py) < 0.045 for px, py in POCKETS):
                ball.pocketed = True; ball.vx = ball.vy = 0.0

        active = [ball for ball in self.balls if not ball.pocketed]
        for index, first in enumerate(active):
            for second in active[index + 1:]:
                dx, dy = second.x - first.x, second.y - first.y
                distance = math.hypot(dx, dy)
                if distance <= 0 or distance >= BALL_RADIUS * 2:
                    continue
                nx, ny = dx / distance, dy / distance
                relative = (first.vx - second.vx) * nx + (first.vy - second.vy) * ny
                if relative > 0:
                    first.vx -= relative * nx; first.vy -= relative * ny
                    second.vx += relative * nx; second.vy += relative * ny
                overlap = BALL_RADIUS * 2 - distance
                first.x -= nx * overlap / 2; first.y -= ny * overlap / 2
                second.x += nx * overlap / 2; second.y += ny * overlap / 2

        still_moving = any(abs(ball.vx) > 0.012 or abs(ball.vy) > 0.012 for ball in self.balls if not ball.pocketed)
        if not still_moving:
            for ball in self.balls:
                ball.vx = ball.vy = 0.0
            self.moving = False
            self.turn_index = (self.turn_index + 1) % 2
            self.turn_deadline = now + TURN_SECONDS
        return True

    def start_turn(self, now: float | None = None) -> None:
        if self.turn_deadline is None and not self.moving:
            self.turn_deadline = float(now if now is not None else time.monotonic()) + TURN_SECONDS

    def _expire_turn_if_needed(self) -> None:
        if self.turn_deadline is not None and time.monotonic() >= self.turn_deadline:
            self.turn_index = (self.turn_index + 1) % 2
            self.turn_deadline = time.monotonic() + TURN_SECONDS

    def public(self) -> dict:
        return {
            "match_key": self.match_key,
            "players": list(self.player_ids),
            "turn_user_id": self.current_player_id,
            "moving": self.moving,
            "shot_number": self.shot_number,
            "turn_remaining": max(0, int(self.turn_deadline - time.monotonic() + 0.999)) if self.turn_deadline and not self.moving else 0,
            "balls": [{"number": b.number, "x": round(b.x, 5), "y": round(b.y, 5), "pocketed": b.pocketed} for b in self.balls],
        }
