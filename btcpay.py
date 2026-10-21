"""Minimal BTCPay Greenfield API client for LTC-only server-budget invoices."""

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
PAYMENT_METHOD = "LTC-CHAIN"


class BTCPayError(RuntimeError):
    """A safe, user-displayable BTCPay request or response error."""


def _config():
    base_url = os.getenv("BTCPAY_URL", "").strip().rstrip("/")
    store_id = os.getenv("BTCPAY_STORE_ID", "").strip()
    api_key = os.getenv("BTCPAY_API_KEY", "").strip()
    webhook_secret = os.getenv("BTCPAY_WEBHOOK_SECRET", "").strip()
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
        raise BTCPayError("قيمة الميزانية يجب أن تكون موجبة.")
    if amount < MIN_CRYPTO_BUDGET:
        raise BTCPayError(
            f"الحد الأدنى للشحن عبر LTC هو ${MIN_CRYPTO_USD:.2f}، أي {MIN_CRYPTO_BUDGET:,} Cosmo حسب سعر الصرف الحالي المعتمد."
        )
    quote = (Decimal(amount) / Decimal(COSMO_PER_USD)).quantize(USD_PRECISION, rounding=ROUND_UP)
    if quote < MIN_CRYPTO_USD:
        raise BTCPayError(
            f"الحد الأدنى للشحن عبر LTC هو ${MIN_CRYPTO_USD:.2f}، أي {MIN_CRYPTO_BUDGET:,} Cosmo حسب سعر الصرف الحالي المعتمد."
        )
    return quote


def format_usd(amount: Decimal) -> str:
    try:
        value = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BTCPayError("مبلغ الدولار غير صالح.") from exc
    return format(value.normalize(), "f")


def _api_request(method: str, endpoint: str, *, params=None, json_body=None):
    base_url, store_id, api_key, _, valid_host = _config()
    if not valid_host or not re.fullmatch(r"[A-Za-z0-9]+", store_id) or not api_key:
        raise BTCPayError("تكامل BTCPay غير مكتمل الإعداد؛ تواصل مع مالك البوت.")
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
        raise BTCPayError("تعذر الاتصال بخادم BTCPay الآن. لم تُضف أي ميزانية؛ أعد المحاولة لاحقًا.") from exc
    if response.status_code not in {200, 201}:
        raise BTCPayError(f"رفض خادم BTCPay الطلب برمز HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BTCPayError("أعاد خادم BTCPay استجابة غير صالحة.") from exc
    if not isinstance(payload, dict):
        raise BTCPayError("أعاد خادم BTCPay بيانات غير متوقعة.")
    return payload


def create_ltc_invoice(*, price_usd: Decimal, provider_order_id: str, item_description: str = "Cosmo server budget") -> dict:
    """Create a USD-denominated invoice whose only allowed payment method is on-chain LTC."""
    try:
        amount = Decimal(price_usd).quantize(USD_PRECISION, rounding=ROUND_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BTCPayError("مبلغ الفاتورة غير صالح.") from exc
    if not amount.is_finite() or amount < MIN_CRYPTO_USD:
        raise BTCPayError(f"الحد الأدنى لفاتورة LTC هو ${MIN_CRYPTO_USD:.2f}.")
    if not re.fullmatch(r"cosmo:\d{1,20}:[0-9a-f]{32}", str(provider_order_id)):
        raise BTCPayError("معرّف طلب الميزانية غير صالح.")
    base_url, store_id, _, _, _ = _config()
    payload = {
        "amount": format(amount, "f"),
        "currency": "USD",
        "metadata": {
            "orderId": provider_order_id,
            "itemDesc": str(item_description)[:120],
        },
        "checkout": {
            "paymentMethods": [PAYMENT_METHOD],
            "defaultPaymentMethod": PAYMENT_METHOD,
            "speedPolicy": "HighSpeed",
            "expirationMinutes": INVOICE_TTL_SECONDS // 60,
            "monitoringMinutes": 1440,
            "paymentTolerance": 0,
            "defaultLanguage": "en",
        },
    }
    invoice = _api_request("POST", f"/api/v1/stores/{store_id}/invoices", json_body=payload)
    try:
        invoice_id = str(invoice["id"]).strip()
        checkout_url = str(invoice["checkoutLink"]).strip()
        returned_store = str(invoice["storeId"]).strip()
        returned_currency = str(invoice["currency"]).strip().upper()
        returned_amount = Decimal(str(invoice["amount"]))
        expiry = int(invoice["expirationTime"])
        metadata = invoice.get("metadata")
        metadata_order_id = str(metadata.get("orderId", "")).strip() if isinstance(metadata, dict) else ""
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise BTCPayError("لم يُرجع BTCPay بيانات الفاتورة المطلوبة؛ لم نعرض رابط دفع.") from exc
    parsed_checkout = urlsplit(checkout_url)
    parsed_base = urlsplit(base_url)
    if (
        not invoice_id or returned_store != store_id or returned_currency != "USD"
        or returned_amount != amount or not parsed_checkout.scheme == "https"
        or parsed_checkout.netloc.lower() != parsed_base.netloc.lower()
        or not parsed_checkout.path or metadata_order_id != str(provider_order_id)
    ):
        raise BTCPayError("بيانات فاتورة BTCPay لا تطابق الطلب أو رابط الخادم؛ لم نعرض رابط دفع.")
    return {
        "payment_id": invoice_id,
        "pay_address": checkout_url,
        "pay_currency": "ltc",
        "provider_status": str(invoice.get("status", "New")),
        "expires_at": expiry,
        "amount_usd": returned_amount,
        "store_id": returned_store,
        "metadata_order_id": metadata_order_id,
    }


def get_invoice(invoice_id: str) -> dict:
    invoice_id = str(invoice_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", invoice_id):
        raise BTCPayError("معرّف فاتورة BTCPay غير صالح.")
    _, store_id, _, _, _ = _config()
    return _api_request(
        "GET",
        f"/api/v1/stores/{store_id}/invoices/{invoice_id}",
        params={"includePaymentMethods": "true"},
    )


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Check BTCPay-Sig: sha256=<hex> against the exact raw request bytes."""
    _, _, _, secret, _ = _config()
    if not secret or not isinstance(raw_body, bytes):
        return False
    signature = str(signature or "").strip()
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def active_store_id() -> str:
    return _config()[1]
