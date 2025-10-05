# news_bot.py — Auto-Heal + Facebook Composer + Image Grabber + Anbar Filter + AI Post
# -*- coding: utf-8 -*-
"""
IraqNews Bot — جامع أخبار العراق بدون أي API للمواقع (نسخة احترافية نهائية)
------------------------------------------------------------------------------
• يجمع أخبار من RSS/Scrape بدون أي API.
• فلترة ذكية: ينشر فقط ما يخص الأنبار/الرمادي (قابلة للتخصيص).
• صياغة بوست عربي مختصر بالذكاء الاصطناعي (OpenAI أو Ollama) جاهز للسوشيال.
• منع التكرار (بصمات + تشابه عناوين) + قاعدة بيانات SQLite.
• إرسال تيليجرام مع إعادة محاولات ذكية (429/أخطاء مؤقتة).
• تعافٍ تلقائي للمصادر: تعطيل مؤقت بعد كثرة الفشل + تدوير User-Agent.
• مُركّب منشورات فيسبوك (Facebook Composer) + تنزيل صور og:image تلقائياً.
• ملفات جاهزة: نص المنشور + الصور في مجلدات منظمة حسب التاريخ.
• ملف مصادر خارجي اختياري: sources.json.

ENV:
- TG_TOKEN / TG_CHAT_ID             : لإرسال تيليجرام (اختياري)
- DRY_RUN=1                         : يمنع الإرسال ويكتب ملفات فقط
- MAX_ITEMS_PER_SOURCE=30           : حد جلب لكل مصدر
- POLL_SECONDS=900                  : فترة التكرار عند التشغيل المستمر
- AUTO_PIP=1                        : تثبيت تلقائي للحزم الناقصة
- SIMILARITY_THRESH=0.92            : عتبة تشابه العناوين
- OUT_DIR=news_out                  : مجلد المخرجات
- FETCH_TIMEOUT=20                  : مهلة الجلب HTTP

فلترة الأنبار/الرمادي:
- ANBAR_FILTER=1                    : تفعيل الفلترة
- REQUIRED_KEYWORDS= الأنبار,...   : كلمات القبول (قائمة مفصولة بفواصل)
- CITY_ALIASES= الأنبار,...        : مرادفات المدن
- STRICT_CITY_ONLY=0                : لو 1 ينشر فقط إن وُجدت مدينة محددة
- PREFIX_LOCALITY=1                 : يضيف سطر 📍المدينة أعلى البوست إن غابت

ذكاء اصطناعي (اختياري):
- LLM_BACKEND=openai|ollama|none
- OPENAI_API_KEY=...               : عند اختيار openai
- OPENAI_MODEL=gpt-4o-mini         : موديل افتراضي
- OLLAMA_MODEL=llama3.1            : عند اختيار ollama

فيسبوك:
- FACEBOOK_MODE=1                  : تفعيل توليد منشورات فيسبوك وحفظها
- FACEBOOK_TEMPLATE=short|summary|qa|bilingual
- FACEBOOK_MAX_IMAGES=3            : أقصى عدد صور تُرفق
"""

import os, re, sys, time, json, html, random, hashlib, sqlite3, logging, difflib, unicodedata
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
try:
    trafilatura = _try_import("trafilatura")
    HAS_TRAF = True
except Exception:
    HAS_TRAF = False

# ====== إعدادات عامة ======
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

# ====== ذكاء اصطناعي ======
LLM_BACKEND   = os.getenv("LLM_BACKEND", "openai").lower()  # openai|ollama|none
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3.1")

# ====== فلترة الأنبار/الرمادي ======
ANBAR_FILTER = os.getenv("ANBAR_FILTER", "1") == "1"
REQ_ENV   = os.getenv("REQUIRED_KEYWORDS", "")
CITY_ENV  = os.getenv("CITY_ALIASES", "")
STRICT_CITY_ONLY = os.getenv("STRICT_CITY_ONLY", "0") == "1"
PREFIX_LOCALITY  = os.getenv("PREFIX_LOCALITY", "1") == "1"

REQUIRED_KEYWORDS = [w.strip() for w in REQ_ENV.split(",") if w.strip()] or [
    "الأنبار","الانبار","الرمادي","رمادي","الفلوجة","هيت","القائم","عنه","عنة","راوة",
    "حديثة","الرطبة","الكرمة","الگرمة","الخالدية","الحبانية","عامرية الفلوجة","عكاشات"
]
CITY_ALIASES = [w.strip() for w in CITY_ENV.split(",") if w.strip()] or REQUIRED_KEYWORDS

# ====== لوج إلى ملف + كونسول ======
logger = logging.getLogger("iraqnews")
logger.setLevel(logging.INFO)
fmt = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt); logger.addHandler(ch)
fh = logging.FileHandler(os.path.join(OUT_DIR, 'bot.log'), encoding='utf-8'); fh.setFormatter(fmt); logger.addHandler(fh)

# ====== قاعدة البيانات ======
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    title TEXT,
    url TEXT,
    published_at TEXT,
    title_hash TEXT,
    content_hash TEXT,
    created_at TEXT
);""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_items_url ON items(url);")
conn.execute("CREATE INDEX IF NOT EXISTS idx_items_titlehash ON items(title_hash);")
conn.execute("""
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    failures INTEGER DEFAULT 0,
    disabled_until TEXT
);""")
conn.execute("""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    val TEXT
);""")
conn.commit()

# ====== أدوات ======
def ensure_dir(p: str): os.makedirs(p, exist_ok=True)

def canonical_url(u: str) -> str:
    try:
        p = urlparse(u); q = parse_qs(p.query)
        filtered = {k: v for k, v in q.items() if not k.lower().startswith("utm") and k.lower() not in {"fbclid","gclid"}}
        new_query = "&".join([f"{k}={v[0]}" for k, v in filtered.items()])
        return urlunparse((p.scheme, p.netloc, p.path, '', new_query, ''))
    except Exception:
        return u

def text_hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode('utf-8','ignore')).hexdigest()

def is_similar(a: str, b: str, thresh: float = SIMILARITY_THRESH) -> bool:
    try:
        return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio() >= thresh
    except Exception:
        return False

def clean_text(t: str) -> str:
    t = html.unescape(t or ""); t = re.sub(r"\s+", " ", t).strip(); return t

def norm_title(t: str) -> str:
    t = clean_text(t); t = re.sub(r"^(عاجل|خبر عاجل|بالصور|فيديو)[:\-\s]+","",t,flags=re.I); return t

def parse_time(ts):
    if not ts: return None
    try:
        dt = dtparser.parse(ts); 
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None

def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[\W_]+", "-", s.strip(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:maxlen]).strip("-") or "post"

# ====== صحة المصادر ======
def source_is_disabled(name: str) -> bool:
    row = conn.execute("SELECT disabled_until FROM sources WHERE name=?", (name,)).fetchone()
    if not row or not row[0]: return False
    try: return dtparser.parse(row[0]) > datetime.now(TZ)
    except Exception: return False

def source_mark_ok(name: str):
    conn.execute("""INSERT INTO sources(name, failures, disabled_until) VALUES(?,0,NULL)
                    ON CONFLICT(name) DO UPDATE SET failures=0, disabled_until=NULL""",(name,))
    conn.commit()

def source_mark_fail(name: str, cool_minutes: int = 180):
    row = conn.execute("SELECT failures FROM sources WHERE name=?", (name,)).fetchone()
    fails = (row[0] if row else 0) + 1
    disabled_until = None
    if fails >= 3:
        disabled_until = (datetime.now(TZ) + timedelta(minutes=cool_minutes)).isoformat()
        logger.warning(f"[SOURCE] تعطيل مؤقت '{name}' لـ {cool_minutes} دقيقة بعد {fails} فشل")
    conn.execute("""INSERT INTO sources(name, failures, disabled_until) VALUES(?,?,?)
                    ON CONFLICT(name) DO UPDATE SET failures=?, disabled_until=?""",
                 (name, fails, disabled_until, fails, disabled_until))
    conn.commit()

# ====== HTTP مع إعادة المحاولة ======
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

# ====== جلب من RSS/Scrape ======
def fetch_rss(src: dict):
    items = []
    parsed = feedparser.parse(src["url"])
    for e in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = norm_title(e.get("title","").strip())
        link = canonical_url(e.get("link","").strip())
        published = parse_time(e.get("published") or e.get("updated"))
        summary = clean_text(e.get("summary",""))
        # محاولة جلب نص المقال الكامل
        full = ""
        if HAS_TRAF and link:
            try:
                downloaded = trafilatura.fetch_url(link, timeout=15)
                if downloaded:
                    ext = trafilatura.extract(downloaded, include_comments=False, include_images=False) or ""
                    if len((ext or "").strip()) > 200:
                        full = clean_text(ext)
            except Exception:
                pass
        items.append({
            "source": src["name"],
            "title": title,
            "url": link,
            "published_at": published.isoformat() if published else "",
            "summary": (full or summary)[:1500]
        })
    return items

def fetch_scrape(src: dict):
    html_text = http_get(src["url"])
    soup = BeautifulSoup(html_text, PARSER)
    items = []
    for a in soup.select(src.get("list_selector","a"))[:MAX_ITEMS_PER_SOURCE]:
        href = a.get("href"); 
        if not href: continue
        link = canonical_url(urljoin(src["url"], href))
        title = norm_title(a.get_text(" "))
        content = ""
        try:
            art_html = http_get(link)
            art = BeautifulSoup(art_html, PARSER)
            node = art.select_one(src.get("content_selector","article")) or art
            content = clean_text(node.get_text(" "))
        except Exception as ex:
            logger.warning(f"Content fetch failed for {link}: {ex}")
        items.append({
            "source": src["name"], "title": title, "url": link,
            "published_at":"", "summary": content[:1500]
        })
    return items

# ====== منع التكرار ======
def is_duplicate(title: str, url: str, content: str) -> bool:
    c = conn.cursor()
    th = text_hash(title); ch = text_hash(content or title); cu = canonical_url(url)
    if c.execute("SELECT 1 FROM items WHERE url=? OR title_hash=? OR content_hash=? LIMIT 1",(cu,th,ch)).fetchone():
        return True
    for (old_title,) in c.execute("SELECT title FROM items ORDER BY id DESC LIMIT 200").fetchall():
        if is_similar(old_title or "", title or ""): return True
    return False

def save_item(it: dict):
    conn.execute("""INSERT INTO items (source,title,url,published_at,title_hash,content_hash,created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (it["source"], it["title"], canonical_url(it["url"]), it.get("published_at",""),
                  text_hash(it["title"]), text_hash((it.get("summary") or it["title"])),
                  datetime.now(TZ).isoformat()))
    conn.commit()

# ====== فلترة الأنبار/الرمادي ======
def _normalize_ar(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    s = re.sub("[إأآا]", "ا", s)
    s = s.replace("ى","ي").replace("ئ","ي").replace("ؤ","و").replace("ة","ه")
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩","0123456789"))
    return s

def is_relevant(text: str) -> bool:
    if not ANBAR_FILTER: return True
    t = _normalize_ar(text or "").lower()
    found_kw = any((_normalize_ar(k).lower() in t) for k in REQUIRED_KEYWORDS if k)
    if not found_kw: return False
    if STRICT_CITY_ONLY:
        return any((_normalize_ar(c).lower() in t) for c in CITY_ALIASES if c)
    return True

def detect_locality(text: str):
    t = _normalize_ar(text or "").lower()
    for c in CITY_ALIASES:
        cc = _normalize_ar(c).lower()
        if cc and cc in t:
            return c
    return "الأنبار"

# ====== Telegram & Files ======
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
        try: j = r.json()
        except Exception: j = {}
        if r.status_code == 429:
            retry_after = int(j.get('parameters', {}).get('retry_after', 2))
            wait_s = min(30, retry_after + i * 2)
            logger.warning(f"Telegram 429 — الانتظار {wait_s}s ثم إعادة…")
            time.sleep(wait_s); continue
        logger.error(f"Telegram error: {r.status_code} {r.text}")
        time.sleep(1.5*(i+1))
    return False

def save_to_md(it: dict):
    md_line = f"- **{it['title']}** — [{it['source']}]({it['url']})\n"
    with open(os.path.join(OUT_DIR, "latest.md"), "a", encoding="utf-8") as f:
        f.write(md_line)

# ====== Facebook Composer + Image Grabber ======
FACEBOOK_MODE = os.getenv("FACEBOOK_MODE","0") == "1"
FACEBOOK_TEMPLATE = os.getenv("FACEBOOK_TEMPLATE","summary").lower()
FACEBOOK_MAX_IMAGES = int(os.getenv("FACEBOOK_MAX_IMAGES","3"))

def extract_og_images(page_url: str):
    imgs = []
    try:
        html_text = http_get(page_url, tries=3)
        soup = BeautifulSoup(html_text, PARSER)
        for tag in soup.select('meta[property="og:image"], meta[name="og:image"]'):
            u = tag.get("content"); 
            if u: imgs.append(urljoin(page_url, u.strip()))
        for tag in soup.select('meta[name="twitter:image"], meta[property="twitter:image"]'):
            u = tag.get("content"); 
            if u: imgs.append(urljoin(page_url, u.strip()))
        art = soup.select_one("article") or soup
        for im in art.select("img"):
            u = im.get("src") or im.get("data-src")
            if u and not u.startswith("data:"):
                imgs.append(urljoin(page_url, u.strip()))
    except Exception as ex:
        logger.warning(f"extract_og_images failed: {ex}")
    uniq, seen = [], set()
    for u in imgs:
        if u and u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

def download_images(urls, base_dir, base_name, max_n=FACEBOOK_MAX_IMAGES):
    ensure_dir(base_dir); saved=[]
    for i, u in enumerate(urls[:max_n], start=1):
        try:
            ua = random.choice(USER_AGENTS)
            r = requests.get(u, headers={"User-Agent": ua}, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            ext = ".jpg"; ct = r.headers.get("Content-Type","").lower()
            if "png" in ct: ext=".png"
            elif "jpeg" in ct or "jpg" in ct: ext=".jpg"
            elif "webp" in ct: ext=".webp"
            out_path = os.path.join(base_dir, f"{base_name}_{i}{ext}")
            with open(out_path,"wb") as f: f.write(r.content)
            saved.append(out_path)
        except Exception as ex:
            logger.warning(f"download image failed for {u}: {ex}")
    return saved

def compose_fb_text(it: dict, template: str = FACEBOOK_TEMPLATE) -> str:
    title = it.get("title","").strip()
    src   = it.get("source","").strip()
    url   = it.get("url","").strip()
    ts    = it.get("published_at")
    when  = (parse_time(ts) or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M")
    summary = (it.get("summary") or "").strip()
    short_sum = summary[:200] + ("…" if len(summary)>200 else "")
    if template == "short":
        return (f"📰 {title}\n"
                f"المصدر: {src} | {when} (بغداد)\n"
                f"🔗 {url}\n#أخبار_الأنبار #الرمادي")
    if template == "summary":
        return (f"📰 {title}\n{short_sum}\n\n"
                f"🌍 المصدر: {src}\n🕒 {when} (بغداد)\n"
                f"🔗 {url}\n#أخبار_الأنبار #الرمادي")
    if template == "qa":
        return (f"🗣️ {title}\nشنو تأثير الخبر محليًا؟\n\n"
                f"المصدر: {src} | {when} (بغداد)\n"
                f"🔗 {url}\n#أخبار_الأنبار #نقاش")
    if template == "bilingual":
        en = short_sum[:180]
        return (f"📰 {title}\n{short_sum}\n\n[EN] {en}\n\n"
                f"Source: {src} | Baghdad Time: {when}\n"
                f"🔗 {url}\n#Anbar #Ramadi")
    return (f"📰 {title}\n{short_sum}\n\n🌍 {src}\n🕒 {when} (بغداد)\n🔗 {url}")

def handle_facebook(it: dict):
    date_dir = datetime.now(TZ).strftime("%Y-%m-%d")
    fb_dir   = os.path.join(OUT_DIR,"facebook",date_dir)
    img_dir  = os.path.join(OUT_DIR,"images",  date_dir)
    ensure_dir(fb_dir); ensure_dir(img_dir)
    slug = slugify(it.get("title","post")) or "post"
    text = compose_fb_text(it)
    img_urls  = extract_og_images(it.get("url","")) if it.get("url") else []
    saved_imgs = download_images(img_urls, img_dir, slug, max_n=FACEBOOK_MAX_IMAGES) if img_urls else []
    post_path = os.path.join(fb_dir, f"{slug}.txt")
    with open(post_path,"w",encoding="utf-8") as f:
        if saved_imgs: f.write("📷 صور/تفاصيل أكثر بالداخل ⤵️\n")
        f.write(text+"\n")
    logger.info(f"Facebook post ready: {post_path} | images: {len(saved_imgs)}")
    return post_path, saved_imgs

# ====== AI بوست جاهز للسوشيال ======
AR_POST_SYSTEM = """أنت محرر أخبار بالعربية الفصيحة المقبولة عراقياً.
حوِّل الخبر إلى منشور مناسب لتليجرام/فيسبوك:
- عنوان موجز قوي.
- 3-5 نقاط (•) تلخص الوقائع بالأسماء/الأرقام/الزمان/المكان.
- سطر: لماذا يهم؟
- سطر: المصدر + الرابط كما هو.
- بدون مبالغة أو تحيز سياسي؛ حقائق فقط.
"""

AR_POST_USER_TMPL = """العنوان: {title}
النص الخام:
{raw_text}

اكتب منشورًا لا يتجاوز {limit} حرفًا، يتضمن:
1) سطر افتتاحي موجز.
2) نقاط (•).
3) "لماذا يهم؟" بسطر واحد.
4) المصدر: {source} | {url}
اكتب بالعربية الفصيحة البسيطة المقبولة للعراقي.
"""

MAX_POST_LEN = int(os.getenv("MAX_POST_LEN","900"))

def llm_post(title: str, body: str, url: str, source: str) -> str:
    backend = LLM_BACKEND
    prompt = AR_POST_USER_TMPL.format(title=title, raw_text=body, limit=MAX_POST_LEN, url=url, source=source)
    if backend == "openai" and OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.responses.create(
                model=OPENAI_MODEL,
                input=[{"role":"system","content":AR_POST_SYSTEM},{"role":"user","content":prompt}],
                temperature=0.4,
            )
            return (resp.output_text or "").strip()
        except Exception as ex:
            logger.warning(f"OpenAI error: {ex}")
    elif backend == "ollama":
        try:
            r = requests.post("http://localhost:11434/api/generate",
                              json={"model": OLLAMA_MODEL, "prompt": f"<<SYS>>{AR_POST_SYSTEM}\n<</SYS>>\n{prompt}", "stream": False, "options":{"temperature":0.3}},
                              timeout=60)
            if r.status_code == 200:
                return (r.json().get("response") or "").strip()
        except Exception as ex:
            logger.warning(f"Ollama error: {ex}")
    # fallback
    trimmed = (body or "")[:MAX_POST_LEN-100]
    return f"📰 {title}\n• {trimmed}...\nالمصدر: {source} | {url}"

def tg_format_ai_post(ai_text: str, locality: str) -> str:
    if PREFIX_LOCALITY and locality:
        if _normalize_ar(locality) not in _normalize_ar(ai_text):
            return f"📍 {locality}\n{ai_text}"
    return ai_text

# ====== مصادر افتراضية ======
DEFAULT_SOURCES = [
    {"name": "Iraq News Agency (INA)", "lang":"ar","type":"rss","url":"https://ina.iq/rss.ashx"},
    {"name": "Alsumaria News",          "lang":"ar","type":"rss","url":"https://www.alsumaria.tv/rss-feed"},
    {"name": "Shafaq News",             "lang":"ar","type":"rss","url":"https://shafaq.com/ar/rss"},
    {"name": "Rudaw Arabic",            "lang":"ar","type":"rss","url":"https://www.rudawarabia.net/rss"},
    {"name": "Kurdistan24 Arabic",      "lang":"ar","type":"rss","url":"https://www.kurdistan24.net/ar/rss"},
    {"name": "Baghdad Today",           "lang":"ar","type":"rss","url":"https://baghdadtoday.news/rss"},
    {"name": "NRT Arabic",              "lang":"ar","type":"rss","url":"https://www.nrttv.com/ar/rss"},
]

def load_sources():
    path = os.path.join(os.getcwd(),"sources.json")
    if os.path.exists(path):
        try:
            with open(path,"r",encoding="utf-8") as f:
                arr = json.load(f)
            if isinstance(arr,list) and arr:
                logger.info(f"Loaded {len(arr)} sources from sources.json")
                return arr
        except Exception as ex:
            logger.warning(f"sources.json parsing failed: {ex}")
    return DEFAULT_SOURCES

# ====== دورة الجمع/النشر ======
def collect_once():
    sources = load_sources()
    total_new = 0
    row = conn.execute("SELECT val FROM meta WHERE key='zero_streak'").fetchone()
    zero_streak = int(row[0]) if row and str(row[0]).isdigit() else 0

    for src in sources:
        name = src.get("name","?")
        if source_is_disabled(name):
            logger.warning(f"[SKIP] '{name}' معطّل مؤقتًا"); continue
        try:
            items = fetch_rss(src) if src.get("type") == "rss" else fetch_scrape(src)
            logger.info(f"{name}: fetched {len(items)} items")
            if not items:
                source_mark_fail(name, cool_minutes=60); continue
            source_mark_ok(name)
        except Exception as ex:
            logger.error(f"Fetch failed for {name}: {ex}")
            source_mark_fail(name); continue

        for it in items:
            title = it.get("title") or ""; url = it.get("url") or ""; content = it.get("summary") or ""
            if not title or not url: continue
            # فلترة الأنبار/الرمادي
            if ANBAR_FILTER:
                combined = f"{title}\n{content}"
                if not is_relevant(combined): 
                    continue
            if is_duplicate(title, url, content):
                logger.info(f"[SKIP] duplicate/similar: {title}"); continue

            # صياغة بوست AI
            locality = detect_locality(f"{title}\n{content}") if ANBAR_FILTER else ""
            ai_text = llm_post(title, content, url, it.get("source",""))
            ai_text = tg_format_ai_post(ai_text, locality)

            # أرسل تيليجرام
            ok = send_telegram(ai_text)
            if not ok:  # لو فشل، احفظ لسجل
                save_to_md(it)

            # حفظ في قاعدة البيانات بعد الإرسال/الحفظ
            save_item(it)

            # فيسبوك
            if FACEBOOK_MODE:
                try:
                    handle_facebook(it)
                except Exception as ex:
                    logger.warning(f"facebook compose failed: {ex}")

            total_new += 1
            time.sleep(0.5)

    # تتبع حالات انعدام الأخبار
    if total_new == 0: zero_streak += 1
    else: zero_streak = 0
    conn.execute("""INSERT INTO meta(key,val) VALUES('zero_streak',?)
                    ON CONFLICT(key) DO UPDATE SET val=?""",(str(zero_streak), str(zero_streak)))
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
