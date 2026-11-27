"""Small persistent command queue shared by the primary bot and second_bot."""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
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
    try:
        lock_handle = LOCK_PATH.open("a+b")
    except OSError as error:
        raise RuntimeError(f"cannot create command queue lock at {LOCK_PATH}: {error}") from error
    locked = False
    try:
        if fcntl is None:
            raise RuntimeError("inter-process command queue locking requires fcntl on Linux")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError as error:
            raise RuntimeError(f"cannot acquire command queue lock at {LOCK_PATH}: {error}") from error
        try:
            payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = []
        if not isinstance(payload, list):
            payload = []
        yield payload
        _atomic_queue_write(payload)
    finally:
        if locked:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _atomic_queue_write(payload: list[dict[str, object]]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=QUEUE_PATH.parent,
            prefix=f".{QUEUE_PATH.name}.",
            suffix=f".{os.getpid()}.{secrets.token_hex(4)}.tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, QUEUE_PATH)
        temporary = None
        if sys.platform.startswith("linux"):
            directory_fd = os.open(QUEUE_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
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
