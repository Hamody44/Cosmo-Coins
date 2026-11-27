# Brain Bot مستقل

Brain Bot عميل Discord Bot حقيقي مستقل يعمل باستخدام `discord.py` العادي. يستخدم `BRAIN_BOT_TOKEN` الخاص به، ولا يستعير توكن primary أو second_bot. لا يعمل كـself-bot ولا كطبقة CLI فقط: `run_bots.sh` يبدأ `python -m brain_bot.main` كعملية ثالثة مستقلة ببيئة Python خاصة.

## الإعداد

اضبط في ملف `.env` الجذري:

```dotenv
BRAIN_BOT_TOKEN=
PRIMARY_OWNER_ID=
BRAIN_BOT_PREFIX=#
```

أنشئ تطبيقًا وبوتًا منفصلين في Discord Developer Portal وأضف البوت للسيرفر مع Message Content Intent. لا تُخزّن التوكن الحقيقي في الملفات أو Git. يتطلب التشغيل تثبيت `brain_bot/requirements.txt` (discord.py). يظل `second_bot` على `discord.py-self` وبيئته المنفصلة.

## الصلاحيات والأوامر

كل الأوامر تفحص `ctx.author.id == PRIMARY_OWNER_ID`، كما يتحقق البروتوكول من requester ID مرة أخرى. الأوامر: `#start primary|second_bot`، `#restart primary|second_bot`، `#shutdown primary|second_bot`، و`#status`. بادئة الأوامر قابلة للضبط.

## الطلبات والحالة

لكل عملية JSON UUID مستقل في `requests/inbox/`. تُكتب إلى ملف مؤقت ثم تُنشر بـ`os.replace` تحت قفل؛ يسحب المشرف كل ملف ذريًا إلى `processing/` ثم ينقله إلى `done/` أو `failed/`. لا يمكن لطلب أن يستبدل طلبًا آخر. الحالة وheartbeat واتصال Gateway تحفظ في `second_bot/.runtime/process_control/status.json`، مع PID وسجلات منفصلة لكل عملية في المجلد نفسه.
