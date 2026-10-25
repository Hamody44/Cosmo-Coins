from __future__ import annotations

import os
import asyncio
from urllib.parse import urlencode

import discord
from discord.ext import commands, tasks

from database import (
    deduct_server_spending,
    get_currency_label,
    parse_amount,
    refund_server_spending,
    credit_eightball_prize,
)
from cogs.games import can_manage_server_games
from dashboard.realtime import tournament_manager
from dashboard.tournament import TournamentError


class EightBallLobbyView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        base_url = os.getenv("DASHBOARD_URL", "https://cosmo-coins.site").rstrip("/")
        query = urlencode({"game": "8ball", "tournament": tournament_id})
        self.add_item(discord.ui.Button(
            label="فتح بطولة 8Ball",
            style=discord.ButtonStyle.link,
            url=f"{base_url}/game?{query}",
            emoji="🎱",
        ))


class EightBall(commands.Cog):
    """Discord entry point for the WebSocket-backed 8Ball tournament."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.settlement_worker.start()

    def cog_unload(self):
        self.settlement_worker.cancel()

    @tasks.loop(seconds=2)
    async def settlement_worker(self):
        for completion in tournament_manager.pending_completions():
            if completion["settlement_status"] == "review_notified":
                continue
            channel = self.bot.get_channel(completion["channel_id"])
            if completion["condition"]:
                if channel:
                    await channel.send(
                        f"🎱 انتهت بطولة 8Ball. الفائز هو <@{completion['winner_id']}>. "
                        f"⚠️ الشرط: `{completion['condition']}`\n"
                        f"يمكن للمسؤول استخدام `/8ball_claim {completion['tournament_id']}` بعد التحقق."
                    )
                tournament_manager.mark_review_notified(completion["tournament_id"])
                continue
            try:
                result = await asyncio.to_thread(
                    credit_eightball_prize,
                    completion["guild_id"], completion["tournament_id"],
                    completion["winner_id"], completion["prize_amount"],
                )
                tournament_manager.mark_settled(completion["tournament_id"])
                if channel and result["status"] in ("paid", "already_paid"):
                    await channel.send(
                        f"🏆 مبروك <@{completion['winner_id']}>! تم تسليم جائزة 8Ball "
                        f"`{completion['prize_amount']:,}` {get_currency_label(completion['guild_id'])}."
                    )
            except Exception as exc:
                print(f"8Ball payout failed for {completion['tournament_id']}: {exc}")

    @settlement_worker.before_loop
    async def before_settlement_worker(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(
        name="8ball",
        description="بدء بطولة 8Ball بنظام خروج المغلوب",
    )
    async def eightball(self, ctx: commands.Context, amount: str, *, condition: str = None):
        if not ctx.guild:
            return await ctx.send("⛔ هذا الأمر متاح داخل السيرفرات فقط.")
        if not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            prize = parse_amount(amount)
        except Exception:
            return await ctx.send("❌ صيغة مبلغ الجائزة غير صحيحة.")
        if prize <= 0:
            return await ctx.send("❌ يجب أن تكون الجائزة أكبر من صفر.")

        allowed, message = deduct_server_spending(ctx.guild.id, prize)
        if not allowed:
            return await ctx.send(message)
        try:
            state = tournament_manager.create(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                created_by=ctx.author.id,
                prize_amount=prize,
                condition=condition,
                registration_seconds=30,
            )
        except (TournamentError, ValueError) as exc:
            refund_server_spending(ctx.guild.id, prize)
            return await ctx.send(f"❌ تعذر إنشاء البطولة: {exc}")

        currency = get_currency_label(ctx.guild.id)
        deadline = state["registration_deadline"]
        embed = discord.Embed(
            title="🎱 بطولة 8Ball بدأت!",
            description=(
                f"التسجيل مفتوح لمدة **30 ثانية**.\n"
                f"الجائزة: `{prize:,}` {currency}\n"
                f"الشرط: {condition if condition else 'لا يوجد شرط'}\n"
                f"ينتهي التسجيل: <t:{deadline}:R>\n\n"
                "بعد انتهاء التسجيل سيتم توزيع اللاعبين عشوائيًا على مباريات 1v1 بنظام خروج المغلوب."
            ),
            color=discord.Color.dark_green(),
        )
        embed.set_footer(text=f"Tournament ID: {state['tournament_id']}")
        await ctx.send(embed=embed, view=EightBallLobbyView(state["tournament_id"]))

    @commands.hybrid_command(name="8ball_claim", description="اعتماد جائزة بطولة 8Ball بعد التحقق من الشرط")
    async def eightball_claim(self, ctx: commands.Context, tournament_id: str):
        if not ctx.guild or not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        completion = tournament_manager.completion(tournament_id)
        if not completion or completion["guild_id"] != ctx.guild.id:
            return await ctx.send("❌ لم أجد بطولة مكتملة بهذا الرقم في هذا السيرفر.")
        if not completion["condition"]:
            return await ctx.send("ℹ️ هذه البطولة لا تحتوي شرطًا؛ تتم تسويتها تلقائيًا.")
        try:
            result = await asyncio.to_thread(
                credit_eightball_prize,
                completion["guild_id"], completion["tournament_id"],
                completion["winner_id"], completion["prize_amount"],
            )
            tournament_manager.mark_settled(tournament_id)
        except Exception as exc:
            return await ctx.send(f"❌ تعذر تسليم الجائزة: {exc}")
        return await ctx.send(
            f"✅ تم اعتماد الشرط وتسليم `{completion['prize_amount']:,}` "
            f"{get_currency_label(ctx.guild.id)} إلى <@{completion['winner_id']}> ({result['status']})."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EightBall(bot))
