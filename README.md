# Cosmo-Coins

## العمليات الثلاث

يشغّل `run_bots.sh` ثلاث عمليات مستقلة: البوت الأساسي `primary`، والبوت `second_bot`، وBrain Bot. لكل عملية PID وملف سجل وبيئة Python افتراضية خاصة بها. يستخدم `second_bot` بيئته الحالية المبنية على `discord.py-self`؛ أما Brain Bot فيعمل على `discord.py` العادي وبـDiscord Bot Token مستقل. لا تُشارك التوكنات بين العمليات.

## إعداد المتغيرات

انسخ `.env.example` إلى `.env` محليًا وأكمل القيم المطلوبة. لا تضف `.env` أو أسرارًا إلى Git:

- `DISCORD_BOT_TOKEN`: توكن البوت الأساسي.
- `SECOND_BOT_TOKEN`: توكن/بيانات حساب second_bot وفق تشغيله الحالي.
- `BRAIN_BOT_TOKEN`: توكن مستقل أنشئه لتطبيق Brain Bot منفصل في Discord Developer Portal، مع تفعيل Message Content Intent.
- `PRIMARY_OWNER_ID`: معرّف Discord الرقمي لمالك البوت الأساسي، وهو المستخدم الوحيد المخوّل بأوامر Brain Bot.
- `BRAIN_BOT_PREFIX`: بادئة Brain Bot، الافتراضي `#` (مقابل `!` للبوت الأساسي و`$` لـsecond_bot).
- `COSMO_AUTO_DELIVERY_SECRET`: سر HMAC مشترك بين البيئة الأساسية و`second_bot/.env` لتوقيع handoff؛ يجب أن تتطابق القيمتان ولا تُنشران.

يتطلب Brain Bot `discord.py` العادي عبر `brain_bot/requirements.txt`. يثبّت المشرف متطلباته في `.venv` مستقلة داخل `brain_bot/`. تبقى بيئة ومتطلبات `second_bot` مستقلة ولا تُضاف إليها `numpy` أو `python-dotenv`.

## أوامر Brain Bot

مالك `PRIMARY_OWNER_ID` فقط يمكنه استخدام:

```text
#start primary
#start second_bot
#restart primary
#restart second_bot
#shutdown primary
#shutdown second_bot
#status
```

استبدل `#` إذا غيّرت `BRAIN_BOT_PREFIX`. ينشئ Brain Bot لكل طلب ملف JSON مستقلًا وذرّيًا في `requests/inbox/`؛ ثم ينقله المشرف إلى `requests/processing/` ويُنهيه في `requests/done/` أو `requests/failed/`. يشمل `#status` حالة الاتصال، PID، heartbeat، اتصال Gateway لكل بوت، وآخر خطأ متاح من سجلات العمليات.

لم تعد أوامر self اليدوية أو أوامر التحكم في `second_bot` موجودة في البوت الأساسي. أوامر second_bot ذات `$`، بما فيها `$numbers` ومسارا type1/type2، تبقى ضمن second_bot. **التسليم التلقائي** وsigned provider handoff وانتظار التأكيد والإبلاغ عن النتيجة وحجز/إرجاع الرصيد وإعادة المحاولة تبقى كما هي؛ ويظل البوت الأساسي قادرًا على تشغيل second_bot آليًا عند الحاجة للتسليم.

## التشغيل والإدارة

شغّل العمليات الثلاث من جذر المشروع:

```bash
bash run_bots.sh
```

يُنشئ المشرف بيئات Python عند أول تشغيل، ويسجّل PID في `second_bot/.runtime/process_control/{primary,second_bot,brain_bot}.pid`، والسجلات في المجلد نفسه بصيغة `{name}.log`. الحالة والاتصالات في `status.json`، والطلبات في مجلدات `requests/` أعلاه. يمكن الاستعلام محليًا عبر `python3 -m brain_bot status`، وإرسال طلب محلي عبر `bash run_bots.sh control start second_bot` (يتطلب `PRIMARY_OWNER_ID` في `.env`).

تُرسل طلبات الإيقاف وإعادة التشغيل عبر Brain Bot؛ عند إيقاف المشرف نفسه استخدم `Ctrl+C` أو `SIGTERM`، فيرسل إشارة إنهاء للعمليات الثلاث وينظف ملفات PID. التغييرات محلية؛ لا تُرفع إلى GitHub تلقائيًا.
