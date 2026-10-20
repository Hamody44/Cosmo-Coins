"""NOWPayments LTC helpers. Live API calls happen only when a user starts a payment."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_UP
from typing import Any

import requests

API_BASE_URL = "https://api.nowpayments.io/v1"
COSMO_PER_USD = 260_000_000
LITOSHI_PER_LTC = 100_000_000
FIXED_RATE_TTL_SECONDS = 10 * 60
REQUEST_TIMEOUT_SECONDS = 15


class NowPaymentsError(Exception):
    """Safe, user-displayable NOWPayments integration error."""


class UnderMinimumPayment(NowPaymentsError):
    def __init__(self, minimum_usd: Decimal):
        self.minimum_usd = minimum_usd
        super().__init__(f"الحد الأدنى الحالي للدفع يقارب ${format_usd(minimum_usd)}.")


def is_configured() -> bool:
    address = os.getenv("NOWPAYMENTS_PAYOUT_ADDRESS", "").strip().lower()
    payout_currency = os.getenv("NOWPAYMENTS_PAYOUT_CURRENCY", "ltc").strip().lower()
    return bool(
        os.getenv("NOWPAYMENTS_API_KEY", "").strip()
        and os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()
        and address.startswith("ltc1")
        and payout_currency == "ltc"
    )


def usd_for_budget(budget_amount: int) -> Decimal:
    amount = int(budget_amount)
    if amount <= 0:
        raise ValueError("budget amount must be positive")
    # NOWPayments accepts a numeric fiat amount. Keep sub-cent precision so the
    # stated 260,000,000 Cosmo/USD rate is not silently rounded to cents.
    return (Decimal(amount) / Decimal(COSMO_PER_USD)).quantize(
        Decimal("0.00000001"), rounding=ROUND_UP
    )


def amount_to_litoshi(value: Any, *, round_up: bool = False) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid LTC amount")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("LTC amount must be positive and finite")
    rounding = ROUND_CEILING if round_up else ROUND_DOWN
    return int((amount * LITOSHI_PER_LTC).to_integral_value(rounding=rounding))


def format_ltc(litoshi: int) -> str:
    amount = Decimal(int(litoshi)) / Decimal(LITOSHI_PER_LTC)
    return format(amount, "f").rstrip("0").rstrip(".") or "0"


def format_usd(amount: Decimal | str | float) -> str:
    value = Decimal(str(amount))
    text = format(value.quantize(Decimal("0.01")), ",.2f")
    return text


def _api_headers() -> dict[str, str]:
    api_key = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
    if not api_key:
        raise NowPaymentsError("تكامل NOWPayments غير مكتمل الإعداد.")
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def _request_json(method: str, path: str, *, params=None, body=None) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{API_BASE_URL}{path}",
            headers=_api_headers(),
            params=params,
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise NowPaymentsError("تعذر الاتصال بخدمة الدفع الآن. لم تُعتمد أي ميزانية؛ حاول لاحقًا.") from exc
    if response.status_code not in (200, 201):
        # Do not relay provider response bodies or request headers into Discord logs/messages.
        raise NowPaymentsError(f"رفض مزود الدفع الطلب (HTTP {response.status_code}). لم تُعتمد أي ميزانية.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise NowPaymentsError("أعاد مزود الدفع ردًا غير صالح. لم تُعتمد أي ميزانية.") from exc
    if not isinstance(payload, dict):
        raise NowPaymentsError("أعاد مزود الدفع بيانات غير متوقعة. لم تُعتمد أي ميزانية.")
    return payload


def quote_ltc_amount(price_usd: Decimal) -> Decimal:
    """Check NOWPayments' live minimum and return its USD-to-LTC estimate."""
    price_usd = Decimal(str(price_usd))
    if not price_usd.is_finite() or price_usd <= 0:
        raise ValueError("USD price must be positive and finite")

    fee_paid_by_user = os.getenv("NOWPAYMENTS_FEE_PAID_BY_USER", "false").strip().lower() == "true"
    common = {
        "currency_from": "ltc",
        "currency_to": "ltc",
        "fiat_equivalent": "usd",
        "is_fixed_rate": "true",
        "is_fee_paid_by_user": str(fee_paid_by_user).lower(),
    }
    minimum = _request_json("GET", "/min-amount", params=common)
    try:
        minimum_usd = Decimal(str(minimum["fiat_equivalent"]))
        minimum_ltc = Decimal(str(minimum["min_amount"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise NowPaymentsError("تعذر قراءة الحد الأدنى للدفع من المزود.") from exc
    if not minimum_usd.is_finite() or not minimum_ltc.is_finite() or minimum_usd <= 0 or minimum_ltc <= 0:
        raise NowPaymentsError("أعاد مزود الدفع حدًا أدنى غير صالح.")
    if price_usd < minimum_usd:
        raise UnderMinimumPayment(minimum_usd)

    estimate = _request_json(
        "GET",
        "/estimate",
        params={"amount": format(price_usd, "f"), "currency_from": "usd", "currency_to": "ltc"},
    )
    try:
        estimated_ltc = Decimal(str(estimate["estimated_amount"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise NowPaymentsError("تعذر حساب تقدير LTC الحالي.") from exc
    if not estimated_ltc.is_finite() or estimated_ltc < minimum_ltc:
        raise UnderMinimumPayment(minimum_usd)
    return estimated_ltc


def _parse_provider_time(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, OverflowError):
        return None


def create_ltc_payment(
    *, price_usd: Decimal, estimated_ltc: Decimal, provider_order_id: str, callback_url: str
) -> dict[str, Any]:
    """Create a fixed-rate LTC payment and return normalized, validated fields."""
    if not provider_order_id or not callback_url.startswith("https://"):
        raise ValueError("order id and public HTTPS IPN callback are required")
    payout_address = os.getenv("NOWPAYMENTS_PAYOUT_ADDRESS", "").strip()
    if not payout_address.lower().startswith("ltc1"):
        raise NowPaymentsError("عنوان تسوية Litecoin غير مضبوط؛ لم يُنشأ طلب دفع.")
    fee_paid_by_user = os.getenv("NOWPAYMENTS_FEE_PAID_BY_USER", "false").strip().lower() == "true"
    result = _request_json(
        "POST",
        "/payment",
        body={
            "price_amount": float(Decimal(str(price_usd))),
            "price_currency": "usd",
            "pay_amount": float(Decimal(str(estimated_ltc))),
            "pay_currency": "ltc",
            "ipn_callback_url": callback_url,
            "order_id": provider_order_id,
            "order_description": "Cosmo Coins server budget",
            "payout_address": payout_address,
            "payout_currency": os.getenv("NOWPAYMENTS_PAYOUT_CURRENCY", "ltc").strip().lower(),
            "is_fixed_rate": True,
            "is_fee_paid_by_user": fee_paid_by_user,
        },
    )
    payment_id = str(result.get("payment_id", "")).strip()
    address = str(result.get("pay_address", "")).strip()
    pay_currency = str(result.get("pay_currency", "")).strip().lower()
    network = str(result.get("network") or pay_currency).strip().lower()
    response_order_id = str(result.get("order_id", "")).strip()
    status = str(result.get("payment_status", "")).strip().lower()
    if not payment_id or not address or pay_currency != "ltc" or network not in {"ltc", "litecoin"}:
        raise NowPaymentsError("استجابة إنشاء دفعة LTC غير مكتملة أو شبكتها غير مطابقة.")
    if response_order_id != provider_order_id:
        raise NowPaymentsError("معرّف الطلب في استجابة الدفع لا يطابق الطلب الداخلي.")
    try:
        pay_amount = Decimal(str(result["pay_amount"]))
        pay_litoshi = amount_to_litoshi(pay_amount, round_up=True)
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise NowPaymentsError("لم يُرجع مزود الدفع مبلغ LTC صالحًا.") from exc
    if status not in {"waiting", "confirming", "confirmed", "sending"}:
        raise NowPaymentsError("لم ينشئ مزود الدفع طلبًا بحالة انتظار صالحة.")

    now = int(time.time())
    expires_at = now + FIXED_RATE_TTL_SECONDS
    provider_expiry = _parse_provider_time(result.get("valid_until") or result.get("expiration_estimate_date"))
    if provider_expiry is not None:
        expires_at = min(expires_at, provider_expiry)
    if expires_at <= now:
        raise NowPaymentsError("انتهت صلاحية السعر قبل عرض الفاتورة؛ أعد المحاولة لإنشاء سعر جديد.")
    return {
        "payment_id": payment_id,
        "pay_address": address,
        "pay_amount": format(pay_amount, "f"),
        "pay_amount_litoshi": pay_litoshi,
        "pay_currency": pay_currency,
        "network": network,
        "payment_status": status,
        "provider_order_id": response_order_id,
        "expires_at": expires_at,
    }


def verify_ipn_signature(payload: dict[str, Any], signature: str, secret: str | None = None) -> bool:
    key = (secret if secret is not None else os.getenv("NOWPAYMENTS_IPN_SECRET", "")).strip()
    if not key or not isinstance(payload, dict) or not signature:
        return False
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    expected = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.strip().lower())
