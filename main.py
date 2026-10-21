import os
import asyncio
import threading
import subprocess
import urllib.request
import stat
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from database import (
    PRIMARY_OWNER_ID, get_guild_db, is_blacklisted, parse_amount, 
    deduct_server_spending, update_balance, send_global_action_log
)
import time
import re
from datetime import datetime

# استيراد دالة تشغيل لوحة التحكم
from dashboard.app import run_dashboard, set_discord_bot_context

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.owner_id = PRIMARY_OWNER_ID

def run_tunnel():
    token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN")
    if not token:
        print("⚠️ لم يتم العثور على CLOUDFLARE_TUNNEL_TOKEN في ملف .env")
        return

    binary_path = os.path.join(os.getcwd(), "cloudflared")
    # تحميل الأداة تلقائياً إذا لم تكن موجودة
    if not os.path.exists(binary_path):
        print("⬇️ جاري تحميل أداة Cloudflare Tunnel...")
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        try:
            urllib.request.urlretrieve(url, binary_path)
            st = os.stat(binary_path)
            os.chmod(binary_path, st.st_mode | stat.S_IEXEC)
            print("✅ تم تحميل cloudflared بنجاح وتعيين الصلاحيات.")
        except Exception as e:
            print(f"❌ فشل تحميل أداة النفق: {e}")
            return

    try:
        # ملفات المستودع قد تُستنسخ دون بت التنفيذ؛ اضبطه قبل تشغيل الأداة.
        os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IXUSR)
    except OSError as e:
        print(f"❌ تعذر ضبط صلاحية تشغيل cloudflared: {e}")
        return

    # تشغيل النفق
    try:
        print("🚀 جاري بدء تشغيل Cloudflare Tunnel...")
        subprocess.Popen([binary_path, "tunnel", "run", "--token", token],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        print("✅ تم تشغيل النفق بنجاح!")
    except Exception as e:
        print(f"❌ خطأ أثناء تشغيل أداة النفق: {e}")

@tasks.loop(minutes=1)
async def check_premium_expiry_task():
    now = int(time.time())
    for guild in bot.guilds:
        g_conn, g_cur = get_guild_db(guild.id)
        g_cur.execute("SELECT premium_end FROM premium_info WHERE premium_end > 0 AND premium_end <= ?", (now,))
        row = g_cur.fetchone()
        if row:
            g_cur.execute("UPDATE premium_info SET premium_end = 0, custom_banner_url = NULL WHERE guild_id = ?", (guild.id,))
            g_conn.commit()
            if guild.me:
                try:
                    await guild.me.edit(nick=None)
                except:
                    pass
                try:
                    await guild.me.edit(avatar=None)
                except:
                    pass
                try:
                    route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild.id)
                    await bot.http.request(route, json={"banner": None})
                except:
                    pass
        g_conn.close()

@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.id == bot.user.id:
        return

    # 1. فحص بوتات الشحن المصرح بها (مثل عجلة الحظ)
    if message.author.bot:
        g_conn, g_cur = get_guild_db(message.guild.id)
        g_cur.execute("SELECT 1 FROM bot_whitelist WHERE bot_id = ? AND channel_id = ?", (message.author.id, message.channel.id))
        whitelisted = g_cur.fetchone()
        g_conn.close()

        if whitelisted:
            content = message.content or ""
            if not content and message.embeds:
                emb = message.embeds[0]
                content = f"{emb.description or ''} {emb.title or ''}"
                for field in emb.fields:
                    content += f" {field.value}"

            match = re.search(r"!(?:add_balance|اضافة)\s+(<@!?(\d+)>)\s+([^\s]+)", content)
            if match:
                target_mention, user_id, amount_str = match.groups()
                target_member = message.guild.get_member(int(user_id))
                
                if target_member:
                    if is_blacklisted(target_member):
                        return await message.channel.send(f"⛔ تعذر الشحن: {target_member.mention} مدرج في البلاك ليست.")

                    try:
                        parsed_amount = parse_amount(amount_str)
                        if parsed_amount > 0:
                            allowed, pool_msg = deduct_server_spending(message.guild.id, parsed_amount)
                            if not allowed:
                                return await message.channel.send(f"⚠️ (عجلة الحظ) {pool_msg}")

                            g_conn, g_cur = get_guild_db(message.guild.id)
                            g_cur.execute("SELECT double_event_end FROM settings WHERE guild_id = ?", (message.guild.id,))
                            d_row = g_cur.fetchone()
                            g_conn.close()

                            is_double = d_row and d_row[0] and int(time.time()) < d_row[0]
                            final_amount = parsed_amount * 2 if is_double else parsed_amount

                            update_balance(target_member.id, final_amount)
                            extra_text = " **(مضاعفة ×2 فعالة! 🔥)**" if is_double else ""
                            await message.channel.send(f"✅ (عجلة الحظ) تمت إضافة `{final_amount:,}` Cosmo إلى رصيد {target_member.mention}.{extra_text}")
                            
                            try:
                                await target_member.send(f"📥 تمت إضافة `{final_amount:,}` Cosmo إلى حسابك من عجلة الحظ!{extra_text}")
                            except:
                                pass

                            global_embed = discord.Embed(
                                title="🌐 شحن رصيد - عجلة الحظ (Wheel Bot)",
                                color=discord.Color.gold(),
                                timestamp=datetime.now()
                            )
                            global_embed.add_field(name="🏛️ السيرفر", value=f"{message.guild.name} (`{message.guild.id}`)", inline=False)
                            global_embed.add_field(name="🤖 البوت المنفّذ", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
                            global_embed.add_field(name="👤 الفائز", value=f"{target_member.mention} (`{target_member.id}`)", inline=True)
                            global_embed.add_field(name="🪙 الجائزة", value=f"`{final_amount:,}` Cosmo{extra_text}", inline=False)
                            global_embed.set_footer(text="Cosmo Global Wheel Log")
                            await send_global_action_log(bot, global_embed)
                    except Exception as e:
                        print(f"Error handling wheel bot: {e}")
        return

    # 2. فحص نظام الرد التلقائي (حذف رسالة العضو وإرسال الرد بدلاً منها)
    if message.content:
        content_lower = message.content.strip().lower()
        g_conn, g_cur = get_guild_db(message.guild.id)
        g_cur.execute("SELECT trigger_word, reply_content FROM auto_replies")
        replies = g_cur.fetchall()
        g_conn.close()

        for trigger_word, reply_content in replies:
            if trigger_word == content_lower or trigger_word in content_lower.split():
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass

                await message.channel.send(reply_content)
                return

    # 3. فحص الاختصارات المعينة (Aliases)
    first_word = message.content.split()[0].replace("!", "").replace("/", "") if message.content else ""
    g_conn, g_cur = get_guild_db(message.guild.id)
    g_cur.execute("SELECT original_command FROM command_aliases WHERE alias = ?", (first_word,))
    alias_row = g_cur.fetchone()
    g_conn.close()

    if alias_row:
        orig_cmd = alias_row[0]
        cmd = bot.get_command(orig_cmd)
        if cmd:
            prefix = message.content[0] if message.content[0] in ["!", "/"] else "!"
            message.content = f"{prefix}{orig_cmd} " + " ".join(message.content.split()[1:])

    # 4. استثناء المنشن التلقائي لجيميناي من معالجة الأوامر لتجنب أي تضارب
    if bot.user in message.mentions and not message.content.startswith(("!", "/")):
        return

    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    if not check_premium_expiry_task.is_running():
        check_premium_expiry_task.start()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Error syncing slash commands: {e}")

async def load_cogs():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py"):
            await bot.load_extension(f"cogs.{filename[:-3]}")
            print(f"Loaded Cog: {filename}")

async def main():
    # 1. تشغيل سيرفر الداشبورد في خيط مستقل
    try:
        set_discord_bot_context(bot, asyncio.get_running_loop())
        web_thread = threading.Thread(target=run_dashboard, daemon=True)
        web_thread.start()
        print("🌐 Dashboard thread started successfully on port 2009!")
    except Exception as e:
        print(f"⚠️ خطأ أثناء بدء خيط الداشبورد: {e}")

    # 2. تشغيل نفق Cloudflare Tunnel في خيط مستقل
    tunnel_thread = threading.Thread(target=run_tunnel, daemon=True)
    tunnel_thread.start()

    print("=" * 60)
    print("🌍 رابط الداشبورد الرسمي: https://cosmo-coins.site")
    print("=" * 60)

    # 3. تشغيل البوت
    async with bot:
        await load_cogs()
        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            raise ValueError("❌ لم يتم العثور على DISCORD_BOT_TOKEN في ملف .env")
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
