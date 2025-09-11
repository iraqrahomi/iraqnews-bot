# IraqNews Bot — جامع أخبار العراق بدون أي API

بوت بايثون يجمع أخبار العراق من مصادر متعددة عبر RSS أو Scrape بدون استخدام أي واجهات API خاصة بالمواقع.
يقوم بحفظ الأخبار في قاعدة بيانات SQLite ويرسلها إلى تيليجرام أو يحفظها في ملفات Markdown/HTML، مع نظام ذكي لمنع التكرار.

## المميزات
- جمع الأخبار من مصادر عراقية وعالمية (RSS وScrape).
- منع التكرار باستخدام بصمات عنوان/محتوى والتحقق من التشابه.
- إرسال تلقائي إلى قناة تيليجرام أو حفظ محليًا.
- دعم اللغة العربية والإنجليزية.

## الإعداد والتشغيل
1. استنسخ المستودع:
   ```bash
   git clone https://github.com/username/iraqnews-bot.git
   cd iraqnews-bot
   ```
2. أنشئ بيئة عمل وثبّت المتطلبات:
   ```bash
   python -m venv venv
   source venv/bin/activate   # على لينكس
   venv\Scripts\activate    # على ويندوز
   pip install -r requirements.txt
   ```
3. إعداد متغيرات البيئة (اختياري للتيليجرام):
   ```bash
   export TG_TOKEN=123:ABC
   export TG_CHAT_ID=123456789
   ```
4. تشغيل مرة واحدة:
   ```bash
   python news_bot.py --once
   ```
   أو تشغيل دائمًا (كل 15 دقيقة):
   ```bash
   python news_bot.py
   ```

## بنية المشروع
```
iraqnews-bot/
├── news_bot.py
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```
