"""ASGI bridge for the Flask dashboard and the future 8Ball game service.

The Discord game client connects to /game-ws while existing Flask routes continue
through the same public origin and port. Game state is intentionally kept behind
an explicit room protocol; this first layer only provides safe connection,
heartbeat and protocol negotiation primitives.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from asgiref.wsgi import WsgiToAsgi
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from dashboard.app import app as flask_app
from dashboard.game_auth import verify_game_token
from dashboard.tournament import TournamentError, TournamentManager

LOGGER = logging.getLogger("cosmo.realtime")
PROTOCOL_VERSION = "cosmo-8ball-v1"
MAX_MESSAGE_BYTES = 16 * 1024


@dataclass
class GameConnection:
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    room_id: str | None = None
    user_id: int | None = None
    display_name: str | None = None
    authenticated: bool = False


class GameRoomManager:
    """Small in-process connection registry for the single-host deployment.

    The authoritative tournament/match state will be added here in the next
    phase. Connections are bounded and removed on disconnect so a stale browser
    cannot retain a seat forever.
    """

    def __init__(self, max_connections: int = 500):
        self.max_connections = int(max_connections)
        self.connections: dict[int, GameConnection] = {}
        self.rooms: dict[str, set[int]] = {}

    @property
    def connection_count(self) -> int:
        return len(self.connections)

    def add(self, connection: GameConnection) -> bool:
        if len(self.connections) >= self.max_connections:
            return False
        self.connections[id(connection.websocket)] = connection
        return True

    def remove(self, connection: GameConnection) -> None:
        key = id(connection.websocket)
        self.connections.pop(key, None)
        if connection.room_id:
            members = self.rooms.get(connection.room_id)
            if members:
                members.discard(key)
                if not members:
                    self.rooms.pop(connection.room_id, None)

    def join_room(self, connection: GameConnection, room_id: str) -> None:
        room_id = room_id.strip()[:100]
        if not room_id:
            raise ValueError("room_id is required")
        if connection.room_id and connection.room_id != room_id:
            self.leave_room(connection)
        connection.room_id = room_id
        self.rooms.setdefault(room_id, set()).add(id(connection.websocket))

    def leave_room(self, connection: GameConnection) -> None:
        if not connection.room_id:
            return
        members = self.rooms.get(connection.room_id)
        if members:
            members.discard(id(connection.websocket))
            if not members:
                self.rooms.pop(connection.room_id, None)
        connection.room_id = None

    def room_connections(self, room_id: str) -> list[GameConnection]:
        return [self.connections[key] for key in self.rooms.get(room_id, set()) if key in self.connections]


room_manager = GameRoomManager()
tournament_manager = TournamentManager()


async def health(request):
    return JSONResponse({
        "service": "cosmo-8ball-realtime",
        "protocol": PROTOCOL_VERSION,
        "status": "ok",
        "connections": room_manager.connection_count,
    })


async def game_socket(websocket: WebSocket):
    """Handle the safe transport contract used by the future game client."""
    await websocket.accept()
    LOGGER.info("WebSocket accepted path=/game-ws client=%s", websocket.client)
    connection = GameConnection(websocket)
    if not room_manager.add(connection):
        await websocket.close(code=1013, reason="server connection limit")
        return
    try:
        await websocket.send_json({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "server_time": int(time.time()),
            "features": ["heartbeat", "rooms", "tournament-ready", "discord-auth-required"],
        })
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                await websocket.close(code=1009, reason="message too large")
                return
            try:
                message: Any = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "invalid_json"})
                continue
            if not isinstance(message, dict):
                await websocket.send_json({"type": "error", "code": "object_required"})
                continue
            message_type = str(message.get("type", ""))[:40]
            if message_type == "ping":
                await websocket.send_json({"type": "pong", "server_time": int(time.time())})
            elif message_type == "auth":
                try:
                    identity = verify_game_token(str(message.get("token", "")))
                    connection.user_id = identity["user_id"]
                    connection.display_name = identity["display_name"]
                    connection.authenticated = True
                    LOGGER.info("game auth accepted user_id=%s", connection.user_id)
                    print(f"[8Ball] launch ticket accepted user_id={connection.user_id}")
                    await websocket.send_json({
                        "type": "authenticated",
                        "user": {"user_id": connection.user_id, "display_name": connection.display_name},
                    })
                except (TypeError, ValueError, RuntimeError) as exc:
                    await websocket.send_json({"type": "error", "code": "auth_rejected", "message": str(exc)[:160]})
            elif message_type == "join_tournament":
                try:
                    if not connection.authenticated or not connection.user_id:
                        raise TournamentError("authenticate with Discord before joining")
                    tournament_id = str(message.get("tournament_id", ""))[:40]
                    state = tournament_manager.join(
                        tournament_id, connection.user_id, connection.display_name or "",
                    )
                    room_manager.join_room(connection, f"tournament:{tournament_id}")
                    await websocket.send_json({"type": "tournament_state", "state": state})
                    await _broadcast_tournament_state(tournament_id)
                except (TournamentError, TypeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "code": "join_rejected", "message": str(exc)[:200]})
            elif message_type == "tournament_state":
                try:
                    if not connection.authenticated:
                        raise TournamentError("authenticate with Discord before reading tournament state")
                    tournament_id = str(message.get("tournament_id", ""))[:40]
                    state = tournament_manager.state(tournament_id, viewer_id=connection.user_id)
                    await websocket.send_json({"type": "tournament_state", "state": state})
                except (TournamentError, TypeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "code": "state_unavailable", "message": str(exc)[:200]})
            elif message_type == "watch_match":
                try:
                    tournament_id = str(message.get("tournament_id", ""))[:40]
                    match_id = str(message.get("match_id", ""))[:100]
                    tournament = tournament_manager.get(tournament_id)
                    if not tournament or match_id not in tournament.matches:
                        raise TournamentError("match not found")
                    room_manager.join_room(connection, f"tournament:{tournament_id}")
                    state = tournament_manager.state(tournament_id)
                    LOGGER.info("spectator joined tournament=%s match=%s", tournament_id, match_id)
                    await websocket.send_json({"type": "tournament_state", "state": state, "watching_match": match_id})
                except (TournamentError, TypeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "code": "watch_rejected", "message": str(exc)[:200]})
            elif message_type == "enter_match":
                try:
                    if not connection.authenticated or not connection.user_id:
                        raise TournamentError("authenticate with the Discord launch button first")
                    tournament_id = str(message.get("tournament_id", ""))[:40]
                    match_id = str(message.get("match_id", ""))[:100]
                    state = tournament_manager.enter_match(tournament_id, match_id, connection.user_id)
                    room_manager.join_room(connection, f"tournament:{tournament_id}")
                    LOGGER.info("match entry accepted tournament=%s match=%s user_id=%s", tournament_id, match_id, connection.user_id)
                    entered = next((item.get("entered_count") for item in state.get("matches", []) if item.get("match_id") == match_id), "?")
                    print(f"[8Ball] player entered tournament={tournament_id} match={match_id} user_id={connection.user_id} entered={entered}")
                    await _broadcast_tournament_state(tournament_id, state=state)
                except (TournamentError, TypeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "code": "match_entry_rejected", "message": str(exc)[:200]})
            elif message_type == "report_match_result":
                try:
                    if not connection.authenticated or not connection.user_id:
                        raise TournamentError("authenticate with Discord before reporting a result")
                    tournament_id = str(message.get("tournament_id", ""))[:40]
                    tournament = tournament_manager.get(tournament_id)
                    if not tournament or connection.user_id != tournament.created_by:
                        raise TournamentError("only the tournament referee may report a result")
                    state = tournament_manager.report_match_result(
                        tournament_id,
                        str(message.get("match_id", ""))[:100],
                        int(message.get("winner_id", 0)),
                        referee_id=connection.user_id,
                        result_nonce=str(message.get("result_nonce", ""))[:120] or None,
                    )
                    await _broadcast_tournament_state(tournament_id, state=state)
                except (TournamentError, TypeError, ValueError) as exc:
                    await websocket.send_json({"type": "error", "code": "result_rejected", "message": str(exc)[:200]})
            elif message_type == "join_room":
                try:
                    room_manager.join_room(connection, str(message.get("room_id", "")))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "code": "invalid_room", "message": str(exc)})
                    continue
                await websocket.send_json({
                    "type": "room_joined",
                    "room_id": connection.room_id,
                    "members": len(room_manager.room_connections(connection.room_id)),
                })
            elif message_type == "leave_room":
                room_manager.leave_room(connection)
                await websocket.send_json({"type": "room_left"})
            else:
                # Do not accept gameplay mutations until the authoritative room
                # and tournament state machine is implemented.
                await websocket.send_json({"type": "error", "code": "unsupported_message"})
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("Unexpected realtime WebSocket failure")
    finally:
        LOGGER.info("WebSocket closed user_id=%s room=%s", connection.user_id, connection.room_id)
        room_manager.remove(connection)


async def _broadcast_tournament_state(tournament_id: str, state: dict[str, Any] | None = None) -> None:
    state = state or tournament_manager.state(tournament_id)
    room_id = f"tournament:{tournament_id}"
    for connection in room_manager.room_connections(room_id):
        try:
            await connection.websocket.send_json({"type": "tournament_state", "state": state})
        except Exception:
            room_manager.remove(connection)


async def _tournament_reaper() -> None:
    while True:
        try:
            transitions = tournament_manager.tick()
            for state in transitions:
                await _broadcast_tournament_state(state["tournament_id"], state=state)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Tournament reaper failure")
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_app):
    reaper = asyncio.create_task(_tournament_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)


asgi_app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/game-health", health, methods=["GET"]),
        WebSocketRoute("/game-ws", game_socket),
        Mount("/", app=WsgiToAsgi(flask_app)),
    ]
)
