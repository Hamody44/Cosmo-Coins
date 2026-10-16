import discord
from discord.ext import commands, tasks
import time
from database import (
    get_guild_db, is_bot_admin, parse_amount, update_balance
)

async def register_member_boost(guild: discord.Guild, member: discord.Member, boost_count: int, is_fake: bool = False):
    g_conn, g_cur = get_guild_db(guild.id)
    g_cur.execute("SELECT is_enabled, single_boost_reward, double_boost_reward, boost_log_channel FROM boost_system_config WHERE guild_id = ?", (guild.id,))
    cfg = g_cur.fetchone()

    if not cfg or cfg[0] == 0:
        g_conn.close()
        return False, "نظام البوست غير مفعّل حالياً في هذا السيرفر."

    is_enabled, single_reward, double_reward, log_chan_id = cfg
    reward_per_week = double_reward if boost_count >= 2 else single_reward

    if reward_per_week <= 0:
        g_conn.close()
        return False, "لم يتم تحديد مكافآت لنظام البوست بعد."

    update_balance(member.id, reward_per_week)
    now = int(time.time())
    next_payment = now + (7 * 86400)

    g_cur.execute(
        "INSERT INTO active_boost_subscriptions (user_id, boost_type, weekly_reward, payments_remaining, next_payment_timestamp, is_fake) VALUES (?, ?, ?, 3, ?, ?)",
        (member.id, boost_count, reward_per_week, next_payment, 1 if is_fake else 0)
    )
    g_conn.commit()
    g_conn.close()

    type_str = "دبل بوست 🚀🚀" if boost_count >= 2 else "بوست 🚀"
    
    try:
        await member.send(
            f"🎉 **شكراً لدعمك سيرفر {guild.name}!**\n"
            f"تم تفعيل اشتراكك في نظام مكافآت الـ {type_str}:\n"
            f"• استلمت الدفعة الأولى فوراً: `{reward_per_week:,}` Cosmo!\n"
            f"• متبقي لك: `3` دفعات أسبوعية (كل 7 أيام دفعة طالما البوست مستمر).\n"
            f"• موعد الدفعة القادمة: <t:{next_payment}:F> (<t:{next_payment}:R>)."
        )
    except:
        pass

    if log_chan_id:
        chan = guild.get_channel(log_chan_id)
        if chan:
            await chan.send(
                f"🚀 قام {member.mention} بعمل **{type_str}** للسيرفر!\n"
                f"تم تسليم الدفعة الأولى تلقائياً (`{reward_per_week:,}` Cosmo) مع جدولة 3 دفعات أسبوعية قادمة."
            )

    return True, f"تم تسجيل اشتراك {type_str} وتسليم الدفعة الأولى بنجاح!"

class Boost(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.process_boost_weekly_payments.is_running():
            self.process_boost_weekly_payments.start()

    def cog_unload(self):
        self.process_boost_weekly_payments.cancel()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.guild and not before.premium_since and after.premium_since:
            await register_member_boost(after.guild, after, boost_count=1, is_fake=False)

    @tasks.loop(minutes=15)
    async def process_boost_weekly_payments(self):
        now = int(time.time())
        for guild in self.bot.guilds:
            g_conn, g_cur = get_guild_db(guild.id)
            g_cur.execute(
                "SELECT id, user_id, boost_type, weekly_reward, payments_remaining, next_payment_timestamp, is_fake "
                "FROM active_boost_subscriptions WHERE next_payment_timestamp <= ?", 
                (now,)
            )
            due_payments = g_cur.fetchall()

            for sub in due_payments:
                sub_id, u_id, b_type, reward, remaining, next_pay_ts, is_fake = sub
                member = guild.get_member(u_id)

                if not is_fake and (not member or not member.premium_since):
                    g_cur.execute("DELETE FROM active_boost_subscriptions WHERE id = ?", (sub_id,))
                    g_conn.commit()
                    continue

                weeks_passed = ((now - next_pay_ts) // (7 * 86400)) + 1
                payments_to_give = min(weeks_passed, remaining)
                total_due_reward = payments_to_give * reward
                update_balance(u_id, total_due_reward)

                new_remaining = remaining - payments_to_give
                if new_remaining <= 0:
                    g_cur.execute("DELETE FROM active_boost_subscriptions WHERE id = ?", (sub_id,))
                else:
                    updated_next_pay = next_pay_ts + (payments_to_give * 7 * 86400)
                    if updated_next_pay <= now:
                        updated_next_pay = now + (7 * 86400)
                    g_cur.execute(
                        "UPDATE active_boost_subscriptions SET payments_remaining = ?, next_payment_timestamp = ? WHERE id = ?", 
                        (new_remaining, updated_next_pay, sub_id)
                    )
                g_conn.commit()

                type_str = "دبل بوست 🚀🚀" if b_type == 2 else "بوست 🚀"
                missed_text = f" (شملت {payments_to_give} دفعة مستحقة)" if payments_to_give > 1 else ""

                if member:
                    try:
                        await member.send(
                            f"💎 **دفعة بوست جديدة:** تم تسليم `{total_due_reward:,}` Cosmo لدعمك السيرفر بـ {type_str}{missed_text}!\n"
                            f"(الدفعات المتبقية: `{new_remaining}`)."
                        )
                    except:
                        pass

                g_cur.execute("SELECT boost_log_channel FROM boost_system_config WHERE guild_id = ?", (guild.id,))
                log_row = g_cur.fetchone()
                if log_row and log_row[0]:
                    log_chan = guild.get_channel(log_row[0])
                    if log_chan:
                        await log_chan.send(
                            f"💎 تم تسليم دفعة البوست للعضو <@{u_id}> بقيمة `{total_due_reward:,}` Cosmo لدعمه بـ {type_str}{missed_text} (المتبقي: `{new_remaining}`)."
                        )

            g_conn.close()

    @commands.hybrid_command(name="setup_boost_rewards", aliases=["ضبط_جوائز_البوست"], description="تحديد مكافأة الدفعة الأسبوعية للبوست الواحد والدبل بوست")
    async def setup_boost_rewards(self, ctx: commands.Context, single_reward: str, double_reward: str):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        try:
            s_amount = parse_amount(single_reward)
            d_amount = parse_amount(double_reward)
        except:
            return await ctx.send("❌ صيغة المبالغ غير صحيحة.")

        if s_amount <= 0 or d_amount <= 0:
            return await ctx.send("❌ المبالغ يجب أن تكون أكبر من 0.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO boost_system_config (guild_id, single_boost_reward, double_boost_reward) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET single_boost_reward = excluded.single_boost_reward, double_boost_reward = excluded.double_boost_reward",
            (ctx.guild.id, s_amount, d_amount)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ **تم ضبط مكافآت البوست:**\n• البوست الواحد: `{s_amount:,}` Cosmo أسبوعياً\n• الدبل بوست: `{d_amount:,}` Cosmo أسبوعياً.")

    @commands.hybrid_command(name="toggle_boost_system", aliases=["تفعيل_نظام_البوست"], description="تفعيل أو تعطيل استقبال مشتركين جدد في نظام البوست")
    async def toggle_boost_system(self, ctx: commands.Context, status: bool):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        val = 1 if status else 0
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO boost_system_config (guild_id, is_enabled) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET is_enabled = excluded.is_enabled",
            (ctx.guild.id, val)
        )
        g_conn.commit()
        g_conn.close()
        status_str = "مفعلاً ويستقبل بوستات جديدة ✅" if status else "معطلاً ⛔"
        await ctx.send(f"⚙️ أصبح نظام البوست الآن: **{status_str}**.")

    @commands.hybrid_command(name="set_boost_log", aliases=["لوج_البوست"], description="تحديد روم إعلانات البوست وتسليم الدفعات الأسبوعية")
    async def set_boost_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO boost_system_config (guild_id, boost_log_channel) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET boost_log_channel = excluded.boost_log_channel",
            (ctx.guild.id, channel.id)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم لوج البوست على: {channel.mention}")

    @commands.hybrid_command(name="fake_boost", aliases=["بوست_وهمي"], description="محاكاة بوست لتجربة النظام واختباره (1 للبوست، 2 للدبل بوست)")
    async def fake_boost(self, ctx: commands.Context, boost_count: int):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        if boost_count not in [1, 2]:
            return await ctx.send("❌ يرجى اختيار إما `1` (بوست واحد) أو `2` (دبل بوست).")

        success, msg = await register_member_boost(ctx.guild, ctx.author, boost_count=boost_count, is_fake=True)
        if not success:
            return await ctx.send(f"⚠️ {msg}")
        await ctx.send(f"🧪 **(تجربة Fake Boost)** {msg}\nتم تسليم الدفعة الأولى فوراً لحسابك!")

    @commands.hybrid_command(name="my_boost_status", aliases=["حالة_البوست_لي"], description="معاينة اشتراكات البوست والدفعات المتبقية لك في هذا السيرفر")
    async def my_boost_status(self, ctx: commands.Context):
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "SELECT id, boost_type, weekly_reward, payments_remaining, next_payment_timestamp FROM active_boost_subscriptions WHERE user_id = ?", 
            (ctx.author.id,)
        )
        subs = g_cur.fetchall()
        g_conn.close()

        if not subs:
            return await ctx.send("ℹ️ ليس لديك أي اشتراك نشط في نظام البوست حالياً.")

        embed = discord.Embed(title=f"🚀 اشتراكات البوست | {ctx.guild.name}", color=discord.Color.magenta())
        for idx, s in enumerate(subs, 1):
            type_str = "دبل بوست (2 Boosts)" if s[1] == 2 else "بوست واحد (1 Boost)"
            embed.add_field(
                name=f"اشتراك #{idx} - {type_str}",
                value=f"• المكافأة: `{s[2]:,}` Cosmo أسبوعياً\n• المتبقي: `{s[3]}` دفعات من أصل 4\n• الدفعة القادمة: <t:{s[4]}:F> (<t:{s[4]}:R>)",
                inline=False
            )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Boost(bot))
