"""Shared filesystem coordination for automatic second-bot guild joins."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "second_bot" / ".runtime"
AUTO_JOIN_QUEUE_PATH = RUNTIME_DIR / "auto_join_queue.json"
SECOND_BOT_STATE_PATH = RUNTIME_DIR / "second_bot_state.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_second_bot_state() -> dict[str, Any]:
    state = _read_json(SECOND_BOT_STATE_PATH, {})
    if not isinstance(state, dict):
        return {"updated_at": 0.0, "user_id": None, "guild_ids": []}
    guild_ids: list[int] = []
    for value in state.get("guild_ids", []):
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            guild_ids.append(value)
    try:
        user_id = int(state.get("user_id")) if state.get("user_id") else None
    except (TypeError, ValueError):
        user_id = None
    return {
        "updated_at": float(state.get("updated_at", 0.0) or 0.0),
        "user_id": user_id,
        "guild_ids": sorted(set(guild_ids)),
    }


def publish_second_bot_state(*, user_id: int | None, guild_ids: list[int] | set[int]) -> None:
    normalized = []
    for value in guild_ids:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized.append(value)
    _write_json(
        SECOND_BOT_STATE_PATH,
        {
            "updated_at": time.time(),
            "user_id": int(user_id) if user_id else None,
            "guild_ids": sorted(set(normalized)),
        },
    )


def load_auto_join_queue() -> dict[str, dict[str, Any]]:
    payload = _read_json(AUTO_JOIN_QUEUE_PATH, {})
    if not isinstance(payload, dict):
        return {}
    queue: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        try:
            guild_id = int(key)
            queued_at = float(value.get("queued_at", 0.0))
        except (TypeError, ValueError):
            continue
        invite_url = str(value.get("invite_url", "")).strip()
        if guild_id > 0 and invite_url:
            queue[str(guild_id)] = {
                "guild_id": guild_id,
                "invite_url": invite_url,
                "queued_at": queued_at,
            }
    return queue


def enqueue_auto_join(*, guild_id: int, invite_url: str) -> bool:
    queue = load_auto_join_queue()
    key = str(int(guild_id))
    if key in queue:
        return False
    queue[key] = {
        "guild_id": int(guild_id),
        "invite_url": str(invite_url).strip(),
        "queued_at": time.time(),
    }
    _write_json(AUTO_JOIN_QUEUE_PATH, queue)
    return True


def remove_auto_join(guild_id: int) -> None:
    queue = load_auto_join_queue()
    if str(int(guild_id)) in queue:
        queue.pop(str(int(guild_id)), None)
        _write_json(AUTO_JOIN_QUEUE_PATH, queue)
