from __future__ import annotations

import asyncio
import os
from urllib.parse import urlencode

import discord
from discord.ext import commands, tasks

from database import (
    credit_eightball_prize,
    deduct_server_spending,
    get_currency_label,
    get_eightball_guess_reward,
    parse_amount,
    payout_from_server_budget,
    refund_server_spending,
    set_eightball_guess_reward,
)
from cogs.games import can_manage_server_games
from dashboard.game_auth import issue_game_token
from dashboard.realtime import tournament_manager
from dashboard.tournament import TournamentError


def _game_url(tournament_id: str, *, match_id: str | None = None, ticket: str | None = None, spectator: bool = False) -> str:
    base_url = os.getenv("DASHBOARD_URL", "https://cosmo-coins.site").rstrip("/")
    query = {"game": "8ball", "tournament": tournament_id}
    if match_id:
        query["match"] = match_id
    if ticket:
        query["ticket"] = ticket
    if spectator:
        query["spectator"] = "1"
    return f"{base_url}/game?{urlencode(query)}"


def _lobby_embed(state: dict, prize: int, currency: str, condition: str | None, guess_reward: int = 0) -> discord.Embed:
    embed = discord.Embed(
        title="🎱 بطولة 8Ball — التسجيل مفتوح",
        description=(
            "اضغط زر **دخول البطولة** مثل نظام الروليت.\n"
            f"الجائزة: `{prize:,}` {currency}\n"
            f"الشرط: {condition or 'لا يوجد شرط'}\n"
            f"مكافأة التخمين الصحيح: `{guess_reward:,}` Cosmo (مجاني، ولا توجد خسارة عند الخطأ)\n"
            f"ينتهي التسجيل: <t:{state['registration_deadline']}:R>\n\n"
            "بعد انتهاء التسجيل سيتم توزيع اللاعبين وإرسال زر مباراة مستقل لكل مواجهة."
        ),
        color=discord.Color.dark_green(),
    )
    embed.add_field(name="المشاركون", value=f"`{state['player_count']} / {state['max_players']}`", inline=True)
    embed.add_field(name="الحالة", value="تسجيل اللاعبين", inline=True)
    embed.set_footer(text=f"Tournament ID: {state['tournament_id']}")
    return embed


class LaunchLinkView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=120)
        self.add_item(discord.ui.Button(label="فتح تطبيق مباراة 8Ball", style=discord.ButtonStyle.link, url=url, emoji="🎱"))


class EightBallLobbyJoinButton(discord.ui.Button):
    def __init__(self, tournament_id: str):
        super().__init__(label="دخول البطولة", style=discord.ButtonStyle.success, custom_id=f"8ball:join:{tournament_id}", emoji="🎱")
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction):
        try:
            state = tournament_manager.join(
                self.tournament_id,
                interaction.user.id,
                getattr(interaction.user, "global_name", None) or interaction.user.display_name,
            )
            view = self.view
            prize = getattr(view, "prize", state["prize_amount"]) if view else state["prize_amount"]
            condition = getattr(view, "condition", None) if view else None
            guess_reward = getattr(view, "guess_reward", 0) if view else 0
            currency = get_currency_label(state["guild_id"])
            await interaction.response.edit_message(embed=_lobby_embed(state, prize, currency, condition, guess_reward), view=view)
            await interaction.followup.send("✅ تم تسجيلك في بطولة 8Ball.", ephemeral=True)
            print(f"[8Ball] player joined tournament={self.tournament_id} user_id={interaction.user.id} count={state['player_count']}")
        except (TournamentError, ValueError) as exc:
            await interaction.response.send_message(f"❌ تعذر دخول البطولة: {exc}", ephemeral=True)


class EightBallLobbyView(discord.ui.View):
    def __init__(self, tournament_id: str, prize: int, condition: str | None, guess_reward: int = 0):
        super().__init__(timeout=None)
        self.prize = prize
        self.condition = condition
        self.guess_reward = guess_reward
        self.add_item(EightBallLobbyJoinButton(tournament_id))


class EightBallMatchInviteButton(discord.ui.Button):
    def __init__(self, tournament_id: str, match_id: str):
        super().__init__(label="دخول مباراة 8Ball", style=discord.ButtonStyle.primary, custom_id=f"8ball:match:{match_id}", emoji="🎱")
        self.tournament_id = tournament_id
        self.match_id = match_id

    async def callback(self, interaction: discord.Interaction):
        tournament = tournament_manager.get(self.tournament_id)
        match = tournament.matches.get(self.match_id) if tournament else None
        if not match or interaction.user.id not in match.player_ids:
            return await interaction.response.send_message("⛔ هذا الزر مخصص للاعبَي هذه المباراة فقط.", ephemeral=True)
        try:
            ticket = issue_game_token({"id": interaction.user.id, "global_name": getattr(interaction.user, "global_name", None) or interaction.user.display_name}, ttl_seconds=120)
        except RuntimeError:
            return await interaction.response.send_message("❌ إعداد GAME_AUTH_SECRET مفقود في الاستضافة.", ephemeral=True)
        await interaction.response.send_message(
            "اضغط الزر التالي لفتح التطبيق. سيتم احتساب دخولك فقط بعد اتصال التطبيق خلال 10 ثوانٍ.",
            view=LaunchLinkView(_game_url(self.tournament_id, match_id=self.match_id, ticket=ticket)),
            ephemeral=True,
        )


class EightBallMatchInviteView(discord.ui.View):
    def __init__(self, tournament_id: str, match_id: str):
        super().__init__(timeout=10)
        self.add_item(EightBallMatchInviteButton(tournament_id, match_id))


class EightBallSpectatorSelect(discord.ui.Select):
    def __init__(self, tournament_id: str, matches: list[dict]):
        options = [discord.SelectOption(
            label=f"{item['players'][0]['display_name']} ضد {item['players'][1]['display_name']}"[:100],
            value=item["match_id"],
            description=f"الجولة {item['round']} — مشاهدة مباشرة",
            emoji="👁️",
        ) for item in matches[:25] if len(item.get("players", [])) == 2]
        super().__init__(placeholder="اختر مباراة لمشاهدتها", min_values=1, max_values=1, options=options, custom_id=f"8ball:spectate:{tournament_id}")
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "اختر الزر لفتح المباراة كمشاهد. المشاهدة متاحة حتى لو خسرت أو لم تلحق التسجيل.",
            view=LaunchLinkView(_game_url(self.tournament_id, match_id=self.values[0], spectator=True)),
            ephemeral=True,
        )


class EightBallSpectatorView(discord.ui.View):
    def __init__(self, tournament_id: str, matches: list[dict]):
        super().__init__(timeout=60)
        self.add_item(EightBallSpectatorSelect(tournament_id, matches))


class EightBallGuessSelect(discord.ui.Select):
    def __init__(self, tournament_id: str, match: dict, reward: int):
        options = [discord.SelectOption(label=player["display_name"][:100], value=str(player["user_id"]), emoji="🎱") for player in match["players"]]
        super().__init__(placeholder="من تتوقع أن يفوز؟", min_values=1, max_values=1, options=options, custom_id=f"8ball:guess:{match['match_id']}")
        self.tournament_id = tournament_id
        self.match_id = match["match_id"]
        self.reward = reward

    async def callback(self, interaction: discord.Interaction):
        try:
            tournament_manager.submit_guess(self.tournament_id, self.match_id, interaction.user.id, int(self.values[0]))
            await interaction.response.send_message(
                f"✅ تم حفظ توقعك. إذا كان صحيحًا ستحصل على `{self.reward:,}` Cosmo. لا يوجد أي خصم أو خسارة عند الخطأ.",
                ephemeral=True,
            )
        except (TournamentError, ValueError) as exc:
            await interaction.response.send_message(f"❌ انتهى الاختيار أو تعذر حفظ التوقع: {exc}", ephemeral=True)


class EightBallGuessView(discord.ui.View):
    def __init__(self, tournament_id: str, match: dict, reward: int):
        super().__init__(timeout=10)
        self.add_item(EightBallGuessSelect(tournament_id, match, reward))


class EightBall(commands.Cog):
    """8Ball Discord lobby, match launch handshake and prize settlement."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.settlement_worker.start()

    def cog_unload(self):
        self.settlement_worker.cancel()

    @tasks.loop(seconds=1)
    async def settlement_worker(self):
        for launch in tournament_manager.pending_match_launches():
            channel = self.bot.get_channel(launch["channel_id"])
            if not channel:
                continue
            match = launch["match"]
            names = " و ".join(player["display_name"] for player in match["players"])
            await channel.send(
                f"🎱 **مباراة الجولة {match['round']} بدأت مرحلة الدخول:** {names}\n"
                "لديكما **10 ثوانٍ** لفتح التطبيق. لن تبدأ المباراة حتى يدخل اللاعبان.",
                view=EightBallMatchInviteView(launch["tournament_id"], match["match_id"]),
            )
            tournament_manager.mark_match_launch_sent(launch["tournament_id"], match["match_id"])
            print(f"[8Ball] match invite sent tournament={launch['tournament_id']} match={match['match_id']} players={match['players']}")

        for lobby in tournament_manager.pending_spectator_lobbies():
            channel = self.bot.get_channel(lobby["channel_id"])
            if not channel:
                continue
            matches = [item for item in lobby["matches"] if len(item.get("players", [])) == 2]
            for start in range(0, len(matches), 25):
                await channel.send(
                    f"👁️ **مشاهدة مباريات الجولة {lobby['round']}**\nاختر المباراة التي تريد مشاهدتها. لا تحتاج إلى أن تكون لاعبًا.",
                    view=EightBallSpectatorView(lobby["tournament_id"], matches[start:start + 25]),
                )
            tournament_manager.mark_spectator_lobby_sent(lobby["tournament_id"], lobby["round"])

        for prompt in tournament_manager.pending_guess_prompts():
            channel = self.bot.get_channel(prompt["channel_id"])
            if not channel:
                continue
            match = prompt["match"]
            await channel.send(
                f"🔮 **خمن واكسب — 10 ثوانٍ فقط!**\n"
                f"من سيفوز: **{match['players'][0]['display_name']}** أم **{match['players'][1]['display_name']}**؟\n"
                f"التوقع مجاني؛ الإجابة الصحيحة تكسب `{prompt['guess_reward']:,}` Cosmo، والخاطئة لا تخصم شيئًا.",
                view=EightBallGuessView(prompt["tournament_id"], match, prompt["guess_reward"]),
            )
            tournament_manager.mark_guess_prompt_sent(prompt["tournament_id"], match["match_id"])

        for reward in tournament_manager.pending_guess_rewards():
            if reward["amount"] <= 0:
                tournament_manager.mark_guess_reward_settled(reward["tournament_id"], reward["match_id"], reward["user_id"])
                continue
            try:
                result = await asyncio.to_thread(
                    payout_from_server_budget,
                    reward["guild_id"], reward["user_id"], reward["amount"],
                    f"8ball-guess:{reward['tournament_id']}:{reward['match_id']}:{reward['user_id']}",
                    "8ball_guess",
                )
                if result.get("status") in ("paid", "already_paid"):
                    tournament_manager.mark_guess_reward_settled(reward["tournament_id"], reward["match_id"], reward["user_id"])
                    # channel_id هو روم إنشاء جلسة/بطولة 8Ball، وليس روم المشاهد أو رومًا عشوائيًا.
                    channel = self.bot.get_channel(reward["channel_id"])
                    if channel:
                        player_names = reward["player_names"]
                        matchup = " ضد ".join(f"**{name}**" for name in player_names)
                        await channel.send(
                            f"🔮 مبروك <@{reward['user_id']}>! قام بالتخمين في مباراة {matchup}، "
                            f"واختار فوز **{reward['guessed_winner_name']}**، وكانت تخميناته صحيحة. "
                            f"كسب `{reward['amount']:,}` {get_currency_label(reward['guild_id'])}."
                        )
                elif result.get("status") == "insufficient":
                    print(f"[8Ball] guess reward waiting for budget tournament={reward['tournament_id']} user_id={reward['user_id']}")
            except Exception as exc:
                print(f"8Ball guess payout failed for {reward['tournament_id']}/{reward['match_id']}: {exc}")

        for refund in tournament_manager.pending_refunds():
            try:
                refund_server_spending(refund["guild_id"], refund["prize_amount"])
                tournament_manager.mark_refunded(refund["tournament_id"])
                channel = self.bot.get_channel(refund["channel_id"])
                if channel:
                    await channel.send("⚠️ لم يدخل لاعبا المباراة خلال 10 ثوانٍ؛ أُلغيت البطولة وأُعيدت الجائزة إلى ميزانية السيرفر.")
                print(f"[8Ball] tournament refunded tournament={refund['tournament_id']} reason={refund['reason']}")
            except Exception as exc:
                print(f"8Ball refund failed for {refund['tournament_id']}: {exc}")

        for completion in tournament_manager.pending_completions():
            if completion["settlement_status"] == "review_notified":
                continue
            channel = self.bot.get_channel(completion["channel_id"])
            if completion["condition"]:
                if channel:
                    await channel.send(
                        f"🎱 انتهت بطولة 8Ball. الفائز هو <@{completion['winner_id']}>. "
                        f"⚠️ الشرط: `{completion['condition']}`\n"
                        f"استخدم `/8ball_claim {completion['tournament_id']}` بعد التحقق."
                    )
                tournament_manager.mark_review_notified(completion["tournament_id"])
                continue
            try:
                result = await asyncio.to_thread(
                    credit_eightball_prize,
                    completion["guild_id"], completion["tournament_id"], completion["winner_id"], completion["prize_amount"],
                )
                tournament_manager.mark_settled(completion["tournament_id"])
                if channel and result["status"] in ("paid", "already_paid"):
                    await channel.send(f"🏆 مبروك <@{completion['winner_id']}>! تم تسليم `{completion['prize_amount']:,}` {get_currency_label(completion['guild_id'])}.")
            except Exception as exc:
                print(f"8Ball payout failed for {completion['tournament_id']}: {exc}")

    @settlement_worker.before_loop
    async def before_settlement_worker(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="8ball", aliases=["eightball", "8_ball", "8-ball"], description="بدء بطولة 8Ball بنظام لوبي الروليت")
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
            guess_reward = get_eightball_guess_reward(ctx.guild.id)
            state = tournament_manager.create(guild_id=ctx.guild.id, channel_id=ctx.channel.id, created_by=ctx.author.id, prize_amount=prize, condition=condition, registration_seconds=30, guess_reward=guess_reward)
            print(f"[8Ball] lobby created tournament={state['tournament_id']} guild_id={ctx.guild.id} prize={prize}")
        except (TournamentError, ValueError) as exc:
            refund_server_spending(ctx.guild.id, prize)
            return await ctx.send(f"❌ تعذر إنشاء البطولة: {exc}")
        currency = get_currency_label(ctx.guild.id)
        await ctx.send(embed=_lobby_embed(state, prize, currency, condition, guess_reward), view=EightBallLobbyView(state["tournament_id"], prize, condition, guess_reward))

    @commands.hybrid_command(name="8ball_guess_reward", description="تحديد مكافأة التخمين الصحيح في 8Ball")
    async def eightball_guess_reward(self, ctx: commands.Context, amount: str):
        if not ctx.guild or not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            reward = parse_amount(amount)
            if reward < 0:
                raise ValueError
            set_eightball_guess_reward(ctx.guild.id, reward, ctx.author.id)
        except Exception:
            return await ctx.send("❌ أدخل مبلغًا صحيحًا أكبر من أو يساوي صفر. استخدم `0` لتعطيل المكافأة.")
        return await ctx.send(f"✅ تم ضبط مكافأة التخمين الصحيح على `{reward:,}` {get_currency_label(ctx.guild.id)}. لا يتم خصم شيء من صاحب التخمين.")

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
            result = await asyncio.to_thread(credit_eightball_prize, completion["guild_id"], completion["tournament_id"], completion["winner_id"], completion["prize_amount"])
            tournament_manager.mark_settled(tournament_id)
        except Exception as exc:
            return await ctx.send(f"❌ تعذر تسليم الجائزة: {exc}")
        return await ctx.send(f"✅ تم اعتماد الشرط وتسليم `{completion['prize_amount']:,}` {get_currency_label(ctx.guild.id)} إلى <@{completion['winner_id']}> ({result['status']}).")


async def setup(bot: commands.Bot):
    await bot.add_cog(EightBall(bot))
