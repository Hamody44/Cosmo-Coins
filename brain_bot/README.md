# Brain Bot

Brain Bot هو مستوى التحكم المستقل في Cosmo-Coins. لا يحمل أي Discord token ولا ينفذ أوامر المستخدمين؛ وظيفته إدارة دورة حياة البوت الأساسي و`second_bot` عبر طلبات JSON محلية آمنة.

## البروتوكول

- `requests/inbox/`: طلبات pending، ملف JSON واحد لكل طلب.
- `requests/processing/`: الطلب الذي أخذه المشرف حاليًا.
- `requests/done/` و`requests/failed/`: سجل قصير للنتيجة.
- `second_bot/.runtime/process_control/status.json`: PID/heartbeat وحالة اتصال كل بوت.

تتم الكتابة إلى ملف مؤقت ثم `os.replace`، ويُستخدم قفل ملف على Linux. لذلك لا يؤدي طلبان متزامنان إلى الكتابة فوق بعضهما كما كان يحدث مع ملف request واحد.

## الصلاحيات

كل طلب يحتوي `requester_id`. تقبل الطبقة فقط `start`, `restart`, `shutdown` للهدفين `primary` و`second_bot`، وتطابق `requester_id` مع `PRIMARY_OWNER_ID` قبل إدخاله إلى الطابور. أوامر Discord نفسها محمية أيضًا بـ`commands.is_owner()`؛ التحقق المزدوج مقصود حتى لا تكون حماية Discord هي الحاجز الوحيد.

## الحالة

يسجل كل بوت `connected` و`disconnected` عند أحداث Gateway، ويسجل المشرف heartbeat مستقلًا. أمر `!status` يعرض حالة العملية والحالة المركزية دون عرض أي سر أو token.

## الاختبار اليدوي

```bash
PYTHONPATH=. python3 -m brain_bot request start second_bot 988840428591808584
PYTHONPATH=. python3 -m brain_bot status
bash run_bots.sh control status second_bot
```
