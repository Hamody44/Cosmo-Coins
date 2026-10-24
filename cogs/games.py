import discord
from discord.ext import commands
import random
import io
import json
import asyncio
import time
import math
from database import (
    parse_amount, parse_duration, is_bot_admin, deduct_server_spending,
    refund_server_spending, update_balance, is_blacklisted, get_guild_db,
    clean_display_name, get_unicode_font, PRIMARY_OWNER_ID, get_currency_label,
    credit_giveaway_prize, giveaway_has_any_payout,
    guarded_server_budget_credit,
)
from PIL import Image, ImageDraw, ImageOps

active_roulettes = {}
_giveaway_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _giveaway_lock(guild_id: int, message_id: int) -> asyncio.Lock:
    return _giveaway_locks.setdefault((guild_id, message_id), asyncio.Lock())


def _set_giveaway_prize_state(
    guild_id: int,
    message_id: int,
    generation: int,
    slot: int,
    user_id: int,
    amount: int,
    action: str,
) -> str:
    """Claim a conditional prize or return it to the reserved guild budget once."""
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            "SELECT status, generation FROM active_giveaways WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if not row or row[0] != "ended" or int(row[1]) != generation:
            conn.rollback()
            return "stale"
        prize = cur.execute(
            "SELECT user_id, state FROM giveaway_winners "
            "WHERE message_id = ? AND generation = ? AND prize_slot = ?",
            (message_id, generation, slot),
        ).fetchone()
        if not prize or int(prize[0] or 0) != user_id:
            conn.rollback()
            return "stale"

        current_state = prize[1]
        if action == "claim":
            if current_state == "paid":
                conn.commit()
                return "already_paid"
            if current_state not in ("pending", "paying"):
                conn.rollback()
                return "stale"
            cur.execute(
                "UPDATE giveaway_winners SET state = 'paying' "
                "WHERE message_id = ? AND generation = ? AND prize_slot = ? AND user_id = ? "
                "AND state IN ('pending', 'paying')",
                (message_id, generation, slot, user_id),
            )
            conn.commit()
            return "ready"

        if action == "deny":
            if current_state == "refunded":
                conn.commit()
                return "already_refunded"
            if current_state != "pending":
                conn.rollback()
                return "stale"
            guarded_server_budget_credit(cur, guild_id, amount)
            cur.execute(
                "UPDATE giveaway_winners SET state = 'refunded' "
                "WHERE message_id = ? AND generation = ? AND prize_slot = ? AND state = 'pending'",
                (message_id, generation, slot),
            )
            conn.commit()
            return "refunded"
        raise ValueError("Unsupported giveaway prize action")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_giveaway_prize_paid(guild_id: int, message_id: int, generation: int, slot: int) -> None:
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute(
            "UPDATE giveaway_winners SET state = 'paid' "
            "WHERE message_id = ? AND generation = ? AND prize_slot = ? AND state = 'paying'",
            (message_id, generation, slot),
        )
        conn.commit()
    finally:
        conn.close()


def _change_giveaway_participant(guild_id: int, message_id: int, user_id: int, add: bool):
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            "SELECT participants_json, amount, winners_count, condition, end_timestamp "
            "FROM active_giveaways WHERE message_id = ? AND status = 'active'",
            (message_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        participants = json.loads(row[0]) if row[0] else []
        if add and user_id not in participants:
            participants.append(user_id)
        elif not add and user_id in participants:
            participants.remove(user_id)
        else:
            conn.commit()
            return None
        cur.execute(
            "UPDATE active_giveaways SET participants_json = ? WHERE message_id = ? AND status = 'active'",
            (json.dumps(participants), message_id),
        )
        conn.commit()
        return (*row[1:], participants)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def can_manage_server_games(ctx: commands.Context) -> bool:
    """مالك السيرفر يستطيع بدء ألعاب الجوائز حتى دون بريميوم؛ بقية الصلاحيات كما هي."""
    if not ctx.guild:
        return False
    return ctx.author.id == ctx.guild.owner_id or is_bot_admin(ctx)

class RouletteSession:
    def __init__(self, guild_id: int, amount: int):
        self.guild_id = guild_id
        self.amount = int(amount)
        self.is_stopped = False
        self.closed = False
        self.current_view = None

    def refund(self):
        if self.closed:
            return False
        refund_server_spending(self.guild_id, self.amount)
        self.closed = True
        return True

    def finish(self):
        if self.closed:
            return False
        self.closed = True
        return True

    def stop(self):
        if self.closed:
            return False
        self.is_stopped = True
        if self.current_view:
            self.current_view.stop()
        return self.refund()

class ClaimVerificationView(discord.ui.View):
    def __init__(
        self,
        winner: discord.Member,
        amount: int,
        reward_type: str = "الجائزة",
        giveaway_prize: tuple[int, int, int, int] | None = None,
    ):
        super().__init__(timeout=None)
        self.winner = winner
        self.amount = amount
        self.reward_type = reward_type
        self.giveaway_prize = giveaway_prize

    @discord.ui.button(label="تسليم", style=discord.ButtonStyle.success, emoji="✅")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT owner_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()

        is_admin = interaction.user.id == interaction.guild.owner_id or interaction.user.id == PRIMARY_OWNER_ID
        if row and row[0]:
            role = interaction.guild.get_role(row[0])
            if role and role in interaction.user.roles:
                is_admin = True

        if not is_admin:
            return await interaction.response.send_message("⛔ المسؤولون فقط يمكنهم اتخاذ هذا الإجراء.", ephemeral=True)

        if is_blacklisted(self.winner):
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.response.send_message(f"⛔ تعذر التسليم: العضو {self.winner.mention} مدرج في البلاك ليست.")

        if self.giveaway_prize:
            guild_id, message_id, generation, slot = self.giveaway_prize
            async with _giveaway_lock(guild_id, message_id):
                state = await asyncio.to_thread(
                    _set_giveaway_prize_state,
                    guild_id, message_id, generation, slot, self.winner.id, self.amount, "claim",
                )
                if state == "already_paid":
                    for item in self.children:
                        item.disabled = True
                    await interaction.message.edit(view=self)
                    return await interaction.response.send_message("ℹ️ سبق تسليم هذه الجائزة.", ephemeral=True)
                if state != "ready":
                    return await interaction.response.send_message("⛔ هذا الزر لم يعد صالحاً؛ تغيّر فائز الجيف أو حالته.", ephemeral=True)
                await asyncio.to_thread(
                    credit_giveaway_prize,
                    guild_id, message_id, generation, slot, self.winner.id, self.amount,
                )
                await asyncio.to_thread(_mark_giveaway_prize_paid, guild_id, message_id, generation, slot)
        else:
            update_balance(self.winner.id, self.amount)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        label = get_currency_label(interaction.guild.id)
        await interaction.response.send_message(f"✅ تم تسليم `{self.amount:,}` {label} إلى {self.winner.mention} بواسطة {interaction.user.mention}.")
        try:
            await self.winner.send(f"🎉 تم تسليمك {self.reward_type} بقيمة `{self.amount:,}` {label}.")
        except:
            pass

    @discord.ui.button(label="عدم التسليم", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT owner_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()

        is_admin = interaction.user.id == interaction.guild.owner_id or interaction.user.id == PRIMARY_OWNER_ID
        if row and row[0]:
            role = interaction.guild.get_role(row[0])
            if role and role in interaction.user.roles:
                is_admin = True

        if not is_admin:
            return await interaction.response.send_message("⛔ المسؤولون فقط يمكنهم اتخاذ هذا الإجراء.", ephemeral=True)

        if self.giveaway_prize:
            guild_id, message_id, generation, slot = self.giveaway_prize
            async with _giveaway_lock(guild_id, message_id):
                state = await asyncio.to_thread(
                    _set_giveaway_prize_state,
                    guild_id, message_id, generation, slot, self.winner.id, self.amount, "deny",
                )
            if state == "already_refunded":
                return await interaction.response.send_message("ℹ️ سبق إرجاع مبلغ هذه الجائزة.", ephemeral=True)
            if state != "refunded":
                return await interaction.response.send_message("⛔ لم يعد هذا الزر صالحاً لهذه الجائزة.", ephemeral=True)
        else:
            refund_server_spending(interaction.guild.id, self.amount)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"🛑 تم إلغاء تسليم الجائزة لـ {self.winner.mention} بواسطة {interaction.user.mention} (أُعيد المبلغ لميزانية السيرفر).")

class BoxClaimView(discord.ui.View):
    def __init__(self, amount: int, condition: str = None):
        super().__init__(timeout=None)
        self.amount = amount
        self.condition = condition
        self.claimed = False

    @discord.ui.button(label="فتح البوكس 🎁", style=discord.ButtonStyle.success)
    async def open_box(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            return await interaction.response.send_message("❌ تم فتح هذا البوكس بالفعل من قبل شخص آخر!", ephemeral=True)
        if is_blacklisted(interaction.user):
            return await interaction.response.send_message("⛔ لا يمكنك فتح البوكس؛ أنت مدرج في البلاك ليست.", ephemeral=True)

        self.claimed = True
        button.disabled = True
        button.label = "تم فتح البوكس"
        button.style = discord.ButtonStyle.secondary
        await interaction.message.edit(view=self)

        winner = interaction.user
        label = get_currency_label(interaction.guild.id)
        if not self.condition:
            update_balance(winner.id, self.amount)
            await interaction.response.send_message(f"🎉 مبروك {winner.mention}! فتحت البوكس وربحت `{self.amount:,}` {label}.")
            try:
                await winner.send(f"🎁 مبروك! حصلت على `{self.amount:,}` {label} من فتح البوكس السريع.")
            except:
                pass
        else:
            await interaction.response.send_message(f"🎉 {winner.mention} ضغط على البوكس أولاً!\n⚠️ **الشرط:** `{self.condition}`\nفي انتظار مراجعة الإدارة.")
            claim_view = ClaimVerificationView(winner, self.amount, "جائزة البوكس")
            await interaction.channel.send(f"مراجعة شرط البوكس لـ {winner.mention}:", view=claim_view)

class RouletteLobbyView(discord.ui.View):
    def __init__(self, amount: int, condition: str):
        super().__init__(timeout=None)
        self.amount = amount
        self.condition = condition
        self.participants = set()

    def make_embed(self, guild: discord.Guild):
        members_text = ""
        if self.participants:
            members_list = [guild.get_member(uid) for uid in self.participants if guild.get_member(uid)]
            members_text = "\n".join([f"• {m.mention}" for m in members_list])
        else:
            members_text = "لا يوجد مشاركون حتى الآن."

        embed = discord.Embed(
            title="🎯 لعبة الروليت | Roulette 🎯",
            description=f"**الجائزة للفائز الأخير:** `{self.amount:,}` {get_currency_label(guild.id)}\n"
                        f"**الشرط:** {self.condition if self.condition else 'لا يوجد شرط'}\n"
                        f"**الحد الأدنى للبدء:** 4 لاعبين\n"
                        f"⏳ **وقت التسجيل:** 30 ثانية!\n\n"
                        f"👥 **المشاركون ({len(self.participants)}):**\n{members_text}",
            color=discord.Color.red()
        )
        return embed

    @discord.ui.button(label="دخول / خروج الروليت 🎯", style=discord.ButtonStyle.danger)
    async def join_roulette(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_blacklisted(interaction.user):
            return await interaction.response.send_message("⛔ لا يمكنك المشاركة؛ أنت في قائمة البلاك ليست.", ephemeral=True)
            
        if interaction.user.id in self.participants:
            self.participants.remove(interaction.user.id)
            await interaction.response.send_message("❌ تم خروجك من الروليت.", ephemeral=True)
        else:
            self.participants.add(interaction.user.id)
            await interaction.response.send_message("✅ تم تسجيل دخولك في الروليت!", ephemeral=True)
        try:
            await interaction.message.edit(embed=self.make_embed(interaction.guild), view=self)
        except:
            pass

class RouletteTargetSelect(discord.ui.Select):
    def __init__(self, active_members, current_turn_member):
        options = [
            discord.SelectOption(label=clean_display_name(m.display_name)[:25], value=str(m.id))
            for m in active_members if m.id != current_turn_member.id
        ]
        super().__init__(placeholder="اختر لاعباً لطرده...", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        self.view.chosen_target_id = int(self.values[0])
        self.view.action_taken = "kick_specific"
        self.view.stop()
        await interaction.response.defer()

class RouletteTurnView(discord.ui.View):
    def __init__(self, current_player: discord.Member, active_players: list):
        super().__init__(timeout=25)
        self.current_player = current_player
        self.action_taken = None
        self.chosen_target_id = None
        self.add_item(RouletteTargetSelect(active_players, current_player))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.current_player.id:
            await interaction.response.send_message("⛔ ليس دورك الآن!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="طرد عشوائي 🎲", style=discord.ButtonStyle.danger, row=1)
    async def kick_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action_taken = "kick_random"
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="انسحاب 🏳️", style=discord.ButtonStyle.secondary, row=1)
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action_taken = "surrender"
        self.stop()
        await interaction.response.defer()

async def generate_roulette_wheel_image(guild: discord.Guild, participants: list, selected_index: int):
    size = 600
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = size // 2
    radius = 260
    num_parts = len(participants)
    angle_per_part = 360 / num_parts
    palette = [(107, 142, 100), (155, 110, 155), (150, 150, 140), (145, 115, 85), (90, 130, 140), (170, 130, 100)]
    font = get_unicode_font(18)

    for i in range(num_parts):
        start_angle = i * angle_per_part
        end_angle = (i + 1) * angle_per_part
        color = palette[i % len(palette)]
        draw.pieslice([center - radius, center - radius, center + radius, center + radius], start=start_angle, end=end_angle, fill=color, outline=(255, 255, 255), width=2)
        mid_angle = math.radians((start_angle + end_angle) / 2)
        tx = center + (radius * 0.65) * math.cos(mid_angle)
        ty = center + (radius * 0.65) * math.sin(mid_angle)
        name_str = clean_display_name(participants[i].display_name)
        draw.text((tx - 24, ty - 10), name_str, fill=(255, 255, 255), font=font)

    inner_radius = 80
    if guild.icon:
        try:
            icon_bytes = await guild.icon.read()
            icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA").resize((inner_radius * 2, inner_radius * 2))
            mask = Image.new('L', (inner_radius * 2, inner_radius * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, inner_radius * 2, inner_radius * 2), fill=255)
            output_icon = ImageOps.fit(icon_img, mask.size, centering=(0.5, 0.5))
            output_icon.putalpha(mask)
            image.paste(output_icon, (center - inner_radius, center - inner_radius), output_icon)
        except:
            draw.ellipse([center - inner_radius, center - inner_radius, center + inner_radius, center + inner_radius], fill=(35, 30, 50), outline=(255, 255, 255), width=4)
    else:
        draw.ellipse([center - inner_radius, center - inner_radius, center + inner_radius, center + inner_radius], fill=(35, 30, 50), outline=(255, 255, 255), width=4)

    target_angle = math.radians((selected_index * angle_per_part) + (angle_per_part / 2))
    ptr_x = center + (radius + 20) * math.cos(target_angle)
    ptr_y = center + (radius + 20) * math.sin(target_angle)
    draw.polygon([(ptr_x, ptr_y), (ptr_x + 25 * math.cos(target_angle + 0.4), ptr_y + 25 * math.sin(target_angle + 0.4)), (ptr_x + 25 * math.cos(target_angle - 0.4), ptr_y + 25 * math.sin(target_angle - 0.4))], fill=(255, 255, 255), outline=(0, 0, 0))

    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    return buf

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        asyncio.create_task(self.resume_all_giveaways())

    async def finish_giveaway(self, guild: discord.Guild, channel: discord.TextChannel, msg_id: int) -> bool:
        async with _giveaway_lock(guild.id, msg_id):
            return await self._finish_giveaway_locked(guild, channel, msg_id)

    async def _finish_giveaway_locked(self, guild: discord.Guild, channel: discord.TextChannel, msg_id: int) -> bool:
        label = get_currency_label(guild.id)
        read_conn, read_cur = get_guild_db(guild.id)
        read_cur.execute(
            "SELECT participants_json FROM active_giveaways WHERE message_id = ? AND status = 'active'",
            (msg_id,),
        )
        participant_row = read_cur.fetchone()
        excluded_rows = read_cur.execute(
            "SELECT DISTINCT user_id FROM giveaway_winners WHERE message_id = ? AND state = 'void' AND user_id IS NOT NULL",
            (msg_id,),
        ).fetchall()
        read_conn.close()
        if not participant_row:
            return False
        participants_json = participant_row[0]
        participants = json.loads(participants_json) if participants_json else []
        excluded_ids = {int(row[0]) for row in excluded_rows}
        valid_ids = []
        for user_id in dict.fromkeys(int(value) for value in participants):
            member = guild.get_member(user_id)
            if user_id not in excluded_ids and member and not is_blacklisted(member):
                valid_ids.append(user_id)

        g_conn, g_cur = get_guild_db(guild.id)
        try:
            g_conn.execute("BEGIN IMMEDIATE")
            row = g_cur.execute(
                "SELECT amount, winners_count, condition, participants_json, status, generation "
                "FROM active_giveaways WHERE message_id = ?",
                (msg_id,),
            ).fetchone()
            if not row or row[4] != "active" or row[3] != participants_json:
                g_conn.rollback()
                return False

            amount, winners_count, condition, _participants_json, _status, generation = row
            winners_ids = random.sample(valid_ids, min(len(valid_ids), int(winners_count)))
            winners = [guild.get_member(user_id) for user_id in winners_ids]

            g_cur.execute(
                "UPDATE active_giveaways SET is_finished = 1, status = 'ended', managed_version = 1 "
                "WHERE message_id = ? AND status = 'active'",
                (msg_id,),
            )
            if g_cur.rowcount != 1:
                g_conn.rollback()
                return False

            for slot in range(int(winners_count)):
                if slot < len(winners):
                    winner_id = int(winners_ids[slot])
                    state = "pending" if condition else "paying"
                else:
                    winner_id = None
                    state = "unfilled"
                g_cur.execute(
                    "INSERT INTO giveaway_winners (message_id, generation, prize_slot, user_id, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (msg_id, generation, slot, winner_id, state),
                )

            unfilled = int(winners_count) - len(winners)
            if unfilled:
                refund_amount = unfilled * int(amount)
                guarded_server_budget_credit(g_cur, guild.id, refund_amount)
            g_conn.commit()
        except Exception:
            g_conn.rollback()
            raise
        finally:
            g_conn.close()

        await self._update_giveaway_post(guild, channel, msg_id, "انتهى السحب")
        if not winners:
            await channel.send(
                f"⚠️ انتهى السحب على `{amount:,}` {label}، ولم يوجد مشاركون مؤهلون؛ أُعيدت الميزانية غير المصروفة للسيرفر."
            )
            return True

        winners_mentions = ", ".join(winner.mention for winner in winners)
        if condition:
            await channel.send(
                f"🎊 الفائزون بالقرعة: {winners_mentions}!\n⚠️ **الشرط:** `{condition}`\nفي انتظار مراجعة الإدارة للتسليم."
            )
            for slot, winner in enumerate(winners):
                claim_view = ClaimVerificationView(
                    winner,
                    int(amount),
                    "جائزة الجيف أواي",
                    (guild.id, msg_id, int(generation), slot),
                )
                await channel.send(f"تحقق من شرط العضو {winner.mention}:", view=claim_view)
        else:
            for slot, winner in enumerate(winners):
                try:
                    await asyncio.to_thread(
                        credit_giveaway_prize,
                        guild.id, msg_id, int(generation), slot, winner.id, int(amount),
                    )
                    await asyncio.to_thread(_mark_giveaway_prize_paid, guild.id, msg_id, int(generation), slot)
                except Exception as exc:
                    print(f"Giveaway prize payout failed for guild {guild.id}, message {msg_id}, slot {slot}: {exc}")
                    continue
                try:
                    await winner.send(f"🎉 مبروك! فزت بـ `{amount:,}` {label} في السحب وتم تسليمها تلقائياً.")
                except discord.HTTPException:
                    pass
            await channel.send(f"🎊 مبارك للفائزين: {winners_mentions}!\nتم تسليم `{amount:,}` {label} تلقائياً لكل فائز.")
        return True

    async def _update_giveaway_post(self, guild, channel, msg_id: int, status_text: str):
        try:
            message = await channel.fetch_message(msg_id)
            if not message.embeds:
                return
            embed = message.embeds[0]
            base = (embed.description or "").split("\n**الحالة:**", 1)[0]
            embed.description = f"{base}\n**الحالة:** {status_text}"
            await message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def monitor_giveaway(self, guild_id: int, channel_id: int, msg_id: int, duration_sec: int):
        await asyncio.sleep(max(0, duration_sec))
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        await self.finish_giveaway(guild, channel, msg_id)

    async def resume_all_giveaways(self):
        await self.bot.wait_until_ready()
        now = int(time.time())
        for guild in self.bot.guilds:
            try:
                g_conn, g_cur = get_guild_db(guild.id)
                g_cur.execute(
                    "SELECT message_id, channel_id, end_timestamp FROM active_giveaways WHERE status = 'active'"
                )
                active_rows = g_cur.fetchall()
                g_cur.execute(
                    "SELECT w.message_id, w.generation, w.prize_slot, w.user_id, a.channel_id, a.amount "
                    "FROM giveaway_winners w JOIN active_giveaways a ON a.message_id = w.message_id "
                    "WHERE a.status = 'ended' AND w.state = 'paying'"
                )
                pending_payouts = g_cur.fetchall()
                g_conn.close()
            except Exception as exc:
                print(f"Could not resume giveaways in guild {guild.id}: {exc}")
                continue

            for msg_id, channel_id, end_timestamp in active_rows:
                remaining = max(0, int(end_timestamp) - now)
                asyncio.create_task(self.monitor_giveaway(guild.id, channel_id, msg_id, remaining))

            for msg_id, generation, slot, user_id, channel_id, amount in pending_payouts:
                try:
                    await asyncio.to_thread(
                        credit_giveaway_prize,
                        guild.id, msg_id, generation, slot, user_id, amount,
                    )
                    await asyncio.to_thread(_mark_giveaway_prize_paid, guild.id, msg_id, generation, slot)
                except Exception as exc:
                    print(f"Could not resume giveaway payout {guild.id}/{msg_id}/{slot}: {exc}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or str(payload.emoji) != "🎉":
            return
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if member and is_blacklisted(member):
            return
        async with _giveaway_lock(payload.guild_id, payload.message_id):
            row = await asyncio.to_thread(
                _change_giveaway_participant, payload.guild_id, payload.message_id, payload.user_id, True
            )
        if row:
            amount, winners_count, condition, end_timestamp, participants = row
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.description = (
                            f"**الجائزة لكل فائز:** `{amount:,}` {get_currency_label(payload.guild_id)}\n"
                            f"**عدد الفائزين:** `{winners_count}`\n**المشاركين:** `{len(participants)}`\n"
                            f"**الشرط:** {condition if condition else 'لا يوجد شرط'}\n**ينتهي:** <t:{end_timestamp}:R>"
                        )
                        await msg.edit(embed=embed)
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or str(payload.emoji) != "🎉":
            return
        if not payload.guild_id:
            return

        async with _giveaway_lock(payload.guild_id, payload.message_id):
            row = await asyncio.to_thread(
                _change_giveaway_participant, payload.guild_id, payload.message_id, payload.user_id, False
            )
        if row:
            amount, winners_count, condition, end_timestamp, participants = row
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.description = (
                            f"**الجائزة لكل فائز:** `{amount:,}` {get_currency_label(payload.guild_id)}\n"
                            f"**عدد الفائزين:** `{winners_count}`\n**المشاركين:** `{len(participants)}`\n"
                            f"**الشرط:** {condition if condition else 'لا يوجد شرط'}\n**ينتهي:** <t:{end_timestamp}:R>"
                        )
                        await msg.edit(embed=embed)
                except discord.HTTPException:
                    pass

    @commands.hybrid_command(name="giveaway", aliases=["سحب", "جيف_اواي"], description="بدء سحب جيف أواي على عملات Cosmo بالريأكشن 🎉")
    async def giveaway(self, ctx: commands.Context, amount: str, duration: str, winners_count: int, *, condition: str = None):
        if not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0 or winners_count <= 0:
            return await ctx.send("❌ القيم غير صحيحة.")

        duration_seconds = parse_duration(duration)
        if not duration_seconds or duration_seconds <= 0:
            return await ctx.send("❌ صيغة الوقت غير صحيحة.")

        total_cost = parsed_amount * winners_count
        allowed, pool_msg = deduct_server_spending(ctx.guild.id, total_cost)
        if not allowed:
            return await ctx.send(pool_msg)

        end_timestamp = int(time.time()) + duration_seconds
        currency_label = get_currency_label(ctx.guild.id)
        embed = discord.Embed(
            title=f"🎉 سحب على {currency_label}! 🎉",
            description=f"**الجائزة لكل فائز:** `{parsed_amount:,}` {currency_label}\n**عدد الفائزين:** `{winners_count}`\n**المشاركين:** `0`\n**الشرط:** {condition if condition else 'لا يوجد شرط'}\n**ينتهي:** <t:{end_timestamp}:R>",
            color=discord.Color.purple()
        )
        embed.set_footer(text="تفاعل بالريأكشن 🎉 للدخول في السحب!")
        msg = None
        row_created = False
        try:
            msg = await ctx.send(embed=embed)
            g_conn, g_cur = get_guild_db(ctx.guild.id)
            try:
                g_cur.execute(
                    "INSERT INTO active_giveaways "
                    "(message_id, channel_id, amount, end_timestamp, winners_count, condition, participants_json, "
                    "is_finished, status, generation, managed_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, '[]', 0, 'active', 0, 1)",
                    (msg.id, ctx.channel.id, parsed_amount, end_timestamp, winners_count, condition),
                )
                g_conn.commit()
                row_created = True
            finally:
                g_conn.close()
            await msg.add_reaction("🎉")
        except Exception as exc:
            refund_completed = False
            if row_created:
                g_conn, g_cur = get_guild_db(ctx.guild.id)
                try:
                    g_conn.execute("BEGIN IMMEDIATE")
                    g_cur.execute(
                        "UPDATE active_giveaways SET status = 'stopped', is_finished = 1 "
                        "WHERE message_id = ? AND status = 'active'",
                        (msg.id,),
                    )
                    if g_cur.rowcount == 1:
                        guarded_server_budget_credit(g_cur, ctx.guild.id, total_cost)
                        refund_completed = True
                    g_conn.commit()
                except Exception:
                    g_conn.rollback()
                    print(f"Could not close failed giveaway setup {ctx.guild.id}/{msg.id}")
                finally:
                    g_conn.close()
            else:
                try:
                    refund_server_spending(ctx.guild.id, total_cost)
                    refund_completed = True
                except Exception as refund_exc:
                    print(f"Could not refund failed giveaway setup {ctx.guild.id}: {refund_exc}")
            if msg:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
            print(f"Could not start giveaway in guild {ctx.guild.id}: {exc}")
            if refund_completed:
                return await ctx.send("❌ تعذر بدء الجيف؛ أُعيدت الميزانية المحجوزة للسيرفر.")
            return await ctx.send("❌ تعذر بدء الجيف وتعذر تأكيد استرداد الميزانية؛ يرجى مراجعة سجل ميزانية السيرفر.")

        asyncio.create_task(self.monitor_giveaway(ctx.guild.id, ctx.channel.id, msg.id, duration_seconds))

    async def _giveaway_record(self, guild_id: int, message_id: int):
        conn, cur = get_guild_db(guild_id)
        try:
            cur.execute(
                "SELECT channel_id, amount, winners_count, status FROM active_giveaways WHERE message_id = ?",
                (message_id,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    async def _giveaway_channel(self, guild: discord.Guild, channel_id: int):
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel

    @commands.hybrid_command(name="stop_giveaway", description="إيقاف جيف نشط وإرجاع كامل ميزانيته للسيرفر")
    async def stop_giveaway(self, ctx: commands.Context, message_id: int):
        if not ctx.guild or not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        async with _giveaway_lock(ctx.guild.id, message_id):
            conn, cur = get_guild_db(ctx.guild.id)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT channel_id, amount, winners_count, status FROM active_giveaways WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if not row or row[3] != "active":
                    conn.rollback()
                    return await ctx.send("❌ لم أجد جيفاً نشطاً بهذا المعرّف؛ ربما انتهى أو أُوقف مسبقاً.")
                channel_id, amount, winners_count, _status = row
                refund_amount = int(amount) * int(winners_count)
                guarded_server_budget_credit(cur, ctx.guild.id, refund_amount)
                cur.execute(
                    "UPDATE active_giveaways SET is_finished = 1, status = 'stopped' "
                    "WHERE message_id = ? AND status = 'active'",
                    (message_id,),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return await ctx.send("❌ تغيّرت حالة الجيف قبل الإيقاف؛ لم تُجرَ أي عملية استرداد.")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        channel = await self._giveaway_channel(ctx.guild, channel_id)
        if channel:
            await self._update_giveaway_post(ctx.guild, channel, message_id, "أُوقف وأُعيدت الميزانية للسيرفر")
        await ctx.send(f"🛑 أُوقف الجيف وأُعيد `{refund_amount:,}` Cosmo إلى ميزانية السيرفر.")

    @commands.hybrid_command(name="end_giveaway", description="إنهاء جيف نشط الآن واختيار الفائزين")
    async def end_giveaway(self, ctx: commands.Context, message_id: int):
        if not ctx.guild or not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        row = await self._giveaway_record(ctx.guild.id, message_id)
        if not row or row[3] != "active":
            return await ctx.send("❌ لم أجد جيفاً نشطاً بهذا المعرّف؛ ربما انتهى أو أُوقف مسبقاً.")
        channel = await self._giveaway_channel(ctx.guild, row[0])
        if channel is None:
            return await ctx.send("❌ تعذر الوصول إلى روم الجيف؛ لم أغيّر حالته.")
        ended = await self.finish_giveaway(ctx.guild, channel, message_id)
        if not ended:
            return await ctx.send("❌ لم يتم إنهاء الجيف؛ ربما عالجه المؤقت للتو.")
        await ctx.send("✅ تم إنهاء الجيف واختيار الفائزين.")

    @commands.hybrid_command(name="reroll_giveaway", description="إعادة اختيار جميع الفائزين إذا لم تُصرف أي جائزة")
    async def reroll_giveaway(self, ctx: commands.Context, message_id: int):
        if not ctx.guild or not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")

        async with _giveaway_lock(ctx.guild.id, message_id):
            initial_record = await self._giveaway_record(ctx.guild.id, message_id)
            if not initial_record:
                return await ctx.send("❌ لم أجد جيفاً بهذا المعرّف.")
            channel = await self._giveaway_channel(ctx.guild, initial_record[0])
            if channel is None:
                return await ctx.send("❌ تعذر الوصول إلى روم الجيف؛ لم أغيّر حالته أو ميزانيته.")
            if await asyncio.to_thread(giveaway_has_any_payout, ctx.guild.id, message_id):
                return await ctx.send("⛔ لا يمكن إعادة السحب؛ سبق تسليم جائزة واحدة على الأقل.")

            conn, cur = get_guild_db(ctx.guild.id)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT channel_id, amount, winners_count, status, generation, managed_version "
                    "FROM active_giveaways WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if not row or row[3] != "ended":
                    conn.rollback()
                    return await ctx.send("❌ يمكن إعادة السحب فقط لجيف منتهٍ ولم يُوقف.")
                channel_id, amount, winners_count, _status, generation, managed_version = row
                if not managed_version:
                    conn.rollback()
                    return await ctx.send("⛔ هذا جيف قديم لا يملك سجل صرف موثوقاً؛ أرفض إعادة السحب لمنع دفع مكرر.")
                current_states = [r[0] for r in cur.execute(
                    "SELECT state FROM giveaway_winners WHERE message_id = ? AND generation = ?",
                    (message_id, generation),
                ).fetchall()]
                if any(state in ("paid", "paying") for state in current_states):
                    conn.rollback()
                    return await ctx.send("⛔ توجد جائزة مدفوعة أو قيد التسليم؛ لا يمكن إعادة السحب.")
                pending_count = sum(state == "pending" for state in current_states)
                total_cost = int(amount) * int(winners_count)
                recharge = max(0, total_cost - pending_count * int(amount))
                cur.execute("INSERT OR IGNORE INTO settings (guild_id, spending_balance) VALUES (?, 0)", (ctx.guild.id,))
                budget_row = cur.execute(
                    "SELECT spending_balance FROM settings WHERE guild_id = ?", (ctx.guild.id,)
                ).fetchone()
                available = int(budget_row[0] or 0) if budget_row else 0
                if available < recharge:
                    conn.rollback()
                    return await ctx.send(
                        f"⛔ الميزانية غير كافية لإعادة حجز الجوائز التي أُعيدت سابقاً. "
                        f"المطلوب `{recharge:,}` والمتاح `{available:,}` Cosmo."
                    )
                cur.execute(
                    "UPDATE settings SET spending_balance = COALESCE(spending_balance, 0) - ? WHERE guild_id = ?",
                    (recharge, ctx.guild.id),
                )
                cur.execute(
                    "UPDATE giveaway_winners SET state = 'void' WHERE message_id = ? AND generation = ?",
                    (message_id, generation),
                )
                cur.execute(
                    "UPDATE active_giveaways SET status = 'active', is_finished = 0, "
                    "generation = generation + 1, end_timestamp = ?, managed_version = 1 "
                    "WHERE message_id = ? AND status = 'ended'",
                    (int(time.time()), message_id),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return await ctx.send("❌ تغيّرت حالة الجيف؛ لم تُنفّذ إعادة السحب.")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

            rerolled = await self._finish_giveaway_locked(ctx.guild, channel, message_id)
        if rerolled:
            await ctx.send("🔄 أُعيد اختيار جميع الفائزين؛ لم تكن أي جائزة قد صُرفت.")
        else:
            await ctx.send("⚠️ لم تكتمل إعادة السحب؛ بقيت الحالة محفوظة لإعادة المحاولة.")

    @commands.hybrid_command(name="box", aliases=["بوكس"], description="إنشاء بوكس سريع لأول شخص يضغط على الزر")
    async def box(self, ctx: commands.Context, amount: str, *, condition: str = None):
        if not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")

        allowed, pool_msg = deduct_server_spending(ctx.guild.id, parsed_amount)
        if not allowed:
            return await ctx.send(pool_msg)

        currency_label = get_currency_label(ctx.guild.id)
        embed = discord.Embed(
            title=f"🎁 بوكس {currency_label} السريع! 🎁",
            description=f"**الجائزة:** `{parsed_amount:,}` {currency_label}\n**الشرط:** {condition if condition else 'لا يوجد شرط (تسليم فوري)'}\n**القاعدة:** أول من يضغط على الزر يفوز بالجائزة فوراً!",
            color=discord.Color.gold()
        )
        embed.set_footer(text="كن الأسرع واضغط على الزر أدناه!")
        view = BoxClaimView(parsed_amount, condition)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="roulette", aliases=["روليت"], description="بدء لعبة الروليت التنافسية مع العجلة الرسومية")
    async def roulette(self, ctx: commands.Context, amount: str, *, condition: str = None):
        if not can_manage_server_games(ctx):
            return await ctx.send("⛔ هذه اللعبة متاحة لمالك السيرفر أو لمسؤول البوت المخوّل.")
        if ctx.guild.id in active_roulettes:
            return await ctx.send("⚠️ هناك لعبة روليت جارية بالفعل!")

        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")

        allowed, pool_msg = deduct_server_spending(ctx.guild.id, parsed_amount)
        if not allowed:
            return await ctx.send(pool_msg)

        session = RouletteSession(ctx.guild.id, parsed_amount)
        active_roulettes[ctx.guild.id] = session

        lobby_view = RouletteLobbyView(parsed_amount, condition)
        session.current_view = lobby_view
        await ctx.send(embed=lobby_view.make_embed(ctx.guild), view=lobby_view)

        for _ in range(30):
            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                session.refund()
                return
            await asyncio.sleep(1)

        participants = [ctx.guild.get_member(uid) for uid in lobby_view.participants if ctx.guild.get_member(uid) and not is_blacklisted(ctx.guild.get_member(uid))]
        if len(participants) < 4:
            active_roulettes.pop(ctx.guild.id, None)
            session.refund()
            return await ctx.channel.send("⚠️ تم إلغاء الروليت لعدم اكتمال 4 لاعبين على الأقل (أُعيدت الجائزة لميزانية السيرفر).")

        round_num = 1
        while len(participants) > 2:
            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                session.refund()
                return

            chosen_index = random.randint(0, len(participants) - 1)
            turn_player = participants[chosen_index]

            wheel_buf = await generate_roulette_wheel_image(ctx.guild, participants, chosen_index)
            wheel_file = discord.File(wheel_buf, filename="wheel.png")
            await ctx.channel.send(f"🎡 **الجولة {round_num}: تم تدوير العجلة ووقفت على {turn_player.mention}!**", file=wheel_file)

            turn_view = RouletteTurnView(turn_player, participants)
            session.current_view = turn_view
            await ctx.channel.send(f"🎲 {turn_player.mention} معك **25 ثانية** لاختيار قرارك:", view=turn_view)
            await turn_view.wait()

            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                session.refund()
                return

            eliminated = None
            if turn_view.action_taken == "kick_random":
                others = [p for p in participants if p.id != turn_player.id]
                eliminated = random.choice(others)
                await ctx.channel.send(f"🎲 طرد عشوائي: تم طرد {eliminated.mention}!")
            elif turn_view.action_taken == "kick_specific" and turn_view.chosen_target_id:
                eliminated = ctx.guild.get_member(turn_view.chosen_target_id)
                await ctx.channel.send(f"🎯 طرد محدد: قرر {turn_player.mention} طرد {eliminated.mention}!")
            else:
                eliminated = turn_player
                await ctx.channel.send(f"⏱️ تم استبعاد {turn_player.mention} لانتهاء الوقت أو الانسحاب.")

            if eliminated and eliminated in participants:
                participants.remove(eliminated)

            round_num += 1
            await asyncio.sleep(2)

        if session.is_stopped:
            active_roulettes.pop(ctx.guild.id, None)
            session.refund()
            return

        await ctx.channel.send(f"🚨 **المواجهة النهائية الحاسمة بين:** {participants[0].mention} و {participants[1].mention}!\nالعجلة تدور الآن لتحديد الفائز...")
        await asyncio.sleep(2)

        if session.is_stopped:
            active_roulettes.pop(ctx.guild.id, None)
            session.refund()
            return
        if not session.finish():
            active_roulettes.pop(ctx.guild.id, None)
            return
        active_roulettes.pop(ctx.guild.id, None)

        winner_index = random.randint(0, 1)
        final_winner = participants[winner_index]

        final_wheel_buf = await generate_roulette_wheel_image(ctx.guild, participants, winner_index)
        final_wheel_file = discord.File(final_wheel_buf, filename="final_wheel.png")
        await ctx.channel.send(f"🎡 **العجلة وقفت على الفائز النهائي:** {final_winner.mention}!", file=final_wheel_file)

        if not condition:
            update_balance(final_winner.id, parsed_amount)
            currency_label = get_currency_label(ctx.guild.id)
            await ctx.channel.send(f"👑 **مبروك للفائز بالروليت:** {final_winner.mention}!\nتم تسليم `{parsed_amount:,}` {currency_label} إلى رصيدك تلقائياً.")
            try:
                await final_winner.send(f"🎉 مبروك! فزت في لعبة الروليت وحصلت على `{parsed_amount:,}` {currency_label}.")
            except:
                pass
        else:
            await ctx.channel.send(
                f"👑 **الفائز النهائي:** {final_winner.mention}!\n"
                f"⚠️ **الشرط المطلوب:** `{condition}`\nفي انتظار مراجعة الإدارة للتسليم."
            )
            claim_view = ClaimVerificationView(final_winner, parsed_amount, "جائزة الروليت")
            await ctx.channel.send(f"تحقق من شرط فائز الروليت {final_winner.mention}:", view=claim_view)

    @commands.hybrid_command(name="stop_roulette", aliases=["ايقاف_الروليت"], description="إيقاف لعبة الروليت الجارية فوراً")
    async def stop_roulette(self, ctx: commands.Context):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية (أو انتهى اشتراك البريميوم بالسيرفر).")
        session = active_roulettes.get(ctx.guild.id)
        if not session:
            return await ctx.send("❌ لا توجد لعبة نشطة.")
        if not session.stop():
            active_roulettes.pop(ctx.guild.id, None)
            return await ctx.send("❌ انتهت اللعبة بالفعل.")
        active_roulettes.pop(ctx.guild.id, None)
        await ctx.send("🛑 تم إيقاف لعبة الروليت بدون فائز (أُعيدت الميزانية للسيرفر).")

async def setup(bot):
    await bot.add_cog(Games(bot))
