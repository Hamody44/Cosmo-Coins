"""Reaction-based photo contests and durable Discord invite attribution.

No prize money is moved by this cog: prizes are announcement-only in v1.
"""
import asyncio
import json
import time
from collections import Counter
from typing import Optional

import discord
from discord.ext import commands, tasks

from database import get_guild_db

VOTE_EMOJI = "👍"
MIN_VOTER_ACCOUNT_AGE_DAYS = 90


def account_age_days(user, now=None):
    created = getattr(user, "created_at", None)
    if created is None:
        return None
    timestamp = created.timestamp()
    return max(0, int(((now or time.time()) - timestamp) // 86400))


def suspicion_score(user) -> int:
    """Small explainable signal only; short usernames are never a signal alone."""
    score = 0
    if getattr(user, "bot", False):
        score += 2
    if getattr(user, "public_flags", None) and getattr(user.public_flags, "value", 0):
        score += 0
    created = getattr(user, "created_at", None)
    if created and time.time() - created.timestamp() < 7 * 86400:
        score += 2
    return min(score, 10)


def rank_entries(rows):
    """Return entries ordered by non-excluded votes, then entry number."""
    return sorted(rows, key=lambda row: (-int(row["votes"]), int(row["entry_no"])))


def record_vote(guild_id, contest_id, entry_no, voter, *, now=None):
    """Atomically record one voter; returns (ok, reason). Used by event handler/tests."""
    now = int(now or time.time())
    conn, cur = get_guild_db(int(guild_id))
    try:
        cur.execute("SELECT author_id FROM contest_entries WHERE contest_id=? AND entry_no=?", (contest_id, entry_no))
        entry = cur.fetchone()
        if not entry:
            return False, "entry_missing"
        if int(entry[0]) == int(voter.id):
            return False, "self_vote"
        age_days = account_age_days(voter, now)
        avatar = getattr(getattr(voter, "display_avatar", None), "url", None)
        banner = getattr(getattr(voter, "banner", None), "url", None)
        flags = getattr(getattr(voter, "public_flags", None), "value", 0)
        if age_days is not None and age_days < MIN_VOTER_ACCOUNT_AGE_DAYS:
            cur.execute(
                "INSERT INTO contest_voter_reviews(contest_id,voter_id,username,avatar_url,banner_url,public_flags,suspicion_score,excluded,created_at,account_created_at,account_age_days,classification) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'fake') ON CONFLICT(contest_id,voter_id) DO UPDATE SET username=excluded.username,avatar_url=excluded.avatar_url,banner_url=excluded.banner_url,public_flags=excluded.public_flags,suspicion_score=excluded.suspicion_score,excluded=1,account_created_at=excluded.account_created_at,account_age_days=excluded.account_age_days,classification='fake'",
                (contest_id, voter.id, str(getattr(voter, "name", voter.id)), str(avatar) if avatar else None,
                 str(banner) if banner else None, int(flags or 0), 10, 1, now,
                 int(getattr(getattr(voter, "created_at", None), "timestamp", lambda: 0)()), age_days),
            )
            conn.commit()
            return False, "account_too_new"
        cur.execute("SELECT entry_no FROM contest_votes WHERE contest_id=? AND voter_id=?", (contest_id, voter.id))
        existing = cur.fetchone()
        # One vote per person. A new reaction deliberately replaces the old one.
        cur.execute("INSERT INTO contest_votes(contest_id,entry_no,voter_id,voted_at,excluded) VALUES(?,?,?,?,0) "
                    "ON CONFLICT(contest_id,voter_id) DO UPDATE SET entry_no=excluded.entry_no,voted_at=excluded.voted_at",
                    (contest_id, entry_no, voter.id, now))
        cur.execute("INSERT INTO contest_voter_reviews(contest_id,voter_id,username,avatar_url,banner_url,public_flags,suspicion_score,created_at,account_created_at,account_age_days,classification) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?, 'eligible') ON CONFLICT(contest_id,voter_id) DO UPDATE SET username=excluded.username,avatar_url=excluded.avatar_url,banner_url=excluded.banner_url,public_flags=excluded.public_flags,account_created_at=excluded.account_created_at,account_age_days=excluded.account_age_days,classification=CASE WHEN contest_voter_reviews.classification='left' THEN 'left' ELSE excluded.classification END",
                    (contest_id, voter.id, str(getattr(voter, "name", voter.id)), str(avatar) if avatar else None,
                     str(banner) if banner else None, int(flags or 0), suspicion_score(voter), now,
                     int(getattr(getattr(voter, "created_at", None), "timestamp", lambda: 0)()), age_days))
        conn.commit()
        return True, "changed" if existing and int(existing[0]) != int(entry_no) else "recorded"
    finally:
        conn.close()


def _channel_mention(guild, channel_id):
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel.mention if channel else "غير محدد"


class PhotoSetupConfigModal(discord.ui.Modal, title="إعدادات مسابقة الصور"):
    conditions = discord.ui.TextInput(
        label="شروط المشاركة",
        placeholder="اكتب الشروط أو اتركها فارغة",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    first_prize = discord.ui.TextInput(label="جائزة المركز الأول", required=False, max_length=200)
    second_prize = discord.ui.TextInput(label="جائزة المركز الثاني", required=False, max_length=200)
    third_prize = discord.ui.TextInput(label="جائزة المركز الثالث", required=False, max_length=200)
    interval = discord.ui.TextInput(label="الفاصل بين تحديثات النتائج بالثواني", required=False, max_length=6, default="300")

    def __init__(self, view):
        super().__init__()
        self.setup_view = view
        self.conditions.default = view.conditions[:1000]
        prizes = view.prizes
        self.first_prize.default = prizes.get("1", "")[:200]
        self.second_prize.default = prizes.get("2", "")[:200]
        self.third_prize.default = prizes.get("3", "")[:200]
        self.interval.default = str(view.interval)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            interval = max(60, min(int(str(self.interval.value or "300").strip()), 86400))
        except ValueError:
            return await interaction.response.send_message("❌ الفاصل الزمني يجب أن يكون رقمًا لا يقل عن 60 ثانية.", ephemeral=True)
        self.setup_view.conditions = str(self.conditions.value or "").strip()
        self.setup_view.prizes = {
            key: value.strip() for key, value in {
                "1": str(self.first_prize.value or ""),
                "2": str(self.second_prize.value or ""),
                "3": str(self.third_prize.value or ""),
            }.items() if value.strip()
        }
        self.setup_view.interval = interval
        await interaction.response.send_message("✅ تم حفظ الشروط والجوائز مؤقتًا. اضغط **حفظ الإعداد** لاعتمادها.", ephemeral=True)
        try:
            await interaction.message.edit(content=self.setup_view.summary(), view=self.setup_view)
        except discord.HTTPException:
            pass


class PhotoSetupView(discord.ui.View):
    def __init__(self, author, guild):
        super().__init__(timeout=600)
        self.author_id = author.id
        self.guild = guild
        self.participation_id = None
        self.voting_id = None
        self.results_id = None
        self.conditions = ""
        self.prizes = {}
        self.interval = 300
        self.add_item(PhotoChannelSelect(self, "participation", "1) اختر روم استقبال الصور", 0))
        self.add_item(PhotoChannelSelect(self, "voting", "2) اختر روم التصويت", 1))
        self.add_item(PhotoChannelSelect(self, "results", "3) اختر روم النتائج", 2))

    def summary(self):
        prizes = self.prizes or {}
        prize_text = "، ".join(f"{key}: {value}" for key, value in sorted(prizes.items())) or "غير محددة"
        return (
            "**الإعداد الحالي:**\n"
            f"• روم المشاركة: {_channel_mention(self.guild, self.participation_id)}\n"
            f"• روم التصويت: {_channel_mention(self.guild, self.voting_id)}\n"
            f"• روم النتائج: {_channel_mention(self.guild, self.results_id)}\n"
            f"• الفاصل: `{self.interval}` ثانية\n"
            f"• الشروط: {self.conditions or 'غير محددة'}\n"
            f"• الجوائز: {prize_text}\n\n"
            "بعد التأكد اضغط **حفظ الإعداد**. سيتم إنشاء المسابقة كمسودة ويمكن تشغيلها عبر `!photo_start`."
        )

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذه لوحة الإعداد تخص الشخص الذي بدأ الأمر فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="الشروط والجوائز", style=discord.ButtonStyle.primary, row=3)
    async def configure_details(self, interaction, button):
        await interaction.response.send_modal(PhotoSetupConfigModal(self))

    @discord.ui.button(label="حفظ الإعداد", style=discord.ButtonStyle.success, row=4)
    async def save_setup(self, interaction, button):
        if not all((self.participation_id, self.voting_id, self.results_id)):
            return await interaction.response.send_message("❌ اختر رومات المشاركة والتصويت والنتائج أولًا.", ephemeral=True)
        conn, cur = get_guild_db(self.guild.id)
        try:
            cur.execute(
                "INSERT INTO photo_contests(guild_id,participation_channel_id,voting_channel_id,results_channel_id,conditions,prizes_json,update_interval,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (self.guild.id, self.participation_id, self.voting_id, self.results_id, self.conditions, json.dumps(self.prizes, ensure_ascii=False), self.interval, int(time.time())),
            )
            conn.commit()
            contest_id = cur.lastrowid
        finally:
            conn.close()
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ تم حفظ إعداد مسابقة الصور رقم `{contest_id}` كمسودة.\n\n{self.summary()}\n\nشغّلها بالأمر `!photo_start`.",
            view=None,
        )

    @discord.ui.button(label="تحديث", style=discord.ButtonStyle.secondary, row=4)
    async def refresh_setup(self, interaction, button):
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="إغلاق", style=discord.ButtonStyle.danger, row=4)
    async def close_setup(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="تم إغلاق معالج إعداد مسابقة الصور.", view=None)


class PhotoChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, setup_view, purpose, placeholder, row):
        self.setup_view = setup_view
        self.purpose = purpose
        super().__init__(placeholder=placeholder, channel_types=[discord.ChannelType.text], row=row)

    async def callback(self, interaction):
        setattr(self.setup_view, f"{self.purpose}_id", self.values[0].id)
        await interaction.response.edit_message(content=self.setup_view.summary(), view=self.setup_view)


class PhotoContest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.refresh_results.start()

    def cog_unload(self):
        self.refresh_results.cancel()

    @staticmethod
    def _admin(ctx):
        return bool(ctx.guild and (ctx.author.id == ctx.guild.owner_id or getattr(ctx.author.guild_permissions, "manage_guild", False)))

    def _active(self, guild_id):
        conn, cur = get_guild_db(guild_id)
        row = cur.execute("SELECT * FROM photo_contests WHERE guild_id=? AND status='active' ORDER BY contest_id DESC LIMIT 1", (guild_id,)).fetchone()
        conn.close()
        return row

    @commands.hybrid_command(name="photo_setup", aliases=["مسابقة_إعداد"], description="معالج إعداد مسابقة الصور خطوة بخطوة")
    async def photo_setup(self, ctx):
        if not self._admin(ctx):
            return await ctx.send("⛔ هذا المعالج للإدارة فقط.")
        view = PhotoSetupView(ctx.author, ctx.guild)
        await ctx.send(
            "📸 **معالج إعداد مسابقة الصور**\n"
            "اختر رومات المشاركة والتصويت والنتائج، ثم اضغط **الشروط والجوائز** لإدخال التفاصيل.\n\n"
            f"{view.summary()}",
            view=view,
        )

    @commands.command(name="photo_prizes", aliases=["مسابقة_جوائز"])
    async def photo_prizes(self, ctx, *, prizes: str):
        if not self._admin(ctx): return await ctx.send("⛔ هذا الأمر للإدارة فقط.")
        parsed = {}
        for part in prizes.split(","):
            if "=" in part:
                k, v = part.split("=", 1); parsed[str(int(k.strip()))] = v.strip()[:200]
        conn, cur = get_guild_db(ctx.guild.id); cur.execute("UPDATE photo_contests SET prizes_json=? WHERE guild_id=? AND status='draft'", (json.dumps(parsed, ensure_ascii=False), ctx.guild.id)); conn.commit(); conn.close()
        await ctx.send("✅ تم حفظ الجوائز كإعلان فقط؛ لا يتم خصم أو دفع عملات تلقائيًا.")

    @commands.command(name="photo_conditions", aliases=["مسابقة_شروط"])
    async def photo_conditions(self, ctx, *, conditions: str):
        if not self._admin(ctx): return await ctx.send("⛔ هذا الأمر للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id); cur.execute("UPDATE photo_contests SET conditions=? WHERE guild_id=? AND status='draft'", (conditions[:1000], ctx.guild.id)); conn.commit(); conn.close()
        await ctx.send("✅ تم حفظ شروط المشاركة.")

    @commands.command(name="photo_start", aliases=["مسابقة_بدء"])
    async def photo_start(self, ctx):
        if not self._admin(ctx): return await ctx.send("⛔ هذا الأمر للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id); row = cur.execute("SELECT contest_id,participation_channel_id,conditions FROM photo_contests WHERE guild_id=? AND status='draft' ORDER BY contest_id DESC LIMIT 1", (ctx.guild.id,)).fetchone()
        if not row: conn.close(); return await ctx.send("❌ أعد الإعداد أولًا.")
        cur.execute("UPDATE photo_contests SET status='active' WHERE contest_id=?", (row[0],)); conn.commit(); conn.close()
        await ctx.send(f"📸 بدأت المسابقة `{row[0]}`. أرسل الصور في <#{row[1]}>. الشرط: {row[2] or 'لا يوجد'}")

    async def _finish(self, ctx, status):
        if not self._admin(ctx): return await ctx.send("⛔ هذا الأمر للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id); row = cur.execute("SELECT contest_id FROM photo_contests WHERE guild_id=? AND status='active' ORDER BY contest_id DESC LIMIT 1", (ctx.guild.id,)).fetchone()
        if not row: conn.close(); return await ctx.send("❌ لا توجد مسابقة نشطة.")
        cur.execute("UPDATE photo_contests SET status=?,ended_at=? WHERE contest_id=?", (status, int(time.time()), row[0])); conn.commit(); conn.close()
        await self._publish_results(ctx.guild, row[0], final=True)
        await ctx.send("✅ تم إعلان نهاية المسابقة." if status == "ended" else "🛑 تم إلغاء المسابقة.")

    @commands.command(name="photo_end", aliases=["مسابقة_إنهاء"])
    async def photo_end(self, ctx): await self._finish(ctx, "ended")

    @commands.command(name="photo_cancel", aliases=["مسابقة_إلغاء"])
    async def photo_cancel(self, ctx): await self._finish(ctx, "cancelled")

    async def _publish_results(self, guild, contest_id, final=False):
        conn, cur = get_guild_db(guild.id)
        contest = cur.execute("SELECT results_channel_id,results_message_id,prizes_json,status FROM photo_contests WHERE contest_id=?", (contest_id,)).fetchone()
        if not contest: conn.close(); return
        rows = cur.execute("SELECT e.entry_no,e.author_id,COALESCE(SUM(CASE WHEN v.excluded=0 THEN 1 ELSE 0 END),0) FROM contest_entries e LEFT JOIN contest_votes v ON v.contest_id=e.contest_id AND v.entry_no=e.entry_no WHERE e.contest_id=? GROUP BY e.entry_no ORDER BY 3 DESC,e.entry_no LIMIT 10", (contest_id,)).fetchall(); conn.close()
        channel = guild.get_channel(contest[0])
        if not channel: return
        lines = [f"**{'🏁 النتائج النهائية' if final else '📊 Top 10'}**"]
        for i, (entry, author, votes) in enumerate(rows, 1): lines.append(f"`{i:02}` الصورة `{entry}` — <@{author}> — **{votes}** صوت")
        text = "\n".join(lines) + ("\n\nالجوائز المعلنة: " + json.dumps(json.loads(contest[2] or '{}'), ensure_ascii=False) if contest[2] and final else "")
        try:
            msg = await channel.fetch_message(contest[1]) if contest[1] else None
            if msg: await msg.edit(content=text)
            else:
                msg = await channel.send(text)
                conn, cur = get_guild_db(guild.id); cur.execute("UPDATE photo_contests SET results_message_id=? WHERE contest_id=?", (msg.id, contest_id)); conn.commit(); conn.close()
        except (discord.Forbidden, discord.HTTPException): pass

    @tasks.loop(seconds=60)
    async def refresh_results(self):
        for guild in list(self.bot.guilds):
            row = self._active(guild.id)
            if row and int(time.time()) % max(60, int(row[8] or 300)) < 60:
                await self._publish_results(guild, row[0])

    @refresh_results.before_loop
    async def before_refresh(self): await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or not message.attachments: return
        active = self._active(message.guild.id)
        if not active or message.channel.id != active[2]: return
        image = next((a for a in message.attachments if (a.content_type or '').startswith('image/')), None)
        if not image: return
        conn, cur = get_guild_db(message.guild.id)
        cur.execute("SELECT COALESCE(MAX(entry_no),0)+1 FROM contest_entries WHERE contest_id=?", (active[0],)); no = cur.fetchone()[0]
        cur.execute("INSERT OR IGNORE INTO contest_entries VALUES(?,?,?,?,?,?,?)", (active[0], no, message.author.id, message.id, None, image.url, int(time.time())))
        conn.commit(); conn.close()
        voting = message.guild.get_channel(active[3])
        if voting:
            try:
                posted = await voting.send(f"📸 **الصورة رقم {no}** من {message.author.mention}\n{image.url}")
                await posted.add_reaction(VOTE_EMOJI)
                conn, cur = get_guild_db(message.guild.id); cur.execute("UPDATE contest_entries SET voting_message_id=? WHERE contest_id=? AND entry_no=?", (posted.id, active[0], no)); conn.commit(); conn.close()
            except (discord.Forbidden, discord.HTTPException): pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if str(payload.emoji) != VOTE_EMOJI or not payload.guild_id or payload.user_id == self.bot.user.id: return
        conn, cur = get_guild_db(payload.guild_id)
        row = cur.execute("SELECT contest_id,entry_no,author_id FROM contest_entries WHERE voting_message_id=?", (payload.message_id,)).fetchone()
        previous = cur.execute("SELECT entry_no FROM contest_votes WHERE contest_id=? AND voter_id=?", (row[0], payload.user_id)).fetchone() if row else None
        previous_message_id = None
        if previous and int(previous[0]) != int(row[1]):
            previous_message_id = cur.execute(
                "SELECT voting_message_id FROM contest_entries WHERE contest_id=? AND entry_no=?",
                (row[0], previous[0]),
            ).fetchone()
        conn.close()
        if not row: return
        user = self.bot.get_user(payload.user_id)
        if not user: return
        ok, reason = record_vote(payload.guild_id, row[0], row[1], user)
        if not ok:
            channel = self.bot.get_channel(payload.channel_id)
            try:
                msg = await channel.fetch_message(payload.message_id); await msg.remove_reaction(payload.emoji, user)
            except (discord.Forbidden, discord.HTTPException): pass
        elif reason == "changed" and previous_message_id and previous_message_id[0]:
            # Keep Discord's reactions aligned with the one-vote database rule.
            old_channel = self.bot.get_channel(payload.channel_id)
            if old_channel:
                try:
                    old_message = await old_channel.fetch_message(previous_message_id[0])
                    await old_message.remove_reaction(VOTE_EMOJI, user)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        """A voter may change their vote by removing the old 👍 then adding it elsewhere."""
        if str(payload.emoji) != VOTE_EMOJI or not payload.guild_id:
            return
        conn, cur = get_guild_db(payload.guild_id)
        row = cur.execute("SELECT contest_id,entry_no FROM contest_entries WHERE voting_message_id=?", (payload.message_id,)).fetchone()
        if row:
            # Removing a previous reaction must not erase a newer vote on another entry.
            cur.execute("DELETE FROM contest_votes WHERE contest_id=? AND entry_no=? AND voter_id=?", (row[0], row[1], payload.user_id))
            conn.commit()
        conn.close()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds: await self._snapshot_invites(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild): await self._snapshot_invites(guild)

    async def _snapshot_invites(self, guild):
        try: invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException): return
        conn, cur = get_guild_db(guild.id)
        for inv in invites: cur.execute("INSERT INTO invite_snapshots VALUES(?,?,?,?,?) ON CONFLICT(guild_id,code) DO UPDATE SET inviter_id=excluded.inviter_id,uses=excluded.uses,captured_at=excluded.captured_at", (guild.id, inv.code, getattr(inv.inviter, 'id', None), inv.uses or 0, int(time.time())))
        conn.commit(); conn.close()

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            current = await member.guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            # Keep the join in the total even when Discord does not reveal its invite.
            current = []
        conn, cur = get_guild_db(member.guild.id); old = {r[0]: r[1:] for r in cur.execute("SELECT code,uses,inviter_id FROM invite_snapshots WHERE guild_id=?", (member.guild.id,))}; found = next((i for i in current if int(i.uses or 0) > int(old.get(i.code, (0, None))[0])), None)
        inviter = getattr(found.inviter, 'id', None) if found else None; code = found.code if found else None
        age_days = account_age_days(member)
        status = 'fake' if age_days is not None and age_days < MIN_VOTER_ACCOUNT_AGE_DAYS else 'valid'
        cur.execute("INSERT INTO invite_uses(guild_id,invite_code,inviter_id,member_id,member_username,joined_at,status,account_age_days) VALUES(?,?,?,?,?,?,?,?)", (member.guild.id, code, inviter, member.id, str(member), int(time.time()), status, age_days))
        conn.commit(); conn.close(); await self._snapshot_invites(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """A departed member is never counted as a valid invite and loses contest votes."""
        conn, cur = get_guild_db(member.guild.id)
        now = int(time.time())
        cur.execute("UPDATE invite_uses SET status='left', left_at=? WHERE guild_id=? AND member_id=? AND status IN ('valid','fake','unknown')", (now, member.guild.id, member.id))
        cur.execute("UPDATE contest_votes SET excluded=1 WHERE voter_id=? AND contest_id IN (SELECT contest_id FROM photo_contests WHERE guild_id=?)", (member.id, member.guild.id))
        cur.execute("UPDATE contest_voter_reviews SET excluded=1, classification='left' WHERE voter_id=? AND contest_id IN (SELECT contest_id FROM photo_contests WHERE guild_id=?)", (member.id, member.guild.id))
        conn.commit(); conn.close()

    @commands.command(name="photo_voter", aliases=["مسابقة_مصوت"])
    async def photo_voter(self, ctx, voter_id: int):
        if not self._admin(ctx): return await ctx.send("⛔ للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id)
        row = cur.execute(
            "SELECT r.* FROM contest_voter_reviews r JOIN photo_contests c ON c.contest_id=r.contest_id "
            "WHERE c.guild_id=? AND r.voter_id=? ORDER BY r.contest_id DESC LIMIT 1",
            (ctx.guild.id, voter_id),
        ).fetchone()
        conn.close()
        await ctx.send("لا توجد بيانات مصوّت." if not row else "بيانات المصوّت: " + " | ".join(map(str, row)))

    @commands.command(name="photo_exclude", aliases=["مسابقة_استبعاد"])
    async def photo_exclude(self, ctx, voter_id: int, all_flag: str = ""):
        if not self._admin(ctx): return await ctx.send("⛔ للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id)
        if all_flag in ("all", "review", "المراجعة"): cur.execute("UPDATE contest_votes SET excluded=1 WHERE contest_id IN (SELECT contest_id FROM contest_voter_reviews WHERE voter_id=? AND suspicion_score>0)", (voter_id,)); msg = "تم استبعاد أصوات المصوّتين الموسومين للمراجعة."
        else: cur.execute("UPDATE contest_votes SET excluded=1 WHERE voter_id=?", (voter_id,)); msg = "تم استبعاد أصوات المصوّت."
        conn.commit(); conn.close(); await ctx.send(msg)

    @commands.command(name="invites", aliases=["دعوات_عضو"])
    async def invites(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        conn, cur = get_guild_db(ctx.guild.id)
        rows = cur.execute(
            "SELECT invite_code,member_id,member_username,joined_at,status FROM invite_uses "
            "WHERE inviter_id=? ORDER BY joined_at DESC LIMIT 50",
            (member.id,),
        ).fetchall()
        counts = {
            status: cur.execute("SELECT COUNT(*) FROM invite_uses WHERE guild_id=? AND inviter_id=? AND status=?", (ctx.guild.id, member.id, status)).fetchone()[0]
            for status in ("valid", "fake", "left")
        }
        total = cur.execute("SELECT COUNT(*) FROM invite_uses WHERE guild_id=? AND inviter_id=?", (ctx.guild.id, member.id)).fetchone()[0]
        conn.close()
        text = f"دعوات {member.mention}: صالح `{counts['valid']}` • وهمي `{counts['fake']}` • غادر `{counts['left']}` • الإجمالي `{total}`\n" + "\n".join(
            f"{index}. {username or member_id} — {code or 'غير محدد'} — {status}"
            for index, (code, member_id, username, _joined_at, status) in enumerate(rows, 1)
        )
        await ctx.send(text or "لا يوجد سجل. التتبع التاريخي قبل التفعيل غير متاح.")

    @commands.command(name="invites_all", aliases=["دعوات_الجميع"])
    async def invites_all(self, ctx):
        if not self._admin(ctx): return await ctx.send("⛔ للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id)
        rows = cur.execute("SELECT inviter_id, SUM(status='valid'), SUM(status='fake'), SUM(status='left'), COUNT(*) FROM invite_uses WHERE guild_id=? GROUP BY inviter_id ORDER BY COUNT(*) DESC", (ctx.guild.id,)).fetchall()
        conn.close()
        await ctx.send("\n".join(f"{r[0] or 'غير محدد'}: صالح `{r[1] or 0}` • وهمي `{r[2] or 0}` • غادر `{r[3] or 0}` • الإجمالي `{r[4]}`" for r in rows) or "لا يوجد سجل. التتبع التاريخي قبل التفعيل غير متاح.")

    @commands.command(name="invites_clear", aliases=["مسح_دعوات"])
    async def invites_clear(self, ctx, member: Optional[discord.Member] = None):
        if not self._admin(ctx): return await ctx.send("⛔ هذا الأمر للإدارة فقط.")
        conn, cur = get_guild_db(ctx.guild.id); cur.execute("DELETE FROM invite_uses" + (" WHERE inviter_id=?" if member else ""), ((member.id,) if member else ())); conn.commit(); conn.close(); await ctx.send("✅ تم مسح سجل الدعوات." )


async def setup(bot):
    await bot.add_cog(PhotoContest(bot))
