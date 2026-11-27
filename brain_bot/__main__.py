"""Command-line entry point used by Discord bots and run_bots.sh."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .protocol import enqueue, read_status, validate_request, write_status


def owner_id() -> int | None:
    raw = os.getenv("PRIMARY_OWNER_ID", "988840428591808584").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def process_snapshot() -> dict[str, dict[str, object]]:
    state_dir = Path(__file__).resolve().parent.parent / "second_bot" / ".runtime" / "process_control"
    snapshot: dict[str, dict[str, object]] = {}
    for name in ("primary", "second_bot"):
        pid_path = state_dir / f"{name}.pid"
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            alive = pid > 0
            if alive:
                os.kill(pid, 0)
        except (OSError, ValueError):
            pid, alive = None, False
        snapshot[name] = {"pid": pid, "alive": alive}
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain_bot")
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request")
    request.add_argument("action", choices=("start", "restart", "shutdown"))
    request.add_argument("target", choices=("primary", "second_bot"))
    request.add_argument("requester_id", type=int)
    request.add_argument("--source", default="discord")
    sub.add_parser("status")
    sub.add_parser("heartbeat")
    args = parser.parse_args(argv)

    if args.command == "request":
        payload = {
            "action": args.action,
            "target": args.target,
            "requester_id": args.requester_id,
        }
        ok, error = validate_request(payload, expected_owner_id=owner_id())
        if not ok:
            print(f"rejected: {error}", file=sys.stderr)
            return 1
        queued = enqueue(args.action, args.target, args.requester_id, source=args.source)
        print(f"queued: {queued['id']} {args.action} {args.target}")
        return 0

    if args.command == "heartbeat":
        write_status(
            brain_bot="running",
            connection="supervisor_alive",
            managed_processes=process_snapshot(),
        )
        print("ok")
        return 0

    print(json.dumps(read_status(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
