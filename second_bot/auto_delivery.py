"""Signed handoff messages shared by the primary bot and the delivery bot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any

HANDOFF_VERSION = 1
HANDOFF_PREFIX = "COSMO_AUTO_DELIVERY_V1:"
HANDOFF_PATTERN = re.compile(
    r"\|\|COSMO_AUTO_DELIVERY_V1:([A-Za-z0-9_-]+)\.([0-9a-f]{64})\|\|"
)
DELIVERY_RESULT_PREFIX = "COSMO_DELIVERY_RESULT_V1:"
DELIVERY_RESULT_PATTERN = re.compile(
    r"\|\|COSMO_DELIVERY_RESULT_V1:([A-Za-z0-9_-]+)\.([0-9a-f]{64})\|\|"
)
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def _provider_for_currency(currency_name: str) -> str | None:
    normalized = str(currency_name or "").strip().casefold()
    if any(token in normalized for token in ("probot", "credit", "كريديت")):
        return "ProBot"
    if any(token in normalized for token in ("adamc", "adam c", "ادم سي", "آدم سي")):
        return "AdamC"
    if any(token in normalized for token in ("robux", "rob", "روبوكس")):
        return "Rob"
    return None


def transfer_details(currency_name: str, user_id: int, amount: int) -> tuple[str, str | None] | None:
    """Return the provider command and optional CAPTCHA confirmation type."""
    provider = _provider_for_currency(currency_name)
    for value in (user_id, amount):
        if isinstance(value, bool):
            return None
        if not isinstance(value, int) and not (
            isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip())
        ):
            return None
    try:
        user_id = int(user_id)
        amount = int(amount)
    except (TypeError, ValueError, OverflowError):
        return None
    if provider is None or not (0 < user_id <= MAX_SQLITE_INTEGER) or not (0 < amount <= MAX_SQLITE_INTEGER):
        return None
    if provider == "ProBot":
        return f"#credit {user_id} {amount}", "type1"
    if provider == "AdamC":
        return f"T {user_id} {amount}", "type2"
    return f"!transfer {user_id} {amount}", None


def _valid_secret(secret: str) -> str | None:
    value = str(secret or "").strip()
    # Require a high-entropy shared secret; token_urlsafe(48) is a suitable default.
    return value if len(value) >= 32 else None


def build_handoff_message(
    *,
    secret: str,
    issuer_id: int,
    order_id: int,
    guild_id: int,
    delivery_channel_id: int,
    user_id: int,
    currency: str,
    amount: int,
) -> str | None:
    """Build an opaque, authenticated Discord message for a supported payout."""
    signing_key = _valid_secret(secret)
    if signing_key is None:
        return None
    details = transfer_details(currency, user_id, amount)
    if details is None:
        return None
    try:
        identifiers = {
            "order_id": int(order_id),
            "issuer_id": int(issuer_id),
            "guild_id": int(guild_id),
            "delivery_channel_id": int(delivery_channel_id),
            "user_id": int(user_id),
            "amount": int(amount),
        }
    except (TypeError, ValueError, OverflowError):
        return None
    if any(value <= 0 or value > MAX_SQLITE_INTEGER for value in identifiers.values()):
        return None
    currency = str(currency or "").strip()
    if not currency or len(currency) > 100:
        return None

    payload = {
        "v": HANDOFF_VERSION,
        **identifiers,
        "currency": currency,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")
    signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"||{HANDOFF_PREFIX}{encoded}.{signature}||"


def verify_handoff_message(content: str, secret: str) -> dict[str, Any] | None:
    """Verify and validate a handoff; return derived command data, never trust commands from input."""
    signing_key = _valid_secret(secret)
    if signing_key is None:
        return None
    match = HANDOFF_PATTERN.search(str(content or ""))
    if match is None:
        return None
    encoded, supplied_signature = match.groups()
    expected_signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != HANDOFF_VERSION:
        return None
    expected_keys = {"v", "issuer_id", "order_id", "guild_id", "delivery_channel_id", "user_id", "amount", "currency"}
    if set(payload) != expected_keys:
        return None
    for key in ("issuer_id", "order_id", "guild_id", "delivery_channel_id", "user_id", "amount"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not (0 < value <= MAX_SQLITE_INTEGER):
            return None
    currency = payload.get("currency")
    if not isinstance(currency, str) or not currency.strip() or len(currency) > 100:
        return None
    details = transfer_details(currency, payload["user_id"], payload["amount"])
    if details is None:
        return None
    command, confirmation_type = details
    return {
        **payload,
        "provider": _provider_for_currency(currency),
        "command": command,
        "confirmation_type": confirmation_type,
    }


def build_delivery_result_message(*, secret: str, issuer_id: int, order_id: int, outcome: str) -> str | None:
    """Create a signed status callback from the delivery bot to the primary bot."""
    signing_key = _valid_secret(secret)
    if signing_key is None or outcome not in {"processing", "automatic", "pending", "rejected"}:
        return None
    try:
        issuer_id = int(issuer_id)
        order_id = int(order_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 < issuer_id <= MAX_SQLITE_INTEGER and 0 < order_id <= MAX_SQLITE_INTEGER):
        return None
    payload = {"v": HANDOFF_VERSION, "issuer_id": issuer_id, "order_id": order_id, "outcome": outcome}
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")
    signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"||{DELIVERY_RESULT_PREFIX}{encoded}.{signature}||"


def verify_delivery_result_message(content: str, secret: str) -> dict[str, Any] | None:
    """Validate the second bot's signed status callback."""
    signing_key = _valid_secret(secret)
    if signing_key is None:
        return None
    match = DELIVERY_RESULT_PATTERN.search(str(content or ""))
    if match is None:
        return None
    encoded, supplied_signature = match.groups()
    expected_signature = hmac.new(signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"v", "issuer_id", "order_id", "outcome"}:
        return None
    if payload.get("v") != HANDOFF_VERSION or payload.get("outcome") not in {
        "processing", "automatic", "pending", "rejected",
    }:
        return None
    for key in ("issuer_id", "order_id"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not (0 < value <= MAX_SQLITE_INTEGER):
            return None
    return payload
