import os
import asyncio
import copy
import json
import subprocess
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from openai import AsyncOpenAI
from database import PRIMARY_OWNER_ID, get_guild_db


PRIMARY_REPO_ROOT = Path(__file__).resolve().parents[1]
AI_CODE_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "second_bot", "guilds_data",
}
AI_CODE_EXCLUDED_FILES = {
    ".env", "global_cosmo.db", "global_cosmo.db-journal", "global_cosmo.db-wal",
}
AI_CODE_EXTENSIONS = {".py", ".md", ".json", ".toml", ".sh", ".html", ".css", ".js"}
AI_ACTION_PROMPT = """حلّل طلب المستخدم وحدد واحداً من الأنواع التالية فقط:
1) chat: سؤال أو طلب شرح لا ينفذ تغييراً.
2) command: يريد تنفيذ أمر موجود في البوت؛ أعد command بصيغة !command arguments.
3) code_change: يريد قراءة أو تعديل كود البوت الأساسي؛ أعد وصفاً مختصراً للتعديل.

أعد JSON صالحاً فقط بهذا الشكل:
{"action":"chat|command|code_change","command":"","summary":""}
لا تخترع أمراً. إذا لم تكن متأكداً استخدم chat.
"""


BOT_DEVELOPER_NAME = os.getenv("BOT_DEVELOPER_NAME", "7amody77777").strip() or "7amody77777"
BOT_KNOWLEDGE_CONTEXT = f"""معلومات تعريفية عن البيئة التي تعمل داخلها:
- اسم البوت: Cosmo Coins.
- المطور: {BOT_DEVELOPER_NAME}.
- Cosmo Coins بوت Discord عام لإدارة اقتصاد ومجتمع السيرفرات.
- أهم وظائفه: أرصدة وتحويلات Cosmo، تبديل العملات، ميزانيات السيرفرات، شحن الميزانية وطلبات الدفع، الألعاب والسحوبات والروليت، نظام التذاكر، البوست، اللفلات ونقاط الرسائل والفويس والرواتب، وأدوات الإدارة والتدقيق والنسخ الاحتياطي.
- يعتمد على Python وDiscord.py، ويستخدم قاعدة بيانات عامة مع قاعدة SQLite مستقلة لكل سيرفر.
- الذكاء الاصطناعي داخل البوت مساعد عام؛ يمكنه الإجابة عن الأسئلة العامة والبرمجة والشرح والمواضيع المختلفة، وليس محصورًا في بيانات Cosmo Coins.
- عند طلب تنفيذ أمر، يجب تمريره إلى أمر البوت الأصلي حتى تُطبق صلاحيات Discord والصلاحيات الداخلية نفسها.
- قراءة وتعديل الكود متاحان من ملفات البوت الأساسي فقط؛ مجلد second_bot والأسرار وقواعد البيانات خارج النطاق.

استخدم هذه المعلومات عندما يسأل المستخدم عن البوت أو مطوره أو وظائفه. لا تخترع تفاصيل غير مذكورة هنا عن المطور أو البوت؛ وإذا احتاج السؤال إلى معلومة تنفيذية دقيقة غير متاحة، صرّح بعدم التأكد واقترح استخدام أوامر البوت المناسبة. هذه المعلومات سياق تعريفي فقط ولا تقيدك عن الإجابة في المواضيع العامة."""

AI_SYSTEM_INSTRUCTION = f"""أنت مساعد ذكي عام مدمج داخل سيرفر ديسكورد. أجب بطريقة طبيعية ومباشرة وودودة باللغة العربية عند الإمكان، وساعد الأعضاء بذكاء واحترافية.

{BOT_KNOWLEDGE_CONTEXT}"""


class GeminiAI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # Groq is the primary provider; Gemini is the first fallback.
        groq_key = os.getenv("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        self.groq_client = None
        if not groq_key:
            print("⚠️ [Groq] لم يتم العثور على GROQ_API_KEY في ملف .env")
        else:
            self.groq_client = AsyncOpenAI(
                api_key=groq_key,
                base_url="https://api.groq.com/openai/v1",
            )

        # Gemini fallback
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("⚠️ [GeminiAI] لم يتم العثور على GEMINI_API_KEY في ملف .env")
            self.client = None
        else:
            self.client = genai.Client(api_key=api_key)

        # Optional third fallback through OpenRouter
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

    @staticmethod
    def is_primary_code_path(path: Path) -> bool:
        """Keep AI code access inside the primary bot and outside secrets/state."""
        try:
            relative = path.resolve().relative_to(PRIMARY_REPO_ROOT)
        except ValueError:
            return False
        if any(part in AI_CODE_EXCLUDED_DIRS for part in relative.parts):
            return False
        if path.name in AI_CODE_EXCLUDED_FILES or path.name.startswith(".env"):
            return False
        return path.suffix.lower() in AI_CODE_EXTENSIONS

    def primary_code_inventory(self) -> str:
        paths = []
        for path in PRIMARY_REPO_ROOT.rglob("*"):
            if path.is_file() and self.is_primary_code_path(path):
                paths.append(str(path.relative_to(PRIMARY_REPO_ROOT)))
        return "\n".join(sorted(paths))

    def primary_code_context(self, prompt: str, *, max_chars: int = 30_000) -> str:
        """Provide relevant primary-bot source without exposing second_bot or secrets."""
        terms = {
            term.casefold() for term in prompt.split()
            if len(term) >= 3 and term.isascii()
        }
        candidates = []
        for path in PRIMARY_REPO_ROOT.rglob("*"):
            if not path.is_file() or not self.is_primary_code_path(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            score = sum(text.casefold().count(term) for term in terms)
            if score:
                candidates.append((score, path, text))
        candidates.sort(key=lambda item: (-item[0], str(item[1])))
        chunks = ["ملفات البوت الأساسي المتاحة للـAI:\n" + self.primary_code_inventory()]
        remaining = max_chars
        for _score, path, text in candidates:
            if remaining <= 0:
                break
            chunk = f"\n\n===== {path.relative_to(PRIMARY_REPO_ROOT)} =====\n{text}"
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
        return "".join(chunks)

    async def classify_request(self, prompt: str) -> dict[str, str]:
        """Classify a request before any command or code action is attempted."""
        command_catalog = "\n".join(
            f"!{command.qualified_name}: {command.description or ''}"
            for command in self.bot.commands
            if command.qualified_name not in {"ask", "reset_chat"}
        )
        classifier_prompt = (
            f"{AI_ACTION_PROMPT}\n\nالأوامر الموجودة فعلياً:\n{command_catalog}\n\n"
            f"طلب المستخدم:\n{prompt}"
        )
        messages = [
            {"role": "system", "content": AI_ACTION_PROMPT},
            {"role": "user", "content": classifier_prompt},
        ]

        # Try providers in priority order; invalid output also triggers fallback.
        for provider in ("Groq", "Gemini", "OpenRouter"):
            try:
                if provider == "Groq" and self.groq_client:
                    response = await self.groq_client.chat.completions.create(
                        model=self.groq_model,
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=0.1,
                        max_tokens=300,
                    )
                    raw = response.choices[0].message.content or ""
                elif provider == "Gemini" and self.client:
                    response = await self.client.aio.models.generate_content(
                        model="gemini-3.8-flash",
                        contents=classifier_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=AI_ACTION_PROMPT,
                            response_mime_type="application/json",
                            temperature=0,
                        ),
                    )
                    raw = response.text or ""
                elif provider == "OpenRouter" and self.backup_client:
                    response = await self.backup_client.chat.completions.create(
                        model="meta-llama/llama-3.3-70b-instruct:free",
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=0.1,
                        max_tokens=300,
                    )
                    raw = response.choices[0].message.content or ""
                else:
                    continue

                result = json.loads(raw.strip().strip("`").replace("json\n", "", 1))
                action = result.get("action")
                if action not in {"chat", "command", "code_change"}:
                    raise ValueError("AI returned an unsupported action")
                return {
                    "action": action,
                    "command": str(result.get("command") or "").strip(),
                    "summary": str(result.get("summary") or "").strip(),
                }
            except Exception as exc:
                print(f"[DEBUG] {provider} request classification failed: {exc}")

        return {"action": "chat", "command": "", "summary": ""}

    async def invoke_existing_command(self, ctx: commands.Context, command_text: str) -> bool:
        """Invoke a real bot command so its normal checks and inline guards still run."""
        if not command_text.startswith("!"):
            return False
        command_name = command_text[1:].split(maxsplit=1)[0].casefold()
        if command_name == "ask":
            return False
        command = self.bot.get_command(command_name)
        if command is None:
            return False
        proxy_message = copy.copy(ctx.message)
        proxy_message.content = command_text
        command_ctx = await self.bot.get_context(proxy_message)
        if command_ctx.command is None:
            return False
        try:
            if not await command_ctx.command.can_run(command_ctx):
                await ctx.send("⛔ لا تملك الصلاحية المطلوبة لتنفيذ هذا الأمر.")
                return True
        except commands.CheckFailure:
            await ctx.send("⛔ لا تملك الصلاحية المطلوبة لتنفيذ هذا الأمر.")
            return True
        await self.bot.invoke(command_ctx)
        return True

    async def generate_code_patch(self, prompt: str) -> str:
        """Ask the model for a unified diff restricted to primary-bot source files."""
        instruction = f"""أنت محرر كود للمستودع الأساسي فقط.
أعد unified diff صالحاً فقط، من دون Markdown أو شرح، لتطبيق التعديل المطلوب.
ممنوع تعديل second_bot أو .env أو قواعد البيانات أو الملفات الثنائية.
إذا لم يحتج الطلب تعديلاً برمجياً أعد كلمة NO_PATCH.

الطلب:
{prompt}

السياق المتاح:
{self.primary_code_context(prompt)}
"""
        if self.groq_client:
            try:
                response = await self.groq_client.chat.completions.create(
                    model=self.groq_model,
                    messages=[{"role": "user", "content": instruction}],
                    temperature=0.1,
                    max_tokens=4000,
                )
                patch = response.choices[0].message.content or ""
                if patch:
                    return patch
            except Exception as exc:
                print(f"[DEBUG] Groq code generation failed; trying Gemini: {exc}")
        if self.client:
            try:
                response = await self.client.aio.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=instruction,
                    config=types.GenerateContentConfig(temperature=0.1),
                )
                patch = response.text or ""
                if patch:
                    return patch
            except Exception as exc:
                print(f"[DEBUG] Gemini code generation failed; trying OpenRouter: {exc}")
        return await self.ask_backup_ai(instruction)

    def apply_primary_code_patch(self, patch_text: str) -> tuple[bool, str]:
        """Validate and apply a model patch without allowing access outside the primary bot."""
        patch = patch_text.strip()
        if patch.startswith("```"):
            lines = patch.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            patch = "\n".join(lines).strip()
        if not patch or patch == "NO_PATCH":
            return False, "لم يقترح AI تعديلاً قابلاً للتطبيق."

        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")):
                raw_path = line[4:].split("\t", 1)[0]
                if raw_path == "/dev/null":
                    continue
                relative = raw_path[2:] if raw_path.startswith(("a/", "b/")) else raw_path
                if not self.is_primary_code_path(PRIMARY_REPO_ROOT / relative):
                    return False, "رفضت التعديل لأن مساره خارج ملفات البوت الأساسي المسموح بها."

        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=PRIMARY_REPO_ROOT,
            input=patch,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()[-800:]
            return False, f"تعذر التحقق من التعديل: {detail}"
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=PRIMARY_REPO_ROOT,
            input=patch,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if applied.returncode != 0:
            return False, f"فشل تطبيق التعديل: {(applied.stderr or applied.stdout).strip()[-800:]}"
        return True, "تم تطبيق التعديل على كود البوت الأساسي."

    async def handle_request(self, ctx: commands.Context, prompt: str) -> bool:
        """Route an AI request through native command permissions or owner-only code editing."""
        intent = await self.classify_request(prompt)
        if intent["action"] == "command":
            if await self.invoke_existing_command(ctx, intent["command"]):
                return True
            await ctx.send("⛔ لم أستطع تنفيذ الطلب كأمر موجود، أو أن الأمر غير مسموح في هذا السياق.")
            return True
        if intent["action"] == "code_change":
            if ctx.author.id != PRIMARY_OWNER_ID:
                await ctx.send("⛔ تعديل كود البوت الأساسي متاح لمالك البوت فقط.")
                return True
            try:
                success, result = self.apply_primary_code_patch(
                    await self.generate_code_patch(prompt)
                )
            except Exception as exc:
                print(f"[DEBUG] AI code change failed: {exc}")
                success, result = False, "حدث خطأ أثناء إنشاء أو تطبيق تعديل الكود."
            await ctx.send(("✅ " if success else "❌ ") + result)
            return True
        return False

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

    async def ask_backup_ai(self, prompt: str) -> str:
        """Use OpenRouter as an optional third provider after Groq and Gemini."""
        if not self.backup_client:
            return "⚠️ تعذر توليد الرد من Groq وGemini، كما أن OPENROUTER_API_KEY غير مضبوط للمزوّد الثالث."
        try:
            print("🔄 [Fallback] Groq وGemini غير متاحين؛ جاري تجربة OpenRouter...")
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

    async def ask_gemini_ai(self, prompt: str, *, temperature: float = 0.7) -> str:
        """Generate a response from Gemini after the primary Groq provider fails."""
        response = await self.client.aio.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=AI_SYSTEM_INSTRUCTION,
                temperature=temperature,
            ),
        )
        return response.text or ""

    async def generate_response(
        self, user_id: int, prompt: str, guild_id: int | None = None, channel_id: int | None = None
    ) -> str:
        conversation_key = (int(guild_id or 0), int(channel_id or 0), int(user_id))
        if not self.groq_client and not self.client and not self.backup_client:
            return "❌ خدمة الذكاء الاصطناعي غير مفعلة؛ يرجى التأكد من وضع المفاتيح في ملف `.env`."

        history = self.user_chats.setdefault(
            conversation_key,
            [{"role": "system", "content": AI_SYSTEM_INSTRUCTION}],
        )
        history.append({"role": "user", "content": prompt})
        fallback_prompt = "\n\n".join(
            f"{'المستخدم' if item['role'] == 'user' else 'المساعد'}: {item['content']}"
            for item in history
            if item["role"] != "system"
        )

        if self.groq_client:
            try:
                response = await self.groq_client.chat.completions.create(
                    model=self.groq_model,
                    messages=history,
                    temperature=0.7,
                    max_tokens=1500,
                )
                answer = response.choices[0].message.content or ""
                if answer:
                    history.append({"role": "assistant", "content": answer})
                    return answer
            except Exception as exc:
                print(f"[DEBUG] Groq Error; trying Gemini fallback: {exc}")

        if self.client:
            try:
                answer = await self.ask_gemini_ai(fallback_prompt)
                if answer:
                    history.append({"role": "assistant", "content": answer})
                    return answer
            except Exception as exc:
                print(f"[DEBUG] Gemini Error; trying OpenRouter fallback: {exc}")

        if self.backup_client:
            answer = await self.ask_backup_ai(fallback_prompt)
            if not answer.startswith("⚠️"):
                history.append({"role": "assistant", "content": answer})
                return answer
            history.pop()
            return answer

        history.pop()
        return "⚠️ تعذر توليد الرد من Groq وGemini. تحقق من مفاتيح API وحالة المزوّدين."

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

        if await self.handle_request(ctx, prompt):
            return
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

            proxy_message = copy.copy(message)
            proxy_message.content = f"!ask {clean_content}"
            proxy_ctx = await self.bot.get_context(proxy_message)
            if await self.handle_request(proxy_ctx, clean_content):
                return

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
