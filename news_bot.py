# news_bot.py — Auto‑Heal + Facebook Composer + Image Grabber
# -*- coding: utf-8 -*-
"""
IraqNews Bot — جامع أخبار العراق بدون أي API خاصة بالمواقع (نسخة احترافية نهائية)
------------------------------------------------------------------------------
• يجمع أخبار من RSS/Scrape بدون أي API.
• منع التكرار (بصمات + تشابه عناوين) + قاعدة بيانات SQLite.
• إرسال تيليجرام مع إعادة محاولات ذكية (429/أخطاء مؤقتة).
• تعافٍ تلقائي للمصادر: تعطيل مؤقت بعد كثرة الفشل + تدوير User-Agent.
• مُركّب منشورات فيسبوك (Facebook Composer) جاهز للنشر + تنزيل صور og:image تلقائياً.
• ملفات جاهزة: نص المنشور + الصور في مجلدات منظمة حسب التاريخ.
• ملف مصادر خارجي اختياري: sources.json.

ENV:
- TG_TOKEN / TG_CHAT_ID             : لإرسال تيليجرام (اختياري)
- DRY_RUN=1                         : يمنع الإرسال ويكتب ملفات فقط
- MAX_ITEMS_PER_SOURCE=30           : حد جلب لكل مصدر
- POLL_SECONDS=900                  : فترة التكرار عند التشغيل المستمر
- AUTO_PIP=1                        : تثبيت تلقائي للحزم الناقصة
- SIMILARITY_THRESH=0.92            : عتبة تشابه العناوين
- FACEBOOK_MODE=1                   : تفعيل توليد منشورات فيسبوك وحفظها
- FACEBOOK_TEMPLATE=short|summary|qa|bilingual : اختيار القالب
- FACEBOOK_MAX_IMAGES=3             : أقصى عدد صور تُرفق للمنشور

المخرجات:
- news_out/bot.log                  : سجل التنفيذ
- news_out/latest.md                : سجل مختصر للأخبار
- news_out/facebook/YYYY-MM-DD/slug.txt         : نص منشور فيسبوك النهائي
- news_out/images/YYYY-MM-DD/slug_1.jpg ...     : صور مرافقة إن توفرت
"""

import os, re, sys, time, json, html, random, hashlib, sqlite3, logging, difflib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qs, urljoin

# ====== تثبيت تلقائي للحزم عند نقصها ======
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

requests   = _try_import("requests")
feedparser = _try_import("feedparser")
bs4        = _try_import("bs4", pip_pkg="beautifulsoup4")
BeautifulSoup = bs4.BeautifulSoup
try:
    _try_import("lxml")
    PARSER = "lxml"
except Exception:
    PARSER = "html.parser"

dtparser = _try_import("dateutil.parser", pip_pkg="python-dateutil").parser

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

FACEBOOK_MODE = os.getenv("FACEBOOK_MODE", "0") == "1"
FACEBOOK_TEMPLATE = os.getenv("FACEBOOK_TEMPLATE", "short").lower()
FACEBOOK_MAX_IMAGES = int(os.getenv("FACEBOOK_MAX_IMAGES", "3"))

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

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


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


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[\W_]+", "-", s.strip(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:maxlen]).strip("-") or "post"

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
    if fails >= 3:
        disabled_until = (datetime.now(TZ) + timedelta(minutes=cool_minutes)).isoformat()
        logger.warning(f"[SOURCE] تعطيل مؤقت للمصدر '{name}' لمدة {cool_minutes} دقيقة بعد {fails} فشل")
    conn.execute(
        "INSERT INTO sources(name, failures, disabled_until) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET failures=?, disabled_until=?",
        (name, fails, disabled_until, fails, disabled_until)
    )
    conn.commit()

# =============== HTTP مع إعادة المحاولة ===============

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
    soup = BeautifulSoup(html_text, PARSER)
    items = []
    for a in soup.select(src.get("list_selector", "a"))[:MAX_ITEMS_PER_SOURCE]:
        href = a.get("href")
        if not href:
            continue
        link = canonical_url(urljoin(src["url"], href))
        title = norm_title(a.get_text(" "))
        content = ""
        try:
            art_html = http_get(link)
            art = BeautifulSoup(art_html, PARSER)
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

# =============== تكرار وتخزين ===============

def is_duplicate(title: str, url: str, content: str) -> bool:
    c = conn.cursor()
    th = text_hash(title)
    ch = text_hash(content or title)
    cu = canonical_url(url)
    if c.execute("SELECT 1 FROM items WHERE url=? OR title_hash=? OR content_hash=? LIMIT 1", (cu, th, ch)).fetchone():
        return True
    for (old_title,) in c.execute("SELECT title FROM items ORDER BY id DESC LIMIT 200").fetchall():
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

# =============== Telegram & Files ===============

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


def save_to_md(it: dict):
    md_line = f"- **{it['title']}** — [{it['source']}]({it['url']})\n"
    with open(os.path.join(OUT_DIR, "latest.md"), "a", encoding="utf-8") as f:
        f.write(md_line)

# =============== Facebook Composer + Image Grabber ===============

def extract_og_images(page_url: str):
    """يرجع قائمة صور محتملة من og:image وبدائل بسيطة."""
    imgs = []
    try:
        html_text = http_get(page_url, tries=3)
        soup = BeautifulSoup(html_text, PARSER)
        # og:image
        for tag in soup.select('meta[property="og:image"], meta[name="og:image"]'):
            u = tag.get("content")
            if u:
                imgs.append(urljoin(page_url, u.strip()))
        # twitter:image
        for tag in soup.select('meta[name="twitter:image"], meta[property="twitter:image"]'):
            u = tag.get("content")
            if u:
                imgs.append(urljoin(page_url, u.strip()))
        # أول صورة ضمن المقال كخيار أخير
        art = soup.select_one("article") or soup
        for im in art.select("img"):
            u = im.get("src") or im.get("data-src")
            if u and not u.startswith("data:"):
                imgs.append(urljoin(page_url, u.strip()))
    except Exception as ex:
        logger.warning(f"extract_og_images failed: {ex}")
    # تنظيف وتوحيد
    uniq, seen = [], set()
    for u in imgs:
        if u and u not in seen:
            seen.add(u); uniq.append(u)
    return uniq


def download_images(urls, base_dir, base_name, max_n=FACEBOOK_MAX_IMAGES):
    ensure_dir(base_dir)
    saved = []
    for i, u in enumerate(urls[:max_n], start=1):
        try:
            ua = random.choice(USER_AGENTS)
            r = requests.get(u, headers={"User-Agent": ua}, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            ext = ".jpg"
            ct = r.headers.get("Content-Type", "").lower()
            if "png" in ct: ext = ".png"
            elif "jpeg" in ct or "jpg" in ct: ext = ".jpg"
            elif "webp" in ct: ext = ".webp"
            out_path = os.path.join(base_dir, f"{base_name}_{i}{ext}")
            with open(out_path, "wb") as f:
                f.write(r.content)
            saved.append(out_path)
        except Exception as ex:
            logger.warning(f"download image failed for {u}: {ex}")
    return saved


def compose_fb_text(it: dict, template: str = FACEBOOK_TEMPLATE) -> str:
    title = it.get("title", "").strip()
    src   = it.get("source", "").strip()
    url   = it.get("url", "").strip()
    ts    = it.get("published_at")
    when  = (parse_time(ts) or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M")
    summary = (it.get("summary") or "").strip()
    short_sum = summary[:200] + ("…" if len(summary) > 200 else "")

    if template == "short":
        return (f"📰 {title}\n"
                f"المصدر: {src} | التوقيت: {when} (بغداد)\n"
                f"🔗 {url}\n"
                f"#أخبار_العراق #بغداد #عاجل")
    if template == "summary":
        return (f"📰 {title}\n{short_sum}\n\n"
                f"🌍 المصدر: {src}\n🕒 {when} (بغداد)\n"
                f"🔗 {url}\n#أخبار_العراق #العراق")
    if template == "qa":
        return (f"🗣️ {title}\nبرأيكم: شنو تأثير هالخبر محليًا؟\n\n"
                f"المصدر: {src} | {when} (بغداد)\n"
                f"🔗 {url}\n#أخبار_العراق #نقاش")
    if template == "bilingual":
        en = short_sum[:180]
        return (f"📰 {title}\n{short_sum}\n\n[EN] {en}\n\n"
                f"Source: {src} | Baghdad Time: {when}\n"
                f"🔗 {url}\n#IraqNews #Baghdad")
    # default
    return (f"📰 {title}\n{short_sum}\n\n"
            f"🌍 المصدر: {src}\n🕒 {when} (بغداد)\n"
            f"🔗 {url}")


def handle_facebook(it: dict):
    """ينشئ ملف منشور فيسبوك + تنزيل صور ويرجّع المسارات."""
    date_dir = datetime.now(TZ).strftime("%Y-%m-%d")
    fb_dir   = os.path.join(OUT_DIR, "facebook", date_dir)
    img_dir  = os.path.join(OUT_DIR, "images",   date_dir)
    ensure_dir(fb_dir); ensure_dir(img_dir)

    slug = slugify(it.get("title", "post")) or "post"
    text = compose_fb_text(it)

    # جلب صور og:image
    img_urls = extract_og_images(it.get("url", "")) if it.get("url") else []
    saved_imgs = download_images(img_urls, img_dir, slug, max_n=FACEBOOK_MAX_IMAGES) if img_urls else []

    post_path = os.path.join(fb_dir, f"{slug}.txt")
    with open(post_path, "w", encoding="utf-8") as f:
        if saved_imgs:
            f.write("📷 صور/تفاصيل أكثر بالداخل ⤵️\n")
        f.write(text)
        f.write("\n")
    logger.info(f"Facebook post ready: {post_path} | images: {len(saved_imgs)}")
    return post_path, saved_imgs

# =============== الحلقة الرئيسية ===============
DEFAULT_SOURCES = [
    {"name": "Iraq News Agency (INA)", "lang": "ar", "type": "rss", "url": "https://ina.iq/rss.ashx"},
    {"name": "Alsumaria News",          "lang": "ar", "type": "rss", "url": "https://www.alsumaria.tv/rss-feed"},
    {"name": "Shafaq News",             "lang": "ar", "type": "rss", "url": "https://shafaq.com/ar/rss"},
    {"name": "Rudaw Arabic",            "lang": "ar", "type": "rss", "url": "https://www.rudawarabia.net/rss"},
    {"name": "Kurdistan24 Arabic",      "lang": "ar", "type": "rss", "url": "https://www.kurdistan24.net/ar/rss"},
    {"name": "Baghdad Today",           "lang": "ar", "type": "rss", "url": "https://baghdadtoday.news/rss"},
    {"name": "NRT Arabic",              "lang": "ar", "type": "rss", "url": "https://www.nrttv.com/ar/rss"},
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


def collect_once():
    sources = load_sources()
    total_new = 0

    row = conn.execute("SELECT val FROM meta WHERE key='zero_streak'").fetchone()
    zero_streak = int(row[0]) if row and str(row[0]).isdigit() else 0

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
            source_mark_ok(name)
        except Exception as ex:
            logger.error(f"Fetch failed for {name}: {ex}")
            source_mark_fail(name)
            continue

        for it in items:
            title = it.get("title") or ""
            url   = it.get("url") or ""
            content = it.get("summary") or ""
            if not title or not url:
                continue
            if is_duplicate(title, url, content):
                logger.info(f"[SKIP] duplicate/similar: {title}")
                continue
            save_item(it)

            # Telegram
            ok = send_telegram(format_message(it))
            if not ok:
                save_to_md(it)

            # Facebook
            if FACEBOOK_MODE:
                try:
                    handle_facebook(it)
                except Exception as ex:
                    logger.warning(f"facebook compose failed: {ex}")

            total_new += 1
            time.sleep(0.5)

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
# requirements.txt
# feedparser
# beautifulsoup4
# lxml
# python-dateutil
# requests
# =====================

# =====================
# .github/workflows/run.yml (مثال تشغيل على GitHub Actions)
# ---------------------------------------------------------
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
#       - name: Run bot (with Facebook compose)
#         env:
#           TG_TOKEN: ${{ secrets.TG_TOKEN }}
#           TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
#           AUTO_PIP: "1"
#           FACEBOOK_MODE: "1"
#           FACEBOOK_TEMPLATE: "summary"
#           FACEBOOK_MAX_IMAGES: "3"
#         run: python news_bot.py --once
# =====================
