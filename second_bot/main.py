"""Standalone Discord self-client with restricted !say and public !ping commands."""
from __future__ import annotations

import logging
import json
import base64
import csv
import os
import sys
import time
import asyncio
import math
import re
import hashlib
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO, StringIO
from pathlib import Path
from functools import lru_cache

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from google.genai import types
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

from local_ocr import detect_style, load_image, load_model, recognize

BASE_DIR = Path(__file__).resolve().parent
RUN_BOTS_SCRIPT = BASE_DIR.parent / "run_bots.sh"
LOCAL_OCR_MODEL_PATH = BASE_DIR / "models" / "local_ocr_model.json"
SHARED_GUILD_STATE_PATH = BASE_DIR / ".runtime" / "shared_guilds.json"
SHARED_GUILD_STATE_MAX_AGE_SECONDS = 90
# The panel may expose Cosmo-Coins' DISCORD_BOT_TOKEN to every process.
# The second bot deliberately accepts only its dedicated token variable.
load_dotenv(BASE_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("second_bot")

# Populated by load_config() in main(). Keeping imports side-effect free lets
# the unit tests exercise commands without a real Discord token.
SAY_ALLOWED_ROLE_ID: int | None = None
COMMAND_PREFIX = "$"
COMMAND_RESPONSE_DELAY_SECONDS = 1
USER_COMMAND_COOLDOWN_SECONDS = 30
user_command_cooldowns: dict[tuple[int, int], float] = {}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# The configured model is intentionally not replaced here; set GEMINI_VISION_MODEL in second_bot/.env.
DEFAULT_VISION_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_VISION_MODEL = "gemini-3.5-flash-lite"
LEGACY_GEMINI_MODEL_REPLACEMENTS = {
    "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
}
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
VISION_REQUEST_TIMEOUT_SECONDS = 20
OCR_SPACE_ENDPOINT = "https://api.ocr.space/parse/image"
OCR_SPACE_TIMEOUT_SECONDS = 20
OCR_SPACE_ENGINES = ("3",)
OCR_SPACE_UPSCALE_FACTOR = 3
OCR_ADAPTIVE_THRESHOLD_DELTA = 20
OCR_PREPROCESSING_VERSION = 2
try:
    OCR_SPACE_MIN_INTERVAL_SECONDS = max(0.0, float(os.getenv("OCR_SPACE_MIN_INTERVAL_SECONDS", "2")))
except ValueError:
    OCR_SPACE_MIN_INTERVAL_SECONDS = 2.0
try:
    OCR_SPACE_DAILY_REQUEST_BUDGET = max(1, int(os.getenv("OCR_SPACE_DAILY_REQUEST_BUDGET", "400")))
except ValueError:
    OCR_SPACE_DAILY_REQUEST_BUDGET = 400
try:
    OCR_SPACE_429_COOLDOWN_SECONDS = max(60.0, float(os.getenv("OCR_SPACE_429_COOLDOWN_SECONDS", "3600")))
except ValueError:
    OCR_SPACE_429_COOLDOWN_SECONDS = 3600.0
TYPE1_TEST_VARIANTS = (
    ("before contrast", "قبل تحسين التباين"),
    ("after contrast", "بعد تحسين التباين"),
    ("upscaled otsu", "بعد التكبير وعتبة Otsu"),
    ("adaptive threshold", "بعد العتبة المحلية التكيفية"),
)
TYPE1_TEST_LOCK = asyncio.Lock()
OCR_SPACE_RATE_LOCK = asyncio.Lock()
TRANSIENT_GEMINI_STATUS_CODES = {429, 500, 502, 503, 504}


class OCRSpaceRateLimitError(RuntimeError):
    """OCR.Space rejected the request with HTTP 429; this is not an OCR miss."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class OCRSpaceCooldownError(RuntimeError):
    """A prior HTTP 429 cooldown is still active; no new request was sent."""


class OCRSpaceDailyBudgetError(RuntimeError):
    """The local daily safety budget prevented an OCR.Space request."""


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def runtime_state_path(filename: str) -> Path:
    """Return an ignored, persistent path for small bot runtime state files."""
    return BASE_DIR / ".runtime" / filename


def read_runtime_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_runtime_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically persist runtime state so restarts do not lose progress."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)


def load_ocr_space_rate_state(*, now: float | None = None) -> dict[str, object]:
    """Load today's local request count and most recent request timestamp."""
    state_path = runtime_state_path("ocr_space_rate_state.json")
    state = read_runtime_json(state_path) or {}
    utc_day = datetime.now(timezone.utc).date().isoformat()
    if now is not None:
        utc_day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if state.get("utc_day") != utc_day:
        return {"utc_day": utc_day, "requests": 0, "last_request_at": 0.0, "cooldown_until": 0.0}
    try:
        requests = max(0, int(state.get("requests", 0)))
        last_request_at = max(0.0, float(state.get("last_request_at", 0.0)))
        cooldown_until = max(0.0, float(state.get("cooldown_until", 0.0)))
    except (TypeError, ValueError):
        return {"utc_day": utc_day, "requests": 0, "last_request_at": 0.0, "cooldown_until": 0.0}
    return {
        "utc_day": utc_day,
        "requests": requests,
        "last_request_at": last_request_at,
        "cooldown_until": cooldown_until,
    }


async def reserve_ocr_space_request_slot() -> None:
    """Wait for the minimum interval and reserve one request from the UTC-day budget."""
    async with OCR_SPACE_RATE_LOCK:
        current_time = time.time()
        state = load_ocr_space_rate_state(now=current_time)
        requests = int(state["requests"])
        cooldown_until = float(state["cooldown_until"])
        if current_time < cooldown_until:
            retry_at = datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()
            raise OCRSpaceCooldownError(
                f"OCR.Space is cooling down after HTTP 429 until {retry_at}; no request was sent"
            )
        if requests >= OCR_SPACE_DAILY_REQUEST_BUDGET:
            raise OCRSpaceDailyBudgetError(
                "OCR.Space local daily request safety budget reached "
                f"({requests}/{OCR_SPACE_DAILY_REQUEST_BUDGET} per UTC day); no request was sent"
            )

        last_request_at = float(state["last_request_at"])
        wait_seconds = OCR_SPACE_MIN_INTERVAL_SECONDS - (current_time - last_request_at)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        current_time = time.time()
        state = load_ocr_space_rate_state(now=current_time)
        requests = int(state["requests"])
        cooldown_until = float(state["cooldown_until"])
        if current_time < cooldown_until:
            retry_at = datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()
            raise OCRSpaceCooldownError(
                f"OCR.Space is cooling down after HTTP 429 until {retry_at}; no request was sent"
            )
        if requests >= OCR_SPACE_DAILY_REQUEST_BUDGET:
            raise OCRSpaceDailyBudgetError(
                "OCR.Space local daily request safety budget reached "
                f"({requests}/{OCR_SPACE_DAILY_REQUEST_BUDGET} per UTC day); no request was sent"
            )
        state["requests"] = requests + 1
        state["last_request_at"] = current_time
        write_runtime_json(runtime_state_path("ocr_space_rate_state.json"), state)


async def register_ocr_space_rate_limit(retry_after_seconds: float | None) -> None:
    """Persist a cooldown after a provider 429; prefer its Retry-After header."""
    delay = max(0.0, retry_after_seconds or OCR_SPACE_429_COOLDOWN_SECONDS)
    async with OCR_SPACE_RATE_LOCK:
        current_time = time.time()
        state = load_ocr_space_rate_state(now=current_time)
        state["cooldown_until"] = max(float(state["cooldown_until"]), current_time + delay)
        write_runtime_json(runtime_state_path("ocr_space_rate_state.json"), state)


def parse_retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def load_config() -> tuple[str, int]:
    """Read and validate required environment variables."""
    token = os.getenv("SECOND_BOT_TOKEN", "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if not token:
        raise ValueError("SECOND_BOT_TOKEN is missing. Set the second bot's token in second_bot/.env.")

    role_id_value = os.getenv("SAY_ALLOWED_ROLE_ID", "").strip()
    try:
        role_id = int(role_id_value)
        if role_id <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            "SAY_ALLOWED_ROLE_ID must be a positive Discord role ID in second_bot/.env."
        ) from exc

    return token, role_id


async def control_primary_bot(action: str) -> str:
    """Queue a control action for the primary bot through the shared supervisor."""
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(RUN_BOTS_SCRIPT),
        "control",
        action,
        "primary",
        cwd=BASE_DIR.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return "انتهت مهلة إرسال الطلب إلى مشرف التشغيل."
    message = output.decode("utf-8", errors="replace").strip()
    return message or f"تم إرسال طلب {action} إلى البوت الأساسي."


def load_vision_config() -> tuple[str, str]:
    """Read the optional Gemini settings used by the image-number command."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL
    return api_key, model


def resolve_gemini_fallback_model(model: str) -> str:
    """Replace a known fallback model unavailable to some new API accounts."""
    selected = model.strip() or DEFAULT_FALLBACK_VISION_MODEL
    replacement = LEGACY_GEMINI_MODEL_REPLACEMENTS.get(selected)
    if replacement:
        logger.warning("Replacing unavailable Gemini fallback model %s with %s.", selected, replacement)
        return replacement
    return selected


@lru_cache(maxsize=1)
def load_local_ocr_model() -> dict:
    """Load the bundled local OCR model once per process."""
    return load_model(LOCAL_OCR_MODEL_PATH)


def extract_numbers_locally(image_bytes: bytes) -> list[str] | None:
    """Return a validated local type2 result, or None for other/invalid images.

    Type1 is deliberately left to the vision providers because its current
    leave-one-image-out validation is not reliable enough for production use.
    """
    result = recognize(image_bytes, load_local_ocr_model())
    if result.get("type") != "type2":
        return None
    text = str(result.get("text", ""))
    if not re.fullmatch(r"\d{5}", text):
        logger.warning("Local type2 OCR returned an invalid result: %r", text)
        return None
    logger.info("Using local OCR for type2 image: %s", text)
    return [text]


def detect_image_style(image_bytes: bytes) -> str:
    """Classify the CAPTCHA before choosing its OCR provider."""
    return detect_style(load_image(image_bytes))


def parse_ocr_space_response(response_data: dict) -> str:
    """Extract exactly one five-digit answer from an OCR.Space response."""
    if response_data.get("IsErroredOnProcessing"):
        message = response_data.get("ErrorMessage") or response_data.get("ErrorDetails") or "unknown error"
        raise RuntimeError(f"OCR.Space processing error: {message}")

    parsed_results = response_data.get("ParsedResults") or []
    text = "\n".join(
        str(result.get("ParsedText", ""))
        for result in parsed_results
        if isinstance(result, dict)
    )
    digits = "".join(character for character in text if character in "0123456789")
    if len(digits) != 5:
        raise RuntimeError(f"OCR.Space returned an invalid five-digit result: {text!r}")
    return digits


def _encode_png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def otsu_threshold(image: Image.Image) -> int:
    """Choose a global grayscale threshold by minimizing within-class variance."""
    histogram = image.convert("L").histogram()
    total = sum(histogram)
    if total == 0:
        return 127

    weighted_total = sum(level * count for level, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    best_threshold = 127
    for threshold, count in enumerate(histogram):
        background_weight += count
        background_sum += threshold * count
        foreground_weight = total - background_weight
        if not background_weight or not foreground_weight:
            continue
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        between_class_variance = (
            background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        )
        if between_class_variance > best_variance:
            best_variance = between_class_variance
            best_threshold = threshold
    return best_threshold


def prepare_ocr_space_variants(image_bytes: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Return original, contrast, enlarged Otsu, and local-adaptive PNG variants."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    enhanced = ImageEnhance.Contrast(image).enhance(2.0)
    enlarged = image.resize(
        (image.width * OCR_SPACE_UPSCALE_FACTOR, image.height * OCR_SPACE_UPSCALE_FACTOR),
        Image.Resampling.LANCZOS,
    )
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(enlarged))
    grayscale = grayscale.filter(ImageFilter.MedianFilter(size=3))
    threshold = otsu_threshold(grayscale)
    otsu_binary = grayscale.point(
        lambda pixel: 0 if pixel <= threshold else 255,
        mode="L",
    )

    adaptive_grayscale = ImageOps.grayscale(enlarged).filter(ImageFilter.MedianFilter(size=3))
    blur_radius = max(2, round(min(adaptive_grayscale.size) / 24))
    local_background = adaptive_grayscale.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    local_darkness = ImageChops.subtract(local_background, adaptive_grayscale)
    adaptive_binary = local_darkness.point(
        lambda difference: 0 if difference >= OCR_ADAPTIVE_THRESHOLD_DELTA else 255,
        mode="L",
    )

    return (
        _encode_png(image),
        _encode_png(enhanced),
        _encode_png(otsu_binary),
        _encode_png(adaptive_binary),
    )


async def extract_numbers_with_ocr_space(
    image_bytes: bytes,
    mime_type: str,
    *,
    api_key: str,
    engine: str = "2",
) -> list[str]:
    """Use OCR.Space for type1 images and return one validated five-digit value."""
    extension_by_mime = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    form = aiohttp.FormData()
    form.add_field(
        "file",
        image_bytes,
        filename=f"captcha{extension_by_mime.get(mime_type, '.png')}",
        content_type=mime_type,
    )
    form.add_field("language", "eng")
    form.add_field("isOverlayRequired", "false")
    form.add_field("detectOrientation", "true")
    form.add_field("scale", "true")
    if engine not in OCR_SPACE_ENGINES:
        raise ValueError(f"Unsupported OCR.Space engine: {engine}")
    form.add_field("OCREngine", engine)

    await reserve_ocr_space_request_slot()
    timeout = aiohttp.ClientTimeout(total=OCR_SPACE_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                OCR_SPACE_ENDPOINT,
                headers={"apikey": api_key},
                data=form,
            ) as result:
                response_data = await result.json(content_type=None)
                parsed_results = response_data.get("ParsedResults") or []
                parsed_text = ""
                if parsed_results and isinstance(parsed_results[0], dict):
                    parsed_text = str(parsed_results[0].get("ParsedText", ""))
                logger.info(
                    "OCR.Space response status=%s engine=%s parsed_text=%r",
                    result.status,
                    engine,
                    parsed_text,
                )
                if result.status == 429:
                    message = response_data.get("ErrorMessage", "HTTP 429")
                    retry_after = result.headers.get("Retry-After")
                    wait_hint = f" Retry-After: {retry_after}." if retry_after else ""
                    retry_after_seconds = parse_retry_after_seconds(retry_after)
                    await register_ocr_space_rate_limit(retry_after_seconds)
                    raise OCRSpaceRateLimitError(
                        f"OCR.Space API error 429: {message}.{wait_hint}",
                        retry_after_seconds=retry_after_seconds,
                    )
                if result.status >= 400:
                    message = response_data.get("ErrorMessage", f"HTTP {result.status}")
                    raise RuntimeError(f"OCR.Space API error {result.status}: {message}")
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"OCR.Space request timed out after {OCR_SPACE_TIMEOUT_SECONDS} seconds"
        ) from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"OCR.Space request failed: {exc}") from exc

    text = parse_ocr_space_response(response_data)
    logger.info("Using OCR.Space for type1 image: %s", text)
    return [text]


async def extract_numbers_with_ocr_space_variants(
    image_bytes: bytes,
    *,
    api_key: str,
) -> dict[tuple[str, str], str | None]:
    """Test the configured OCR.Space engine on each prepared PNG variant."""
    original_png, enhanced_png, otsu_png, adaptive_png = prepare_ocr_space_variants(image_bytes)
    variants = (
        ("before contrast", original_png),
        ("after contrast", enhanced_png),
        ("upscaled otsu", otsu_png),
        ("adaptive threshold", adaptive_png),
    )
    results: dict[tuple[str, str], str | None] = {}
    failures: list[str] = []
    rate_limited = False
    for label, payload in variants:
        for engine in OCR_SPACE_ENGINES:
            try:
                result = await extract_numbers_with_ocr_space(
                    payload,
                    "image/png",
                    api_key=api_key,
                    engine=engine,
                )
                results[(label, engine)] = result[0] if result else None
            except RuntimeError as exc:
                logger.warning("OCR.Space %s engine=%s attempt failed: %s", label, engine, exc)
                failures.append(f"{label}/engine{engine}: {exc}")
                results[(label, engine)] = None
                if isinstance(exc, OCRSpaceRateLimitError):
                    # Do not spend more requests on variants after a provider-level limit.
                    rate_limited = True
                    break
        if rate_limited:
            break

    if not any(results.values()):
        raise RuntimeError(f"OCR.Space failed for all image variants: {'; '.join(failures)}")
    return results


def load_type1_test_samples(samples_dir: Path) -> list[dict[str, str]]:
    """Load and validate type1 image labels from a captcha_samples directory."""
    label_path = samples_dir / "label.csv"
    with label_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"filename", "type", "answer"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("label.csv must contain filename,type,answer columns")
        rows = [row for row in reader if row.get("type") == "type1"]

    if not rows:
        raise ValueError("label.csv contains no type1 samples")

    seen: set[str] = set()
    for row in rows:
        filename = row["filename"].strip()
        answer = row["answer"].strip()
        if not filename or Path(filename).name != filename:
            raise ValueError(f"Invalid type1 sample filename: {filename!r}")
        if filename in seen:
            raise ValueError(f"Duplicate type1 sample filename: {filename}")
        if not re.fullmatch(r"[0-9]{5}", answer):
            raise ValueError(f"Expected five ASCII digits for {filename}: {answer!r}")
        if not (samples_dir / filename).is_file():
            raise FileNotFoundError(samples_dir / filename)
        row["filename"] = filename
        row["answer"] = answer
        seen.add(filename)
    return rows


def fingerprint_type1_dataset(samples_dir: Path, samples: list[dict[str, str]]) -> str:
    """Hash labels and image contents so stale progress is never applied to new data."""
    digest = hashlib.sha256()
    digest.update(f"ocr-preprocessing-v{OCR_PREPROCESSING_VERSION}\0".encode("ascii"))
    for variant, _ in TYPE1_TEST_VARIANTS:
        digest.update(variant.encode("utf-8"))
        digest.update(b"\0")
    for sample in samples:
        digest.update(sample["filename"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(sample["answer"].encode("ascii"))
        digest.update(b"\0")
        with (samples_dir / sample["filename"]).open("rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def new_testall_checkpoint(dataset_fingerprint: str) -> dict[str, object]:
    return {
        "version": OCR_PREPROCESSING_VERSION,
        "dataset_fingerprint": dataset_fingerprint,
        "records": {},
        "in_progress": None,
        "last_stop": None,
    }


def load_testall_checkpoint(path: Path, dataset_fingerprint: str) -> tuple[dict[str, object], bool]:
    """Reuse a checkpoint only when its schema and complete dataset still match."""
    checkpoint = read_runtime_json(path)
    if (
        checkpoint
        and checkpoint.get("version") == OCR_PREPROCESSING_VERSION
        and checkpoint.get("dataset_fingerprint") == dataset_fingerprint
        and isinstance(checkpoint.get("records"), dict)
    ):
        return checkpoint, True
    return new_testall_checkpoint(dataset_fingerprint), False


def testall_resume_position(
    samples: list[dict[str, str]], checkpoint: dict[str, object]
) -> tuple[str, str] | None:
    records = checkpoint.get("records")
    if not isinstance(records, dict):
        return None
    for sample in samples:
        record = records.get(sample["filename"])
        predictions = record.get("predictions", {}) if isinstance(record, dict) else {}
        if not isinstance(predictions, dict):
            predictions = {}
        for variant, _ in TYPE1_TEST_VARIANTS:
            if variant not in predictions:
                return sample["filename"], variant
    return None


def testall_remaining_request_count(
    samples: list[dict[str, str]], checkpoint: dict[str, object]
) -> int:
    records = checkpoint.get("records")
    if not isinstance(records, dict):
        return len(samples) * len(TYPE1_TEST_VARIANTS)
    remaining = 0
    for sample in samples:
        record = records.get(sample["filename"])
        predictions = record.get("predictions", {}) if isinstance(record, dict) else {}
        if not isinstance(predictions, dict):
            predictions = {}
        remaining += sum(variant not in predictions for variant, _ in TYPE1_TEST_VARIANTS)
    return remaining


def testall_report_records(
    samples: list[dict[str, str]],
    checkpoint: dict[str, object],
    current_failure: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Build a full-dataset report, including untested rows and the current API failure."""
    stored_records = checkpoint.get("records")
    if not isinstance(stored_records, dict):
        stored_records = {}
    report_records: list[dict[str, object]] = []
    for sample in samples:
        stored = stored_records.get(sample["filename"])
        predictions = stored.get("predictions", {}) if isinstance(stored, dict) else {}
        if not isinstance(predictions, dict):
            predictions = {}
        provider_errors: dict[str, str] = {}
        if current_failure and current_failure.get("filename") == sample["filename"]:
            variant = current_failure.get("variant", "")
            if variant:
                provider_errors[variant] = current_failure.get("reason", "OCR.Space request failed")
        report_records.append({
            "filename": sample["filename"],
            "expected": sample["answer"],
            "predictions": dict(predictions),
            "provider_errors": provider_errors,
        })
    return report_records


def compare_five_digit_answer(expected: str, predicted: str | None) -> tuple[int | None, list[tuple[int, str, str]]]:
    """Return mismatch count and one-based (position, expected, predicted) tuples."""
    if not predicted or not re.fullmatch(r"[0-9]{5}", predicted):
        return None, []
    mismatches = [
        (position, expected_digit, predicted_digit)
        for position, (expected_digit, predicted_digit) in enumerate(zip(expected, predicted), start=1)
        if expected_digit != predicted_digit
    ]
    return len(mismatches), mismatches


def build_type1_test_report(records: list[dict[str, object]]) -> tuple[str, bytes]:
    """Build per-variant accuracy summaries and an Excel-friendly CSV report."""
    summaries: list[str] = []
    values_by_variant: dict[str, list[list[str]]] = {}
    total = len(records)
    for variant, arabic_label in TYPE1_TEST_VARIANTS:
        counts: Counter[str] = Counter()
        confusions: Counter[tuple[str, str]] = Counter()
        variant_rows: list[list[str]] = []
        correct = 0
        tested = 0

        for record in records:
            expected = str(record["expected"])
            predictions = record["predictions"]
            provider_errors = record.get("provider_errors")
            if isinstance(provider_errors, dict) and variant in provider_errors:
                counts["api_error"] += 1
                variant_rows.append(["", "فشل API", "", str(provider_errors[variant])[:300]])
                continue
            if not isinstance(predictions, dict) or variant not in predictions:
                counts["not_tested"] += 1
                variant_rows.append(["", "لم تُختبر", "", ""])
                continue
            predicted = predictions.get(variant) if isinstance(predictions, dict) else None
            tested += 1
            predicted = str(predicted) if predicted else None
            error_count, mismatches = compare_five_digit_answer(expected, predicted)
            if error_count is None:
                counts["unreadable"] += 1
                variant_rows.append(["", "غير مقروء", "", ""])
                continue

            variant_rows.append([
                predicted or "",
                str(error_count),
                "; ".join(f"{position}:{truth}>{guess}" for position, truth, guess in mismatches),
                "",
            ])
            if error_count == 0:
                counts["correct"] += 1
                correct += 1
            elif error_count <= 3:
                counts[f"{error_count}_error"] += 1
            else:
                counts["4plus_error"] += 1
            confusions.update((truth, guess) for _, truth, guess in mismatches)

        rate = correct / tested * 100 if tested else 0.0
        summaries.append(
            f"{arabic_label}: صحيح بالكامل {correct}/{tested} من المختبر ({rate:.1f}%)، "
            f"خطأ برقم واحد {counts['1_error']}، برقمين {counts['2_error']}، "
            f"بثلاثة {counts['3_error']}، بأربعة أو خمسة {counts['4plus_error']}، "
            f"غير مقروء {counts['unreadable']}، فشل API {counts['api_error']}، "
            f"لم تُختبر {counts['not_tested']}"
        )
        top_confusions = confusions.most_common(5)
        if top_confusions:
            confusion_text = "، ".join(f"{truth}→{guess} ({count})" for (truth, guess), count in top_confusions)
            summaries.append(f"أكثر الالتباسات (الرقم الصحيح→المقروء): {confusion_text}")
        values_by_variant[variant] = variant_rows

    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    headers = ["filename", "expected"]
    for variant, _ in TYPE1_TEST_VARIANTS:
        prefix = variant.replace(" ", "_")
        headers.extend(
            f"{prefix}_{suffix}"
            for suffix in ("prediction", "error_count", "wrong_digits", "api_error")
        )
    writer.writerow(headers)
    for row_index, record in enumerate(records):
        row = [record["filename"], record["expected"]]
        for variant, _ in TYPE1_TEST_VARIANTS:
            row.extend(values_by_variant[variant][row_index])
        writer.writerow(row)

    summary = f"تقرير اختبار OCR.Space Engine 3 — عدد الصور في المجموعة: {total}\n" + "\n".join(summaries)
    return summary, buffer.getvalue().encode("utf-8-sig")


def attachment_image_mime_type(attachment) -> str | None:
    """Return a supported image MIME type even when Discord omits content_type."""
    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].lower()
    if content_type in SUPPORTED_IMAGE_TYPES:
        return content_type
    filename = str(getattr(attachment, "filename", ""))
    return IMAGE_MIME_BY_EXTENSION.get(Path(filename).suffix.lower())


def parse_numbers_response(content: str) -> list[str]:
    """Parse Gemini JSON, tolerating Markdown code fences around the JSON."""
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid JSON for the image") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Gemini returned an invalid response object")
    numbers = payload.get("numbers", [])
    if not isinstance(numbers, list):
        raise RuntimeError("Gemini returned an invalid numbers list")
    return [str(value).strip() for value in numbers if str(value).strip()]


def parse_openrouter_numbers_response(content: str) -> list[str]:
    """Accept JSON, or one unambiguous five-digit value from plain model text."""
    try:
        return parse_numbers_response(content)
    except RuntimeError as exc:
        candidates = list(dict.fromkeys(re.findall(r"(?<![0-9])[0-9]{5}(?![0-9])", content)))
        if len(candidates) == 1:
            return candidates
        raise RuntimeError(
            "OpenRouter returned invalid JSON and no single unambiguous five-digit value"
        ) from exc


def first_five_digit_result(numbers: list[str]) -> str | None:
    """Return the first provider result containing exactly five digits."""
    for value in numbers:
        digits = "".join(character for character in str(value) if character in "0123456789")
        if len(digits) == 5:
            return digits
    return None


async def extract_numbers_with_openrouter(
    image_bytes: bytes,
    mime_type: str,
    *,
    api_key: str,
    model: str = DEFAULT_OPENROUTER_MODEL,
) -> list[str]:
    """Use OpenRouter's free vision router as a provider-level fallback."""
    prompt = (
        "استخرج كل الأرقام الظاهرة في الصورة. أعد JSON صالحًا فقط بهذا الشكل: "
        '{"numbers":["..."]}. ضع كل رقم أو قيمة رقمية في عنصر مستقل، وحافظ على ترتيب الظهور. '
        "لا تخمّن أرقامًا غير واضحة، ولا تضع أي نص آخر داخل JSON."
    )
    request_body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                    },
                },
            ],
        }],
        "temperature": 0,
        "max_tokens": 128,
    }
    timeout = aiohttp.ClientTimeout(total=VISION_REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_body,
            ) as result:
                response_data = await result.json(content_type=None)
                if result.status >= 400:
                    message = response_data.get("error", {}).get("message", f"HTTP {result.status}")
                    raise RuntimeError(f"OpenRouter API error {result.status}: {message}")
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"OpenRouter vision request timed out after {VISION_REQUEST_TIMEOUT_SECONDS} seconds"
        ) from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenRouter returned no readable text") from exc
    return parse_openrouter_numbers_response(content or "")


async def extract_type1_with_fallback(
    image_bytes: bytes,
    mime_type: str,
    *,
    ocr_space_key: str,
) -> tuple[dict[tuple[str, str], str | None], str | None, str | None]:
    """Try OCR.Space, then Gemini and OpenRouter when OCR.Space cannot read type1."""
    ocr_results: dict[tuple[str, str], str | None] = {}
    failure_details: list[str] = []
    if ocr_space_key:
        try:
            ocr_results = await extract_numbers_with_ocr_space_variants(
                image_bytes,
                api_key=ocr_space_key,
            )
            if any(ocr_results):
                return ocr_results, None, None
        except RuntimeError as exc:
            logger.warning("OCR.Space failed for type1; trying vision fallbacks: %s", exc)
            failure_details.append(f"OCR.Space: {exc}")
    else:
        logger.warning("OCR_SPACE_API_KEY is missing; skipping OCR.Space for type1 fallback path.")
        failure_details.append("OCR_SPACE_API_KEY is missing")

    gemini_key, gemini_model = load_vision_config()
    if gemini_key:
        try:
            gemini_result = first_five_digit_result(
                await extract_numbers_from_image(image_bytes, mime_type, model=gemini_model)
            )
            if gemini_result:
                logger.info("Using Gemini fallback for type1 image: %s", gemini_result)
                return ocr_results, gemini_result, None
        except RuntimeError as exc:
            logger.warning("Gemini type1 fallback failed: %s", exc)
            failure_details.append(f"Gemini: {exc}")
    else:
        logger.info("GEMINI_API_KEY is not configured; skipping Gemini type1 fallback.")

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if openrouter_key:
        try:
            openrouter_result = first_five_digit_result(
                await extract_numbers_with_openrouter(image_bytes, mime_type, api_key=openrouter_key)
            )
            if openrouter_result:
                logger.info("Using OpenRouter fallback for type1 image: %s", openrouter_result)
                return ocr_results, None, openrouter_result
        except RuntimeError as exc:
            logger.warning("OpenRouter type1 fallback failed: %s", exc)
            failure_details.append(f"OpenRouter: {exc}")
    else:
        logger.info("OPENROUTER_API_KEY is not configured; skipping OpenRouter type1 fallback.")

    details = "; ".join(failure_details)
    suffix = f" Details: {details}" if details else ""
    raise RuntimeError(
        "OCR.Space and all configured vision fallbacks failed to return five digits." + suffix
    )


async def extract_numbers_from_image(
    image_bytes: bytes,
    mime_type: str,
    *,
    client=None,
    model: str | None = None,
) -> list[str]:
    """Ask Gemini Vision to return every number visible in an image."""
    api_key, configured_model = load_vision_config()
    selected_model = model or configured_model
    prompt = (
        "استخرج كل الأرقام الظاهرة في الصورة. أعد JSON صالحًا فقط بهذا الشكل: "
        '{"numbers":["..."]}. ضع كل رقم أو قيمة رقمية في عنصر مستقل، وحافظ على ترتيب الظهور. '
        "لا تخمّن أرقامًا غير واضحة، ولا تضع أي نص آخر داخل JSON."
    )

    if client is not None:
        # Kept for unit tests and callers that inject a Gemini-compatible client.
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=selected_model,
                    contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)],
                    config=types.GenerateContentConfig(
                        temperature=0,
                        max_output_tokens=128,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=VISION_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Gemini vision request timed out after {VISION_REQUEST_TIMEOUT_SECONDS} seconds"
            ) from exc
        try:
            content = response.text
        except (AttributeError, ValueError) as exc:
            raise RuntimeError("Gemini returned no readable text") from exc
    else:
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        request_body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
                ]
            }],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 128,
                "responseMimeType": "application/json",
            },
        }
        fallback_model = resolve_gemini_fallback_model(
            os.getenv("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_VISION_MODEL)
        )
        models_to_try = [selected_model]
        if fallback_model and fallback_model not in models_to_try:
            models_to_try.append(fallback_model)

        timeout = aiohttp.ClientTimeout(total=VISION_REQUEST_TIMEOUT_SECONDS)
        response_data = None
        last_failure = None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for model_name in models_to_try:
                endpoint = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_name}:generateContent"
                )
                try:
                    async with session.post(endpoint, params={"key": api_key}, json=request_body) as result:
                        response_data = await result.json(content_type=None)
                        if result.status < 400:
                            break
                        message = response_data.get("error", {}).get("message", f"HTTP {result.status}")
                        last_failure = RuntimeError(f"Gemini API error {result.status}: {message}")
                        if result.status not in TRANSIENT_GEMINI_STATUS_CODES:
                            raise last_failure
                        logger.warning(
                            "Gemini model %s returned %s; trying fallback model if available.",
                            model_name,
                            result.status,
                        )
                except asyncio.TimeoutError as exc:
                    last_failure = RuntimeError(
                        f"Gemini model {model_name} timed out after {VISION_REQUEST_TIMEOUT_SECONDS} seconds"
                    )
                    logger.warning("%s", last_failure)

        if response_data is None or last_failure is not None and response_data.get("candidates") is None:
            openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
            if openrouter_key:
                try:
                    logger.warning("Gemini was unavailable; trying OpenRouter free vision fallback.")
                    return await extract_numbers_with_openrouter(
                        image_bytes,
                        mime_type,
                        api_key=openrouter_key,
                        model=os.getenv("OPENROUTER_VISION_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
                        or DEFAULT_OPENROUTER_MODEL,
                    )
                except RuntimeError as openrouter_failure:
                    logger.warning("OpenRouter fallback failed: %s", openrouter_failure)
            raise last_failure or RuntimeError("Gemini returned no response")

        try:
            content = response_data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini returned no readable text") from exc

    if not content:
        return []
    return parse_numbers_response(content)


def create_bot() -> commands.Bot:
    """Create an account-authenticated client that accepts commands from users.

    In discord.py-self, ``self_bot=True`` intentionally ignores messages whose
    author is not the logged-in account. The requested behavior is for the
    account-authenticated client to answer commands sent by other accounts,
    so ``user_bot=True`` is required here.
    """
    return commands.Bot(command_prefix=COMMAND_PREFIX, user_bot=True, help_command=None)


bot = create_bot()

class NotInSharedGuild(commands.CheckFailure):
    """Raised when a command is outside the primary bot's active guilds."""


class UserCommandCooldown(commands.CheckFailure):
    """Raised when a user invokes a second command during their cooldown."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"retry after {retry_after:.3f} seconds")

def load_shared_guild_ids(now: float | None = None) -> set[int]:
    """Return fresh primary-bot guild IDs; fail closed for missing or stale state."""
    try:
        with SHARED_GUILD_STATE_PATH.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        updated_at = state.get("updated_at")
        guild_ids = state.get("guild_ids")
        if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
            return set()
        current_time = time.time() if now is None else now
        age = current_time - float(updated_at)
        if age < -30 or age > SHARED_GUILD_STATE_MAX_AGE_SECONDS or not isinstance(guild_ids, list):
            return set()
        return {
            int(guild_id)
            for guild_id in guild_ids
            if not isinstance(guild_id, bool) and int(guild_id) > 0
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return set()

@bot.check
async def check_shared_guild(ctx: commands.Context) -> bool:
    """Only process commands in guilds where this account is currently present.

    The shared state file remains a fast path, but it is not authoritative: on
    hosts where the primary bot's background task is delayed, the self-bot's
    own guild cache is enough to confirm that it is in the server.
    """
    if ctx.guild is None:
        raise NotInSharedGuild("Commands are only enabled in guilds shared with Cosmo-Coins.")
    guild_id = int(ctx.guild.id)
    if guild_id in load_shared_guild_ids():
        return True
    if bot.get_guild(guild_id) is not None:
        logger.info("Accepted command in guild %s from the self-bot guild cache.", guild_id)
        return True
    logger.warning("Ignoring command in guild %s: guild is absent from the state file and cache.", guild_id)
    raise NotInSharedGuild("Commands are only enabled in guilds shared with Cosmo-Coins.")


@bot.check
async def check_user_command_cooldown(ctx: commands.Context) -> bool:
    """Apply a separate 30-second cooldown to each user in each guild."""
    if ctx.command and ctx.command.name in {"start", "restart", "shutdown", "status"}:
        return True
    user_id = getattr(ctx.author, "id", None)
    if user_id is None:
        return True
    guild_id = int(getattr(ctx.guild, "id", None) or 0)
    cooldown_key = (int(user_id), guild_id)

    now = time.monotonic()
    retry_at = user_command_cooldowns.get(cooldown_key, 0.0)
    if retry_at > now:
        raise UserCommandCooldown(retry_at - now)

    user_command_cooldowns[cooldown_key] = now + USER_COMMAND_COOLDOWN_SECONDS
    return True


async def send_notice(ctx: commands.Context, message: str) -> None:
    """Send a plain, non-pinging notice; report permission/API failures in logs."""
    try:
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.warning(
            "Could not send a notice in channel %s: the bot lacks permission to send messages.",
            getattr(ctx.channel, "id", "unknown"),
        )
    except discord.HTTPException:
        logger.exception("Discord rejected a notice in channel %s.", getattr(ctx.channel, "id", "unknown"))


def can_send_in(channel: discord.abc.GuildChannel, permissions: discord.Permissions) -> bool:
    if isinstance(channel, discord.Thread):
        return permissions.send_messages_in_threads
    return permissions.send_messages


async def check_control_owner(ctx: commands.Context) -> bool:
    """Allow only the second-bot account, its owner, or the configured role."""
    if bot.user is not None and getattr(ctx.author, "id", None) == bot.user.id:
        return True
    if (
        SAY_ALLOWED_ROLE_ID is not None
        and isinstance(ctx.author, discord.Member)
        and any(role.id == SAY_ALLOWED_ROLE_ID for role in ctx.author.roles)
    ):
        return True
    try:
        return await bot.is_owner(ctx.author)
    except discord.HTTPException:
        return False


@bot.command(name="start")
@commands.check(check_control_owner)
async def start_primary_bot(ctx: commands.Context) -> None:
    await send_notice(ctx, f"🟢 {await control_primary_bot('start')}")


@bot.command(name="restart")
@commands.check(check_control_owner)
async def restart_primary_bot(ctx: commands.Context) -> None:
    await send_notice(ctx, f"🔄 {await control_primary_bot('restart')}")


@bot.command(name="shutdown")
@commands.check(check_control_owner)
async def shutdown_primary_bot(ctx: commands.Context) -> None:
    await send_notice(ctx, f"🔴 {await control_primary_bot('shutdown')}")


@bot.command(name="status")
@commands.check(check_control_owner)
async def status_primary_bot(ctx: commands.Context) -> None:
    await send_notice(ctx, f"ℹ️ {await control_primary_bot('status')}")


@bot.command(name="say")
async def say(ctx: commands.Context, *, text: str) -> None:
    """Delete the caller's command and repeat its non-empty text without mentions."""
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        await send_notice(ctx, "هذا الأمر متاح داخل السيرفر فقط.")
        return

    text = text.strip()
    if not text:
        await send_notice(ctx, f"النص فارغ. الاستخدام: `{COMMAND_PREFIX}say النص المطلوب`.")
        return
    if len(text) > 2000:
        await send_notice(ctx, "النص أطول من الحد المسموح (2000 حرف).")
        return

    allowed_by_role = (
        SAY_ALLOWED_ROLE_ID is not None
        and any(role.id == SAY_ALLOWED_ROLE_ID for role in ctx.author.roles)
    )
    if not allowed_by_role:
        # In self-bot mode, commands are processed only when sent by this account.
        # Keep the owner fallback for isolated tests and compatible implementations.
        allowed_by_owner = bot.user is not None and ctx.author.id == bot.user.id
        if not allowed_by_owner:
            try:
                allowed_by_owner = await bot.is_owner(ctx.author)
            except discord.HTTPException:
                logger.exception("Could not verify whether user %s owns this account.", ctx.author.id)
                await send_notice(ctx, "تعذر التحقق من مالك الحساب الآن. حاول مرة أخرى لاحقًا.")
                return
        if not allowed_by_owner:
            await send_notice(ctx, "ليس لديك صلاحية استخدام هذا الأمر.")
            return

    bot_member = ctx.guild.me
    if bot_member is None:
        await send_notice(ctx, "تعذر التحقق من صلاحيات البوت في هذا السيرفر.")
        return

    permissions = ctx.channel.permissions_for(bot_member)
    if not can_send_in(ctx.channel, permissions):
        logger.warning(
            "Cannot run !say in channel %s: missing send permission.",
            ctx.channel.id,
        )
        return
    if not permissions.manage_messages:
        await send_notice(ctx, "لا أستطيع حذف رسالة الأمر؛ امنحني صلاحية Manage Messages.")
        return

    await asyncio.sleep(COMMAND_RESPONSE_DELAY_SECONDS)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await send_notice(ctx, "تعذر حذف رسالة الأمر. تحقق من صلاحية Manage Messages في هذه القناة.")
        return
    except discord.HTTPException:
        logger.exception("Failed to delete !say command message %s.", ctx.message.id)
        await send_notice(ctx, "حدث خطأ أثناء حذف رسالة الأمر. حاول مرة أخرى.")
        return

    try:
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.exception("Could not send !say output in channel %s.", ctx.channel.id)
        await send_notice(ctx, "حُذفت رسالة الأمر، لكن تعذر إرسال النص. تحقق من صلاحية إرسال الرسائل.")
    except discord.HTTPException:
        logger.exception("Discord rejected !say output in channel %s.", ctx.channel.id)
        await send_notice(ctx, "حُذفت رسالة الأمر، لكن تعذر إرسال النص بسبب خطأ من Discord.")


@bot.command(name="ping")
async def ping(ctx: commands.Context) -> None:
    """Show the bot's connected state and Discord gateway latency."""
    await asyncio.sleep(COMMAND_RESPONSE_DELAY_SECONDS)
    latency_ms = round(bot.latency * 1000)
    response = f"حالة البوت: متصل\nسرعة الاستجابة (Ping): {latency_ms} ms"
    await ctx.send(response, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="numbers", aliases=["استخراج_الأرقام", "ارقام"])
async def numbers(ctx: commands.Context) -> None:
    """Extract visible numbers from the first image attached to the command."""
    image_attachment = next(
        (
            (item, attachment_image_mime_type(item))
            for item in getattr(ctx.message, "attachments", [])
            if attachment_image_mime_type(item) is not None
        ),
        None,
    )
    if image_attachment is None:
        await send_notice(ctx, f"أرفق صورة مع الأمر، مثل: `{COMMAND_PREFIX}numbers`.")
        return
    attachment, mime_type = image_attachment

    if getattr(attachment, "size", 0) > MAX_IMAGE_BYTES:
        await send_notice(ctx, "حجم الصورة كبير جدًا. الحد الأقصى هو 8 ميغابايت.")
        return

    try:
        image_bytes = await attachment.read()
        if len(image_bytes) > MAX_IMAGE_BYTES:
            await send_notice(ctx, "حجم الصورة كبير جدًا. الحد الأقصى هو 8 ميغابايت.")
            return

        image_style = detect_image_style(image_bytes)
        if image_style == "type2":
            numbers_found = extract_numbers_locally(image_bytes)
            if numbers_found is None:
                await send_notice(ctx, "تعذر قراءة أرقام صورة النوع الثاني محليًا.")
                return
        else:
            ocr_space_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
            ocr_results, gemini_result, openrouter_result = await extract_type1_with_fallback(
                image_bytes,
                mime_type,
                ocr_space_key=ocr_space_key,
            )
            variant_labels = (
                ("before contrast", "قبل تحسين التباين"),
                ("after contrast", "بعد تحسين التباين"),
                ("upscaled otsu", "بعد التكبير وعتبة Otsu"),
                ("adaptive threshold", "بعد العتبة المحلية التكيفية"),
            )
            result_lines = [
                f"{label} (المحرك {engine}): `{ocr_results.get((variant, engine)) or 'لم تُقرأ خمسة أرقام'}`"
                for variant, label in variant_labels
                for engine in OCR_SPACE_ENGINES
            ]
            if gemini_result:
                result_lines.append(f"نتيجة Gemini الاحتياطية: `{gemini_result}`")
            if openrouter_result:
                result_lines.append(f"نتيجة OpenRouter الاحتياطية: `{openrouter_result}`")
            await ctx.send(
                "نتيجة قراءة النوع الأول:\n" + "\n".join(result_lines),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
    except RuntimeError as exc:
        logger.warning("Image number extraction failed: %s", exc)
        error_text = str(exc).lower()
        if isinstance(exc, OCRSpaceDailyBudgetError):
            await send_notice(
                ctx,
                "بلغ البوت سقف الطلبات المحلي الآمن لـOCR.Space لهذا اليوم؛ لم يُرسل طلب جديد. "
                "انتظر تجدد الحصة ثم حاول مرة أخرى.",
            )
        elif isinstance(exc, OCRSpaceCooldownError):
            await send_notice(ctx, f"OCR.Space في فترة تهدئة بعد 429؛ لم يُرسل طلب جديد. {exc}")
        elif "ocr.space api error 429" in error_text:
            await send_notice(
                ctx,
                "رفض OCR.Space الطلبات مؤقتًا (HTTP 429)، لذلك أوقف البوت بقية محاولات OCR. "
                "قد تكون الحصة أو سرعة الطلبات قد تجاوزت الحد؛ هذا لا يعني أن الصورة غير مقروءة. "
                "انتظر تجدد الحصة أو تحقق من لوحة OCR.Space ثم أعد المحاولة.",
            )
        elif "timed out" in error_text or "api error" in error_text:
            await send_notice(ctx, "خدمة OCR الخارجية تأخرت أو غير متاحة مؤقتًا. حاول مرة أخرى بعد قليل.")
        else:
            await send_notice(
                ctx,
                "تعذر قراءة الأرقام من الصورة. تأكد من إعداد OCR_SPACE_API_KEY أو GEMINI_API_KEY أو OPENROUTER_API_KEY.",
            )
        return
    except Exception:
        logger.exception("Unexpected failure while extracting numbers from an image.")
        await send_notice(ctx, "حدث خطأ أثناء استخراج الأرقام من الصورة. حاول مرة أخرى لاحقًا.")
        return

    if not numbers_found:
        await send_notice(ctx, "لم أجد أرقامًا واضحة في الصورة.")
        return

    result = "\n".join(f"{index}. `{value}`" for index, value in enumerate(numbers_found, start=1))
    await ctx.send(f"الأرقام المستخرجة:\n{result}", allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="testall")
@commands.check(check_control_owner)
async def testall(ctx: commands.Context) -> None:
    """Evaluate OCR.Space Engine 3, resuming from a saved per-variant checkpoint."""
    api_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if not api_key:
        await send_notice(ctx, "لم يُضبط OCR_SPACE_API_KEY في ملف إعدادات البوت.")
        return
    if TYPE1_TEST_LOCK.locked():
        await send_notice(ctx, "يوجد اختبار شامل قيد التشغيل بالفعل.")
        return

    async with TYPE1_TEST_LOCK:
        samples_dir = BASE_DIR.parent / "captcha_samples"
        try:
            samples = load_type1_test_samples(samples_dir)
            dataset_fingerprint = fingerprint_type1_dataset(samples_dir, samples)
        except (OSError, ValueError, csv.Error) as exc:
            await send_notice(ctx, f"تعذر تحميل عينات النوع الأول: {exc}")
            return

        checkpoint_path = runtime_state_path("testall_engine3_checkpoint.json")
        checkpoint, resumed = load_testall_checkpoint(checkpoint_path, dataset_fingerprint)
        checkpoint_records = checkpoint.get("records")
        if not isinstance(checkpoint_records, dict):
            checkpoint_records = {}
            checkpoint["records"] = checkpoint_records
        write_runtime_json(checkpoint_path, checkpoint)

        resume_position = testall_resume_position(samples, checkpoint)
        remaining_requests = testall_remaining_request_count(samples, checkpoint)
        if resumed and resume_position:
            start_description = (
                f"استئناف من {resume_position[0]} / {resume_position[1]}؛ "
                "النتائج المكتملة محفوظة ولن تُرسل إلى OCR.Space مرة أخرى."
            )
        elif resumed:
            start_description = "كل المعالجات مكتملة في نقطة الحفظ؛ سأعيد إرسال التقرير دون طلبات OCR جديدة."
        else:
            start_description = "لا توجد نقطة حفظ مطابقة لهذه المجموعة؛ يبدأ الاختبار من أول صورة."
        progress_message = await ctx.send(
            f"اختبار {len(samples)} صورة من النوع الأول باستخدام OCR.Space Engine 3. {start_description} "
            f"المعالجات المتبقية: {remaining_requests} طلبًا كحد أقصى؛ "
            f"الفاصل {OCR_SPACE_MIN_INTERVAL_SECONDS:g} ثانية، والسقف المحلي "
            f"{OCR_SPACE_DAILY_REQUEST_BUDGET} طلب/يوم UTC."
        )
        request_count = 0
        stop_reason: str | None = None
        current_failure: dict[str, str] | None = None

        for image_index, sample in enumerate(samples, start=1):
            stored_record = checkpoint_records.get(sample["filename"])
            if not isinstance(stored_record, dict):
                stored_record = {"expected": sample["answer"], "predictions": {}}
                checkpoint_records[sample["filename"]] = stored_record
            predictions = stored_record.get("predictions")
            if not isinstance(predictions, dict):
                predictions = {}
                stored_record["predictions"] = predictions

            if all(variant in predictions for variant, _ in TYPE1_TEST_VARIANTS):
                continue

            image_path = samples_dir / sample["filename"]
            try:
                image_bytes = image_path.read_bytes()
                prepared_variants = prepare_ocr_space_variants(image_bytes)
            except (OSError, ValueError) as exc:
                logger.warning("Could not prepare test image %s: %s", image_path.name, exc)
                for variant, _ in TYPE1_TEST_VARIANTS:
                    predictions.setdefault(variant, None)
                write_runtime_json(checkpoint_path, checkpoint)
                continue

            for (variant, _), payload in zip(TYPE1_TEST_VARIANTS, prepared_variants):
                if variant in predictions:
                    continue
                checkpoint["in_progress"] = {
                    "filename": sample["filename"],
                    "variant": variant,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                write_runtime_json(checkpoint_path, checkpoint)
                request_count += 1
                try:
                    result = await extract_numbers_with_ocr_space(
                        payload,
                        "image/png",
                        api_key=api_key,
                        engine="3",
                    )
                    predictions[variant] = result[0] if result else None
                    checkpoint["in_progress"] = None
                    checkpoint["last_stop"] = None
                    write_runtime_json(checkpoint_path, checkpoint)
                except RuntimeError as exc:
                    logger.warning("Engine 3 test failed for %s/%s: %s", image_path.name, variant, exc)
                    error_text = str(exc).lower()
                    is_provider_failure = (
                        isinstance(exc, (OCRSpaceRateLimitError, OCRSpaceCooldownError, OCRSpaceDailyBudgetError))
                        or "api error" in error_text
                        or "request timed out" in error_text
                        or "request failed" in error_text
                    )
                    if is_provider_failure:
                        current_failure = {
                            "filename": sample["filename"],
                            "variant": variant,
                            "reason": str(exc),
                        }
                        checkpoint["in_progress"] = None
                        checkpoint["last_stop"] = {
                            **current_failure,
                            "stopped_at": datetime.now(timezone.utc).isoformat(),
                        }
                        write_runtime_json(checkpoint_path, checkpoint)
                        if isinstance(exc, OCRSpaceCooldownError):
                            stop_reason = "لا يزال OCR.Space في فترة التهدئة؛ لم يُرسل طلب جديد وستُعاد المعالجة لاحقًا."
                        elif isinstance(exc, OCRSpaceRateLimitError) or "429" in error_text or "quota" in error_text or "limit reached" in error_text:
                            stop_reason = "أوقف الاختبار طلبات OCR بسبب 429/الحصة؛ ستُعاد هذه المعالجة عند الاستئناف."
                        elif isinstance(exc, OCRSpaceDailyBudgetError):
                            stop_reason = "بلغ البوت السقف اليومي المحلي؛ لم يُرسل هذا الطلب وستُعاد المحاولة لاحقًا."
                        else:
                            stop_reason = "تعطل طلب OCR.Space؛ حُفظت نقطة الاستئناف ولن تُعاد النتائج السابقة."
                        break

                    predictions[variant] = None
                    checkpoint["in_progress"] = None
                    write_runtime_json(checkpoint_path, checkpoint)

            if stop_reason:
                break
            if image_index % 10 == 0 or image_index == len(samples):
                try:
                    await progress_message.edit(
                        content=f"اختبار Engine 3 جارٍ: اكتملت {image_index}/{len(samples)} صورة "
                        f"({request_count} محاولة طلب في هذه الجلسة)."
                    )
                except discord.HTTPException:
                    logger.info("Could not update testall progress message.")

        records = testall_report_records(samples, checkpoint, current_failure)
        summary, report_csv = build_type1_test_report(records)
        summary += f"\nمحاولات الطلب في هذه الجلسة: {request_count} من {remaining_requests} المتبقية عند بدايتها."
        if stop_reason:
            summary += (
                f"\n{stop_reason} نقطة الحفظ: {current_failure['filename']} / {current_failure['variant']}. "
                "شغّل $testall لاحقًا للمتابعة من هذه المعالجة."
            )

        try:
            final_status = (
                f"توقف testall؛ حُفظت نقطة الاستئناف عند {current_failure['filename']} / "
                f"{current_failure['variant']}."
                if stop_reason and current_failure
                else "اكتمل اختبار الصور؛ أرفقت التقرير التفصيلي."
            )
            await progress_message.edit(content=final_status)
        except discord.HTTPException:
            logger.info("Could not update final testall progress message.")
        await ctx.send(
            summary,
            file=discord.File(BytesIO(report_csv), filename="testall_engine3_report.csv"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if not stop_reason:
            checkpoint_path.unlink(missing_ok=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    """Process self-account messages explicitly and log command-shaped input."""
    content = message.content or ""
    if content.startswith(COMMAND_PREFIX):
        logger.info(
            "Received command-shaped message guild=%s author=%s content=%r",
            getattr(message.guild, "id", None),
            getattr(message.author, "id", None),
            content,
        )
    await bot.process_commands(message)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, NotInSharedGuild):
        return
    if isinstance(error, UserCommandCooldown):
        await send_notice(
            ctx,
            f"انتظر {math.ceil(error.retry_after)} ثانية قبل استخدام أمر آخر.",
        )
        return
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument) and ctx.command and ctx.command.name == "say":
        await send_notice(ctx, f"النص فارغ. الاستخدام: `{COMMAND_PREFIX}say النص المطلوب`.")
        return

    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        logger.error(
            "Unhandled error while running a command.",
            exc_info=(type(original), original, original.__traceback__),
        )
    else:
        logger.error("Unhandled command error: %s", error, exc_info=(type(error), error, error.__traceback__))
    await send_notice(ctx, "حدث خطأ غير متوقع أثناء تنفيذ الأمر. تم تسجيله، ويمكنك المحاولة مجددًا.")


def main() -> None:
    global SAY_ALLOWED_ROLE_ID

    try:
        token, SAY_ALLOWED_ROLE_ID = load_config()
    except ValueError as exc:
        logger.critical("%s", exc)
        sys.exit(2)

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.critical(
            "Discord rejected the configured account token. Verify that the configured token is valid."
        )
        sys.exit(2)
    except (discord.HTTPException, OSError):
        logger.exception("The bot stopped because it could not connect to Discord.")
        sys.exit(1)


if __name__ == "__main__":
    main()
