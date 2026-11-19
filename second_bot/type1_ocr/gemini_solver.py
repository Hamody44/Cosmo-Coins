"""Low-latency cloud vision fallback for Type1 CAPTCHA recognition.

This module is intentionally isolated from the local OCR implementation.
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request

from PIL import Image


def _png_base64(image):
    if isinstance(image, Image.Image):
        source = image
    elif isinstance(image, bytes):
        source = Image.open(io.BytesIO(image))
    else:
        source = Image.open(image)
    with source:
        rgb = source.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _request_json(url, payload, headers=None, timeout=4.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _valid_digits(text):
    digits = re.sub(r"\D", "", str(text or ""))
    return digits if len(digits) == 5 else None


def _groq_text(data):
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content)


def _gemini_text(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))


def _solve_groq(encoded, timeout):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    configured = os.getenv("GROQ_MODEL")
    models = [configured] if configured else [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "llama-3.2-11b-vision-preview",
    ]
    prompt = "Extract the 5 numeric digits shown in this image. Output ONLY the 5 digits and nothing else."
    for model in models:
        try:
            data = _request_json(
                "https://api.groq.com/openai/v1/chat/completions",
                {
                    "model": model,
                    "temperature": 0.0,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
                    ]}],
                },
                {"Authorization": "Bearer " + api_key},
                timeout,
            )
            result = _valid_digits(_groq_text(data))
            if result:
                return result
        except Exception:
            continue
    return None


def _solve_gemini(encoded, api_key, timeout):
    if not api_key:
        return None
    configured = os.getenv("GEMINI_MODEL")
    models = [configured] if configured else ["gemini-2.5-flash-lite", "gemini-2.0-flash"]
    prompt = "Extract the 5 numeric digits shown in this image. Output ONLY the 5 digits and nothing else."
    for model in models:
        try:
            data = _request_json(
                "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent?key=" + api_key,
                {
                    "contents": [{"parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/png", "data": encoded}},
                    ]}],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": 16},
                },
                timeout=timeout,
            )
            result = _valid_digits(_gemini_text(data))
            if result:
                return result
        except Exception:
            continue
    return None


def solve_with_gemini_flash(image, api_key=None):
    """Return a five-digit cloud prediction, or None for local OCR fallback."""
    if os.getenv("TYPE1_DISABLE_GEMINI") == "1" or os.getenv("TYPE1_DISABLE_CLOUD") == "1":
        return None
    try:
        encoded = _png_base64(image)
        timeout = float(os.getenv("GEMINI_TIMEOUT", "4.0"))
        result = _solve_groq(encoded, timeout)
        if result:
            return result
        return _solve_gemini(encoded, api_key or os.getenv("GEMINI_API_KEY"), timeout)
    except Exception:
        return None


solve_with_cloud_vision = solve_with_gemini_flash
