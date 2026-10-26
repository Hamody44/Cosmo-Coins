"""Minimal LTCPay Greenfield API client for LTC-only server-budget invoices."""

import hmac
import hashlib
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_UP
from urllib.parse import urlsplit

import requests

COSMO_PER_USD = 260_000_000
USD_PRECISION = Decimal("0.00000001")
MIN_CRYPTO_USD = Decimal("0.10")
MIN_CRYPTO_BUDGET = 26_000_000
INVOICE_TTL_SECONDS = 10 * 60
REQUEST_TIMEOUT_SECONDS = 12
PAYMENT_METHOD = "LTC"
LTC_PAYMENT_METHOD_IDS = {"LTC", "LTC-CHAIN"}


class LTCPayError(RuntimeError):
    """A safe, user-displayable LTCPay request or response error."""


def _config():
    base_url = os.getenv("LTCPAY_URL", "https://ltcpay.com").strip().rstrip("/")
    store_id = os.getenv("LTCPAY_STORE_ID", "").strip()
    api_key = os.getenv("LTCPAY_API_KEY", "").strip()
    webhook_secret = os.getenv("LTCPAY_WEBHOOK_SECRET", "").strip()
    parsed = urlsplit(base_url)
    valid_host = bool(parsed.hostname and parsed.scheme in {"https", "http"})
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        valid_host = False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        valid_host = False
    return base_url, store_id, api_key, webhook_secret, valid_host


def is_configured() -> bool:
    base_url, store_id, api_key, webhook_secret, valid_host = _config()
    return bool(
        valid_host and store_id and api_key and webhook_secret
        and re.fullmatch(r"[A-Za-z0-9]+", store_id)
        and len(store_id) <= 128 and len(api_key) <= 512 and len(webhook_secret) <= 512
    )


def usd_for_budget(budget_amount: int) -> Decimal:
    amount = int(budget_amount)
    if amount <= 0:
        raise LTCPayError("قيمة الميزانية يجب أن تكون موجبة.")
    if amount < MIN_CRYPTO_BUDGET:
        raise LTCPayError(
            f"الحد الأدنى للشحن عبر LTC هو ${MIN_CRYPTO_USD:.2f}، أي {MIN_CRYPTO_BUDGET:,} Cosmo حسب سعر الصرف الحالي المعتمد."
        )
    quote = (Decimal(amount) / Decimal(COSMO_PER_USD)).quantize(USD_PRECISION, rounding=ROUND_UP)
    if quote < MIN_CRYPTO_USD:
        raise LTCPayError(
            f"الحد الأدنى للشحن عبر LTC هو ${MIN_CRYPTO_USD:.2f}، أي {MIN_CRYPTO_BUDGET:,} Cosmo حسب سعر الصرف الحالي المعتمد."
        )
    return quote


def format_usd(amount: Decimal) -> str:
    try:
        value = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LTCPayError("مبلغ الدولار غير صالح.") from exc
    return format(value.normalize(), "f")


def _provider_error_detail(response) -> str:
    """Extract a short validation message, never logging the raw response body."""
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("message") or payload.get("detail") or payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    if not detail and isinstance(payload.get("errors"), dict):
        parts = []
        for key, value in list(payload["errors"].items())[:3]:
            if isinstance(value, list):
                value = "; ".join(str(item) for item in value[:2])
            parts.append(f"{key}: {value}")
        detail = " | ".join(parts)
    if not isinstance(detail, (str, int, float)):
        return ""
    detail = " ".join(str(detail).split())
    return detail[:320]


def _api_request(method: str, endpoint: str, *, params=None, json_body=None, config=None):
    # Use one configuration snapshot for a multi-request operation. This avoids
    # changing the store between invoice creation and canonical verification.
    base_url, store_id, api_key, _, valid_host = config or _config()
    if not valid_host or not re.fullmatch(r"[A-Za-z0-9]+", store_id) or not api_key:
        raise LTCPayError("تكامل LTCPay غير مكتمل الإعداد؛ تواصل مع مالك البوت.")
    try:
        response = requests.request(
            method,
            f"{base_url}{endpoint}",
            headers={"Authorization": f"token {api_key}", "Content-Type": "application/json"},
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise LTCPayError("تعذر الاتصال بخادم LTCPay الآن. لم تُضف أي ميزانية؛ أعد المحاولة لاحقًا.") from exc
    if response.status_code not in {200, 201}:
        detail = _provider_error_detail(response)
        suffix = f" سبب الخادم: {detail}" if detail else " راجع إعدادات المتجر وطريقة الدفع LTC."
        raise LTCPayError(f"رفض خادم LTCPay الطلب برمز HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LTCPayError("أعاد خادم LTCPay استجابة غير صالحة.") from exc
    if not isinstance(payload, dict):
        raise LTCPayError("أعاد خادم LTCPay بيانات غير متوقعة.")
    return payload


def create_ltc_invoice(*, price_usd: Decimal, provider_order_id: str, discord_user_id: int, item_description: str = "Cosmo server budget") -> dict:
    """Create a USD-denominated invoice whose only allowed payment method is on-chain LTC."""
    try:
        amount = Decimal(price_usd).quantize(USD_PRECISION, rounding=ROUND_UP)
        discord_user_id = str(int(discord_user_id))
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise LTCPayError("مبلغ الفاتورة أو معرّف المستخدم غير صالح.") from exc
    if not amount.is_finite() or amount < MIN_CRYPTO_USD:
        raise LTCPayError(f"الحد الأدنى لفاتورة LTC هو ${MIN_CRYPTO_USD:.2f}.")
    if not re.fullmatch(r"\d{1,20}", discord_user_id):
        raise LTCPayError("معرّف مستخدم Discord غير صالح.")
    if not re.fullmatch(r"(?:cosmo|premium):\d{1,20}:[0-9a-f]{32}", str(provider_order_id)):
        raise LTCPayError("معرّف طلب الميزانية غير صالح.")
    config = _config()
    base_url, store_id, _, _, _ = config
    payload = {
        "amount": format(amount, "f"),
        "currency": "USD",
        "metadata": {
            "orderId": provider_order_id,
            "discord_user_id": discord_user_id,
            "itemDesc": str(item_description)[:120],
        },
        "checkout": {
            "speedPolicy": "MediumSpeed",
            "paymentMethods": [PAYMENT_METHOD],
            "expirationMinutes": INVOICE_TTL_SECONDS // 60,
            "monitoringMinutes": 1440,
            "paymentTolerance": 0,
            "defaultLanguage": "en",
        },
    }
    created_invoice = _api_request(
        "POST", f"/api/v1/stores/{store_id}/invoices", json_body=payload, config=config
    )
    try:
        invoice_id = str(created_invoice["id"]).strip()
        if not invoice_id:
            raise ValueError("missing invoice id")
    except (KeyError, TypeError, ValueError) as exc:
        raise LTCPayError("لم يُرجع LTCPay معرّف الفاتورة؛ لم نعرض رابط دفع.") from exc

    # Some Greenfield-compatible providers return only a compact create response.
    # Fetch the canonical invoice before exposing its checkout link or trusting its fields.
    invoice = get_invoice(invoice_id, config=config)
    try:
        canonical_id = str(invoice.get("id", invoice_id)).strip()
        checkout_url = str(invoice.get("checkoutLink") or created_invoice.get("checkoutLink") or "").strip()
        # The provider may omit storeId in a compact response. In that case,
        # the store embedded in the pinned GET path is the reference. An
        # explicit provider value is still checked below and must match it.
        returned_store = str(invoice.get("storeId") or created_invoice.get("storeId") or store_id).strip()
        returned_currency = str(invoice.get("currency") or created_invoice.get("currency") or "").strip().upper()
        returned_amount = Decimal(str(invoice.get("amount", created_invoice.get("amount"))))
        expiry = int(invoice.get("expirationTime", created_invoice.get("expirationTime")))
        metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else created_invoice.get("metadata")
        metadata_order_id = str(metadata.get("orderId", "")).strip() if isinstance(metadata, dict) else ""
        metadata_user_id = str(metadata.get("discord_user_id", "")).strip() if isinstance(metadata, dict) else ""
        methods = _invoice_payment_methods(invoice)
        if methods is None:
            methods = _invoice_payment_methods(created_invoice)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise LTCPayError("تعذر التحقق من بيانات فاتورة LTCPay؛ لم نعرض رابط دفع.") from exc
    parsed_checkout = urlsplit(checkout_url)
    parsed_base = urlsplit(base_url)
    methods_match = (
        isinstance(methods, list) and len(methods) == 1
        and str(methods[0]).upper() in LTC_PAYMENT_METHOD_IDS
    )
    mismatches = []
    if not invoice_id or canonical_id != invoice_id:
        mismatches.append("معرّف الفاتورة")
    if returned_store != store_id:
        mismatches.append("معرّف المتجر")
    if returned_currency != "USD" or not returned_amount.is_finite() or returned_amount != amount:
        mismatches.append("العملة أو المبلغ")
    if (
        parsed_checkout.scheme != "https"
        or parsed_checkout.netloc.lower() != parsed_base.netloc.lower()
        or not parsed_checkout.path
    ):
        mismatches.append("رابط الدفع")
    if metadata_order_id != str(provider_order_id):
        mismatches.append("رقم الطلب في metadata")
    if metadata_user_id != discord_user_id:
        mismatches.append("معرّف المستخدم في metadata")
    if not methods_match:
        mismatches.append("طرق الدفع المسموحة؛ يجب أن تكون LTC فقط")
    if mismatches:
        fields = "، ".join(mismatches)
        raise LTCPayError(f"لم نتمكن من مطابقة فاتورة LTCPay في الحقول التالية: {fields}. لم نعرض رابط الدفع.")
    return {
        "payment_id": invoice_id,
        "pay_address": checkout_url,
        "pay_currency": "ltc",
        "provider_status": str(invoice.get("status", created_invoice.get("status", "New"))),
        "expires_at": expiry,
        "amount_usd": returned_amount,
        "store_id": returned_store,
        "metadata_order_id": metadata_order_id,
        "metadata_user_id": metadata_user_id,
    }


def _invoice_payment_methods(invoice: dict):
    checkout = invoice.get("checkout")
    if isinstance(checkout, dict) and "paymentMethods" in checkout:
        return checkout.get("paymentMethods")
    return invoice.get("paymentMethods")


def get_invoice(invoice_id: str, *, config=None) -> dict:
    invoice_id = str(invoice_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice_id):
        raise LTCPayError("معرّف فاتورة LTCPay غير صالح.")
    request_config = config or _config()
    _, store_id, _, _, _ = request_config
    return _api_request(
        "GET",
        f"/api/v1/stores/{store_id}/invoices/{invoice_id}",
        params={"includePaymentMethods": "true"},
        config=request_config,
    )


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify LTCPay's BTCPAY-SIG against the exact raw request body."""
    _, _, _, secret, _ = _config()
    if not secret or not isinstance(raw_body, bytes):
        return False
    signature = str(signature or "").strip()
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def active_store_id() -> str:
    return _config()[1]
