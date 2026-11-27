"""Local management CLI used by the process supervisor, not a Discord client."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from .protocol import enqueue, finish, read_status, validate_request, write_status
ROOT_DIR = Path(__file__).resolve().parent.parent

def load_local_env() -> None:
    for path in (ROOT_DIR / ".env", ROOT_DIR / "brain_bot" / ".env"):
        try: lines = path.read_text(encoding="utf-8").splitlines()
        except OSError: continue
        for line in lines:
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key,value=line.split("=",1); key=key.strip(); value=value.strip().strip('"').strip("'")
            if key and key not in os.environ: os.environ[key]=value

def owner_id() -> int | None:
    load_local_env()
    try: value=int(os.getenv("PRIMARY_OWNER_ID", "").strip())
    except ValueError: return None
    return value if value > 0 else None

def process_snapshot() -> dict[str, dict[str, object]]:
    state_dir=ROOT_DIR / "second_bot" / ".runtime" / "process_control"
    snapshot={}
    for name in ("primary", "second_bot", "brain_bot"):
        try:
            pid=int((state_dir / f"{name}.pid").read_text(encoding="utf-8").strip()); os.kill(pid,0); alive=True
        except (OSError,ValueError): pid,alive=None,False
        snapshot[name]={"pid":pid,"alive":alive}
    return snapshot

def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(prog="python -m brain_bot")
    sub=parser.add_subparsers(dest="command",required=True)
    request=sub.add_parser("request"); request.add_argument("action",choices=("start","restart","shutdown")); request.add_argument("target",choices=("primary","second_bot")); request.add_argument("requester_id",type=int,nargs="?"); request.add_argument("--source",default="discord")
    sub.add_parser("status"); sub.add_parser("heartbeat")
    finish_parser=sub.add_parser("finish"); finish_parser.add_argument("request_id"); finish_parser.add_argument("status",choices=("done","failed")); finish_parser.add_argument("error",nargs="?")
    args=parser.parse_args(argv)
    if args.command == "request":
        expected=owner_id()
        if expected is None:
            print("rejected: PRIMARY_OWNER_ID is required",file=sys.stderr); return 1
        requester_id=args.requester_id if args.requester_id is not None else expected
        payload={"action":args.action,"target":args.target,"requester_id":requester_id}
        ok,error=validate_request(payload,expected_owner_id=expected)
        if not ok: print(f"rejected: {error}",file=sys.stderr); return 1
        queued=enqueue(args.action,args.target,requester_id,source=args.source)
        print(f"queued: {queued['id']} {args.action} {args.target}"); return 0
    if args.command == "finish":
        finish(args.request_id,status=args.status,error=args.error); return 0
    if args.command == "heartbeat":
        write_status(heartbeat_at=time.time(),managed_processes=process_snapshot())
        print("ok"); return 0
    print(json.dumps(read_status(),ensure_ascii=False,sort_keys=True)); return 0
if __name__ == "__main__": raise SystemExit(main())
