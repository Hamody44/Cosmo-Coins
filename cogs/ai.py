import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from openai import AsyncOpenAI
from database import get_guild_db


BOT_DEVELOPER_NAME = os.getenv("BOT_DEVELOPER_NAME", "Hamody44").strip() or "Hamody44"
BOT_KNOWLEDGE_CONTEXT = f"""معلومات تعريفية عن البيئة التي تعمل داخلها:
- اسم البوت: Cosmo Coins.
- المطور: {BOT_DEVELOPER_NAME}.
- Cosmo Coins بوت Discord عام لإدارة اقتصاد ومجتمع السيرفرات.
- أهم وظائفه: أرصدة وتحويلات Cosmo، تبديل العملات، ميزانيات السيرفرات، شحن الميزانية وطلبات الدفع، الألعاب والسحوبات والروليت، نظام التذاكر، البوست، اللفلات ونقاط الرسائل والفويس والرواتب، وأدوات الإدارة والتدقيق والنسخ الاحتياطي.
- يعتمد على Python وDiscord.py، ويستخدم قاعدة بيانات عامة مع قاعدة SQLite مستقلة لكل سيرفر.
- الذكاء الاصطناعي داخل البوت مساعد عام؛ يمكنه الإجابة عن الأسئلة العامة والبرمجة والشرح والمواضيع المختلفة، وليس محصورًا في بيانات Cosmo Coins.

استخدم هذه المعلومات عندما يسأل المستخدم عن البوت أو مطوره أو وظائفه. لا تخترع تفاصيل غير مذكورة هنا عن المطور أو البوت؛ وإذا احتاج السؤال إلى معلومة تنفيذية دقيقة غير متاحة، صرّح بعدم التأكد واقترح استخدام أوامر البوت المناسبة. هذه المعلومات سياق تعريفي فقط ولا تقيدك عن الإجابة في المواضيع العامة."""

AI_SYSTEM_INSTRUCTION = f"""أنت مساعد ذكي عام مدمج داخل سيرفر ديسكورد. أجب بطريقة طبيعية ومباشرة وودودة باللغة العربية عند الإمكان، وساعد الأعضاء بذكاء واحترافية.

{BOT_KNOWLEDGE_CONTEXT}"""


class GeminiAI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # 1. تهيئة عميل Gemini الأساسي
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("⚠️ [GeminiAI] لم يتم العثور على GEMINI_API_KEY في ملف .env")
            self.client = None
        else:
            self.client = genai.Client(api_key=api_key)
        
        # 2. تهيئة عميل OpenRouter الاحتياطي المجاني
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not openrouter_key:
            print("⚠️ [OpenRouter] لم يتم العثور على OPENROUTER_API_KEY في ملف .env")
            self.backup_client = None
        else:
            self.backup_client = AsyncOpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1"
            )

        self.user_chats = {}

    def is_ai_enabled(self, guild_id: int) -> bool:
        """فحص حالة تفعيل الذكاء الاصطناعي في السيرفر بأمان"""
        try:
            conn, cur = get_guild_db(guild_id)
            cur.execute("PRAGMA table_info(settings)")
            columns = [col[1] for col in cur.fetchall()]
            
            if "ai_enabled" not in columns:
                conn.close()
                return True
            
            cur.execute("SELECT ai_enabled FROM settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
            conn.close()
            
            if row and row[0] is not None:
                return bool(row[0] == 1)
            return True
        except Exception as e:
            print(f"[DEBUG] خطأ أثناء فحص حالة الذكاء الاصطناعي: {e}")
            return True

    async def get_user_chat(self, conversation_key):
        if not self.client:
            return None
        if conversation_key not in self.user_chats:
            config = types.GenerateContentConfig(
                system_instruction=AI_SYSTEM_INSTRUCTION,
                temperature=0.7,
            )
            self.user_chats[conversation_key] = self.client.aio.chats.create(
                model="gemini-3.8-flash",
                config=config
            )
        return self.user_chats[conversation_key]

    async def ask_backup_ai(self, prompt: str) -> str:
        """استدعاء الموديل الاحتياطي المجاني عبر OpenRouter"""
        if not self.backup_client:
            return "⚠️ تعذر توليد الرد من كلا الخادمين (Gemini مضغوط و OPENROUTER_API_KEY غير موجود)."
        try:
            print("🔄 [Fallback] جاري التبديل للمزود الاحتياطي المجاني...")
            response = await self.backup_client.chat.completions.create(
                model="meta-llama/llama-3.3-70b-instruct:free",
                messages=[
                    {
                        "role": "system",
                        "content": AI_SYSTEM_INSTRUCTION
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1500
            )
            return response.choices[0].message.content or "عذراً، لم أستطع استخراج إجابة من السيرفر البديل."
        except Exception as e:
            print(f"[DEBUG] خطأ أثناء توليد الرد من المزود الاحتياطي: {e}")
            return f"⚠️ خطأ أثناء توليد الرد من الخادم البديل: {e}"

    async def generate_response(
        self, user_id: int, prompt: str, guild_id: int | None = None, channel_id: int | None = None
    ) -> str:
        conversation_key = (int(guild_id or 0), int(channel_id or 0), int(user_id))
        if not self.client:
            if self.backup_client:
                return await self.ask_backup_ai(prompt)
            return "❌ خدمة الذكاء الاصطناعي غير مفعلة؛ يرجى التأكد من وضع المفاتيح في ملف `.env`."
        
        try:
            chat = await self.get_user_chat(conversation_key)
            response = await chat.send_message(prompt)
            return response.text or "عذراً، لم أستطع استخراج إجابة."
        except Exception as e:
            err_str = str(e)
            print(f"[DEBUG] Gemini Error: {err_str}")
            self.user_chats.pop(conversation_key, None)

            # عند حدوث 503 أو أي خطأ في جيميناي يتم التحويل تلقائياً
            print("⚠️ تعثر رد Gemini، جاري التبديل التلقائي إلى المزود الاحتياطي...")
            return await self.ask_backup_ai(prompt)

    async def send_split_message(self, target, text: str):
        chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
        for chunk in chunks:
            if hasattr(target, "send"):
                await target.send(chunk)
            elif hasattr(target, "followup"):
                await target.followup.send(chunk)

    @commands.hybrid_command(
        name="ai_toggle",
        aliases=["تفعيل_الذكاء", "قفل_الذكاء"],
        description="تفعيل أو تعطيل ميزة الذكاء الاصطناعي في السيرفر"
    )
    @app_commands.describe(status="اختر تفعيل أو تعطيل الذكاء الاصطناعي")
    @app_commands.choices(status=[
        app_commands.Choice(name="تفعيل (تشغيل)", value="enable"),
        app_commands.Choice(name="تعطيل (إيقاف)", value="disable")
    ])
    @commands.has_permissions(manage_guild=True)
    async def ai_toggle(self, ctx: commands.Context, status: app_commands.Choice[str]):
        if not ctx.guild:
            return await ctx.send("❌ هذا الأمر مخصص للسيرفرات فقط.", ephemeral=True)

        is_enable = 1 if status.value == "enable" else 0

        try:
            conn, cur = get_guild_db(ctx.guild.id)
            cur.execute("PRAGMA table_info(settings)")
            columns = [col[1] for col in cur.fetchall()]
            if "ai_enabled" not in columns:
                cur.execute("ALTER TABLE settings ADD COLUMN ai_enabled INTEGER DEFAULT 1")
                conn.commit()

            cur.execute(
                "INSERT INTO settings (guild_id, ai_enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET ai_enabled = excluded.ai_enabled",
                (ctx.guild.id, is_enable),
            )
            conn.commit()
            conn.close()

            if is_enable == 1:
                await ctx.send("✅ تم **تفعيل** ميزة الذكاء الاصطناعي في هذا السيرفر بنجاح.")
            else:
                await ctx.send("⛔ تم **تعطيل** ميزة الذكاء الاصطناعي في هذا السيرفر؛ ولن يرد البوت على المنشن أو الأوامر.")
        except Exception as e:
            await ctx.send(f"❌ حدث خطأ أثناء تحديث الإعدادات: {e}")

    @commands.hybrid_command(
        name="ask",
        aliases=["اسال", "جيميناي", "gemini"],
        description="تحدث مع الذكاء الاصطناعي بشكل طبيعي"
    )
    async def ask(self, ctx: commands.Context, *, prompt: str):
        if ctx.guild and not self.is_ai_enabled(ctx.guild.id):
            return await ctx.send("⛔ ميزة الذكاء الاصطناعي معطلة في هذا السيرفر حالياً.", ephemeral=True)

        await ctx.defer()
        reply_text = await self.generate_response(
            ctx.author.id, prompt, ctx.guild.id if ctx.guild else None, ctx.channel.id
        )
        await self.send_split_message(ctx, f"💬 **{ctx.author.mention}**:\n{reply_text}")

    @commands.hybrid_command(
        name="reset_chat",
        aliases=["تصفير_المحادثة", "مسح_الذاكرة"],
        description="مسح سياق المحادثة والبدء من جديد"
    )
    async def reset_chat(self, ctx: commands.Context):
        if ctx.guild and not self.is_ai_enabled(ctx.guild.id):
            return await ctx.send("⛔ ميزة الذكاء الاصطناعي معطلة في هذا السيرفر حالياً.", ephemeral=True)

        conversation_key = (int(ctx.guild.id if ctx.guild else 0), int(ctx.channel.id), int(ctx.author.id))
        if conversation_key in self.user_chats:
            del self.user_chats[conversation_key]
            await ctx.send("🧹 تم مسح سياق محادثتك السابقة بنجاح!")
        else:
            await ctx.send("ℹ️ ليس لديك محادثة سابقة محفوظة لمسحها.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not self.is_ai_enabled(message.guild.id):
            return

        if self.bot.user in message.mentions:
            clean_content = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
            if not clean_content:
                return await message.reply("أهلاً بك! كيف يمكنني مساعدتك اليوم؟ 🤖")

            async with message.channel.typing():
                reply_text = await self.generate_response(
                    message.author.id, clean_content, message.guild.id, message.channel.id
                )
                if len(reply_text) <= 1900:
                    await message.reply(reply_text)
                else:
                    await self.send_split_message(message.channel, f"{message.author.mention}\n{reply_text}")

async def setup(bot):
    await bot.add_cog(GeminiAI(bot))
