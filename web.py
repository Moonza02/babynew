"""
BabyDiary sayt backend — botlar bilan BIR Railway service'da ishlaydi.
  - Saytni ko'rsatadi (index.html)
  - /api/products  -> botning products.json idan mahsulotlar (real-time)
  - /api/photo     -> Telegram'dagi mahsulot rasmini ko'rsatadi (file_id orqali)
  - /api/order     -> saytdan buyurtma -> 3 adminga Telegram xabar + orders.json ga yozadi
Ishga tushirish: python web.py   (PORT ni Railway beradi)
"""
import os, json, time, datetime, sqlite3, urllib.request, urllib.parse, hashlib, secrets, re, threading
from flask import Flask, request, jsonify, send_file, send_from_directory, Response, make_response

# ─── Oddiy rate-limiter (xotirada) — SMS spam, kod brute-force, login himoyasi ───
_RL_LOCK = threading.Lock()
_RL = {}  # key -> [timestamps]

def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"

def _rl_blocked(key, limit, window):
    """Joriy oynada `limit` ga yetgan bo'lsa (True, retry_sekund)."""
    now = time.time()
    with _RL_LOCK:
        arr = [t for t in _RL.get(key, []) if now - t < window]
        _RL[key] = arr
        if len(arr) >= limit:
            return True, int(window - (now - arr[0])) + 1
        return False, 0

def _rl_event(key, window):
    """Hodisani (yuborish yoki muvaffaqiyatsiz urinish) qayd qiladi."""
    now = time.time()
    with _RL_LOCK:
        arr = [t for t in _RL.get(key, []) if now - t < window]
        arr.append(now)
        _RL[key] = arr

def _rl_clear(key):
    with _RL_LOCK:
        _RL.pop(key, None)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/data" if os.path.isdir("/data") else BASE
PRODUCTS_FILE         = os.path.join(DATA_DIR, "products.json")
ORDERS_FILE           = os.path.join(DATA_DIR, "orders.json")
REVIEWS_FILE          = os.path.join(DATA_DIR, "reviews.json")
PENDING_REVIEWS_FILE  = os.path.join(DATA_DIR, "pending_reviews.json")
TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "babydiaryuz_bot")

# ── Payme Merchant API sozlamalari ──
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")   # kassa ID
PAYME_KEY         = os.getenv("PAYME_KEY", "")           # Merchant API kaliti (maxfiy)
PAYME_CHECKOUT    = "https://checkout.paycom.uz"         # to'lov sahifasi

# ── Click SHOP API (Prepare/Complete) ──
# https://docs.click.uz/en/click-api-request/
CLICK_SERVICE_ID       = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID      = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SECRET_KEY       = os.getenv("CLICK_SECRET_KEY", "")
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "")
CLICK_PAY_URL          = "https://my.click.uz/services/pay"
DB_FILE = os.path.join(DATA_DIR, "babydiary.db")

LIMITED_ADMINS = [8733385729]

# ─── GitHub real-time backup & auto-restore ──────────────────────────────────
_GH_TOKEN  = os.getenv("GITHUB_TOKEN", "")
_GH_REPO   = os.getenv("GITHUB_REPO", "Moonza02/babydiary")
_GH_BRANCH = os.getenv("GITHUB_BRANCH", "main")
_GH_API    = "https://api.github.com"

def _gh_headers():
    return {"Authorization": f"Bearer {_GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json"}

def _gh_get_sha(path):
    if not _GH_TOKEN: return None
    try:
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/{path}?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()).get("sha")
    except Exception:
        return None

def _gh_push_file(path, content_bytes, label="backup"):
    """Faylni GitHub'ga fon thread'da push qiladi."""
    if not _GH_TOKEN: return
    import base64
    def _push():
        try:
            b64 = base64.b64encode(content_bytes).decode()
            sha = _gh_get_sha(path)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            payload = {"message": f"{label} [{stamp}]", "content": b64, "branch": _GH_BRANCH}
            if sha: payload["sha"] = sha
            url = f"{_GH_API}/repos/{_GH_REPO}/contents/{path}"
            req = urllib.request.Request(url, json.dumps(payload).encode(), _gh_headers(), method="PUT")
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            print(f"GitHub push ({path}): {e}", flush=True)
    threading.Thread(target=_push, daemon=True).start()

def _gh_restore(path, local_path):
    """Lokal fayl yo'q bo'lsa GitHub'dan tiklaydi."""
    if os.path.exists(local_path) or not _GH_TOKEN: return False
    import base64
    try:
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/{path}?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f: f.write(raw)
        print(f"✅ GitHub'dan tiklandi: {local_path}", flush=True)
        return True
    except Exception as e:
        print(f"GitHub restore ({path}): {e}", flush=True)
        return False

# ─── products.json ni GitHub'dan davriy yangilash ─────────────────────────────
# Bot alohida servisda ishlaydi va o'zgarishlarni GitHub'ga push qiladi.
# Web startupda bir marta tiklaydi; shu funksiya ishlayotganda ham (TTL bilan)
# eng so'nggi ro'yxatni tortib turadi — botdagi o'chirish/qo'shish saytga yetsin.
_prod_pull_lock = threading.Lock()
_prod_pull_ts = 0.0
PROD_PULL_TTL = 20  # sekund

def refresh_products_from_gh(force=False):
    global _prod_pull_ts
    if not _GH_TOKEN:
        return
    now = time.time()
    if not force and (now - _prod_pull_ts) < PROD_PULL_TTL:
        return
    if not _prod_pull_lock.acquire(blocking=False):
        return  # boshqa so'rov allaqachon tortyapti
    try:
        if not force and (time.time() - _prod_pull_ts) < PROD_PULL_TTL:
            return
        import base64
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/products.json?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            meta = json.loads(r.read())
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        parsed = json.loads(raw.decode("utf-8"))   # faqat to'g'ri JSON bo'lsa yozamiz
        if isinstance(parsed, list):
            with open(PRODUCTS_FILE, "wb") as f:
                f.write(raw)
        _prod_pull_ts = time.time()
    except Exception as e:
        print("products GitHub pull xato:", e, flush=True)
        _prod_pull_ts = time.time()  # xatoda ham TTL — spam bo'lmasin
    finally:
        try:
            _prod_pull_lock.release()
        except Exception:
            pass

# ─── Konfliktsiz stok yangilash ───────────────────────────────────────────────
# MUAMMO: bot alohida servisda. Agar web o'zining (eski) products.json'ini butunlay
# GitHub'ga push qilsa — bot yangi qo'shgan tovarlar o'chib ketadi.
# YECHIM: web faqat STOK o'zgarishini (delta) GitHub'dagi ENG SO'NGGI ro'yxatga
# qo'llaydi. Konflikt (SHA o'zgargan) bo'lsa qayta urinadi.
def _gh_fetch_products():
    """GitHub'dan (ro'yxat, sha). Xato/yo'q bo'lsa (None, None)."""
    if not _GH_TOKEN:
        return None, None
    import base64
    try:
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/products.json?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list):
            return None, None
        return data, meta.get("sha")
    except Exception as e:
        print("products fetch xato:", e, flush=True)
        return None, None

def _gh_put_products(products_list, sha):
    """products.json ni SHA bilan PUT qiladi. True=ok, False=konflikt(409)/xato."""
    if not _GH_TOKEN:
        return False
    import base64
    try:
        raw = json.dumps(products_list, ensure_ascii=False, indent=2).encode("utf-8")
        b64 = base64.b64encode(raw).decode()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        payload = {"message": f"stock update [{stamp}]", "content": b64, "branch": _GH_BRANCH}
        if sha:
            payload["sha"] = sha
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/products.json"
        req = urllib.request.Request(url, json.dumps(payload).encode(), _gh_headers(), method="PUT")
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except Exception as e:
        if getattr(e, "code", None) == 409:   # SHA konflikt — kimdir o'zgartirdi
            return False
        print("products PUT xato:", e, flush=True)
        return False

def _apply_stock_deltas_to_list(products, deltas):
    byid = {str(p.get("id")): p for p in products}
    for dl in deltas:
        p = byid.get(str(dl.get("product_id")))
        if not p:
            continue  # o'chirilgan bo'lishi mumkin — o'tkazamiz
        size = (dl.get("size") or "").strip()
        qd = int(dl.get("qty", 0))   # manfiy = kamaytirish, musbat = qaytarish
        _sizes = p.get("sizes") or []
        if _sizes and size:
            for s in _sizes:
                if s.get("label") == size:
                    s["stock"] = max(0, int(s.get("stock", 0)) + qd)
            p["stock"] = sum(int(s.get("stock", 0)) for s in _sizes)
        else:
            p["stock"] = max(0, int(p.get("stock", 0)) + qd)

def apply_stock_deltas(deltas):
    """Stok o'zgarishini GitHub'dagi eng so'nggi ro'yxatga qo'llaydi (tovar yo'qolmasin)."""
    if not deltas:
        return
    with _prod_pull_lock:   # /api/products refresh bilan aralashmasin
        for _ in range(5):
            gh_list, sha = _gh_fetch_products()
            if gh_list is None:
                # GitHub yo'q — oddiy lokal (fallback)
                products = load(PRODUCTS_FILE, [])
                _apply_stock_deltas_to_list(products, deltas)
                try:
                    with open(PRODUCTS_FILE, "w", encoding="utf-8") as fh:
                        json.dump(products, fh, ensure_ascii=False, indent=2)
                except Exception as e:
                    print("stock lokal yozish xato:", e, flush=True)
                return
            _apply_stock_deltas_to_list(gh_list, deltas)
            try:
                with open(PRODUCTS_FILE, "w", encoding="utf-8") as fh:
                    json.dump(gh_list, fh, ensure_ascii=False, indent=2)
            except Exception as e:
                print("stock lokal yozish xato:", e, flush=True)
            if _gh_put_products(gh_list, sha):
                return
            time.sleep(0.35)   # konflikt — qayta urinamiz
        print("apply_stock_deltas: konflikt, urinishlar tugadi", flush=True)

# ─── Buyurtmalarni yo'qotmaslik: GitHub bilan birlashtirib yozish ─────────────
# MUAMMO: bot ham, sayt ham orders.json ni butunlay ustidan yozadi. Bir-birining
# buyurtmasini o'chirib yuborishi mumkin. Yechim: yozishdan oldin GitHub'dagi
# nusxa bilan id bo'yicha birlashtiramiz va SHA bilan yozamiz (konfliktda qayta).
_orders_lock = threading.Lock()

def _gh_fetch_orders():
    """GitHub'dan (buyurtmalar, sha). Xato bo'lsa (None, None)."""
    if not _GH_TOKEN:
        return None, None
    import base64
    try:
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/orders.json?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        data = json.loads(raw.decode("utf-8"))
        return (data, meta.get("sha")) if isinstance(data, list) else (None, None)
    except Exception as e:
        print("orders fetch xato:", e, flush=True)
        return None, None

def _gh_put_orders(orders_list, sha):
    if not _GH_TOKEN:
        return False
    import base64
    try:
        raw = json.dumps(orders_list, ensure_ascii=False, indent=2).encode("utf-8")
        payload = {"message": f"orders [{datetime.datetime.now():%Y-%m-%d %H:%M}]",
                   "content": base64.b64encode(raw).decode(), "branch": _GH_BRANCH}
        if sha:
            payload["sha"] = sha
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/orders.json"
        req = urllib.request.Request(url, json.dumps(payload).encode(), _gh_headers(), method="PUT")
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except Exception as e:
        if getattr(e, "code", None) == 409:
            return False
        print("orders PUT xato:", e, flush=True)
        return False

def _merge_orders(base, extra):
    """extra'dagi (GitHub) id'lari base'da yo'q buyurtmalarni qo'shadi."""
    have = {str(o.get("id")) for o in base if o.get("id")}
    added = [o for o in extra if str(o.get("id")) not in have]
    if not added:
        return base
    out = added + base
    out.sort(key=lambda o: str(o.get("date", "")))
    return out

def save_orders_synced(mutate):
    """GitHub'dagi eng so'nggi ro'yxatni olib, lokal bilan birlashtiradi,
    `mutate(orders)` ni qo'llaydi va SHA bilan qaytadan yozadi.
    mutate qaytargan qiymat natija sifatida qaytariladi (masalan buyurtma raqami)."""
    with _orders_lock:
        for _ in range(5):
            gh, sha = _gh_fetch_orders()
            local = load(ORDERS_FILE, [])
            merged = _merge_orders(local, gh) if gh is not None else local
            result = mutate(merged)
            # MUHIM: lokalga faqat GitHub'ga yozib bo'lgach yozamiz. Aks holda
            # konfliktda buyurtma lokalda qolib, chaqiruvchiga xato qaytadi (dubl).
            if gh is None or _gh_put_orders(merged, sha):
                try:
                    with open(ORDERS_FILE, "w", encoding="utf-8") as fh:
                        json.dump(merged, fh, ensure_ascii=False, indent=2)
                except Exception as e:
                    print("orders lokal yozish xato:", e, flush=True)
                return result
            time.sleep(0.35)   # SHA konflikti — qayta
        print("save_orders_synced: konflikt, urinishlar tugadi", flush=True)
        return None

def get_admins():
    try:
        a = json.load(open(os.path.join(BASE, "admins.json"), encoding="utf-8")).get("admins", [])
    except Exception:
        a = [5285940949, 512101064]
    for x in LIMITED_ADMINS:
        if x not in a:
            a.append(x)
    return a

def full_admins_only():
    """Faqat to'liq adminlar (Ibrohim, Rustam) — cheklanganlarsiz."""
    try:
        a = json.load(open(os.path.join(BASE, "admins.json"), encoding="utf-8")).get("admins", [])
    except Exception:
        a = [5285940949, 512101064]
    return [x for x in a if x not in LIMITED_ADMINS]

def status_admins():
    """Buyurtma holati tugmalari faqat shu (cheklangan) adminlarda."""
    return list(LIMITED_ADMINS) if LIMITED_ADMINS else full_admins_only()

def load(f, default):
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default

def save(f, data):
    try:
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        # GitHub real-time backup
        fname = os.path.basename(f)
        if fname in ("orders.json", "products.json"):
            _gh_push_file(f"data/{fname}", open(f, "rb").read(), fname)
    except Exception as e:
        print("save xato:", e, flush=True)

def somm(n):
    return format(int(n), ",").replace(",", " ")

def get_setting(key):
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None

def get_work_hours():
    """Botning ish vaqti sozlamasi (start/end/enabled)."""
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM work_hours WHERE id=1").fetchone()
        con.close()
        if row:
            return {"start_hour": row["start_hour"], "end_hour": row["end_hour"],
                    "enabled": row["enabled"]}
    except Exception:
        pass
    return {"start_hour": 9, "end_hour": 22, "enabled": 0}

def tashkent_hour():
    """Asia/Tashkent bo'yicha joriy soat (UTC+5)."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5)).hour

def is_open_now():
    wh = get_work_hours()
    if not wh["enabled"]:
        return True
    h = tashkent_hour()
    return wh["start_hour"] <= h < wh["end_hour"]

def add_web_tracking(order_id, customer_name):
    """Saytdagi buyurtma uchun tracking yozuvi (chat_id=0 — mijoz keyin botda bog'lanadi)."""
    try:
        con = sqlite3.connect(DB_FILE)
        now = datetime.datetime.now().isoformat()
        con.execute("""INSERT OR REPLACE INTO order_tracking
                       (order_id, chat_id, status, customer_name, created_at, updated_at)
                       VALUES (?, 0, 'new', ?, ?, ?)""", (order_id, customer_name, now, now))
        con.commit(); con.close()
    except Exception as e:
        print("tracking xato:", e, flush=True)

def set_tracking_status(order_id, status):
    """Buyurtma holatini yangilaydi (to'lov kelganda darrov 'confirmed')."""
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("UPDATE order_tracking SET status=?, updated_at=? WHERE order_id=?",
                    (status, datetime.datetime.now().isoformat(), str(order_id)))
        con.commit(); con.close()
    except Exception as e:
        print("tracking status xato:", e, flush=True)

def tg_send(chat_id, text, reply_markup=None):
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        body = urllib.parse.urlencode(payload).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TOKEN, data=body, timeout=10)
    except Exception as e:
        print("tg_send xato (%s): %s" % (chat_id, e), flush=True)

def tg_photo(chat_id, file_id, caption="", reply_markup=None):
    """Mahsulot rasmini (Telegram file_id) izoh bilan yuboradi."""
    if not (TOKEN and file_id):
        return
    try:
        payload = {"chat_id": chat_id, "photo": file_id, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        body = urllib.parse.urlencode(payload).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendPhoto" % TOKEN, data=body, timeout=15)
    except Exception as e:
        print("tg_photo xato (%s): %s" % (chat_id, e), flush=True)

def _order_item_photos(order_items, byid):
    """Buyurtma itemlari uchun (file_id, izoh) ro'yxati — admin ko'rishi uchun."""
    out = []
    for it in order_items:
        p = byid.get(str(it.get("product_id"))) if byid else None
        photos = []
        if p:
            photos = p.get("photos") or ([p["photo_id"]] if p.get("photo_id") else [])
        fid = photos[0] if photos else ""
        if not fid:
            continue
        size = (it.get("size") or "").strip()
        sz = "\n📏 Razmer: <b>%s</b>" % size if size else ""
        cap = ("📌 <b>%s</b>%s\n🔢 %d ta · %s so'm" % (
            it.get("name", ""), sz, int(it.get("qty", 1)), somm(int(it.get("subtotal", 0)))))
        out.append((fid, cap))
    return out

def notify_admin_order(chat_id, text, reply_markup=None, item_photos=None):
    """Buyurtmani adminga YUBORADI: birinchi mahsulot rasmi + to'liq ma'lumot IZOH sifatida.
    (Telegram caption limiti 1024 belgi — oshsa matn alohida ketadi.)"""
    item_photos = item_photos or []
    if item_photos and len(text) <= 1024:
        tg_photo(chat_id, item_photos[0][0], text, reply_markup=reply_markup)
        for fid, cap in item_photos[1:]:
            tg_photo(chat_id, fid, cap)
    else:
        tg_send(chat_id, text, reply_markup=reply_markup)
        for fid, cap in item_photos:
            tg_photo(chat_id, fid, cap)

def tracking_kb(order_id):
    """Admin uchun status tugmalari — bot.py dagi track_ handler bilan bir xil."""
    return {"inline_keyboard": [
        [{"text": "✅ Tasdiqlandi",     "callback_data": "track_confirmed_%s" % order_id}],
        [{"text": "🚚 Yetkazilmoqda",   "callback_data": "track_delivering_%s" % order_id}],
        [{"text": "✅ Yetkazildi",       "callback_data": "track_delivered_%s" % order_id}],
        [{"text": "❌ Bekor qilindi",    "callback_data": "track_cancelled_%s" % order_id}],
    ]}

# ─────────────────────────  AUTH (sayt mijozlari)  ─────────────────────────
# SMS hozircha "onsite" — kod javobda qaytadi va saytda chiqadi.
# Keyin real SMS ulash uchun: SMS_MODE=real qiling va send_sms() ichini to'ldiring.
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
SMS_MODE = os.getenv("SMS_MODE", "disabled").strip().lower()
ALLOW_DEV_SMS_CODE = os.getenv("ALLOW_DEV_SMS_CODE", "false").strip().lower() == "true"
CODE_TTL = int(os.getenv("SMS_CODE_TTL_SECONDS", "300"))
SESSION_TTL = int(os.getenv("SESSION_DAYS", "30")) * 86400
SESSION_COOKIE = "bd_session"

def _dev_sms_allowed():
    return APP_ENV != "production" and SMS_MODE == "onsite" and ALLOW_DEV_SMS_CODE


def ensure_web_tables():
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("""CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, phone TEXT UNIQUE, pass_hash TEXT, created_at TEXT, chat_id INTEGER DEFAULT 0)""")
        try:
            con.execute("ALTER TABLE web_users ADD COLUMN chat_id INTEGER DEFAULT 0")
        except Exception:
            pass
        con.execute("""CREATE TABLE IF NOT EXISTS web_codes (
            phone TEXT PRIMARY KEY, code TEXT, expires REAL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
            token TEXT PRIMARY KEY, phone TEXT, created REAL)""")
        con.execute("DELETE FROM web_sessions WHERE created < ?", (time.time() - SESSION_TTL,))
        con.execute("DELETE FROM web_codes WHERE expires < ?", (time.time(),))
        con.commit(); con.close()
    except Exception as e:
        print("web auth tables xato:", e, flush=True)

def normalize_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 9:
        d = "998" + d
    if d.startswith("8") and len(d) == 10:
        d = "99" + d
    return d

def hash_pw(pw, salt=None):
    if salt is None:
        salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), 100000).hex()
    return salt + "$" + h

def verify_pw(pw, stored):
    try:
        salt, h = (stored or "").split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), 100000).hex()
        return secrets.compare_digest(actual, h)
    except Exception:
        return False

def gen_code():
    return "%06d" % secrets.randbelow(1000000)

def send_sms(phone, code):
    """SMS provider adapter. Productionda kod faqat real provider orqali yuboriladi."""
    if _dev_sms_allowed():
        return True
    if SMS_MODE != "real":
        return False
    # Provider ulanmaguncha xavfsiz fail-closed.
    # Eskiz/Play Mobile integratsiyasi shu yerga qo'shiladi.
    return False

def new_session(phone):
    token = secrets.token_urlsafe(24)
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("INSERT OR REPLACE INTO web_sessions (token, phone, created) VALUES (?,?,?)",
                    (token, phone, time.time()))
        con.commit(); con.close()
    except Exception as e:
        print("session xato:", e, flush=True)
    return token

def get_session_phone(token=None):
    token = (token or request.cookies.get(SESSION_COOKIE, "")).strip()
    if not token:
        return None
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT phone, created FROM web_sessions WHERE token=?", (token,)).fetchone()
        if not row:
            con.close()
            return None
        if time.time() - float(row[1] or 0) > SESSION_TTL:
            con.execute("DELETE FROM web_sessions WHERE token=?", (token,))
            con.commit(); con.close()
            return None
        con.close()
        return row[0]
    except Exception:
        return None

def _set_session_cookie(response, token):
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        secure=(APP_ENV == "production"), samesite="Lax", path="/")
    return response

def _clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response

def get_user_by_phone(phone):
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM web_users WHERE phone=?", (phone,)).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None

# ── Yetkazib berish (bot.py bilan AYNAN bir xil formula) ──
def get_shop_location():
    lat = get_setting("shop_lat"); lon = get_setting("shop_lon")
    if lat and lon:
        try:
            return float(lat), float(lon)
        except Exception:
            return None
    return None

def get_price_per_km():
    try:
        return int(get_setting("price_per_km") or 3000)
    except Exception:
        return 3000

def get_min_delivery():
    """Eng kam yetkazish summasi — botdagi sozlamadan (bir xil SQLite baza)."""
    try:
        return int(get_setting("min_delivery") or 30000)
    except Exception:
        return 30000

def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371
    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def calc_delivery(lat, lon):
    """Mijoz lokatsiyasiga qarab yetkazib berish (bot bilan bir xil).
    Do'kon nuqtasi yo'q yoki lokatsiya yo'q bo'lsa (0, None)."""
    shop = get_shop_location()
    if not shop or lat is None or lon is None:
        return 0, None
    try:
        dist = haversine_km(shop[0], shop[1], float(lat), float(lon))
    except Exception:
        return 0, None
    real_km = dist * 1.3
    summa = int(round(real_km * get_price_per_km() / 1000) * 1000)
    minimal = get_min_delivery()
    if summa < minimal:
        summa = minimal          # eng kam dastavka (botdagi sozlamadan)
    return summa, round(real_km, 1)

# ── Promo & Cashback (bot bilan AYNAN bir xil bazadan) ──
def get_promo(code):
    if not code:
        return None
    try:
        con = sqlite3.connect(DB_FILE); con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM promo_codes WHERE code=? AND active=1", (code.upper(),)).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None

def use_promo(code):
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT uses_left FROM promo_codes WHERE code=?", (code.upper(),)).fetchone()
        if row and row[0] is not None and row[0] > 0:
            nl = row[0] - 1
            if nl == 0:
                con.execute("UPDATE promo_codes SET uses_left=0, active=0 WHERE code=?", (code.upper(),))
            else:
                con.execute("UPDATE promo_codes SET uses_left=? WHERE code=?", (nl, code.upper()))
            con.commit()
        con.close()
    except Exception:
        pass

def promo_amount(promo, subtotal):
    if not promo:
        return 0
    try:
        disc = int(promo.get("discount", 0)); typ = promo.get("type", "percent")
        if typ == "percent":
            return max(0, int(subtotal * disc / 100))
        return max(0, min(disc, subtotal))
    except Exception:
        return 0

def cashback_on():
    return get_setting("cashback_on") == "1"

def cashback_percent():
    try:
        return int(get_setting("cashback_percent") or "5")
    except Exception:
        return 5

def get_cashback(chat_id):
    if not chat_id:
        return 0
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
        con.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0

def spend_cashback(chat_id, amount):
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
        bal = int(row[0]) if row else 0
        use = min(bal, max(0, int(amount)))
        con.execute("INSERT OR REPLACE INTO cashback (chat_id, balance) VALUES (?, ?)", (chat_id, bal - use))
        con.commit(); con.close()
        return use
    except Exception:
        return 0

def add_cashback(chat_id, amount):
    """Cashback qo'shadi (bekor qilinganda qaytarish uchun). Yangi balansni qaytaradi."""
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
        bal = (int(row[0]) if row else 0) + max(0, int(amount))
        con.execute("INSERT OR REPLACE INTO cashback (chat_id, balance) VALUES (?, ?)", (chat_id, bal))
        con.commit(); con.close()
        return bal
    except Exception as e:
        print("add_cashback xato:", e, flush=True)
        return 0

def refund_promo(code):
    """Promo kodning bitta ishlatilishini qaytaradi (cheksiz kodga tegmaydi)."""
    code = (code or "").strip().upper()
    if not code:
        return
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT uses_left FROM promo_codes WHERE code=?", (code,)).fetchone()
        if row and row[0] is not None and int(row[0]) >= 0:
            con.execute("UPDATE promo_codes SET uses_left=uses_left+1, active=1 WHERE code=?", (code,))
            con.commit()
        con.close()
    except Exception as e:
        print("refund_promo xato:", e, flush=True)

def get_web_user_chat(phone):
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT chat_id FROM web_users WHERE phone=?", (normalize_phone(phone),)).fetchone()
        con.close()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0

def find_chat_by_phone(phone):
    """Botdagi buyurtmalardan shu telefonga tegishli Telegram chat_id ni topadi.
    (Telefon SMS bilan tasdiqlangani uchun xavfsiz.)"""
    ph = normalize_phone(phone)
    if not ph:
        return 0
    try:
        orders = load(ORDERS_FILE, [])
        ids = [str(o.get("id")) for o in orders if normalize_phone(o.get("phone")) == ph]
        if not ids:
            return 0
        con = sqlite3.connect(DB_FILE)
        rows = con.execute("SELECT order_id, chat_id FROM order_tracking WHERE chat_id != 0").fetchall()
        con.close()
        m = {str(r[0]): r[1] for r in rows}
        # eng oxirgi buyurtmadan boshlab qidiramiz
        for oid in reversed(ids):
            cid = m.get(oid)
            if cid:
                return int(cid)
    except Exception:
        return 0
    return 0

def link_web_user_if_possible(phone):
    """Sayt akkauntini telefon bo'yicha botdagi akkauntga avtomatik bog'laydi."""
    if get_web_user_chat(phone):
        return get_web_user_chat(phone)
    cid = find_chat_by_phone(phone)
    if cid:
        try:
            con = sqlite3.connect(DB_FILE)
            con.execute("UPDATE web_users SET chat_id=? WHERE phone=?", (cid, normalize_phone(phone)))
            con.commit(); con.close()
        except Exception:
            pass
    return cid

ensure_web_tables()

app = Flask(__name__, static_folder=None)
app.url_map.strict_slashes = False
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response   # /payme va /payme/ ikkalasi ham ishlaydi (303 redirect bo'lmaydi)

@app.route("/health")
def health():
    """UptimeRobot / monitoring uchun. 200 qaytsa — sayt ishlayapti."""
    import datetime as _dt
    orders_ok = os.path.exists(ORDERS_FILE)
    products_ok = os.path.exists(PRODUCTS_FILE)
    token_ok = bool(TOKEN)
    return jsonify({
        "status": "ok",
        "time": _dt.datetime.now().isoformat(),
        "orders": orders_ok,
        "products": products_ok,
        "telegram": "configured" if token_ok else "not_configured",
    }), 200


@app.route("/api/photo-test")
def api_photo_test():
    """Faqat development rejimida Telegram ulanishini tekshiradi."""
    if APP_ENV == "production":
        return jsonify({"ok": False, "error": "not_found"}), 404
    if not TOKEN:
        return jsonify({"ok": False, "error": "BOT_TOKEN yo'q"}), 200
    try:
        u = "https://api.telegram.org/bot%s/getMe" % TOKEN
        r = json.load(urllib.request.urlopen(u, timeout=5))
        return jsonify({"ok": True, "bot": r.get("result", {}).get("username")}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/")
def home():
    return send_file(os.path.join(BASE, "index.html"))

@app.route("/assets/<path:filename>")
def assets(filename):
    # Logo, favicon, hero/gift rasmlari — repo ildizidagi assets/ papkadan
    return send_from_directory(os.path.join(BASE, "assets"), filename)

@app.route("/api/reviews")
def api_reviews():
    """Tasdiqlangan fikrlar — bot va sayt birgalikda reviews.json ishlatadi."""
    try:
        reviews = load(REVIEWS_FILE, [])
        out = [{"name": r.get("name","Mijoz"), "text": r.get("text",""),
                "stars": int(r.get("stars",5)), "photo_id": r.get("photo_id","")}
               for r in reviews]
        return jsonify(out)
    except Exception:
        return jsonify([])


@app.route("/api/reviews/submit", methods=["POST"])
def api_review_submit():
    """Saytdan fikr yuborish → pending_reviews.json → admin botda tasdiqlaydi → reviews.json."""
    ip = _client_ip()
    b, retry = _rl_blocked("review:" + ip, 1, 1800)
    if b:
        return jsonify({"ok": False, "error": "limit", "retry": retry}), 200

    d    = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()[:80] or "Mijoz"
    text = (d.get("text") or "").strip()[:1000]
    stars = max(1, min(5, int(d.get("stars") or 5)))

    if not text or len(text) < 5:
        return jsonify({"ok": False, "error": "short"}), 200

    # Login bo'lsa — ismni sessiyadan olamiz
    token = (d.get("token") or "").strip()
    if token and not d.get("name"):
        phone = get_session_phone(token)
        if phone:
            u = get_user_by_phone(phone)
            if u and u.get("name"):
                name = u["name"]

    import uuid as _uuid
    rid = str(_uuid.uuid4())[:8]
    review = {"id": rid, "name": name, "text": text,
              "stars": stars, "source": "web", "photo_id": ""}

    try:
        pending = load(PENDING_REVIEWS_FILE, [])
        pending.append(review)
        save(PENDING_REVIEWS_FILE, pending)
    except Exception as e:
        print(f"review submit xato: {e}", flush=True)
        return jsonify({"ok": False, "error": "server"}), 200

    # Adminlarga xabar
    if TOKEN:
        try:
            msg = (f"💬 Saytdan yangi fikr\n{'⭐'*stars} ({stars}/5)\n\n"
                   f"👤 {name}\n{text}\n\nID: {rid}\n/pending_reviews")
            admins = load(os.path.join(BASE, "admins.json"), {}).get("admins", [5285940949, 512101064])
            for aid in admins:
                try:
                    body = json.dumps({"chat_id": aid, "text": msg}).encode()
                    urllib.request.urlopen(urllib.request.Request(
                        "https://api.telegram.org/bot%s/sendMessage" % TOKEN,
                        data=body, headers={"Content-Type": "application/json"}), timeout=5)
                except Exception:
                    pass
        except Exception:
            pass

    _rl_event("review:" + ip, 1800)
    return jsonify({"ok": True, "id": rid})


@app.route("/api/products")
def api_products():
    refresh_products_from_gh()   # botdagi o'zgarishlar (o'chirish/qo'shish) saytga yetsin
    # Real sotilgan sonini orders.json dan hisoblaymiz
    sold_map = {}  # {product_id: count}
    try:
        orders = load(ORDERS_FILE, [])
        for o in orders:
            # Bekor qilingan / ombori qaytarilgan buyurtmalar sotuv emas
            if o.get("status") == "cancelled" or o.get("stock_restored"):
                continue
            for it in o.get("items", []):
                pid = str(it.get("product_id", ""))
                if pid:
                    sold_map[pid] = sold_map.get(pid, 0) + int(it.get("qty", 1))
    except Exception:
        pass

    out = []
    for p in load(PRODUCTS_FILE, []):
        sizes = p.get("sizes") or []
        total = sum(int(s.get("stock", 0)) for s in sizes) if sizes else int(p.get("stock", 0))
        if total <= 0:
            continue
        photos = p.get("photos") or ([p["photo_id"]] if p.get("photo_id") else [])
        pid = str(p.get("id", ""))
        out.append({
            "id":         p.get("id"),
            "name_uz":    p.get("name_uz", ""),
            "name_ru":    p.get("name_ru", ""),
            "price":      int(p.get("price", 0)),
            "category":   p.get("category", "Boshqa"),
            "gender":     p.get("gender", "unisex"),
            "stock":      total,
            "sizes":      [{"label": s.get("label", ""), "stock": int(s.get("stock", 0))} for s in sizes],
            "desc_uz":    p.get("desc_uz", ""),
            "desc_ru":    p.get("desc_ru", ""),
            "age":        p.get("age", []),
            "photos":     [x for x in photos if x],
            "added_at":   p.get("added_at", ""),
            "sold_count": sold_map.get(pid, 0),  # real sotilgan son
        })
    return jsonify(out)

@app.route("/api/categories")
def api_categories():
    try:
        names = json.loads(get_setting("category_list") or "[]")
        if not isinstance(names, list):
            names = []
    except Exception:
        names = []
    try:
        imgs = json.loads(get_setting("category_images") or "{}")
        if not isinstance(imgs, dict):
            imgs = {}
    except Exception:
        imgs = {}
    try:
        ru = json.loads(get_setting("category_ru") or "{}")
        if not isinstance(ru, dict):
            ru = {}
    except Exception:
        ru = {}
    # name — kanonik (uz) nom, mahsulotdagi `category` bilan mos.
    # name_ru — faqat ko'rsatish uchun.
    return jsonify([{"name": n, "name_ru": ru.get(n, ""), "photo": imgs.get(n, "")} for n in names])

_fpcache = {}

@app.route("/api/photo")
def api_photo():
    fid = request.args.get("id", "")
    if not fid:
        return ("", 404)
    if not TOKEN:
        print("api/photo: BOT_TOKEN yo'q — rasm ko'rsatib bo'lmaydi", flush=True)
        return ("", 404)
    try:
        path = _fpcache.get(fid)
        if not path:
            u = "https://api.telegram.org/bot%s/getFile?file_id=%s" % (TOKEN, urllib.parse.quote(fid))
            r = json.load(urllib.request.urlopen(u, timeout=10))
            if not r.get("ok"):
                print(f"api/photo getFile xato: {r}", flush=True)
                return ("", 404)
            path = r["result"]["file_path"]
            _fpcache[fid] = path
        data = urllib.request.urlopen(
            "https://api.telegram.org/file/bot%s/%s" % (TOKEN, path), timeout=20).read()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return Response(data, mimetype=mime, headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        print(f"api/photo xato (fid={fid[:20]}): {e}", flush=True)
        return ("", 404)

@app.route("/api/settings")
def api_settings():
    wh = get_work_hours()
    return jsonify({
        "workhours": {
            "enabled": bool(wh["enabled"]),
            "start": wh["start_hour"],
            "end": wh["end_hour"],
            "open": is_open_now(),
        },
        "pay": {
            # Karta raqami saqlanmaydi/uzatilmaydi — faqat onlayn kassa
            "click_online": click_enabled(),                 # Click SHOP API sozlanganmi
            "payme_online": payme_enabled(),                # Payme kassa sozlanganmi
        },
        "operator": get_setting("operator") or "",
        "bot": BOT_USERNAME,
        "delivery": _delivery_settings(),
        "cashback": {"on": cashback_on(), "percent": cashback_percent()},
    })

def _delivery_settings():
    shop = get_shop_location()
    return {
        "shop_lat": shop[0] if shop else None,
        "shop_lon": shop[1] if shop else None,
        "per_km": get_price_per_km(),
        "min": get_min_delivery(),
        "coef": 1.3,
        "has_shop": bool(shop),
    }

@app.route("/api/delivery-quote", methods=["POST"])
def api_delivery_quote():
    d = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(d.get("lat")); lon = float(d.get("lng"))
    except Exception:
        return jsonify({"ok": False, "error": "coords"}), 200
    summa, km = calc_delivery(lat, lon)
    return jsonify({"ok": True, "delivery": summa, "km": km, "has_shop": bool(get_shop_location())})

@app.route("/api/promo", methods=["POST"])
def api_promo():
    d = request.get_json(force=True, silent=True) or {}
    code = (d.get("code") or "").strip()
    subtotal = int(d.get("subtotal", 0) or 0)
    if not code:
        return jsonify({"ok": False, "error": "empty"}), 200
    promo = get_promo(code)
    if not promo:
        return jsonify({"ok": False, "error": "invalid"}), 200
    if promo.get("uses_left", -1) == 0:
        return jsonify({"ok": False, "error": "used"}), 200
    amount = promo_amount(promo, subtotal)
    return jsonify({"ok": True, "code": code.upper(), "type": promo.get("type", "percent"),
                    "discount": int(promo.get("discount", 0)), "amount": amount})

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    d = request.get_json(force=True, silent=True) or {}
    phone = get_session_phone((d.get("token") or "").strip())
    if not phone:
        return jsonify({"ok": False, "error": "auth"}), 200
    chat = get_web_user_chat(phone) or link_web_user_if_possible(phone)
    return jsonify({"ok": True, "enabled": cashback_on(), "percent": cashback_percent(),
                    "linked": bool(chat), "balance": (get_cashback(chat) if chat else 0)})

def next_number(orders):
    n = 0
    for o in orders:
        try:
            n = max(n, int(o.get("number", 0)))
        except Exception:
            pass
    return n + 1

# ══════════════════════════════════════════════════════════════
#                    PAYME MERCHANT API
# ══════════════════════════════════════════════════════════════
# Payme bizning serverga JSON-RPC so'rov yuboradi. Biz javob beramiz.
# Tranzaksiyalar orders.json ichida har buyurtmaning "payme" maydonida saqlanadi.

# Payme xato kodlari
PAYME_ERR = {
    "auth":         {"code": -32504, "message": {"uz": "Ruxsat yo'q", "ru": "Нет доступа", "en": "No access"}},
    "method":       {"code": -32601, "message": {"uz": "Metod topilmadi", "ru": "Метод не найден", "en": "Method not found"}},
    "order_404":    {"code": -31050, "message": {"uz": "Buyurtma topilmadi", "ru": "Заказ не найден", "en": "Order not found"}},
    "amount":       {"code": -31001, "message": {"uz": "Summa noto'g'ri", "ru": "Неверная сумма", "en": "Wrong amount"}},
    "tx_404":       {"code": -31003, "message": {"uz": "Tranzaksiya topilmadi", "ru": "Транзакция не найдена", "en": "Transaction not found"}},
    "cant_perform": {"code": -31008, "message": {"uz": "Amalni bajarib bo'lmaydi", "ru": "Невозможно выполнить", "en": "Cannot perform"}},
    "cant_cancel":  {"code": -31007, "message": {"uz": "Bekor qilib bo'lmaydi", "ru": "Невозможно отменить", "en": "Cannot cancel"}},
}

def _payme_error(req_id, key, data=None):
    e = dict(PAYME_ERR[key])
    if data:
        e["data"] = data
    return jsonify({"error": e, "id": req_id})

def _payme_auth_ok(req):
    """Authorization: Basic base64('Paycom:KEY') tekshiruvi."""
    import base64 as _b64
    hdr = req.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return False
    try:
        decoded = _b64.b64decode(hdr[6:]).decode()
        # format: "Paycom:KEY"
        _, _, key = decoded.partition(":")
        return key == PAYME_KEY and bool(PAYME_KEY)
    except Exception:
        return False

def _find_order_by_number(number):
    """Payme account.order_id → orders.json dagi number."""
    try:
        num = int(number)
    except Exception:
        return None
    for o in load(ORDERS_FILE, []):
        if int(o.get("number", -1)) == num:
            return o
    return None

def _save_order_obj(order):
    """Buyurtmani yangilaydi (Payme holati). GitHub bilan birlashtirib yozadi —
    boshqa manbadan kelgan buyurtmalar o'chib ketmasin."""
    def _upd(merged):
        for i, o in enumerate(merged):
            if o.get("id") == order.get("id"):
                merged[i] = order
                return True
        merged.append(order)
        return True
    save_orders_synced(_upd)

@app.route("/payme", methods=["POST"])
@app.route("/payme/", methods=["POST"])
def payme_endpoint():
    # 1. Autentifikatsiya
    body = request.get_json(force=True, silent=True) or {}
    req_id = body.get("id")
    if not _payme_auth_ok(request):
        return _payme_error(req_id, "auth")

    method = body.get("method", "")
    params = body.get("params", {})

    # ── CheckPerformTransaction: buyurtma bormi, to'lash mumkinmi? ──
    if method == "CheckPerformTransaction":
        account = params.get("account", {})
        amount  = int(params.get("amount", 0))  # tiyin
        order = _find_order_by_number(account.get("order_id"))
        if not order:
            return _payme_error(req_id, "order_404", "order_id")
        if int(order.get("total", 0)) * 100 != amount:
            return _payme_error(req_id, "amount")
        if order.get("status") in ("cancelled",):
            return _payme_error(req_id, "cant_perform")
        if _order_stock_shortages(order):     # tovar tugagan -> to'lovga ruxsat yo'q
            return _payme_error(req_id, "cant_perform", "stock")
        return jsonify({"result": {"allow": True}, "id": req_id})

    # ── CreateTransaction: tranzaksiya yaratish ──
    if method == "CreateTransaction":
        account = params.get("account", {})
        amount  = int(params.get("amount", 0))
        tx_id   = params.get("id")   # Payme tranzaksiya ID
        ptime   = params.get("time", int(time.time() * 1000))
        order = _find_order_by_number(account.get("order_id"))
        if not order:
            return _payme_error(req_id, "order_404", "order_id")
        if int(order.get("total", 0)) * 100 != amount:
            return _payme_error(req_id, "amount")

        pm = order.get("payme") or {}
        # Faqat FAOL tranzaksiya (yaratilgan=1 yoki to'langan=2) boshqa tx_id bilan
        # kelsa bloklaymiz. Bekor qilingan (-1/-2) bo'lsa — qayta to'lashga ruxsat.
        if pm.get("tx_id") and pm.get("tx_id") != tx_id and pm.get("state") in (1, 2):
            return _payme_error(req_id, "cant_perform")
        # Yangi tranzaksiya (yoki bekor qilingandan keyin qayta urinish).
        # Tovar bu yerda BAND QILINMAYDI — faqat mavjudligini tekshiramiz.
        if pm.get("tx_id") != tx_id:
            if _order_stock_shortages(order):
                return _payme_error(req_id, "cant_perform", "stock")
            pm = {"tx_id": tx_id, "state": 1, "create_time": ptime,
                  "perform_time": 0, "cancel_time": 0, "reason": None}
            order["payme"] = pm
            order["status"] = "pending_pay"
            _save_order_obj(order)
        return jsonify({"result": {
            "create_time": pm["create_time"],
            "transaction": str(order.get("number")),
            "state": pm["state"],
        }, "id": req_id})

    # ── PerformTransaction: to'lovni tasdiqlash (pul o'tdi) ──
    if method == "PerformTransaction":
        tx_id = params.get("id")
        order = None
        for o in load(ORDERS_FILE, []):
            if (o.get("payme") or {}).get("tx_id") == tx_id:
                order = o; break
        if not order:
            return _payme_error(req_id, "tx_404")
        pm = order["payme"]
        if pm["state"] == 2:  # allaqachon bajarilgan
            return jsonify({"result": {
                "transaction": str(order.get("number")),
                "perform_time": pm["perform_time"],
                "state": 2,
            }, "id": req_id})
        if pm["state"] != 1:
            return _payme_error(req_id, "cant_perform")
        # ⬇️ TOVAR AYNAN SHU YERDA OMBORDAN YECHILADI (to'lov yakunlanmoqda).
        # Yetmasa — xato qaytaramiz, Payme pulni yechmaydi va bekor qiladi.
        if not take_order_stock(order):
            _notify_admins_stock_fail(order, "Payme")
            return _payme_error(req_id, "cant_perform", "stock")
        # To'lovni tasdiqlaymiz
        now_ms = int(time.time() * 1000)
        pm["state"] = 2
        pm["perform_time"] = now_ms
        order["status"] = "paid"
        _save_order_obj(order)
        # Botga xabar — mijoz va adminlarga
        _payme_notify_paid(order)
        return jsonify({"result": {
            "transaction": str(order.get("number")),
            "perform_time": now_ms,
            "state": 2,
        }, "id": req_id})

    # ── CancelTransaction: bekor qilish ──
    if method == "CancelTransaction":
        tx_id  = params.get("id")
        reason = params.get("reason")
        order = None
        for o in load(ORDERS_FILE, []):
            if (o.get("payme") or {}).get("tx_id") == tx_id:
                order = o; break
        if not order:
            return _payme_error(req_id, "tx_404")
        pm = order["payme"]
        now_ms = int(time.time() * 1000)
        if pm["state"] in (1, 2):
            # Tovar faqat PerformTransaction'da yechilgan. state=1 (to'lanmagan)
            # bo'lsa yechilmagan — release_order_stock() hech narsa qilmaydi.
            release_order_stock(order)
            pm["state"] = -2 if pm["state"] == 2 else -1
            pm["cancel_time"] = now_ms
            pm["reason"] = reason
            order["status"] = "cancelled"
            _save_order_obj(order)
        return jsonify({"result": {
            "transaction": str(order.get("number")),
            "cancel_time": pm["cancel_time"],
            "state": pm["state"],
        }, "id": req_id})

    # ── CheckTransaction: holatni tekshirish ──
    if method == "CheckTransaction":
        tx_id = params.get("id")
        order = None
        for o in load(ORDERS_FILE, []):
            if (o.get("payme") or {}).get("tx_id") == tx_id:
                order = o; break
        if not order:
            return _payme_error(req_id, "tx_404")
        pm = order["payme"]
        return jsonify({"result": {
            "create_time":  pm.get("create_time", 0),
            "perform_time": pm.get("perform_time", 0),
            "cancel_time":  pm.get("cancel_time", 0),
            "transaction":  str(order.get("number")),
            "state":        pm.get("state", 1),
            "reason":       pm.get("reason"),
        }, "id": req_id})

    # ── GetStatement: davr bo'yicha tranzaksiyalar ──
    if method == "GetStatement":
        frm = int(params.get("from", 0))
        to  = int(params.get("to", 0))
        txs = []
        for o in load(ORDERS_FILE, []):
            pm = o.get("payme")
            if not pm or not pm.get("tx_id"):
                continue
            ct = pm.get("create_time", 0)
            if frm <= ct <= to:
                txs.append({
                    "id": pm["tx_id"],
                    "time": ct,
                    "amount": int(o.get("total", 0)) * 100,
                    "account": {"order_id": str(o.get("number"))},
                    "create_time": ct,
                    "perform_time": pm.get("perform_time", 0),
                    "cancel_time": pm.get("cancel_time", 0),
                    "transaction": str(o.get("number")),
                    "state": pm.get("state", 1),
                    "reason": pm.get("reason"),
                })
        return jsonify({"result": {"transactions": txs}, "id": req_id})

    return _payme_error(req_id, "method")

def _is_stock_taken(order):
    """Bu buyurtma uchun tovar hozir ombordan yechilganmi?"""
    if "stock_taken" in order:
        return bool(order["stock_taken"])
    # Eski (deploy'dan oldingi) buyurtmalar: tovar yaratilganda yechilgan edi
    return order.get("stock_model") != "on_payment" and not order.get("stock_restored")

def _order_stock_shortages(order):
    """Ombordan oshgan pozitsiyalar: [(nom, kerak, bor)]."""
    refresh_products_from_gh(force=True)
    byid = {str(p.get("id")): p for p in load(PRODUCTS_FILE, [])}
    out = []
    for it in order.get("items", []):
        need = int(it.get("qty", 1))
        size = (it.get("size") or "").strip()
        nm = it.get("name", "") or str(it.get("product_id"))
        if size:
            nm = "%s (%s)" % (nm, size)
        p = byid.get(str(it.get("product_id")))
        if not p:
            out.append((nm, need, 0))
            continue
        sizes = p.get("sizes") or []
        if sizes and size:
            rec = next((s for s in sizes if s.get("label") == size), None)
            avail = int(rec.get("stock", 0)) if rec else 0
        else:
            avail = int(p.get("stock", 0))
        if need > avail:
            out.append((nm, need, avail))
    return out

def _notify_low_stock_web(order):
    try:
        threshold = int(get_setting("low_stock_threshold") or 3)
    except Exception:
        threshold = 3
    byid = {str(p.get("id")): p for p in load(PRODUCTS_FILE, [])}
    lines = []
    for it in order.get("items", []):
        p = byid.get(str(it.get("product_id")))
        if not p:
            continue
        size = (it.get("size") or "").strip()
        sizes = p.get("sizes") or []
        if sizes and size:
            rec = next((s for s in sizes if s.get("label") == size), None)
            left = int(rec.get("stock", 0)) if rec else 0
        else:
            left = int(p.get("stock", 0))
        if left <= threshold:
            nm = p.get("name_uz", "")
            if size:
                nm = "%s (%s)" % (nm, size)
            lines.append(("🔴 %s — tugadi (0 dona)" % nm) if left <= 0
                         else ("🟡 %s — %d dona qoldi" % (nm, left)))
    if lines:
        msg = "⚠️ Ombor kam qoldi!\n\n" + "\n".join(lines) + ("\n\nChegara: %d dona." % threshold)
        for aid in full_admins_only():
            tg_send(aid, msg)

def take_order_stock(order):
    """Tovarni ombordan YECHADI — faqat to'lov yakunlanganda. Idempotent.
    False qaytsa: omborda yetarli emas, to'lov qabul QILINMASLIGI kerak."""
    if _is_stock_taken(order):
        return True
    short = _order_stock_shortages(order)
    if short:
        print("take_order_stock: ombor yetarli emas ->", short, flush=True)
        return False
    apply_stock_deltas([{"product_id": str(it.get("product_id")), "size": it.get("size", ""),
                         "qty": -int(it.get("qty", 1))} for it in order.get("items", [])])
    order["stock_taken"] = True
    order.pop("stock_restored", None)
    try:
        _notify_low_stock_web(order)
    except Exception as e:
        print("low stock notify:", e, flush=True)
    return True

def release_order_stock(order):
    """Tovarni omborga QAYTARADI — faqat yechilgan bo'lsa (bir marta)."""
    if not _is_stock_taken(order):
        order["stock_taken"] = False
        return
    try:
        apply_stock_deltas([{"product_id": str(it.get("product_id")), "size": it.get("size", ""),
                             "qty": int(it.get("qty", 1))} for it in order.get("items", [])])
        order["stock_taken"] = False
        order["stock_restored"] = True
    except Exception as e:
        print("release_order_stock xato:", e, flush=True)

def _notify_admins_stock_fail(order, provider):
    """To'lov ombor yetmagani uchun rad etilganda adminlarga shoshilinch xabar."""
    try:
        short = _order_stock_shortages(order)
        lines = "\n".join("• %s: kerak %d, bor %d" % s for s in short)
        msg = ("🚨 TO'LOV RAD ETILDI — omborda yetarli emas\n\n"
               "Buyurtma #%05d (%s)\n%s\n\n"
               "Mijozdan pul yechilmadi. Omborni to'ldiring va mijoz bilan bog'laning."
               % (int(order.get("number") or 0), provider, lines))
        for aid in get_admins():
            tg_send(aid, msg)
    except Exception as e:
        print("stock fail notify:", e, flush=True)

def _payme_notify_paid(order):
    _notify_admins_paid(order, "Payme")

# ═══════════════════════ CLICK SHOP API ═══════════════════════
# Rasmiy hujjat: https://docs.click.uz/en/click-api-request/
#   Prepare  (action=0): md5(click_trans_id + service_id + SECRET + merchant_trans_id + amount + action + sign_time)
#   Complete (action=1): md5(click_trans_id + service_id + SECRET + merchant_trans_id + merchant_prepare_id + amount + action + sign_time)
# Xato kodlari: 0 ok, -1 imzo, -2 summa, -3 action, -4 allaqachon to'langan,
#               -5 buyurtma topilmadi, -6 tranzaksiya yo'q, -8 so'rov xato, -9 bekor qilingan
CLICK_OK, CLICK_SIGN, CLICK_AMOUNT, CLICK_ACTION = 0, -1, -2, -3
CLICK_PAID, CLICK_NO_ORDER, CLICK_NO_TX, CLICK_BAD_REQ, CLICK_CANCELLED = -4, -5, -6, -8, -9

def payme_enabled():
    """Kassa ID ham, Merchant API kaliti ham bo'lsa. Kalit bo'lmasa
    _payme_auth_ok() hamma callback'ni rad etadi — to'lov ishlamaydi."""
    return bool(PAYME_MERCHANT_ID and PAYME_KEY)

def click_enabled():
    return bool(CLICK_SERVICE_ID and CLICK_MERCHANT_ID and CLICK_SECRET_KEY)

def _click_err(code, note, extra=None):
    out = {"error": code, "error_note": note}
    if extra:
        out.update(extra)
    return jsonify(out)

def _click_sign_ok(f, with_prepare_id):
    """sign_string ni tekshiradi. Qatorlar AYNAN kelgan holida qo'shiladi."""
    parts = [f.get("click_trans_id", ""), f.get("service_id", ""), CLICK_SECRET_KEY,
             f.get("merchant_trans_id", "")]
    if with_prepare_id:
        parts.append(f.get("merchant_prepare_id", ""))
    parts += [f.get("amount", ""), f.get("action", ""), f.get("sign_time", "")]
    calc = hashlib.md5("".join(parts).encode()).hexdigest()
    return calc == (f.get("sign_string", "") or "").lower()

def _order_maps_link(order):
    if order.get("maps"):
        return order["maps"]
    lat = order.get("lat")
    lon = order.get("lng") if order.get("lng") is not None else order.get("lon")
    if lat is None or lon is None or str(lat) == "" or str(lon) == "":
        return ""
    return "https://maps.google.com/?q=%s,%s" % (lat, lon)

def _admin_paid_message(order, provider):
    """To'lov tasdiqlangandan keyin adminlarga ketadigan TO'LIQ xabar.
    Bot va sayt buyurtmalari uchun bir xil ishlaydi."""
    num = int(order.get("number") or 0)
    manba = "saytdan" if order.get("source") == "sayt" else "botdan"

    def _pi(it):
        size = (it.get("size") or "").strip()
        return "• %s%s x%d — %s so'm" % (it.get("name", ""), " (%s)" % size if size else "",
                                          it.get("qty", 1), somm(it.get("subtotal", 0)))
    lines = "\n".join(_pi(it) for it in order.get("items", []))

    maps = _order_maps_link(order)
    maps_line = ("📍 <a href=\"%s\">Xaritada ochish</a>\n" % maps) if maps else ""
    km = order.get("delivery_km")
    km_txt = (" (%s km)" % km) if km else ""

    disc = ""
    if int(order.get("promo_discount", 0) or 0) > 0:
        disc += "🏷 Promo (%s): -%s so'm\n" % (order.get("promo_code", ""), somm(order["promo_discount"]))
    if int(order.get("cashback_used", 0) or 0) > 0:
        disc += "💰 Cashback: -%s so'm\n" % somm(order["cashback_used"])

    return ("💰 <b>TO'LANDI — %s</b>\n\n"
            "🧾 <b>Buyurtma #%05d</b> (%s)\n\n"
            "👤 %s\n📱 %s\n📍 %s\n%s\n%s\n\n"
            "🚚 Yetkazib berish: %s so'm%s\n🎁 Qadoq: %s\n%s"
            "💵 <b>Jami: %s so'm</b> ✅\n\n"
            "<i>Tovar ombordan yechildi. Buyurtmani tayyorlang.</i>") % (
        provider, num, manba, order.get("name", ""), order.get("phone", ""),
        order.get("district") or "—", maps_line, lines,
        somm(order.get("delivery", 0)), km_txt, order.get("packaging") or "—",
        disc, somm(order.get("total", 0)))

def _notify_admins_paid(order, provider):
    """To'langan buyurtmani adminlarga rasm + to'liq ma'lumot bilan BIR MARTA yuboradi.
    Holat darrov 'confirmed' bo'ladi — mijoz «Buyurtmalarim» da 'Yangi' ko'rmasin.
    Cashback esa botdagi order_watch_loop() tomonidan beriladi (db mantiqi o'sha yerda)."""
    try:
        set_tracking_status(order.get("id"), "confirmed")
    except Exception as e:
        print("tracking confirm xato:", e, flush=True)
    try:
        byid = {str(p.get("id")): p for p in load(PRODUCTS_FILE, [])}
        photos = _order_item_photos(order.get("items", []), byid)
        msg = _admin_paid_message(order, provider)
        kb = tracking_kb(order.get("id"))
        managers = set(status_admins())
        for aid in get_admins():
            notify_admin_order(aid, msg, reply_markup=(kb if aid in managers else None),
                               item_photos=photos)
        # Mijozga xabar bu yerda YUBORILMAYDI — uni bot yuboradi (paid_watch_loop),
        # chunki faqat bot pastdagi «asosiy menyu» klaviaturasini qo'ya oladi.
    except Exception as e:
        print("notify paid xato:", e, flush=True)

def _click_notify_paid(order):
    _notify_admins_paid(order, "Click")

@app.route("/click/prepare", methods=["POST"])
def click_prepare():
    f = request.form
    if not click_enabled():
        return _click_err(CLICK_BAD_REQ, "Click sozlanmagan")
    if not _click_sign_ok(f, with_prepare_id=False):
        return _click_err(CLICK_SIGN, "SIGN CHECK FAILED!")
    if str(f.get("action")) != "0":
        return _click_err(CLICK_ACTION, "Action not found")

    order = _find_order_by_number(f.get("merchant_trans_id"))
    if not order:
        return _click_err(CLICK_NO_ORDER, "User does not exist")

    ck = order.get("click") or {}
    if ck.get("state") == 2 or order.get("status") == "paid":
        return _click_err(CLICK_PAID, "Already paid")
    if ck.get("state") == -1:
        return _click_err(CLICK_CANCELLED, "Transaction cancelled")

    try:
        if abs(float(f.get("amount", 0)) - float(order.get("total", 0))) > 0.01:
            return _click_err(CLICK_AMOUNT, "Incorrect parameter amount")
    except Exception:
        return _click_err(CLICK_AMOUNT, "Incorrect parameter amount")

    if _order_stock_shortages(order):      # tovar tugagan -> to'lovga ruxsat yo'q
        return _click_err(CLICK_BAD_REQ, "Product is out of stock")

    prepare_id = int(time.time() * 1000) % 2147483647
    order["click"] = {"click_trans_id": f.get("click_trans_id"),
                      "prepare_id": prepare_id, "state": 1,
                      "prepare_time": int(time.time() * 1000)}
    order["status"] = "pending_pay"
    _save_order_obj(order)
    return _click_err(CLICK_OK, "Success", {
        "click_trans_id": f.get("click_trans_id"),
        "merchant_trans_id": f.get("merchant_trans_id"),
        "merchant_prepare_id": prepare_id,
    })

@app.route("/click/complete", methods=["POST"])
def click_complete():
    f = request.form
    if not click_enabled():
        return _click_err(CLICK_BAD_REQ, "Click sozlanmagan")
    if not _click_sign_ok(f, with_prepare_id=True):
        return _click_err(CLICK_SIGN, "SIGN CHECK FAILED!")
    if str(f.get("action")) != "1":
        return _click_err(CLICK_ACTION, "Action not found")

    order = _find_order_by_number(f.get("merchant_trans_id"))
    if not order:
        return _click_err(CLICK_NO_ORDER, "User does not exist")

    ck = order.get("click") or {}
    if str(ck.get("prepare_id")) != str(f.get("merchant_prepare_id")):
        return _click_err(CLICK_NO_TX, "Transaction does not exist")
    if ck.get("state") == 2:
        return _click_err(CLICK_PAID, "Already paid")
    if ck.get("state") == -1:
        return _click_err(CLICK_CANCELLED, "Transaction cancelled")

    # Click tomonda xato/bekor -> tovarni omborga qaytaramiz, -9 qaytaramiz
    try:
        click_err = int(f.get("error", 0))
    except Exception:
        click_err = 0
    if click_err < 0:
        release_order_stock(order)           # faqat yechilgan bo'lsa qaytadi
        ck["state"] = -1
        ck["cancel_time"] = int(time.time() * 1000)
        order["click"] = ck
        order["status"] = "cancelled"
        _save_order_obj(order)
        return _click_err(CLICK_CANCELLED, "Transaction cancelled")

    try:
        if abs(float(f.get("amount", 0)) - float(order.get("total", 0))) > 0.01:
            return _click_err(CLICK_AMOUNT, "Incorrect parameter amount")
    except Exception:
        return _click_err(CLICK_AMOUNT, "Incorrect parameter amount")

    # ⬇️ TOVAR AYNAN SHU YERDA OMBORDAN YECHILADI (to'lov yakunlanmoqda)
    if not take_order_stock(order):
        _notify_admins_stock_fail(order, "Click")
        return _click_err(CLICK_BAD_REQ, "Product is out of stock")

    ck["state"] = 2
    ck["perform_time"] = int(time.time() * 1000)
    order["click"] = ck
    order["status"] = "paid"
    _save_order_obj(order)
    _click_notify_paid(order)
    return _click_err(CLICK_OK, "Success", {
        "click_trans_id": f.get("click_trans_id"),
        "merchant_trans_id": f.get("merchant_trans_id"),
        "merchant_confirm_id": ck.get("prepare_id"),
    })

@app.route("/api/click-link", methods=["POST"])
def api_click_link():
    """Buyurtma raqami + summaga qarab Click to'lov sahifasi linkini yasaydi."""
    d = request.get_json(force=True, silent=True) or {}
    order = _find_order_by_number(d.get("number"))
    if not order:
        return jsonify({"ok": False, "error": "order_404"}), 200
    if not click_enabled():
        return jsonify({"ok": False, "error": "no_merchant"}), 200
    ret = request.host_url.rstrip("/") + "/#paid" + str(order.get("number"))
    q = urllib.parse.urlencode({
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "amount": int(order.get("total", 0)),
        "transaction_param": order.get("number"),
        "return_url": ret,
    })
    return jsonify({"ok": True, "link": "%s?%s" % (CLICK_PAY_URL, q)})

@app.route("/api/payme-link", methods=["POST"])
def api_payme_link():
    """Buyurtma raqami + summaga qarab Payme to'lov linkini yasaydi."""
    import base64 as _b64
    d = request.get_json(force=True, silent=True) or {}
    number = d.get("number")
    order = _find_order_by_number(number)
    if not order:
        return jsonify({"ok": False, "error": "order_404"}), 200
    if not PAYME_MERCHANT_ID:
        return jsonify({"ok": False, "error": "no_merchant"}), 200
    amount_tiyin = int(order.get("total", 0)) * 100
    lng = (d.get("lang") or "uz").strip()[:2] or "uz"
    # To'lovdan keyin saytga qaytish. Hash (#) ishlatamiz — Payme parseri '=' bo'yicha
    # bo'ladi, shuning uchun callback URL ichida '=' bo'lmasligi kerak.
    ret = request.host_url.rstrip("/") + "/#paid" + str(order.get("number"))
    # Payme link formati: base64("m=MERCHANT;ac.order_id=NUM;a=AMOUNT;c=RETURN;l=LANG")
    raw = "m=%s;ac.order_id=%s;a=%d;c=%s;l=%s" % (
        PAYME_MERCHANT_ID, order.get("number"), amount_tiyin, ret, lng)
    encoded = _b64.b64encode(raw.encode()).decode()
    link = "%s/%s" % (PAYME_CHECKOUT, encoded)
    return jsonify({"ok": True, "link": link})

@app.route("/api/payme-status", methods=["POST"])
def api_payme_status():
    """To'lovdan qaytgach: buyurtma to'langanmi (server PerformTransaction'da 'paid' qiladi)."""
    d = request.get_json(force=True, silent=True) or {}
    order = _find_order_by_number(d.get("number"))
    if not order:
        return jsonify({"ok": False, "paid": False, "error": "order_404"}), 200
    return jsonify({"ok": True, "paid": order.get("status") == "paid",
                    "number": order.get("number"),
                    "status": order.get("status") or "new"})


@app.route("/api/order/cancel", methods=["POST"])
def api_order_cancel():
    """Mijoz o'z to'lanmagan buyurtmasini bekor qiladi (saytdagi «Bekor qilish»).
    Tovar band emas — u faqat to'lovda yechiladi. Bu yerda cashback va promo
    qaytariladi, buyurtma yopiladi."""
    d = request.get_json(force=True, silent=True) or {}
    sess_phone = get_session_phone((d.get("token") or "").strip())
    if not sess_phone:
        return jsonify({"ok": False, "error": "auth"}), 200

    order = _find_order_by_number(d.get("number"))
    if not order:
        return jsonify({"ok": False, "error": "order_404"}), 200

    # Faqat o'z buyurtmasi
    if normalize_phone(order.get("phone", "")) != normalize_phone(sess_phone):
        return jsonify({"ok": False, "error": "forbidden"}), 200

    # To'langan buyurtmani mijoz bekor qila olmaydi
    if (order.get("status") == "paid"
            or (order.get("payme") or {}).get("state") == 2
            or (order.get("click") or {}).get("state") == 2):
        return jsonify({"ok": False, "error": "paid"}), 200

    if order.get("status") == "cancelled":
        return jsonify({"ok": True, "already": True})

    oid = str(order.get("id"))
    num = int(order.get("number") or 0)

    def _upd(orders):
        for o in orders:
            if str(o.get("id")) != oid:
                continue
            release_order_stock(o)               # yechilmagan -> hech narsa qilmaydi
            # Cashback qaytarish
            used = int(o.get("cashback_used", 0) or 0)
            if used > 0 and not o.get("cashback_refunded"):
                chat = o.get("tg_chat_id") or get_web_user_chat(o.get("phone", ""))
                if chat:
                    add_cashback(chat, used)
                    o["cashback_refunded"] = True
            # Promo qaytarish
            if o.get("promo_code") and not o.get("promo_refunded"):
                refund_promo(o.get("promo_code"))
                o["promo_refunded"] = True
            o["status"] = "cancelled"
            pm = o.get("payme") or {}
            if pm.get("state") != 2:
                o["payme"] = dict(pm, state=-1, cancel_time=int(time.time() * 1000),
                                  reason=pm.get("reason") or 4)
            return True
        return False

    if save_orders_synced(_upd) is None:
        return jsonify({"ok": False, "error": "save_conflict"}), 200

    set_tracking_status(oid, "cancelled")
    for aid in get_admins():
        tg_send(aid, "❌ Mijoz buyurtmani bekor qildi: #%05d (%s)" % (num, order.get("name", "")))
    return jsonify({"ok": True, "number": num})


@app.route("/api/order", methods=["POST"])
def api_order():
    d = request.get_json(force=True, silent=True) or {}
    sess_phone = get_session_phone((d.get("token") or "").strip())
    if not sess_phone:
        return jsonify({"ok": False, "error": "auth"}), 200
    name = (d.get("name") or "").strip()
    phone = (d.get("phone") or "").strip() or sess_phone
    if not name or not phone:
        return jsonify({"ok": False, "error": "name/phone"}), 400

    # Ish vaqti tekshiruvi (serverda ham himoya)
    if not is_open_now():
        wh = get_work_hours()
        return jsonify({"ok": False, "error": "closed",
                        "start": wh["start_hour"], "end": wh["end_hour"]}), 200

    pay_sel = (d.get("payment") or "Naqd").strip()
    if pay_sel == "Payme" and not payme_enabled():
        return jsonify({"ok": False, "error": "pay_off"}), 200
    if pay_sel == "Click" and not click_enabled():
        return jsonify({"ok": False, "error": "pay_off"}), 200

    refresh_products_from_gh(force=True)   # eng so'nggi ro'yxat/ombor bilan tekshiramiz
    products = load(PRODUCTS_FILE, [])
    byid = {str(p.get("id")): p for p in products}
    lines, oitems, subtotal = [], [], 0
    shortages = []
    for it in d.get("items", []):
        p = byid.get(str(it.get("id")))
        if not p:
            continue
        qty = max(1, int(it.get("qty", 1)))
        size = (it.get("size") or "").strip()
        _sizes = p.get("sizes") or []
        if _sizes:
            srec = next((s for s in _sizes if s.get("label") == size), None)
            avail = int(srec.get("stock", 0)) if srec else 0
        else:
            avail = int(p.get("stock", 0))
        if qty > avail:        # ombordan ko'p — rad etamiz
            nm = p.get("name_uz", "")
            if size:
                nm = "%s (%s)" % (nm, size)
            shortages.append({"id": str(p.get("id")), "name": nm, "available": avail})
            continue
        price = int(p.get("price", 0))
        st = price * qty
        subtotal += st
        szt = " (%s)" % size if size else ""
        lines.append("• %s%s x%d — %s so'm" % (p.get("name_uz", ""), szt, qty, somm(st)))
        oitems.append({"product_id": str(p.get("id")), "name": p.get("name_uz", ""),
                       "size": size, "price": price, "cost": int(p.get("cost", 0)),
                       "qty": qty, "subtotal": st})

    if shortages:
        return jsonify({"ok": False, "error": "stock", "items": shortages}), 200
    if not oitems:
        return jsonify({"ok": False, "error": "empty"}), 400

    # DIQQAT: bu yerda ombor TEGILMAYDI. Tovar faqat to'lov yakunlanganda
    # (Payme PerformTransaction / Click complete) yoki admin buyurtmani
    # tasdiqlaganda (Naqd) ombordan yechiladi. Yuqoridagi tekshiruv — mijozga
    # darrov javob berish uchun, band qilish uchun emas.

    # Lokatsiya bo'yicha yetkazib berish (serverda qayta hisob — bot bilan bir xil)
    lat = d.get("lat"); lng = d.get("lng")
    dkm = None
    try:
        if lat is not None and lng is not None and str(lat) != "" and str(lng) != "":
            delivery, dkm = calc_delivery(float(lat), float(lng))
        else:
            delivery = int(d.get("delivery", 0))
    except Exception:
        delivery = int(d.get("delivery", 0))
    pack = int(d.get("packaging_price", 0))
    # Promo kod (botdagi bilan bir xil baza)
    promo_code = (d.get("promo") or "").strip().upper()
    promo = get_promo(promo_code) if promo_code else None
    promo_disc = promo_amount(promo, subtotal) if promo else 0
    # Cashback (mijoz Telegramga bog'langan bo'lsa)
    cb_chat = get_web_user_chat(sess_phone) or link_web_user_if_possible(sess_phone)
    want_cb = int(d.get("use_cashback", 0) or 0)
    cb_used = 0
    if want_cb > 0 and cb_chat and cashback_on():
        payable = max(0, subtotal + delivery + pack - promo_disc)
        cb_used = min(get_cashback(cb_chat), want_cb, payable)
    total = max(0, subtotal + delivery + pack - promo_disc - cb_used)
    district = d.get("district", "")
    pay = d.get("payment", "Naqd")
    packname = d.get("packaging_name", "")
    maps_link = ""
    try:
        if lat is not None and lng is not None and str(lat) != "" and str(lng) != "":
            maps_link = "https://maps.google.com/?q=%s,%s" % (float(lat), float(lng))
    except Exception:
        maps_link = ""

    # GitHub'dagi eng so'nggi ro'yxat bilan birlashtirib yozamiz — botdan kelgan
    # buyurtmalar o'chmasin va raqam takrorlanmasin (Payme raqam bo'yicha topadi).
    _holder = {}
    def _add(merged):
        num = next_number(merged)
        tok = "w%dx%d" % (num, int(time.time()) % 100000)
        merged.append({
            "id": tok,
            "number": num,
            "date": datetime.datetime.now().isoformat(),
            "name": name, "phone": phone, "pay_type": pay,
            "packaging": packname, "packaging_price": pack,
            "qadoq_cost": (int(get_setting("gift_box_cost") or 0) if pack > 0
                           else int(get_setting("qadoq_oddiy_cost") or 0)),
            "delivery": delivery, "delivery_km": dkm, "total": total,
            "promo_code": promo_code, "promo_discount": promo_disc,
            "cashback_used": cb_used, "subtotal": subtotal,
            "district": district, "lat": lat, "lng": lng, "maps": maps_link,
            "items": oitems, "source": "sayt",
            "tg_chat_id": 0, "status": "new",
            "stock_model": "on_payment", "stock_taken": False,
        })
        _holder["number"], _holder["token"] = num, tok
        return num

    number = save_orders_synced(_add)
    if number is None:
        # Ombor tegilmagan — hech narsa qaytarish shart emas
        return jsonify({"ok": False, "error": "save_conflict"}), 200
    token = _holder["token"]

    if promo and promo_disc > 0:
        use_promo(promo_code)
    if cb_used > 0 and cb_chat:
        spend_cashback(cb_chat, cb_used)

    # Mijoz keyin botda bog'lanishi uchun tracking yozuvi
    add_web_tracking(token, name)

    # Adminlarga status tugmalari bilan xabar
    maps_line = ("📍 <a href=\"%s\">Xaritada ochish</a>\n" % maps_link) if maps_link else ""
    km_txt = (" (%s km)" % dkm) if dkm else ""
    disc_line = ""
    if promo_disc > 0:
        disc_line += "🏷 Promo (%s): -%s so'm\n" % (promo_code, somm(promo_disc))
    if cb_used > 0:
        disc_line += "💰 Cashback: -%s so'm\n" % somm(cb_used)
    msg = ("🌐 <b>SAYTDAN buyurtma #%05d</b> — BabyDiary\n\n"
           "👤 %s\n📱 %s\n📍 %s\n%s\n%s\n\n"
           "🚚 Yetkazib berish: %s so'm%s\n🎁 Qadoq: %s\n💳 To'lov: %s\n%s💰 <b>Jami: %s so'm</b>\n\n"
           "<i>Mijoz botda tasdiqlash uchun kutilmoqda.</i>") % (
        number, name, phone, district or "—", maps_line, "\n".join(lines),
        somm(delivery), km_txt, packname or "—", pay, disc_line, somm(total))
    # Onlayn to'lovda (Payme/Click) adminga hozir YUBORMAYMIZ — to'lov tasdiqlangach
    # _notify_admins_paid() to'liq ma'lumot bilan bir marta yuboradi (takror bo'lmasin).
    if pay == "Naqd":
        kb = tracking_kb(token)
        managers = set(status_admins())
        item_photos = _order_item_photos(oitems, byid)
        for aid in get_admins():
            notify_admin_order(aid, msg, reply_markup=(kb if aid in managers else None),
                               item_photos=item_photos)

    deeplink = "https://t.me/%s?start=%s" % (BOT_USERNAME, token)
    out = {"ok": True, "number": number, "token": token, "deeplink": deeplink,
           "pay": pay, "delivery": delivery, "km": dkm, "total": total}
    return jsonify(out)

@app.route("/api/auth/send-code", methods=["POST"])
def api_send_code():
    d = request.get_json(force=True, silent=True) or {}
    phone = normalize_phone(d.get("phone"))
    if len(phone) != 12:
        return jsonify({"ok": False, "error": "phone"}), 200
    if get_user_by_phone(phone):
        return jsonify({"ok": False, "error": "exists"}), 200
    # ── Rate-limit: SMS spam/xarajat himoyasi ──
    ip = _client_ip()
    b, retry = _rl_blocked("smscd:" + phone, 1, 60)        # 60 sek cooldown / telefon
    if b:
        return jsonify({"ok": False, "error": "cooldown", "retry": retry}), 200
    b, retry = _rl_blocked("smsph:" + phone, 5, 3600)      # 5 ta / soat / telefon
    if b:
        return jsonify({"ok": False, "error": "too_many", "retry": retry}), 200
    b, retry = _rl_blocked("smsip:" + ip, 20, 3600)        # 20 ta / soat / IP
    if b:
        return jsonify({"ok": False, "error": "too_many", "retry": retry}), 200
    code = gen_code()
    if not send_sms(phone, code):
        return jsonify({"ok": False, "error": "sms_unavailable"}), 503
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("INSERT OR REPLACE INTO web_codes (phone, code, expires) VALUES (?,?,?)",
                    (phone, code, time.time() + CODE_TTL))
        con.commit(); con.close()
    except Exception as e:
        print("code xato:", e, flush=True)
        return jsonify({"ok": False, "error": "server"}), 500
    _rl_event("smscd:" + phone, 60)
    _rl_event("smsph:" + phone, 3600)
    _rl_event("smsip:" + ip, 3600)
    out = {"ok": True, "ttl": CODE_TTL}
    if _dev_sms_allowed():
        out["code"] = code
    return jsonify(out)

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    phone = normalize_phone(d.get("phone"))
    pw = d.get("password") or ""
    code = (d.get("code") or "").strip()
    if not name or len(name) > 80 or len(phone) != 12 or len(pw) < 8:
        return jsonify({"ok": False, "error": "fields"}), 200
    if get_user_by_phone(phone):
        return jsonify({"ok": False, "error": "exists"}), 200
    # ── Kod brute-force himoyasi: 5 noto'g'ri urinish / 10 daqiqa ──
    b, retry = _rl_blocked("code:" + phone, 5, 600)
    if b:
        return jsonify({"ok": False, "error": "too_many", "retry": retry}), 200
    try:
        con = sqlite3.connect(DB_FILE)
        row = con.execute("SELECT code, expires FROM web_codes WHERE phone=?", (phone,)).fetchone()
        if not row or row[0] != code:
            con.close()
            _rl_event("code:" + phone, 600)   # noto'g'ri urinishni qayd qilamiz
            return jsonify({"ok": False, "error": "code"}), 200
        if time.time() > row[1]:
            con.close(); return jsonify({"ok": False, "error": "expired"}), 200
        con.execute("INSERT INTO web_users (name, phone, pass_hash, created_at) VALUES (?,?,?,?)",
                    (name, phone, hash_pw(pw), datetime.datetime.now().isoformat()))
        con.execute("DELETE FROM web_codes WHERE phone=?", (phone,))
        con.commit(); con.close()
        _rl_clear("code:" + phone)   # muvaffaqiyat — hisoblagichni tozalaymiz
    except Exception as e:
        print("register xato:", e, flush=True)
        return jsonify({"ok": False, "error": "server"}), 200
    token = new_session(phone)
    link_web_user_if_possible(phone)   # botdagi akkaunt bilan avtomatik bog'lash (telefon bo'yicha)
    response = make_response(jsonify({"ok": True, "token": token, "user": {"name": name, "phone": phone}}))
    return _set_session_cookie(response, token)

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    phone = normalize_phone(d.get("phone"))
    pw = d.get("password") or ""
    # ── Login brute-force himoyasi: 5 noto'g'ri urinish / 10 daqiqa (telefon+IP) ──
    key = "login:" + phone + ":" + _client_ip()
    b, retry = _rl_blocked(key, 5, 600)
    if b:
        return jsonify({"ok": False, "error": "locked", "retry": retry}), 200
    u = get_user_by_phone(phone)
    if not u or not verify_pw(pw, u.get("pass_hash")):
        _rl_event(key, 600)   # noto'g'ri urinishni qayd qilamiz
        return jsonify({"ok": False, "error": "invalid"}), 200
    _rl_clear(key)   # muvaffaqiyat — hisoblagichni tozalaymiz
    token = new_session(phone)
    link_web_user_if_possible(phone)   # botdagi akkaunt bilan avtomatik bog'lash (telefon bo'yicha)
    response = make_response(jsonify({"ok": True, "token": token, "user": {"name": u.get("name"), "phone": phone}}))
    return _set_session_cookie(response, token)

@app.route("/api/auth/me", methods=["POST"])
def api_me():
    d = request.get_json(force=True, silent=True) or {}
    phone = get_session_phone((d.get("token") or "").strip())
    if not phone:
        return jsonify({"ok": False}), 200
    u = get_user_by_phone(phone)
    if not u:
        return jsonify({"ok": False}), 200
    return jsonify({"ok": True, "user": {"name": u.get("name"), "phone": phone}})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    d = request.get_json(force=True, silent=True) or {}
    tok = (d.get("token") or request.cookies.get(SESSION_COOKIE, "")).strip()
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("DELETE FROM web_sessions WHERE token=?", (tok,))
        con.commit(); con.close()
    except Exception:
        pass
    response = make_response(jsonify({"ok": True}))
    return _clear_session_cookie(response)

@app.route("/api/my-orders", methods=["POST"])
def api_my_orders():
    d = request.get_json(force=True, silent=True) or {}
    phone = get_session_phone((d.get("token") or "").strip())
    if not phone:
        return jsonify({"ok": False, "error": "auth"}), 200
    orders = load(ORDERS_FILE, [])
    statuses = {}
    try:
        con = sqlite3.connect(DB_FILE)
        for r in con.execute("SELECT order_id, status FROM order_tracking").fetchall():
            statuses[r[0]] = r[1]
        con.close()
    except Exception:
        pass
    mine = [o for o in orders if normalize_phone(o.get("phone")) == phone]
    mine.sort(key=lambda o: o.get("number", 0), reverse=True)
    out = []
    for o in mine:
        out.append({
            "id": o.get("id"),                 # bot deeplink uchun token
            "number": o.get("number"),
            "date": o.get("date"),
            "total": o.get("total", 0),
            "pay_type": o.get("pay_type", ""),
            "district": o.get("district", ""),
            "items": o.get("items", []),
            "status": statuses.get(o.get("id"), o.get("status", "new")),
        })
    return jsonify({"ok": True, "orders": out})

# Auth jadvallari import vaqtida ham tayyorlanadi (gunicorn uchun).
os.makedirs(DATA_DIR, exist_ok=True)
ensure_web_tables()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print("BabyDiary sayt serveri ishga tushdi, port %d" % port, flush=True)
    # Startup: yo'q bo'lgan fayllarni GitHub'dan tiklaymiz
    _gh_restore("data/orders.json",   ORDERS_FILE)
    _gh_restore("data/products.json", PRODUCTS_FILE)
    app.run(host="0.0.0.0", port=port)
