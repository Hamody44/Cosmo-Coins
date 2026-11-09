"""Standalone Discord self-client with restricted !say and public !ping commands."""
from __future__ import annotations

import logging
import json
import os
import sys
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SHARED_GUILD_STATE_PATH = BASE_DIR / ".runtime" / "shared_guilds.json"
SHARED_GUILD_STATE_MAX_AGE_SECONDS = 90
# The panel may expose Cosmo-Coins' DISCORD_BOT_TOKEN to every process.
# Prefer this bot's local .env so it cannot accidentally inherit the other bot's token.
load_dotenv(BASE_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("second_bot")

# Populated by load_config() in main(). Keeping imports side-effect free lets
# the unit tests exercise commands without a real Discord token.
SAY_ALLOWED_ROLE_ID: int | None = None


def load_config() -> tuple[str, int]:
    """Read and validate required environment variables."""
    token = (
        os.getenv("SECOND_BOT_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if not token:
        raise ValueError("SECOND_BOT_TOKEN (or DISCORD_BOT_TOKEN) is missing. Set the account token in second_bot/.env.")

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


def create_bot() -> commands.Bot:
    """Create the self-client without configuring Gateway Intents."""
    return commands.Bot(command_prefix="!", self_bot=True, help_command=None)


bot = create_bot()

class NotInSharedGuild(commands.CheckFailure):
    """Raised when a command is outside the primary bot's active guilds."""

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
    """Only process commands in guilds where both bots are currently present."""
    if ctx.guild is None or int(ctx.guild.id) not in load_shared_guild_ids():
        raise NotInSharedGuild("Commands are only enabled in guilds shared with Cosmo-Coins.")
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
        await send_notice(ctx, "النص فارغ. الاستخدام: `!say النص المطلوب`.")
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
    latency_ms = round(bot.latency * 1000)
    response = f"حالة البوت: متصل\nسرعة الاستجابة (Ping): {latency_ms} ms"
    await ctx.send(response, allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, NotInSharedGuild):
        return
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument) and ctx.command and ctx.command.name == "say":
        await send_notice(ctx, "النص فارغ. الاستخدام: `!say النص المطلوب`.")
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
