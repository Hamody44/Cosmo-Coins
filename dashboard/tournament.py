"""Authoritative in-process tournament state machine for the 8Ball activity.

This module owns tournament transitions; the browser only requests joins and
reports referee-approved match results. It deliberately never credits Cosmo
itself: payout remains a bot/database responsibility after a signed completion.
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "cosmo-8ball-v1"
MAX_PLAYERS = 64


class TournamentError(ValueError):
    pass


@dataclass(frozen=True)
class Player:
    user_id: int
    display_name: str


@dataclass
class Match:
    match_id: str
    round_number: int
    slot: int
    player_ids: list[int]
    status: str = "ready"
    winner_id: int | None = None
    loser_id: int | None = None
    result_nonce: str | None = None
    completed_at: float | None = None
    entry_deadline: float | None = None
    entered_ids: set[int] = field(default_factory=set)
    launch_sent: bool = False
    guess_deadline: float | None = None
    guesses: dict[int, int] = field(default_factory=dict)
    guess_prompt_sent: bool = False
    guess_settled_ids: set[int] = field(default_factory=set)

    def public(self, players: dict[int, Player]) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "round": self.round_number,
            "slot": self.slot,
            "players": [
                {"user_id": player_id, "display_name": players[player_id].display_name}
                for player_id in self.player_ids if player_id in players
            ],
            "status": self.status,
            "entry_deadline": int(self.entry_deadline) if self.entry_deadline else None,
            "entry_remaining": max(0, int(self.entry_deadline - time.time())) if self.entry_deadline and self.status == "waiting_entry" else 0,
            "entered_ids": sorted(self.entered_ids),
            "entered_count": len(self.entered_ids),
            "guess_deadline": int(self.guess_deadline) if self.guess_deadline else None,
            "guess_remaining": max(0, int(self.guess_deadline - time.time())) if self.guess_deadline and self.status == "ready" else 0,
            "guess_open": bool(self.guess_deadline and self.status == "ready" and time.time() <= self.guess_deadline),
            "winner_id": self.winner_id,
            "loser_id": self.loser_id,
        }


@dataclass
class Tournament:
    tournament_id: str
    guild_id: int
    channel_id: int
    created_by: int
    prize_amount: int
    condition: str | None
    registration_seconds: int
    created_at: float
    registration_deadline: float
    status: str = "registration"
    players: dict[int, Player] = field(default_factory=dict)
    round_number: int = 0
    matches: dict[str, Match] = field(default_factory=dict)
    round_match_ids: list[str] = field(default_factory=list)
    winner_id: int | None = None
    completed_at: float | None = None
    completion_nonce: str | None = None
    cancelled_reason: str | None = None
    settlement_status: str = "pending"
    guess_reward: int = 0
    spectator_rounds_sent: set[int] = field(default_factory=set)

    def state(self, viewer_id: int | None = None, now: float | None = None) -> dict[str, Any]:
        now = float(now if now is not None else time.time())
        visible_matches = [self.matches[mid].public(self.players) for mid in self.round_match_ids if mid in self.matches]
        return {
            "protocol": PROTOCOL_VERSION,
            "tournament_id": self.tournament_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "status": self.status,
            "registration_deadline": int(self.registration_deadline),
            "registration_remaining": max(0, int(self.registration_deadline - now)) if self.status == "registration" else 0,
            "players": [{"user_id": p.user_id, "display_name": p.display_name} for p in self.players.values()],
            "player_count": len(self.players),
            "max_players": MAX_PLAYERS,
            "round": self.round_number,
            "matches": visible_matches,
            "winner_id": self.winner_id,
            "prize_amount": self.prize_amount,
            "has_condition": bool(self.condition),
            "completion_nonce": self.completion_nonce,
            "settlement_status": self.settlement_status,
            "guess_reward": self.guess_reward,
            "cancelled_reason": self.cancelled_reason,
            "viewer_id": viewer_id,
        }


class TournamentManager:
    def __init__(self, max_tournaments: int = 100):
        self.max_tournaments = int(max_tournaments)
        self._lock = threading.RLock()
        self._tournaments: dict[str, Tournament] = {}

    def create(
        self,
        *,
        guild_id: int,
        channel_id: int,
        created_by: int,
        prize_amount: int,
        condition: str | None = None,
        registration_seconds: int = 30,
        guess_reward: int = 0,
        now: float | None = None,
    ) -> dict[str, Any]:
        prize_amount = int(prize_amount)
        registration_seconds = int(registration_seconds)
        guess_reward = int(guess_reward)
        if prize_amount <= 0:
            raise TournamentError("prize_amount must be positive")
        if registration_seconds <= 0 or registration_seconds > 300:
            raise TournamentError("registration_seconds must be between 1 and 300")
        if guess_reward < 0:
            raise TournamentError("guess_reward cannot be negative")
        with self._lock:
            active = [t for t in self._tournaments.values() if t.guild_id == int(guild_id) and t.status in ("registration", "active")]
            if active:
                raise TournamentError("an active tournament already exists for this guild")
            if len(self._tournaments) >= self.max_tournaments:
                self._prune_locked()
            now = float(now if now is not None else time.time())
            tournament_id = uuid.uuid4().hex[:12]
            tournament = Tournament(
                tournament_id=tournament_id,
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                created_by=int(created_by),
                prize_amount=prize_amount,
                condition=(str(condition).strip()[:500] if condition else None),
                registration_seconds=registration_seconds,
                created_at=now,
                registration_deadline=now + registration_seconds,
                guess_reward=guess_reward,
            )
            self._tournaments[tournament_id] = tournament
            return tournament.state(now=now)

    def get(self, tournament_id: str) -> Tournament | None:
        with self._lock:
            return self._tournaments.get(str(tournament_id))

    def join(self, tournament_id: str, user_id: int, display_name: str, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            self._advance_if_due_locked(tournament, now)
            if tournament.status != "registration":
                raise TournamentError("registration is closed")
            user_id = int(user_id)
            if user_id <= 0:
                raise TournamentError("invalid user_id")
            if user_id in tournament.players:
                return tournament.state(viewer_id=user_id, now=now)
            if len(tournament.players) >= MAX_PLAYERS:
                raise TournamentError("tournament is full")
            name = " ".join(str(display_name or f"Player {user_id}").split())[:80]
            tournament.players[user_id] = Player(user_id, name or f"Player {user_id}")
            return tournament.state(viewer_id=user_id, now=now)

    def tick(self, now: float | None = None) -> list[dict[str, Any]]:
        transitions = []
        with self._lock:
            for tournament in list(self._tournaments.values()):
                before = (tournament.status, tournament.round_number, tournament.winner_id)
                self._advance_if_due_locked(tournament, now)
                self._expire_match_entries_locked(tournament, float(now if now is not None else time.time()))
                after = (tournament.status, tournament.round_number, tournament.winner_id)
                if after != before:
                    transitions.append(tournament.state(now=now))
            self._prune_locked(now=now)
        return transitions

    def state(self, tournament_id: str, viewer_id: int | None = None, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            self._advance_if_due_locked(tournament, now)
            self._expire_match_entries_locked(tournament, float(now if now is not None else time.time()))
            return tournament.state(viewer_id=viewer_id, now=now)

    def report_match_result(
        self,
        tournament_id: str,
        match_id: str,
        winner_id: int,
        *,
        referee_id: int | None = None,
        result_nonce: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Apply one idempotent result and advance the bracket.

        The WebSocket layer must only call this after authenticating the referee
        or after the future authoritative physics server emits a signed result.
        """
        with self._lock:
            tournament = self._require(tournament_id)
            self._advance_if_due_locked(tournament, now)
            if referee_id is None or int(referee_id) != tournament.created_by:
                raise TournamentError("only the tournament referee may report a result")
            match = tournament.matches.get(str(match_id))
            if not match or match.match_id not in tournament.round_match_ids:
                raise TournamentError("match is not active in the current round")
            winner_id = int(winner_id)
            if winner_id not in match.player_ids:
                raise TournamentError("winner must be one of the match players")
            if match.status == "completed":
                if match.winner_id == winner_id:
                    return tournament.state(viewer_id=referee_id, now=now)
                raise TournamentError("match already has a different result")
            if tournament.status != "active":
                raise TournamentError("tournament is not active")
            match.status = "completed"
            match.winner_id = winner_id
            match.loser_id = next(player_id for player_id in match.player_ids if player_id != winner_id)
            match.result_nonce = str(result_nonce or uuid.uuid4().hex)
            match.completed_at = float(now if now is not None else time.time())
            self._advance_round_if_complete_locked(tournament, now)
            return tournament.state(viewer_id=referee_id, now=now)

    def enter_match(self, tournament_id: str, match_id: str, user_id: int, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            self._advance_if_due_locked(tournament, now)
            self._expire_match_entries_locked(tournament, float(now if now is not None else time.time()))
            match = tournament.matches.get(str(match_id))
            user_id = int(user_id)
            if tournament.status != "active" or not match or match.match_id not in tournament.round_match_ids:
                raise TournamentError("match is not accepting players")
            if match.status != "waiting_entry":
                if user_id in match.entered_ids and match.status == "ready":
                    return tournament.state(viewer_id=user_id, now=now)
                raise TournamentError("match entry is closed")
            if user_id not in match.player_ids:
                raise TournamentError("you are not a player in this match")
            match.entered_ids.add(user_id)
            if len(match.entered_ids) == 2:
                match.status = "ready"
                match.guess_deadline = float(now if now is not None else time.time()) + 10
            return tournament.state(viewer_id=user_id, now=now)

    def submit_guess(self, tournament_id: str, match_id: str, user_id: int, guessed_winner_id: int, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            match = tournament.matches.get(str(match_id))
            now_value = float(now if now is not None else time.time())
            if not match or match.match_id not in tournament.round_match_ids or match.status != "ready":
                raise TournamentError("guessing is not open for this match")
            if not match.guess_deadline or now_value > match.guess_deadline:
                raise TournamentError("the 10-second guessing window is closed")
            if int(guessed_winner_id) not in match.player_ids:
                raise TournamentError("choose one of the two match players")
            match.guesses[int(user_id)] = int(guessed_winner_id)
            return tournament.state(viewer_id=int(user_id), now=now)

    def pending_guess_prompts(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for tournament in self._tournaments.values():
                for match_id in tournament.round_match_ids:
                    match = tournament.matches.get(match_id)
                    if tournament.status == "active" and match and match.status == "ready" and not match.guess_prompt_sent:
                        result.append({"tournament_id": tournament.tournament_id, "guild_id": tournament.guild_id,
                                       "channel_id": tournament.channel_id, "guess_reward": tournament.guess_reward,
                                       "match": match.public(tournament.players)})
            return result

    def mark_guess_prompt_sent(self, tournament_id: str, match_id: str) -> None:
        with self._lock:
            tournament = self._require(tournament_id)
            match = tournament.matches.get(str(match_id))
            if match:
                match.guess_prompt_sent = True

    def pending_guess_rewards(self, now: float | None = None) -> list[dict[str, Any]]:
        now_value = float(now if now is not None else time.time())
        with self._lock:
            result = []
            for tournament in self._tournaments.values():
                for match in tournament.matches.values():
                    if match.status != "completed" or not match.guess_deadline or now_value < match.guess_deadline:
                        continue
                    for user_id, guessed_winner_id in match.guesses.items():
                        if guessed_winner_id == match.winner_id and user_id not in match.guess_settled_ids:
                            result.append({
                                "tournament_id": tournament.tournament_id,
                                "match_id": match.match_id,
                                "guild_id": tournament.guild_id,
                                "channel_id": tournament.channel_id,
                                "user_id": user_id,
                                "amount": tournament.guess_reward,
                                "winner_id": match.winner_id,
                                "guessed_winner_name": tournament.players.get(guessed_winner_id, Player(guessed_winner_id, str(guessed_winner_id))).display_name,
                                "player_names": [
                                    tournament.players.get(player_id, Player(player_id, str(player_id))).display_name
                                    for player_id in match.player_ids
                                ],
                            })
            return result

    def mark_guess_reward_settled(self, tournament_id: str, match_id: str, user_id: int) -> None:
        with self._lock:
            tournament = self._require(tournament_id)
            match = tournament.matches.get(str(match_id))
            if match:
                match.guess_settled_ids.add(int(user_id))

    def pending_match_launches(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for tournament in self._tournaments.values():
                for match_id in tournament.round_match_ids:
                    match = tournament.matches.get(match_id)
                    if tournament.status == "active" and match and match.status == "waiting_entry" and not match.launch_sent:
                        result.append({
                            "tournament_id": tournament.tournament_id,
                            "guild_id": tournament.guild_id,
                            "channel_id": tournament.channel_id,
                            "match": match.public(tournament.players),
                        })
            return result

    def mark_match_launch_sent(self, tournament_id: str, match_id: str) -> None:
        with self._lock:
            tournament = self._require(tournament_id)
            match = tournament.matches.get(str(match_id))
            if match:
                match.launch_sent = True

    def pending_spectator_lobbies(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for tournament in self._tournaments.values():
                if tournament.status != "active" or tournament.round_number in tournament.spectator_rounds_sent:
                    continue
                matches = [tournament.matches[mid].public(tournament.players) for mid in tournament.round_match_ids if mid in tournament.matches]
                result.append({"tournament_id": tournament.tournament_id, "guild_id": tournament.guild_id,
                               "channel_id": tournament.channel_id, "round": tournament.round_number, "matches": matches})
            return result

    def mark_spectator_lobby_sent(self, tournament_id: str, round_number: int) -> None:
        with self._lock:
            tournament = self._require(tournament_id)
            tournament.spectator_rounds_sent.add(int(round_number))

    def cancel(self, tournament_id: str, reason: str = "cancelled") -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            if tournament.status in ("completed", "cancelled"):
                return tournament.state()
            tournament.status = "cancelled"
            tournament.cancelled_reason = str(reason)[:300]
            return tournament.state()

    def completion(self, tournament_id: str) -> dict[str, Any] | None:
        with self._lock:
            tournament = self._require(tournament_id)
            if tournament.status != "completed":
                return None
            return {
                "tournament_id": tournament.tournament_id,
                "guild_id": tournament.guild_id,
                "channel_id": tournament.channel_id,
                "winner_id": tournament.winner_id,
                "prize_amount": tournament.prize_amount,
                "condition": tournament.condition,
                "completion_nonce": tournament.completion_nonce,
                "settlement_status": tournament.settlement_status,
            }

    def pending_completions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.completion(t.tournament_id) for t in self._tournaments.values()
                    if t.status == "completed" and t.settlement_status in ("pending", "review_notified")]

    def mark_settled(self, tournament_id: str) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            tournament.settlement_status = "paid"
            return tournament.state()

    def mark_review_notified(self, tournament_id: str) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            if tournament.settlement_status == "pending":
                tournament.settlement_status = "review_notified"
            return tournament.state()

    def pending_refunds(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "tournament_id": t.tournament_id,
                    "guild_id": t.guild_id,
                    "channel_id": t.channel_id,
                    "prize_amount": t.prize_amount,
                    "reason": t.cancelled_reason,
                }
                for t in self._tournaments.values()
                if t.status == "cancelled" and t.settlement_status == "pending_refund"
            ]

    def mark_refunded(self, tournament_id: str) -> dict[str, Any]:
        with self._lock:
            tournament = self._require(tournament_id)
            tournament.settlement_status = "refunded"
            return tournament.state()

    def _require(self, tournament_id: str) -> Tournament:
        tournament = self._tournaments.get(str(tournament_id))
        if not tournament:
            raise TournamentError("tournament not found")
        return tournament

    def _advance_if_due_locked(self, tournament: Tournament, now: float | None = None) -> None:
        now = float(now if now is not None else time.time())
        if tournament.status == "registration" and now >= tournament.registration_deadline:
            if len(tournament.players) < 2:
                tournament.status = "cancelled"
                tournament.cancelled_reason = "not enough players"
                return
            self._seed_first_round_locked(tournament, now)

    def _seed_first_round_locked(self, tournament: Tournament, now: float | None = None) -> None:
        now = float(now if now is not None else time.time())
        ids = list(tournament.players)
        random.SystemRandom().shuffle(ids)
        tournament.status = "active"
        tournament.round_number = 1
        tournament.round_match_ids = []
        for slot in range(0, len(ids), 2):
            players = ids[slot:slot + 2]
            match_id = f"{tournament.tournament_id}-r1-m{slot // 2 + 1}"
            match = Match(match_id, 1, slot // 2 + 1, players, status="waiting_entry", entry_deadline=now + 10)
            tournament.matches[match_id] = match
            tournament.round_match_ids.append(match_id)
            if len(players) == 1:
                match.status = "completed"
                match.winner_id = players[0]
                match.result_nonce = f"bye:{match_id}"
                match.completed_at = now
        self._advance_round_if_complete_locked(tournament, now)

    def _advance_round_if_complete_locked(self, tournament: Tournament, now: float | None = None) -> None:
        current = [tournament.matches[mid] for mid in tournament.round_match_ids]
        if not current or any(match.status != "completed" for match in current):
            return
        winners = [match.winner_id for match in current if match.winner_id is not None]
        if len(winners) == 1:
            tournament.status = "completed"
            tournament.winner_id = int(winners[0])
            tournament.completed_at = float(now if now is not None else time.time())
            tournament.completion_nonce = uuid.uuid4().hex
            return
        tournament.round_number += 1
        tournament.round_match_ids = []
        for slot in range(0, len(winners), 2):
            players = [int(item) for item in winners[slot:slot + 2]]
            match_id = f"{tournament.tournament_id}-r{tournament.round_number}-m{slot // 2 + 1}"
            match = Match(match_id, tournament.round_number, slot // 2 + 1, players, status="waiting_entry", entry_deadline=float(now if now is not None else time.time()) + 10)
            tournament.matches[match_id] = match
            tournament.round_match_ids.append(match_id)
            if len(players) == 1:
                match.status = "completed"
                match.winner_id = players[0]
                match.result_nonce = f"bye:{match_id}"
                match.completed_at = float(now if now is not None else time.time())
        self._advance_round_if_complete_locked(tournament, now)

    def _expire_match_entries_locked(self, tournament: Tournament, now: float) -> None:
        if tournament.status != "active":
            return
        changed = False
        for match_id in list(tournament.round_match_ids):
            match = tournament.matches.get(match_id)
            if not match or match.status != "waiting_entry" or not match.entry_deadline or now < match.entry_deadline:
                continue
            if len(match.entered_ids) == 2:
                match.status = "ready"
                match.guess_deadline = now + 10
            elif len(match.entered_ids) == 1:
                match.status = "completed"
                match.winner_id = next(iter(match.entered_ids))
                match.loser_id = next(pid for pid in match.player_ids if pid != match.winner_id)
                match.result_nonce = f"entry-forfeit:{match.match_id}"
                match.completed_at = now
            else:
                tournament.status = "cancelled"
                tournament.cancelled_reason = "both players did not enter the match within 10 seconds"
                tournament.settlement_status = "pending_refund"
                return
            changed = True
        if changed:
            self._advance_round_if_complete_locked(tournament, now)

    def _prune_locked(self, now: float | None = None) -> None:
        now = float(now if now is not None else time.time())
        completed = [t for t in self._tournaments.values() if t.status in ("completed", "cancelled") and (t.completed_at or t.created_at) < now - 86400]
        for tournament in completed:
            self._tournaments.pop(tournament.tournament_id, None)
