"""File-backed control protocol shared by the primary bot and the supervisor.

The protocol deliberately uses one JSON file per request. A request is moved
atomically between inbox/processing/done/failed, which prevents the old
single-file race where a fast second request could overwrite the first one.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT_DIR = Path(__file__).resolve().parent.parent
REQUESTS_DIR = ROOT_DIR / "requests"
INBOX_DIR = REQUESTS_DIR / "inbox"
PROCESSING_DIR = REQUESTS_DIR / "processing"
DONE_DIR = REQUESTS_DIR / "done"
FAILED_DIR = REQUESTS_DIR / "failed"
STATE_DIR = ROOT_DIR / "second_bot" / ".runtime" / "process_control"
STATUS_PATH = STATE_DIR / "status.json"
STATUS_LOCK_PATH = STATE_DIR / ".status.lock"
LOCK_PATH = REQUESTS_DIR / ".lock"
VALID_TARGETS = {"primary", "second_bot"}
VALID_ACTIONS = {"start", "restart", "shutdown"}
REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def ensure_layout() -> None:
    for path in (INBOX_DIR, PROCESSING_DIR, DONE_DIR, FAILED_DIR, STATE_DIR):
        path.mkdir(parents=True, exist_ok=True)


@contextmanager
def _lock() -> Iterator[None]:
    ensure_layout()
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=f".{os.getpid()}.{secrets.token_hex(4)}.tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if sys.platform.startswith("linux"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def _status_lock() -> Iterator[None]:
    """Serialize the complete status read/merge/write transaction."""
    ensure_layout()
    try:
        handle = STATUS_LOCK_PATH.open("a+b")
    except OSError as error:
        raise RuntimeError(f"cannot create status lock at {STATUS_LOCK_PATH}: {error}") from error
    with handle:
        if fcntl is None:
            raise RuntimeError("inter-process status locking requires fcntl on Linux")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise RuntimeError(f"cannot acquire status lock at {STATUS_LOCK_PATH}: {error}") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_status_unlocked() -> dict[str, Any]:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_status(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _status_lock():
        current = _read_status_unlocked()
        mutator(current)
        current["updated_at"] = time.time()
        _atomic_json(STATUS_PATH, current)
        return current


def validate_request(payload: dict[str, Any], *, expected_owner_id: int | None = None) -> tuple[bool, str]:
    action = payload.get("action")
    target = payload.get("target")
    requester_id = payload.get("requester_id")
    if action not in VALID_ACTIONS:
        return False, "unsupported action"
    if target not in VALID_TARGETS:
        return False, "unsupported target"
    if not isinstance(requester_id, int) or requester_id <= 0:
        return False, "requester_id must be a positive integer"
    if expected_owner_id is not None and requester_id != expected_owner_id:
        return False, "requester is not the configured owner"
    return True, "ok"


def enqueue(action: str, target: str, requester_id: int, *, source: str = "discord") -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "id": request_id,
        "action": action,
        "target": target,
        "requester_id": requester_id,
        "source": source,
        "created_at": time.time(),
        "status": "pending",
    }
    ok, error = validate_request(payload)
    if not ok:
        raise ValueError(error)
    with _lock():
        _atomic_json(INBOX_DIR / f"{request_id}.json", payload)
    return payload


def claim_next() -> dict[str, Any] | None:
    ensure_layout()
    with _lock():
        for path in sorted(INBOX_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            ok, error = validate_request(payload)
            if not ok:
                payload["status"] = "failed"
                payload["error"] = error
                _atomic_json(FAILED_DIR / path.name, payload)
                path.unlink(missing_ok=True)
                continue
            payload["status"] = "processing"
            payload["claimed_at"] = time.time()
            destination = PROCESSING_DIR / path.name
            _atomic_json(destination, payload)
            path.unlink(missing_ok=True)
            return payload
    return None


def finish(request_id: str, *, status: str, error: str | None = None) -> None:
    if not REQUEST_ID_RE.fullmatch(request_id):
        return
    source = PROCESSING_DIR / f"{request_id}.json"
    if not source.exists():
        return
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"id": request_id}
    payload["status"] = status if status in {"done", "failed"} else "failed"
    payload["finished_at"] = time.time()
    if error:
        payload["error"] = str(error)[:500]
    destination_dir = DONE_DIR if payload["status"] == "done" else FAILED_DIR
    with _lock():
        _atomic_json(destination_dir / source.name, payload)
        source.unlink(missing_ok=True)


def write_status(**updates: Any) -> dict[str, Any]:
    return _update_status(lambda current: current.update(updates))


def read_status() -> dict[str, Any]:
    return _read_status_unlocked()


def write_connection(bot_name: str, state: str, *, user_id: int | None = None) -> dict[str, Any]:
    """Publish gateway state without exposing tokens or message content."""
    def update(current: dict[str, Any]) -> None:
        connections = current.get("connections")
        if not isinstance(connections, dict):
            connections = {}
        entry: dict[str, Any] = {"state": state, "updated_at": time.time()}
        if user_id:
            entry["user_id"] = int(user_id)
        connections[bot_name] = entry
        current["connections"] = connections

    return _update_status(update)
