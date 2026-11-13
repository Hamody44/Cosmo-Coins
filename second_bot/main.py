"""Standalone Discord self-client with restricted !say and public !ping commands."""
from __future__ import annotations

import logging
import json
import base64
import os
import sys
import time
import asyncio
import math
import re
from io import BytesIO
from pathlib import Path
from functools import lru_cache

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from google.genai import types
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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
DEFAULT_FALLBACK_VISION_MODEL = "gemini-2.5-flash-lite"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
VISION_REQUEST_TIMEOUT_SECONDS = 20
OCR_SPACE_ENDPOINT = "https://api.ocr.space/parse/image"
OCR_SPACE_TIMEOUT_SECONDS = 20
OCR_SPACE_ENGINES = ("2", "3")
OCR_SPACE_UPSCALE_FACTOR = 3
TRANSIENT_GEMINI_STATUS_CODES = {429, 500, 502, 503, 504}
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


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


def prepare_ocr_space_variants(image_bytes: bytes) -> tuple[bytes, bytes, bytes]:
    """Return original, contrast-enhanced, and enlarged binary PNG variants."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")

    enhanced = ImageEnhance.Contrast(image).enhance(2.0)
    enlarged = image.resize(
        (image.width * OCR_SPACE_UPSCALE_FACTOR, image.height * OCR_SPACE_UPSCALE_FACTOR),
        Image.Resampling.LANCZOS,
    )
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(enlarged))
    grayscale = grayscale.filter(ImageFilter.MedianFilter(size=3))
    binary = grayscale.point(lambda pixel: 255 if pixel >= 150 else 0, mode="1")

    return _encode_png(image), _encode_png(enhanced), _encode_png(binary)


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
    """Test both OCR.Space engines independently on all three PNG variants."""
    original_png, enhanced_png, binary_png = prepare_ocr_space_variants(image_bytes)
    variants = (
        ("before contrast", original_png),
        ("after contrast", enhanced_png),
        ("upscaled binary", binary_png),
    )
    results: dict[tuple[str, str], str | None] = {}
    failures: list[str] = []
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

    if not any(results.values()):
        raise RuntimeError(f"OCR.Space failed for all image variants: {'; '.join(failures)}")
    return results


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
    return parse_numbers_response(content or "")


async def extract_type1_with_fallback(
    image_bytes: bytes,
    mime_type: str,
    *,
    ocr_space_key: str,
) -> tuple[dict[tuple[str, str], str | None], str | None, str | None]:
    """Try OCR.Space, then Gemini and OpenRouter when OCR.Space cannot read type1."""
    ocr_results: dict[tuple[str, str], str | None] = {}
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
    else:
        logger.warning("OCR_SPACE_API_KEY is missing; skipping OCR.Space for type1 fallback path.")

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
    else:
        logger.info("OPENROUTER_API_KEY is not configured; skipping OpenRouter type1 fallback.")

    raise RuntimeError(
        "OCR.Space and all configured vision fallbacks failed to return five digits"
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
        fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_VISION_MODEL).strip()
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
                ("upscaled binary", "بعد التكبير والأبيض والأسود"),
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
        if "timed out" in str(exc).lower() or "api error" in str(exc).lower():
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
