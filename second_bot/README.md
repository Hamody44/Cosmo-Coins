# البوت الثاني: `!say` و`!ping`

هذا بوت Discord مستقل بلغة Python باستخدام `discord.py`. يوجد بالكامل داخل `second_bot/` ولا يعدّل ملفات بوت Cosmo-Coins أو يستورد منها. عند كتابة `!say hi` يحذف رسالة الأمر ثم يرسل `hi`. يسمح باستخدامه لمالك تطبيق البوت أو لأي عضو يحمل الرتبة المحددة في `SAY_ALLOWED_ROLE_ID`. يعرض الأمر `!ping` حالة الاتصال وزمن استجابة Discord بالميلي ثانية.

## الإعداد

1. أنشئ تطبيقًا وبوتًا منفصلين من [Discord Developer Portal](https://discord.com/developers/applications)، ثم انسخ رمز البوت الخاص بهذا التطبيق فقط.
2. انسخ `second_bot/.env.example` إلى `second_bot/.env`، ثم أدخل `SECOND_BOT_TOKEN` و`SAY_ALLOWED_ROLE_ID`. يقبل البرنامج أيضًا الاسم القديم `DISCORD_BOT_TOKEN`، ويفضّل القيمة الموجودة في `.env` الخاص بهذا البوت على متغيرات البيئة العامة لتفادي استخدام توكن Cosmo-Coins بالخطأ. قيمة الرتبة هي **Role ID** من السيرفر، وليست اسمها. فعّل Developer Mode في Discord ثم اختر Copy ID. لا تشارك الرمز ولا تضعه في Git.
3. من صفحة التطبيق، افتح **Bot → Privileged Gateway Intents** وفعّل **Message Content Intent**؛ يحتاج البوت إلى قراءة محتوى أوامر البادئة `!`.
4. ادعُ البوت إلى السيرفر عبر OAuth2 باستخدام النطاق `bot`، مع الصلاحيات المذكورة أدناه.

## الصلاحيات المطلوبة

- **View Channels** و**Send Messages** في القنوات التي سيعمل فيها.
- **Manage Messages** حتى يتمكن من حذف رسالة الأمر التي كتبها المستخدم.
- في سلاسل النقاش (Threads)، امنحه أيضًا **Send Messages in Threads**.
- لا يحتاج البوت إلى صلاحية `Administrator`. إذا لم تتوفر صلاحية الإرسال، فسيسجّل ذلك في الطرفية لأن Discord لن يسمح له بإرسال رسالة خطأ داخل القناة.
- المطلوب هو **Bot Token** من تبويب **Bot** في Developer Portal. لا يعمل `Application ID` أو `Client Secret` أو توكن مستخدم بدلًا منه؛ يمكن إدخال Bot Token الخام أو مع بادئة `Bot ` اختيارية.

يستخدم البوت `discord.AllowedMentions.none()` لكل ما يرسله، لذلك لا يؤدي النص إلى تنبيه أشخاص أو أدوار أو `@everyone`.

## تثبيت التبعيات

تُثبّت كل مجموعة متطلبات في بيئة افتراضية مستقلة لتجنب تعارض تبعيات البوتين. من جذر المستودع:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

python3 -m venv second_bot/.venv
second_bot/.venv/bin/python -m pip install --upgrade pip
second_bot/.venv/bin/python -m pip install -r second_bot/requirements.txt
```

قبل التشغيل، أنشئ أيضًا ملف `.env` الخاص ببوت Cosmo-Coins في جذر المستودع وفق إعداداته الحالية، إن لم يكن موجودًا بالفعل. لا تستبدل ملف `.env` قائمًا.

## تشغيل البوتين معًا

من جذر المستودع:

```bash
chmod +x run_bots.sh
./run_bots.sh
```

يشغّل السكربت `main.py` الخاص بـ Cosmo-Coins من جذر المستودع، ثم يشغّل `second_bot/main.py` من مجلد `second_bot/`؛ وبذلك يقرأ كل بوت ملف `.env` الخاص بمجلده وبيئته الافتراضية. إذا لم توجد بيئة افتراضية لأحدهما، يستخدم السكربت `python3` المتاح. أوقف البوتين معًا باستخدام `Ctrl+C`.

يمكن تشغيل البوت الثاني وحده للاختبار:

```bash
cd second_bot
.venv/bin/python main.py
```

## سلوك الأمر والأخطاء

- `!say hi` يحذف رسالة الأمر ويرسل `hi` دون تفعيل المنشنات.
- `!ping` يعرض أن البوت متصل وزمن استجابة Gateway الحالي بالميلي ثانية.
- يرفض النص الفارغ أو الذي يتجاوز 2000 حرف.
- تُرفض محاولة الاستخدام من شخص ليس مالك البوت ولا يحمل الرتبة المحددة.
- يتطلب حذف أمر العضو `Manage Messages`. عند نقصها تظهر رسالة واضحة ما دام للبوت إذن الإرسال.
- تُعالج أخطاء الأمر وDiscord وتسجّل في الطرفية بدل إيقاف البوت.

## اختبارات الوحدة

الاختبارات مكتوبة باستخدام `unittest` ولا تتصل بـDiscord ولا تحتاج رمز بوت حقيقي. بعد تثبيت متطلبات البوت الثاني، شغّلها من داخله:

```bash
cd second_bot
.venv/bin/python -m unittest discover -s tests -v
```

تغطي اختبارات الإعدادات، صلاحيات `!say`، حذف الرسالة، النص الفارغ وحد 2000 حرف، معالجة أخطاء الأمر، وتعطيل المنشنات ونتيجة `!ping`.
