import os, re, time, html, hashlib, sqlite3, logging, difflib, requests, feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qs
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# إعدادات عامة
TZ = timezone(timedelta(hours=+3))
DB_PATH = os.getenv("NEWS_DB", "iraq_news.db")
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; IraqNewsBot/1.0)")
FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "15"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))
MAX_ITEMS_PER_SOURCE = int(os.getenv("MAX_ITEMS_PER_SOURCE", "20"))
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# المصادر
SOURCES_CFG = [
    {"name": "Iraq News Agency (INA)","lang": "ar","type": "rss","url": "https://ina.iq/rss.ashx"},
    {"name": "Rudaw Arabic","lang": "ar","type": "rss","url": "https://www.rudawarabia.net/rss"},
    {"name": "Shafaq News","lang": "ar","type": "rss","url": "https://shafaq.com/ar/rss"},
    {"name": "Alsumaria News","lang": "ar","type": "rss","url": "https://www.alsumaria.tv/rss-feed"},
    {"name": "Kurdistan24 Arabic","lang": "ar","type": "rss","url": "https://www.kurdistan24.net/ar/rss"},
    {"name": "Iraq Oil Ministry (News)","lang": "ar","type": "scrape",
     "url": "https://oil.gov.iq/category/news/","list_selector": "article h2 a",
     "content_selector": "article .entry-content, .post-content"},
]

# قاعدة البيانات
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, title TEXT, url TEXT,
    published_at TEXT, title_hash TEXT,
    content_hash TEXT, created_at TEXT
);""")
conn.commit()

def canonical_url(u): 
    try:
        p=urlparse(u); q=parse_qs(p.query)
        f={k:v for k,v in q.items() if not k.lower().startswith("utm") and k.lower() not in {"fbclid","gclid"}}
        newq="&".join([f"{k}={v[0]}" for k,v in f.items()])
        return urlunparse((p.scheme,p.netloc,p.path,'',newq,''))
    except: return u

def text_hash(s): return hashlib.sha256(s.strip().encode()).hexdigest()
def is_similar(a,b,t=0.9): return difflib.SequenceMatcher(None,a.strip(),b.strip()).ratio()>=t
def clean_text(t): return re.sub(r"\s+"," ",html.unescape(t or "")).strip()
def norm_title(t): return re.sub(r"^(عاجل|خبر عاجل|بالصور|فيديو)[:\\-\\s]+","",clean_text(t),flags=re.I)
def parse_time(ts):
    if not ts: return None
    try:
        dt=dtparser.parse(ts)
        if not dt.tzinfo: dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except: return None

def fetch_rss(src):
    feed=feedparser.parse(src["url"]); out=[]
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        out.append({
            "source":src["name"],"title":norm_title(e.get("title","")),
            "url":canonical_url(e.get("link","")),
            "published_at":(parse_time(e.get("published") or e.get("updated")) or datetime.now(TZ)).isoformat(),
            "summary":clean_text(e.get("summary",""))
        })
    return out

def http_get(u): 
    r=requests.get(u,headers={"User-Agent":USER_AGENT},timeout=FETCH_TIMEOUT)
    r.raise_for_status(); return r.text

def fetch_scrape(src):
    soup=BeautifulSoup(http_get(src["url"]),"lxml"); out=[]
    for a in soup.select(src.get("list_selector","a"))[:MAX_ITEMS_PER_SOURCE]:
        link=canonical_url(a.get("href","")); title=norm_title(a.get_text(" "))
        content=""
        try:
            art=BeautifulSoup(http_get(link),"lxml")
            node=art.select_one(src.get("content_selector","article"))
            if node: content=clean_text(node.get_text(" "))
        except Exception as ex: logging.warning(f"Content fetch failed for {link}: {ex}")
        out.append({"source":src["name"],"title":title,"url":link,"published_at":"",
                   "summary":content[:500]})
    return out

def is_duplicate(title,url,content):
    th, ch, cu = text_hash(title), text_hash(content or title), canonical_url(url)
    c=conn.cursor()
    if c.execute("SELECT 1 FROM items WHERE url=? OR title_hash=? OR content_hash=? LIMIT 1",(cu,th,ch)).fetchone():
        return True
    for (old,) in c.execute("SELECT title FROM items ORDER BY id DESC LIMIT 200").fetchall():
        if is_similar(old,title): return True
    return False

def save_item(it):
    conn.execute("INSERT INTO items (source,title,url,published_at,title_hash,content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                 (it["source"],it["title"],canonical_url(it["url"]),it.get("published_at",""),
                  text_hash(it["title"]),text_hash(it.get("summary") or it["title"]),datetime.now(TZ).isoformat()))
    conn.commit()

def format_message(it):
    when = (parse_time(it.get("published_at")) or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M")
    return (f"📰 <b>{html.escape(it['title'])}</b>\\n"
            f"🌍 المصدر: <i>{html.escape(it['source'])}</i>\\n"
            f"🕒 الوقت: {when} (Baghdad)\\n"
            f"🔗 <a href='{html.escape(it['url'])}'>اقرأ التفاصيل</a>")

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID: return False
    if DRY_RUN: return True
    r=requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data={"chat_id":TG_CHAT_ID,"text":msg,"parse_mode":"HTML"})
    return r.status_code==200

OUT_DIR = os.getenv("OUT_DIR","news_out")
os.makedirs(OUT_DIR, exist_ok=True)

def save_to_md_html(it):
    md=f"- **{it['title']}** — [{it['source']}]({it['url']})\\n"
    with open(os.path.join(OUT_DIR,"latest.md"),"a",encoding="utf-8") as f:f.write(md)

def collect_once():
    total=0
    for src in SOURCES_CFG:
        try: items = fetch_rss(src) if src["type"]=="rss" else fetch_scrape(src)
        except Exception as ex: logging.error(f"Fetch failed for {src['name']}: {ex}"); continue
        for it in items:
            if not it["title"] or not it["url"] or is_duplicate(it["title"],it["url"],it.get("summary")): continue
            save_item(it)
            if not send_telegram(format_message(it)): save_to_md_html(it)
            total += 1
            time.sleep(0.6)
    logging.info(f"New items: {total}")

def main():
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--once",action="store_true")
    if p.parse_args().once: collect_once()
    else:
        while True: collect_once(); time.sleep(POLL_SECONDS)

if __name__=="__main__": main()
