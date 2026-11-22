"""Authoritative, deterministic-enough 8-ball simulation for tournament matches.

The server owns every position, collision, foul, group assignment, turn, and
result. Browser clients render ``public()`` and submit aim/power or cue-ball
placement requests; they never decide whether a shot was legal or who won.
Coordinates are normalized to the playable cloth (0..1).
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field


POCKETS = ((0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (0.0, 1.0), (0.5, 1.0), (1.0, 1.0))
TABLE_ASPECT = 2.0
BALL_RADIUS = 0.0235
POCKET_RADIUS = 0.060
TURN_SECONDS = 30
DRAG_PER_FRAME = 0.982


def _table_distance(dx: float, dy: float) -> float:
    """Measure normalized positions in physical table units (width:height 2:1)."""
    return math.hypot(dx * TABLE_ASPECT, dy)


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
    phase: str = "break"
    groups: dict[int, str | None] = field(default_factory=dict)
    winner_id: int | None = None
    loser_id: int | None = None
    ball_in_hand_user_id: int | None = None
    last_shot: dict | None = None
    result_reported: bool = False
    _shot_first_contact: int | None = field(default=None, repr=False)
    _shot_pocketed: list[int] = field(default_factory=list, repr=False)
    _shot_cue_scratched: bool = field(default=False, repr=False)
    _shot_rail_or_pocket: bool = field(default=False, repr=False)
    _shot_rail_hits: set[int] = field(default_factory=set, repr=False)
    _shot_was_break: bool = field(default=False, repr=False)

    @classmethod
    def create(cls, match_key: str, player_ids: list[int]) -> "PoolGame":
        if len(player_ids) != 2 or int(player_ids[0]) == int(player_ids[1]):
            raise ValueError("a pool game needs exactly two distinct players")
        ids = (int(player_ids[0]), int(player_ids[1]))
        game = cls(
            match_key=str(match_key),
            player_ids=ids,
            groups={ids[0]: None, ids[1]: None},
        )
        game._rack_balls()
        return game

    def _rack_balls(self) -> None:
        """Set a 15-ball triangle with the 8 centered and mixed rear corners."""
        rng = random.Random(self.match_key)
        solids = list(range(1, 8))
        stripes = list(range(9, 16))
        rng.shuffle(solids)
        rng.shuffle(stripes)
        solids.remove(1)  # the apex ball is fixed at number 1
        rear_solid, rear_stripe = solids.pop(), stripes.pop()
        remaining = solids + stripes
        rng.shuffle(remaining)

        positions: dict[int, tuple[float, float]] = {}
        number_at: dict[tuple[int, int], int] = {
            (0, 0): 1,
            (2, 1): 8,
            (4, 0): rear_solid,
            (4, 4): rear_stripe,
        }
        next_number = iter(remaining)
        for row in range(5):
            for column in range(row + 1):
                if (row, column) not in number_at:
                    number_at[(row, column)] = next(next_number)
                number = number_at[(row, column)]
                positions[number] = (
                    0.685 + row * BALL_RADIUS * math.sqrt(3) / TABLE_ASPECT,
                    0.5 + (column - row / 2) * BALL_RADIUS * 2,
                )

        self.balls = [Ball(0, 0.22, 0.5)]
        self.balls.extend(
            Ball(number, x, y)
            for number, (x, y) in sorted(positions.items())
        )

    @property
    def current_player_id(self) -> int | None:
        return self.player_ids[self.turn_index]

    @property
    def turn_remaining(self) -> int:
        if self.turn_deadline is not None and not self.moving and self.phase != "finished":
            return max(0, int(self.turn_deadline - time.monotonic() + 0.999))
        return 0

    def start_turn(self, now: float | None = None) -> None:
        if self.turn_deadline is None and not self.moving and self.phase != "finished":
            current = float(now if now is not None else time.monotonic())
            self.turn_deadline = current + TURN_SECONDS

    def shoot(self, user_id: int, angle: float, power: float) -> None:
        self._expire_turn_if_needed()
        if self.phase == "finished":
            raise ValueError("this match is already finished")
        if int(user_id) != self.current_player_id:
            raise ValueError("it is not your turn")
        if self.moving:
            raise ValueError("the table is still moving")
        if self.ball_in_hand_user_id is not None:
            raise ValueError("place the cue ball before shooting")
        if not math.isfinite(float(angle)) or not math.isfinite(float(power)):
            raise ValueError("angle and power must be finite numbers")

        cue = self._cue_ball()
        if cue.pocketed:
            raise ValueError("the cue ball must be placed before shooting")
        power = max(0.10, min(float(power), 1.0))
        angle = float(angle)
        speed = 1.60 * power
        cue.vx = math.cos(angle) * speed
        cue.vy = math.sin(angle) * speed
        self.moving = True
        self.turn_deadline = None
        self.shot_number += 1
        self.last_update = time.monotonic()
        self._shot_first_contact = None
        self._shot_pocketed = []
        self._shot_cue_scratched = False
        self._shot_rail_or_pocket = False
        self._shot_rail_hits = set()
        self._shot_was_break = self.phase == "break"

    def place_cue_ball(self, user_id: int, x: float, y: float) -> None:
        self._expire_turn_if_needed()
        if self.phase == "finished":
            raise ValueError("this match is already finished")
        if int(user_id) != self.current_player_id:
            raise ValueError("it is not your turn")
        if self.moving:
            raise ValueError("the table is still moving")
        if self.ball_in_hand_user_id != int(user_id):
            raise ValueError("you do not have the cue ball in hand")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError) as exc:
            raise ValueError("cue-ball position is invalid") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("cue-ball position is invalid")
        if not (BALL_RADIUS / TABLE_ASPECT <= x <= 1 - BALL_RADIUS / TABLE_ASPECT and BALL_RADIUS <= y <= 1 - BALL_RADIUS):
            raise ValueError("place the cue ball inside the table")
        if any(_table_distance(x - px, y - py) < POCKET_RADIUS for px, py in POCKETS):
            raise ValueError("the cue ball cannot be placed in a pocket")
        for ball in self.balls:
            if ball.number and not ball.pocketed and _table_distance(x - ball.x, y - ball.y) < BALL_RADIUS * 2.05:
                raise ValueError("the cue ball cannot overlap another ball")
        cue = self._cue_ball()
        cue.x, cue.y = x, y
        cue.vx = cue.vy = 0.0
        cue.pocketed = False
        self.ball_in_hand_user_id = None
        self.start_turn()

    def step(self, now: float | None = None) -> bool:
        now = float(now if now is not None else time.monotonic())
        if self.phase == "finished":
            return False
        if not self.moving:
            if self.turn_deadline is not None and now >= self.turn_deadline:
                self._expire_turn_if_needed(now)
                return True
            return False

        dt = max(0.0, min(now - self.last_update, 0.05))
        self.last_update = now
        if dt <= 0:
            return False

        substeps = max(1, math.ceil(dt / 0.008))
        sub_dt = dt / substeps
        drag = DRAG_PER_FRAME ** (sub_dt * 60)
        for _ in range(substeps):
            active = [ball for ball in self.balls if not ball.pocketed]
            if self._shot_first_contact is None:
                self._shot_first_contact = self._first_cue_contact(active, sub_dt)
            for ball in active:
                ball.x += ball.vx * sub_dt / TABLE_ASPECT
                ball.y += ball.vy * sub_dt
                ball.vx *= drag
                ball.vy *= drag
                self._bounce_from_rails(ball)
                self._pocket_if_needed(ball)

            active = [ball for ball in self.balls if not ball.pocketed]
            for index, first in enumerate(active):
                for second in active[index + 1:]:
                    self._resolve_ball_collision(first, second)

        still_moving = any(
            abs(ball.vx) > 0.010 or abs(ball.vy) > 0.010
            for ball in self.balls if not ball.pocketed
        )
        if not still_moving:
            for ball in self.balls:
                ball.vx = ball.vy = 0.0
            self.moving = False
            self._resolve_shot(now)
        return True

    def _bounce_from_rails(self, ball: Ball) -> None:
        bounced = False
        horizontal_radius = BALL_RADIUS / TABLE_ASPECT
        if ball.x < horizontal_radius:
            ball.x = horizontal_radius
            ball.vx = abs(ball.vx) * 0.86
            bounced = True
        elif ball.x > 1.0 - horizontal_radius:
            ball.x = 1.0 - horizontal_radius
            ball.vx = -abs(ball.vx) * 0.86
            bounced = True
        if ball.y < BALL_RADIUS:
            ball.y = BALL_RADIUS
            ball.vy = abs(ball.vy) * 0.86
            bounced = True
        elif ball.y > 1.0 - BALL_RADIUS:
            ball.y = 1.0 - BALL_RADIUS
            ball.vy = -abs(ball.vy) * 0.86
            bounced = True
        if bounced and self._shot_first_contact is not None:
            self._shot_rail_or_pocket = True
            if ball.number:
                self._shot_rail_hits.add(ball.number)

    def _pocket_if_needed(self, ball: Ball) -> None:
        if ball.pocketed:
            return
        if any(_table_distance(ball.x - px, ball.y - py) <= POCKET_RADIUS for px, py in POCKETS):
            ball.pocketed = True
            ball.vx = ball.vy = 0.0
            self._shot_pocketed.append(ball.number)
            if ball.number == 0:
                self._shot_cue_scratched = True
            elif self._shot_first_contact is not None:
                self._shot_rail_or_pocket = True

    def _first_cue_contact(self, active: list[Ball], horizon: float) -> int | None:
        """Return the object ball with the earliest swept cue-ball collision."""
        cue = next((ball for ball in active if ball.number == 0), None)
        if cue is None:
            return None
        earliest_time: float | None = None
        first_number: int | None = None
        diameter = BALL_RADIUS * 2
        for target in active:
            if target.number == 0:
                continue
            rx = (target.x - cue.x) * TABLE_ASPECT
            ry = target.y - cue.y
            vx = target.vx - cue.vx
            vy = target.vy - cue.vy
            c = rx * rx + ry * ry - diameter * diameter
            radial_velocity = rx * vx + ry * vy
            if c <= 1e-12:
                if radial_velocity >= 0:
                    continue
                contact_time = 0.0
            else:
                a = vx * vx + vy * vy
                b = 2 * radial_velocity
                discriminant = b * b - 4 * a * c
                if a <= 1e-12 or b >= 0 or discriminant < 0:
                    continue
                contact_time = (-b - math.sqrt(discriminant)) / (2 * a)
                if contact_time < 0 or contact_time > horizon:
                    continue
            if (
                earliest_time is None
                or contact_time < earliest_time - 1e-9
                or (abs(contact_time - earliest_time) <= 1e-9 and target.number < first_number)
            ):
                earliest_time = contact_time
                first_number = target.number
        return first_number

    def _resolve_ball_collision(self, first: Ball, second: Ball) -> None:
        dx, dy = (second.x - first.x) * TABLE_ASPECT, second.y - first.y
        distance = math.hypot(dx, dy)
        if distance <= 0 or distance >= BALL_RADIUS * 2:
            return
        nx, ny = dx / distance, dy / distance
        relative = (first.vx - second.vx) * nx + (first.vy - second.vy) * ny
        if relative > 0:
            impulse = relative * 0.97
            first.vx -= impulse * nx
            first.vy -= impulse * ny
            second.vx += impulse * nx
            second.vy += impulse * ny
        overlap = BALL_RADIUS * 2 - distance
        first.x -= nx * overlap / 2 / TABLE_ASPECT
        first.y -= ny * overlap / 2
        second.x += nx * overlap / 2 / TABLE_ASPECT
        second.y += ny * overlap / 2

    def _resolve_shot(self, now: float) -> None:
        shooter = self.current_player_id
        if shooter is None:
            return
        opponent = self.player_ids[1 - self.turn_index]
        cue_scratch = self._shot_cue_scratched
        eight_pocketed = 8 in self._shot_pocketed
        foul_reason = None
        retain_turn = False
        won = False
        lost_on_eight = False
        group_before = self.groups.get(shooter)
        own_remaining_before = self._remaining_for(shooter)

        if self._shot_was_break:
            break_pocketed = [number for number in self._shot_pocketed if number != 0]
            if eight_pocketed:
                self._respot_eight_ball()
                self._shot_pocketed = [number for number in self._shot_pocketed if number != 8]
            if cue_scratch:
                foul_reason = "دخلت الكرة البيضاء في الجيب؛ للخصم كرة في اليد."
            elif self._shot_first_contact != 1:
                foul_reason = "ضربة البداية لم تصب الكرة الأمامية أولًا؛ للخصم كرة في اليد."
            elif not break_pocketed and len(self._shot_rail_hits) < 4:
                foul_reason = "ضربة البداية لم تُدخل كرة أو تُوصل أربع كرات إلى الحافة؛ للخصم كرة في اليد."
            self.phase = "open"
            retain_turn = bool(break_pocketed) and foul_reason is None
        else:
            if cue_scratch:
                foul_reason = "دخلت الكرة البيضاء في الجيب؛ للخصم كرة في اليد."
            elif self._shot_first_contact is None:
                foul_reason = "لم تلمس الكرة البيضاء أي كرة؛ للخصم كرة في اليد."
            elif group_before is None and self._shot_first_contact == 8:
                foul_reason = "يجب لمس كرة ملونة أولًا والطاولة مفتوحة؛ للخصم كرة في اليد."
            elif group_before is not None:
                target_is_hit = (
                    self._shot_first_contact == 8
                    if own_remaining_before == 0
                    else self._shot_first_contact in self._group_numbers(group_before)
                )
                if not target_is_hit:
                    foul_reason = "لم تلمس كرة مجموعتك أولًا؛ للخصم كرة في اليد."
            if foul_reason is None and self._shot_first_contact is not None and not self._shot_rail_or_pocket:
                foul_reason = "لم تصل أي كرة إلى الحافة بعد التصادم؛ للخصم كرة في اليد."

            if eight_pocketed:
                if group_before is not None and own_remaining_before == 0 and foul_reason is None:
                    won = True
                else:
                    lost_on_eight = True
            elif foul_reason is None:
                if group_before is None:
                    assigned = next((number for number in self._shot_pocketed if number not in (0, 8)), None)
                    if assigned is not None:
                        assigned_group = self._group_for_number(assigned)
                        self.groups[shooter] = assigned_group
                        self.groups[opponent] = "stripes" if assigned_group == "solids" else "solids"
                        self.phase = "playing"
                        retain_turn = True
                else:
                    own_numbers = self._group_numbers(group_before)
                    retain_turn = any(number in own_numbers for number in self._shot_pocketed)

        if won:
            self.winner_id, self.loser_id = shooter, opponent
            self.phase = "finished"
            message = "أُدخلت الكرة 8 قانونيًا بعد إنهاء مجموعتك؛ فزت بالمباراة!"
        elif lost_on_eight:
            self.winner_id, self.loser_id = opponent, shooter
            self.phase = "finished"
            message = "أُدخلت الكرة 8 قبل أوانها أو مع خطأ؛ خسر اللاعب المباراة."
        elif foul_reason:
            self.ball_in_hand_user_id = opponent
            self.turn_index = 1 - self.turn_index
            self.turn_deadline = now + TURN_SECONDS
            message = foul_reason
        else:
            if self._shot_was_break:
                # A legal break leaves the table open. Pocketing a ball keeps
                # the breaker at the table; otherwise play passes normally.
                pass
            elif not retain_turn:
                self.turn_index = 1 - self.turn_index
            self.ball_in_hand_user_id = None
            self.turn_deadline = now + TURN_SECONDS
            message = "ضربة قانونية؛ دورك مستمر." if retain_turn else "انتهى الدور؛ دور اللاعب الآخر."

        if cue_scratch and self.phase != "finished":
            self._cue_ball().pocketed = True
        self.last_shot = {
            "shooter_id": shooter,
            "foul": bool(foul_reason),
            "message": message,
            "pocketed": list(self._shot_pocketed),
            "first_contact": self._shot_first_contact,
            "winner_id": self.winner_id,
        }
        self._shot_first_contact = None
        self._shot_cue_scratched = False
        self._shot_rail_or_pocket = False
        self._shot_rail_hits.clear()

    def _respot_eight_ball(self) -> None:
        eight = next(ball for ball in self.balls if ball.number == 8)
        preferred = (0.78, 0.5)
        spacing = BALL_RADIUS * 2 + 0.001
        candidates = [preferred]
        for ring in range(1, 24):
            distance = spacing * ring
            for index in range(32):
                angle = 2 * math.pi * index / 32
                candidates.append((preferred[0] + math.cos(angle) * distance / TABLE_ASPECT, preferred[1] + math.sin(angle) * distance))
        # The table is spacious, but a deterministic grid is a final fallback
        # if a crowded break blocks every point around the foot spot.
        candidates.extend((column / 40, row / 40) for column in range(2, 39) for row in range(2, 39))
        candidates.sort(key=lambda point: ((point[0] - preferred[0]) * TABLE_ASPECT) ** 2 + (point[1] - preferred[1]) ** 2)
        for x, y in candidates:
            if not (BALL_RADIUS / TABLE_ASPECT <= x <= 1 - BALL_RADIUS / TABLE_ASPECT and BALL_RADIUS <= y <= 1 - BALL_RADIUS):
                continue
            if any(_table_distance(x - px, y - py) < POCKET_RADIUS for px, py in POCKETS):
                continue
            if any(
                ball.number != 8 and not ball.pocketed and _table_distance(x - ball.x, y - ball.y) < spacing
                for ball in self.balls
            ):
                continue
            eight.x, eight.y = x, y
            break
        else:
            raise RuntimeError("no clear position available to respot the 8-ball")
        eight.pocketed = False
        eight.vx = eight.vy = 0.0

    def _cue_ball(self) -> Ball:
        return next(ball for ball in self.balls if ball.number == 0)

    @staticmethod
    def _group_for_number(number: int) -> str:
        return "solids" if 1 <= int(number) <= 7 else "stripes"

    @staticmethod
    def _group_numbers(group: str) -> set[int]:
        return set(range(1, 8)) if group == "solids" else set(range(9, 16))

    def _remaining_for(self, user_id: int) -> int:
        group = self.groups.get(int(user_id))
        if group is None:
            return 0
        numbers = self._group_numbers(group)
        return sum(1 for ball in self.balls if ball.number in numbers and not ball.pocketed)

    def _expire_turn_if_needed(self, now: float | None = None) -> None:
        if self.turn_deadline is None or self.moving:
            return
        current = float(now if now is not None else time.monotonic())
        if current < self.turn_deadline:
            return
        self.turn_index = 1 - self.turn_index
        if self.ball_in_hand_user_id is not None:
            self.ball_in_hand_user_id = self.current_player_id
        self.turn_deadline = current + TURN_SECONDS

    def public(self) -> dict:
        current = self.current_player_id
        return {
            "match_key": self.match_key,
            "players": list(self.player_ids),
            "turn_user_id": current,
            "moving": self.moving,
            "shot_number": self.shot_number,
            "turn_remaining": self.turn_remaining,
            "phase": self.phase,
            "groups": {str(user_id): group for user_id, group in self.groups.items()},
            "remaining_balls": {str(user_id): self._remaining_for(user_id) for user_id in self.player_ids},
            "ball_in_hand": self.ball_in_hand_user_id == current,
            "ball_in_hand_user_id": self.ball_in_hand_user_id,
            "winner_id": self.winner_id,
            "loser_id": self.loser_id,
            "last_shot": dict(self.last_shot) if self.last_shot else None,
            "balls": [
                {
                    "number": ball.number,
                    "x": round(ball.x, 5),
                    "y": round(ball.y, 5),
                    "pocketed": ball.pocketed,
                }
                for ball in self.balls
            ],
        }
