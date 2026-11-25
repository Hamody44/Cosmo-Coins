"""Small persistent command queue shared by the primary bot and second_bot."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - the deployment target is Linux
    fcntl = None

ROOT_DIR = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT_DIR / "second_bot" / ".runtime" / "command_queue.json"
LOCK_PATH = QUEUE_PATH.with_suffix(".lock")


@contextmanager
def _locked_queue():
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = []
        if not isinstance(payload, list):
            payload = []
        yield payload
        temporary = QUEUE_PATH.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, QUEUE_PATH)
    finally:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def enqueue_command(command: dict[str, object]) -> str:
    request_id = uuid.uuid4().hex
    item = {"id": request_id, "created_at": time.time(), "status": "pending", **command}
    with _locked_queue() as queue:
        queue.append(item)
    return request_id


def claim_next_command() -> dict[str, object] | None:
    with _locked_queue() as queue:
        for item in queue:
            if isinstance(item, dict) and item.get("status") == "pending":
                item["status"] = "processing"
                item["claimed_at"] = time.time()
                return dict(item)
    return None


def finish_command(request_id: str, *, status: str, error: str | None = None) -> None:
    with _locked_queue() as queue:
        for item in queue:
            if isinstance(item, dict) and item.get("id") == request_id:
                item["status"] = status
                item["finished_at"] = time.time()
                if error:
                    item["error"] = error[:500]
                break
        queue[:] = [item for item in queue if not (
            isinstance(item, dict)
            and item.get("status") in {"completed", "failed"}
            and time.time() - float(item.get("finished_at", time.time())) > 300
        )]
