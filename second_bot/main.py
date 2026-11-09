"""Standalone Discord self-client with restricted !say and public !ping commands."""
from __future__ import annotations

import logging
import json
import os
import sys
import time
import asyncio
import base64
import math
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

BASE_DIR = Path(__file__).resolve().parent
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
DEFAULT_VISION_MODEL = "gpt-4o-mini"
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


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


def load_vision_config() -> tuple[str, str]:
    """Read the optional OpenAI settings used by the image-number command."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL
    return api_key, model


async def extract_numbers_from_image(
    image_bytes: bytes,
    mime_type: str,
    *,
    client=None,
    model: str | None = None,
) -> list[str]:
    """Ask a vision model to return every number visible in an image."""
    api_key, configured_model = load_vision_config()
    if client is None:
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing")
        client = AsyncOpenAI(api_key=api_key)

    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    response = await client.chat.completions.create(
        model=model or configured_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "استخرج كل الأرقام الظاهرة في الصورة. أعد JSON صالحًا فقط بهذا الشكل: "
                    '{"numbers":["..."]}. ضع كل رقم أو قيمة رقمية في عنصر مستقل، وحافظ على ترتيب الظهور. '
                    "لا تخمّن أرقامًا غير واضحة، ولا تضع أي نص آخر داخل JSON."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "استخرج الأرقام من هذه الصورة."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"},
                    },
                ],
            },
        ],
    )

    content = response.choices[0].message.content if response.choices else None
    if not content:
        return []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The vision model returned invalid JSON") from exc
    numbers = payload.get("numbers", [])
    if not isinstance(numbers, list):
        raise RuntimeError("The vision model returned an invalid numbers list")
    return [str(value).strip() for value in numbers if str(value).strip()]


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
    attachment = next(
        (
            item
            for item in getattr(ctx.message, "attachments", [])
            if (item.content_type or "").split(";", 1)[0].lower() in SUPPORTED_IMAGE_TYPES
        ),
        None,
    )
    if attachment is None:
        await send_notice(ctx, f"أرفق صورة مع الأمر، مثل: `{COMMAND_PREFIX}numbers`.")
        return

    if getattr(attachment, "size", 0) > MAX_IMAGE_BYTES:
        await send_notice(ctx, "حجم الصورة كبير جدًا. الحد الأقصى هو 8 ميغابايت.")
        return

    api_key, _ = load_vision_config()
    if not api_key:
        logger.error("The image-number command was used without OPENAI_API_KEY.")
        await send_notice(ctx, "ميزة استخراج الأرقام غير مفعلة حاليًا؛ أضف OPENAI_API_KEY إلى إعدادات البوت.")
        return

    try:
        image_bytes = await attachment.read()
        if len(image_bytes) > MAX_IMAGE_BYTES:
            await send_notice(ctx, "حجم الصورة كبير جدًا. الحد الأقصى هو 8 ميغابايت.")
            return
        numbers_found = await extract_numbers_from_image(
            image_bytes,
            (attachment.content_type or "image/png").split(";", 1)[0].lower(),
        )
    except Exception:
        logger.exception("Failed to extract numbers from an image.")
        await send_notice(ctx, "تعذر استخراج الأرقام من الصورة الآن. حاول مرة أخرى لاحقًا.")
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
