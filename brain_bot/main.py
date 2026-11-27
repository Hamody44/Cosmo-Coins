"""Standalone Discord bot for securely controlling the Cosmo-Coins processes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

try:
    from .protocol import enqueue, read_status, write_connection
except ImportError:  # direct execution through the existing process launcher
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from brain_bot.protocol import enqueue, read_status, write_connection

ROOT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT_DIR / "second_bot" / ".runtime" / "process_control"


def load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE files without overriding the process environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env(ROOT_DIR / ".env")
load_local_env(Path(__file__).resolve().parent / ".env")

def configured_second_bot_token() -> str:
    """Read the second bot token only to enforce credential separation."""
    if os.getenv("SECOND_BOT_TOKEN", "").strip():
        return os.environ["SECOND_BOT_TOKEN"].strip()
    try:
        for line in (ROOT_DIR / "second_bot" / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SECOND_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))
    except OSError:
        pass
    return ""

try:
    OWNER_ID = int(os.getenv("PRIMARY_OWNER_ID", "").strip())
except ValueError:
    OWNER_ID = 0
TOKEN = os.getenv("BRAIN_BOT_TOKEN", "").strip()
PREFIX = os.getenv("BRAIN_BOT_PREFIX", "#").strip() or "#"
if not TOKEN:
    raise RuntimeError("BRAIN_BOT_TOKEN is required for the independent Brain Bot")
if TOKEN == os.getenv("DISCORD_BOT_TOKEN", "").strip() or TOKEN == configured_second_bot_token():
    raise RuntimeError("BRAIN_BOT_TOKEN must be unique; do not reuse another Discord bot token")
if OWNER_ID <= 0:
    raise RuntimeError("PRIMARY_OWNER_ID must be a positive Discord user ID")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def process_state(name: str) -> dict[str, Any]:
    pid_path = STATE_DIR / f"{name}.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        alive = True
    except (OSError, ValueError):
        pid, alive = None, False
    log_path = STATE_DIR / f"{name}.log"
    last_error = None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
        for line in reversed(lines):
            if any(word in line.lower() for word in ("error", "exception", "traceback", "failed", "خطأ", "تعذر")):
                last_error = line[-350:]
                break
    except OSError:
        pass
    return {"pid": pid, "alive": alive, "last_error": last_error}


def format_status() -> str:
    data = read_status()
    connections = data.get("connections", {})
    brain_connection = connections.get("brain_bot", {}) if isinstance(connections, dict) else {}
    brain_state = brain_connection.get("state", "غير معروف")
    lines = [f"Brain Bot: {brain_state} (PID {os.getpid()})"]
    for name, label in (("primary", "primary"), ("second_bot", "second_bot")):
        state = process_state(name)
        connection = connections.get(name, {}) if isinstance(connections, dict) else {}
        gateway = connection.get("state", "غير معروف")
        pid = state["pid"] if state["alive"] else "—"
        lines.append(f"{label}: {'يعمل' if state['alive'] else 'متوقف'} | PID: {pid} | اتصال Discord: {gateway}")
        if state["last_error"]:
            lines.append(f"آخر خطأ ({label}): {state['last_error']}")
    heartbeat = data.get("heartbeat_at")
    if isinstance(heartbeat, (int, float)):
        lines.append(f"آخر heartbeat: <t:{int(heartbeat)}:R>")
    else:
        lines.append("آخر heartbeat: غير متاح")
    return "\n".join(lines)[:1900]


async def owner_only(ctx: commands.Context) -> bool:
    return bool(ctx.author and int(ctx.author.id) == OWNER_ID)


async def submit(ctx: commands.Context, action: str, target: str) -> None:
    try:
        request = enqueue(action, target, int(ctx.author.id), source="brain_bot")
    except (ValueError, OSError) as exc:
        await ctx.reply(f"تعذر تسجيل الطلب: {exc}", mention_author=False)
        return
    await ctx.reply(
        f"تمت إضافة الطلب `{action} {target}` إلى الطابور (`{request['id']}`).",
        mention_author=False,
    )


@bot.check
async def enforce_single_owner(ctx: commands.Context) -> bool:
    return await owner_only(ctx)


@bot.command(name="start")
async def start_process(ctx: commands.Context, target: str) -> None:
    await submit(ctx, "start", target.strip().lower())


@bot.command(name="restart")
async def restart_process(ctx: commands.Context, target: str) -> None:
    await submit(ctx, "restart", target.strip().lower())


@bot.command(name="shutdown")
async def shutdown_process(ctx: commands.Context, target: str) -> None:
    await submit(ctx, "shutdown", target.strip().lower())


@bot.command(name="status")
async def status_command(ctx: commands.Context) -> None:
    await ctx.reply(format_status(), mention_author=False)


@bot.event
async def on_ready() -> None:
    user_id = int(bot.user.id) if bot.user else None
    write_connection("brain_bot", "connected", user_id=user_id)
    print(f"Brain Bot connected as {bot.user} (PID {os.getpid()})", flush=True)


@bot.event
async def on_disconnect() -> None:
    write_connection("brain_bot", "disconnected")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("هذا الأمر متاح لمالك البوت الأساسي فقط.", mention_author=False)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"الاستخدام: `{PREFIX}{ctx.command.qualified_name} <primary|second_bot>`", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("الهدف المقبول هو `primary` أو `second_bot` فقط.", mention_author=False)
    else:
        print(f"Brain Bot command error: {error!r}", flush=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
