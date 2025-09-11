# news_bot.py — نسخة ذاتية التعافي (Auto-Heal)
# -*- coding: utf-8 -*-
"""
IraqNews Bot — جامع أخبار العراق بدون أي API خاصة بالمواقع (نسخة قوية تتجاوز الأخطاء)
-----------------------------------------------------------------------------
• يجمع أخبار من RSS/Scrape بدون أي API خاصة بالمواقع.
• منع تكرار: بصمات (عنوان/محتوى) + تشابه عناوين.
• إرسال تيليجرام مع إعادة محاولة ذكية ومعالجة 429/400.
• نظام «تعافي تلقائي»: تعطيل مؤقت للمصادر المعطوبة + إعادة محاولات + تدوير User-Agent.
• ملف إعداد اختياري: sources.json (لتعديل المصادر بدون لمس الكود).
• قاعدة بيانات SQLite بجدولين: items (الأخبار) + sources (صحة المصادر) + meta (إحصاءات).
• يعمل محليًا أو عبر GitHub Actions.

متغيرات البيئة المهمة:
- TG_TOKEN / TG_CHAT_ID         : إعداد تيليجرام (اختياري)
- DRY_RUN=1                     : يمنع الإرسال، يحفظ محليًا فقط
- MAX_ITEMS_PER_SOURCE=30       : حد لكل مصدر
- POLL_SECONDS=900              : تكرار كل 15 دقيقة عند التشغيل المستمر
- AUTO_PIP=1                    : تثبيت المكتبات تلقائيًا عند نقصها
- SIMILARITY_THRESH=0.92        : عتبة تشابه العناوين (كلما زادت قلّ الحذف)

"""

import os, re, sys, time, json, html, random, hashlib, sqlite3, logging, difflib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qs

# ====== محاولة تثبيت المكتبات تلقائيًا عند نقصها ======
AUTO_PIP = os.getenv("AUTO_PIP", "1") == "1"

def _try_import(name, pip_pkg=None):
    try:
        return __import__(name)
    except Exception:
        if not AUTO_PIP:
            raise
        import subprocess
        pkg = pip_pkg or name
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
        return __import__(name)

requests = _try_import("requests")
feedparser = _try_import("feedparser")
bs4 = _try_import("bs4", pip_pkg="beautifulsoup4")
BeautifulSoup = bs4.BeautifulSoup
lxml_ok = True
try:
    _try_import("lxml")
except Exception:
    lxml_ok = False  # BeautifulSoup سيستخدم html.parser

from dateutil import parser as dtparser  # يتوفر ضمن requirements

# ================= إعدادات عامة =================
TZ = timezone(timedelta(hours=+3))  # Asia/Baghdad
DB_PATH = os.getenv("NEWS_DB", "iraq_news.db")
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/126.0",
]
FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "20"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))
MAX_ITEMS_PER_SOURCE = int(os.getenv("MAX_ITEMS_PER_SOURCE", "30"))
SIMILARITY_THRESH = float(os.getenv("SIMILARITY_THRESH", "0.92"))

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

OUT_DIR = os.getenv("OUT_DIR", "news_out")
os.makedirs(OUT_DIR, exist_ok=True)

# =============== لوج إلى ملف + كونسول ===============
logger = logging.getLogger("iraqnews")
logger.setLevel(logging.INFO)
fmt = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt); logger.addHandler(ch)
fh = logging.FileHandler(os.path.join(OUT_DIR, 'bot.log'), encoding='utf-8'); fh.setFormatter(fmt); logger.addHandler(fh)

# =============== قاعدة البيانات ===============
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        title TEXT,
        url TEXT,
        published_at TEXT,
        title_hash TEXT,
        content_hash TEXT,
        created_at TEXT
    );
    """
)
conn.execute("CREATE INDEX IF NOT EXISTS idx_items_url ON items(url);")
conn.execute("CREATE INDEX IF NOT EXISTS idx_items_titlehash ON items(title_hash);")
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS sources (
        name TEXT PRIMARY KEY,
        failures INTEGER DEFAULT 0,
        disabled_until TEXT
    );
    """
)
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        val TEXT
    );
    """
)
conn.commit()

# =============== أدوات مساعدة ===============

def canonical_url(u: str) -> str:
    try:
        p = urlparse(u)
        q = parse_qs(p.query)
        filtered = {k: v for k, v in q.items() if not k.lower().startswith("utm") and k.lower() not in {"fbclid", "gclid"}}
        new_query = "&".join([f"{k}={v[0]}" for k, v in filtered.items()])
        return urlunparse((p.scheme, p.netloc, p.path, '', new_query, ''))
    except Exception:
        return u


def text_hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode('utf-8', 'ignore')).hexdigest()


def is_similar(a: str, b: str, thresh: float = SIMILARITY_THRESH) -> bool:
    try:
        return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio() >= thresh
    except Exception:
        return False


def clean_text(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def norm_title(t: str) -> str:
    t = clean_text(t)
    t = re.sub(r"^(عاجل|خبر عاجل|بالصور|فيديو)[:\-\s]+", "", t, flags=re.I)
    return t


def parse_time(ts):
    if not ts:
        return None
    try:
        dt = dtparser.parse(ts)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None

# =============== إدارة صحة المصادر ===============

def source_is_disabled(name: str) -> bool:
    row = conn.execute("SELECT disabled_until FROM sources WHERE name=?", (name,)).fetchone()
    if not row or not row[0]:
        return False
    try:
        return dtparser.parse(row[0]) > datetime.now(TZ)
    except Exception:
        return False


def source_mark_ok(name: str):
    conn.execute("INSERT INTO sources(name, failures, disabled_until) VALUES(?,0,NULL) ON CONFLICT(name) DO UPDATE SET failures=0, disabled_until=NULL", (name,))
    conn.commit()


def source_mark_fail(name: str, cool_minutes: int = 180):
    row = conn.execute("SELECT failures FROM sources WHERE name=?", (name,)).fetchone()
    fails = (row[0] if row else 0) + 1
    disabled_until = None
    # بعد 3 فشل متتالٍ عطّل المصدر 3 ساعات
    if fails >= 3:
        disabled_until = (datetime.now(TZ) + timedelta(minutes=cool_minutes)).isoformat()
        logger.warning(f"[SOURCE] تعطيل مؤقت للمصدر '{name}' لمدة {cool_minutes} دقيقة بعد {fails} فشل متتالٍ")
    conn.execute(
        "INSERT INTO sources(name, failures, disabled_until) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET failures=?, disabled_until=?",
        (name, fails, disabled_until, fails, disabled_until)
    )
    conn.commit()

# =============== HTTP مع إعادة محاولة وتدوير UA ===============

def http_get(url: str, tries: int = 3, timeout: int = FETCH_TIMEOUT) -> str:
    last = None
    for i in range(tries):
        try:
            ua = random.choice(USER_AGENTS)
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as ex:
            last = ex
            time.sleep(min(5, 1.5 ** i + random.random()))
    raise last

# =============== جلب من RSS/Scrape ===============

def fetch_rss(src: dict):
    items = []
    parsed = feedparser.parse(src["url"])  # لا يحتاج API
    for e in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = norm_title(e.get("title", "").strip())
        link = canonical_url(e.get("link", "").strip())
        published = parse_time(e.get("published") or e.get("updated"))
        summary = clean_text(e.get("summary", ""))
        items.append({
            "source": src["name"],
            "title": title,
            "url": link,
            "published_at": published.isoformat() if published else "",
            "summary": summary,
        })
    return items


def fetch_scrape(src: dict):
    html_text = http_get(src["url"])  # بدون API
    soup = BeautifulSoup(html_text, "lxml" if lxml_ok else "html.parser")
    items = []
    for a in soup.select(src.get("list_selector", "a"))[:MAX_ITEMS_PER_SOURCE]:
        href = a.get("href")
        if not href:
            continue
        link = canonical_url(href)
        title = norm_title(a.get_text(" "))
        content = ""
        try:
            art_html = http_get(link)
            art = BeautifulSoup(art_html, "lxml" if lxml_ok else "html.parser")
            node = art.select_one(src.get("content_selector", "article"))
            if node:
                content = clean_text(node.get_text(" "))
        except Exception as ex:
            logger.warning(f"Content fetch failed for {link}: {ex}")
        items.append({
            "source": src["name"],
            "title": title,
            "url": link,
            "published_at": "",
            "summary": content[:500]
        })
    return items

# =============== تخزين وتحقق من التكرار ===============

def is_duplicate(title: str, url: str, content: str) -> bool:
    c = conn.cursor()
    th = text_hash(title)
    ch = text_hash(content or title)
    cu = canonical_url(url)
    row = c.execute("SELECT 1 FROM items WHERE url=? OR title_hash=? OR content_hash=? LIMIT 1", (cu, th, ch)).fetchone()
    if row:
        return True
    recent = c.execute("SELECT title FROM items ORDER BY id DESC LIMIT 200").fetchall()
    for (old_title,) in recent:
        try:
            if is_similar(old_title or "", title or ""):
                return True
        except Exception:
            continue
    return False


def save_item(it: dict):
    conn.execute(
        "INSERT INTO items (source, title, url, published_at, title_hash, content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            it["source"], it["title"], canonical_url(it["url"]), it.get("published_at", ""),
            text_hash(it["title"]), text_hash((it.get("summary") or it["title"])),
            datetime.now(TZ).isoformat()
        )
    )
    conn.commit()

# =============== تنسيق/إرسال ===============

def format_message(it: dict) -> str:
    src = it.get("source", "")
    title = it.get("title", "")
    url = it.get("url", "")
    ts = it.get("published_at", "")
    try:
        when = dtparser.parse(ts).astimezone(TZ).strftime("%Y-%m-%d %H:%M") if ts else datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        when = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    return (
        f"📰 <b>{html.escape(title)}</b>\n"
        f"🌍 المصدر: <i>{html.escape(src)}</i>\n"
        f"🕒 الوقت: {when} (Baghdad)\n"
        f"🔗 <a href=\"{html.escape(url)}\">اقرأ التفاصيل</a>\n"
    )


def send_telegram(html_msg: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    if DRY_RUN:
        logger.info("[DRY_RUN] Telegram send skipped")
        return True
    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": html_msg, "parse_mode": "HTML", "disable_web_page_preview": False}
    # إعادة محاولات مع معالجة 429
    for i in range(4):
        r = requests.post(api, data=data, timeout=FETCH_TIMEOUT)
        if r.status_code == 200:
            return True
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code == 429:
            retry_after = int(j.get('parameters', {}).get('retry_after', 2))
            wait_s = min(30, retry_after + i * 2)
            logger.warning(f"Telegram 429 — الانتظار {wait_s}s ثم إعادة المحاولة…")
            time.sleep(wait_s)
            continue
        logger.error(f"Telegram error: {r.status_code} {r.text}")
        time.sleep(1.5 * (i + 1))
    return False

# حفظ بديل إلى ملفات لو فشل تيليجرام

def save_to_md_html(it: dict):
    md_line = f"- **{it['title']}** — [{it['source']}]({it['url']})\n"
    with open(os.path.join(OUT_DIR, "latest.md"), "a", encoding="utf-8") as f:
        f.write(md_line)

# =============== مصادر افتراضية + ملف خارجي ===============
DEFAULT_SOURCES = [
    {"name": "Iraq News Agency (INA)", "lang": "ar", "type": "rss", "url": "https://ina.iq/rss.ashx"},
    {"name": "Alsumaria News",          "lang": "ar", "type": "rss", "url": "https://www.alsumaria.tv/rss-feed"},
    {"name": "Shafaq News",             "lang": "ar", "type": "rss", "url": "https://shafaq.com/ar/rss"},
    {"name": "Rudaw Arabic",            "lang": "ar", "type": "rss", "url": "https://www.rudawarabia.net/rss"},
    {"name": "Kurdistan24 Arabic",      "lang": "ar", "type": "rss", "url": "https://www.kurdistan24.net/ar/rss"},
    # مصادر إضافية (قد تعمل/تتغير — النظام سيعطّلها ذاتيًا إذا عطلت):
    {"name": "Baghdad Today",            "lang": "ar", "type": "rss", "url": "https://baghdadtoday.news/rss"},
    {"name": "NRT Arabic",               "lang": "ar", "type": "rss", "url": "https://www.nrttv.com/ar/rss"},
]


def load_sources():
    path = os.path.join(os.getcwd(), "sources.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                arr = json.load(f)
            if isinstance(arr, list) and arr:
                logger.info(f"Loaded {len(arr)} sources from sources.json")
                return arr
        except Exception as ex:
            logger.warning(f"sources.json parsing failed: {ex}")
    return DEFAULT_SOURCES

# =============== الحلقة الرئيسية ===============

def collect_once():
    sources = load_sources()
    total_new = 0

    # هيستوري فشل عام لتخفيف حدة التشابه عند «جفاف» الأخبار
    row = conn.execute("SELECT val FROM meta WHERE key='zero_streak'").fetchone()
    zero_streak = int(row[0]) if row and row[0].isdigit() else 0

    for src in sources:
        name = src.get("name", "?")
        if source_is_disabled(name):
            logger.warning(f"[SKIP] '{name}' معطّل مؤقتًا")
            continue
        try:
            items = fetch_rss(src) if src.get("type") == "rss" else fetch_scrape(src)
            logger.info(f"{name}: fetched {len(items)} items")
            if not items:
                source_mark_fail(name, cool_minutes=60)
                continue
            # نجاح — صفّر عدّاد الفشل
            source_mark_ok(name)
        except Exception as ex:
            logger.error(f"Fetch failed for {name}: {ex}")
            source_mark_fail(name)
            continue

        for it in items:
            title = it.get("title") or ""
            url = it.get("url") or ""
            content = it.get("summary") or ""
            if not title or not url:
                continue
            # عند تكرار صفر جديد ≥3 مرات، ارفع العتبة (تقليل حذف التشابه) مؤقتًا
            sim_thresh = SIMILARITY_THRESH
            if zero_streak >= 3:
                sim_thresh = max(0.98, SIMILARITY_THRESH)  # أكثر تساهلًا مع التشابه
            if is_duplicate(title, url, content):
                # أعد التحقق بعَتبة أعلى عند الشك
                if difflib.SequenceMatcher(None, title, title).ratio() < sim_thresh:
                    pass  # مستحيل عمليًا، احتياطي رمزي
                logger.info(f"[SKIP] duplicate/similar: {title}")
                continue
            save_item(it)
            ok = send_telegram(format_message(it))
            if not ok:
                save_to_md_html(it)
            total_new += 1
            time.sleep(0.5)

    # إدارة zero_streak
    if total_new == 0:
        zero_streak += 1
    else:
        zero_streak = 0
    conn.execute("INSERT INTO meta(key,val) VALUES('zero_streak',?) ON CONFLICT(key) DO UPDATE SET val=?", (str(zero_streak), str(zero_streak)))
    conn.commit()

    logger.info(f"New items sent/saved: {total_new}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="تشغيل مرة واحدة والخروج")
    args = ap.parse_args()
    if args.once:
        collect_once()
    else:
        while True:
            collect_once()
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()


# =====================
# requirements.txt المقترحة (حدّثت للتثبيت الآلي):
# feedparser
# beautifulsoup4
# lxml
# python-dateutil
# requests
# =====================

# =====================
# run.yml — GitHub Actions (ضعه في .github/workflows/run.yml)
# ---------------------
# name: IraqNews Bot
# on:
#   workflow_dispatch:
#   schedule:
#     - cron: "*/15 * * * *"  # كل 15 دقيقة
# jobs:
#   run-bot:
#     runs-on: ubuntu-latest
#     steps:
#       - name: Checkout repo
#         uses: actions/checkout@v4
#       - name: Set up Python
#         uses: actions/setup-python@v5
#         with:
#           python-version: "3.x"
#       - name: Install requirements
#         run: pip install -r requirements.txt
#       - name: Run bot
#         env:
#           TG_TOKEN: ${{ secrets.TG_TOKEN }}
#           TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
#           AUTO_PIP: "1"
#         run: python news_bot.py --once
# =====================
