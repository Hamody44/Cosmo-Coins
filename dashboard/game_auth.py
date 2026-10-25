"""Short-lived signed identity tickets for the 8Ball WebSocket."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time


def _secret() -> bytes:
    value = os.getenv("GAME_AUTH_SECRET") or os.getenv("FLASK_SECRET_KEY")
    if not value:
        raise RuntimeError("GAME_AUTH_SECRET must be configured for game authentication")
    return value.encode("utf-8")


def issue_game_token(user: dict, ttl_seconds: int = 600) -> str:
    user_id = int(user["id"])
    payload = {"sub": user_id, "name": str(user.get("global_name") or user.get("username") or user_id)[:80], "exp": int(time.time()) + int(ttl_seconds)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(_secret(), body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def verify_game_token(token: str) -> dict:
    try:
        body_text, signature_text = str(token).split(".", 1)
        body = body_text.encode()
        provided = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(provided, expected):
            raise ValueError("invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4)))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("expired token")
        return {"user_id": int(payload["sub"]), "display_name": str(payload["name"])[:80]}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("invalid game token") from exc
