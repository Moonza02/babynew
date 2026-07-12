import sqlite3
import json
from datetime import datetime, timedelta

import os
# Railway Volume (/data) bo'lsa o'sha yerga, bo'lmasa joriy papkaga saqlaydi
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_FILE = os.path.join(DATA_DIR, "babydiary.db")

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS langs (
        chat_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'uz'
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS carts (
        chat_id INTEGER PRIMARY KEY, data TEXT DEFAULT '{}'
    )""")
    # Savat eslatish uchun ustunlar (migration)
    for col, typ in [("updated_at", "TEXT DEFAULT ''"), ("reminded", "INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE carts ADD COLUMN {col} {typ}")
        except:
            pass

    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        chat_id INTEGER PRIMARY KEY, data TEXT DEFAULT '{}'
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS pending_orders (
        order_id TEXT PRIMARY KEY, chat_id INTEGER
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS catalog_pages (
        chat_id INTEGER PRIMARY KEY, page INTEGER DEFAULT 0
    )""")
    try:
        c.execute("ALTER TABLE catalog_pages ADD COLUMN category TEXT DEFAULT 'all'")
    except:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS admin_steps (
        chat_id INTEGER PRIMARY KEY, data TEXT DEFAULT '{}'
    )""")

    # Foydalanuvchilar (broadcast uchun)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_at TEXT
    )""")

    # Buyurtma holati tracking
    c.execute("""CREATE TABLE IF NOT EXISTS order_tracking (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        status TEXT DEFAULT 'new',
        customer_name TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    )""")
    # Eski bazaga customer_name ustunini qo'shish (migration)
    try:
        c.execute("ALTER TABLE order_tracking ADD COLUMN customer_name TEXT DEFAULT ''")
    except:
        pass

    # Promo kodlar
    c.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY,
        discount INTEGER,
        type TEXT DEFAULT 'percent',
        uses_left INTEGER DEFAULT -1,
        active INTEGER DEFAULT 1
    )""")

    # Ish vaqti
    c.execute("""CREATE TABLE IF NOT EXISTS work_hours (
        id INTEGER PRIMARY KEY DEFAULT 1,
        start_hour INTEGER DEFAULT 9,
        end_hour INTEGER DEFAULT 22,
        enabled INTEGER DEFAULT 0
    )""")
    c.execute("INSERT OR IGNORE INTO work_hours (id, start_hour, end_hour, enabled) VALUES (1, 9, 22, 0)")

    # Cashback (mijoz balansi)
    c.execute("""CREATE TABLE IF NOT EXISTS cashback (
        chat_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 0
    )""")

    # Kutilayotgan cashback (admin tasdiqlaganda beriladi)
    c.execute("""CREATE TABLE IF NOT EXISTS pending_cashback (
        order_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        amount INTEGER
    )""")

    # Saytdan ro'yxatdan o'tgan mijozlar (klient bazasi — web.py ham yaratadi)
    c.execute("""CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, phone TEXT UNIQUE, pass_hash TEXT, created_at TEXT,
        chat_id INTEGER DEFAULT 0
    )""")
    try:
        c.execute("ALTER TABLE web_users ADD COLUMN chat_id INTEGER DEFAULT 0")
    except Exception:
        pass

    defaults = [
        ("delivery",       "30000"),
        ("gift_box_price", "50000"),
        ("operator",       "@babydiaryuz_admin"),
        ("payme_card",     "5614 6819 1723 2450"),
        ("click_card",     "5614 6819 1723 2450"),
        ("cashback_percent", "5"),
        ("cashback_on", "1"),
    ]
    for key, value in defaults:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()

# ─── CASHBACK ─────────────────────────────────────────────────────────────────

def get_cashback(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row["balance"] if row else 0

def add_cashback(chat_id, amount):
    conn = get_conn()
    current = conn.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
    if current:
        new_balance = current["balance"] + amount
        conn.execute("UPDATE cashback SET balance=? WHERE chat_id=?", (new_balance, chat_id))
    else:
        new_balance = amount
        conn.execute("INSERT INTO cashback (chat_id, balance) VALUES (?, ?)", (chat_id, amount))
    # Jami berilgan cashback hisoblagichi (statistika uchun)
    row = conn.execute("SELECT value FROM settings WHERE key='cashback_total_given'").fetchone()
    total_given = (int(row["value"]) if row else 0) + amount
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cashback_total_given', ?)",
                 (str(total_given),))
    conn.commit()
    conn.close()
    return new_balance

def get_cashback_totals():
    """Statistika uchun: jami berilgan va hozirgi balansdagi cashback."""
    conn = get_conn()
    given_row = conn.execute("SELECT value FROM settings WHERE key='cashback_total_given'").fetchone()
    total_given = int(given_row["value"]) if given_row else 0
    bal_row = conn.execute("SELECT SUM(balance) as s FROM cashback").fetchone()
    current_balance = bal_row["s"] if bal_row and bal_row["s"] else 0
    conn.close()
    return {"total_given": total_given, "current_balance": current_balance}

def spend_cashback(chat_id, amount):
    conn = get_conn()
    row = conn.execute("SELECT balance FROM cashback WHERE chat_id=?", (chat_id,)).fetchone()
    bal = row["balance"] if row else 0
    use = min(bal, amount)
    conn.execute("INSERT OR REPLACE INTO cashback (chat_id, balance) VALUES (?, ?)", (chat_id, bal - use))
    conn.commit()
    conn.close()
    return use

def set_pending_cashback(order_id, chat_id, amount):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO pending_cashback (order_id, chat_id, amount) VALUES (?, ?, ?)",
                 (order_id, chat_id, amount))
    conn.commit()
    conn.close()

def get_pending_cashback(order_id):
    conn = get_conn()
    row = conn.execute("SELECT chat_id, amount FROM pending_cashback WHERE order_id=?", (order_id,)).fetchone()
    conn.close()
    if row:
        return {"chat_id": row["chat_id"], "amount": row["amount"]}
    return None

def delete_pending_cashback(order_id):
    conn = get_conn()
    conn.execute("DELETE FROM pending_cashback WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()

def get_lang(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT lang FROM langs WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row["lang"] if row else "uz"

def set_lang(chat_id, lang):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO langs (chat_id, lang) VALUES (?, ?)", (chat_id, lang))
    conn.commit()
    conn.close()

# ─── USERS ────────────────────────────────────────────────────────────────────

def register_user(chat_id, username, first_name):
    conn = get_conn()
    conn.execute("""INSERT OR IGNORE INTO users (chat_id, username, first_name, joined_at)
                    VALUES (?, ?, ?, ?)""", (chat_id, username or "", first_name or "", datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [row["chat_id"] for row in rows]

def get_user_count():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
    conn.close()
    return count

# ─── CART ─────────────────────────────────────────────────────────────────────

def get_cart(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT data FROM carts WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else {}

def set_cart(chat_id, data):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute("""INSERT OR REPLACE INTO carts (chat_id, data, updated_at, reminded)
                    VALUES (?, ?, ?, 0)""", (chat_id, json.dumps(data), now))
    conn.commit()
    conn.close()

def get_abandoned_carts(hours=6):
    """Tashlab ketilgan savatlar: hours soatdan oldin yangilangan, eslatilmagan, bo'sh emas."""
    conn = get_conn()
    rows = conn.execute("SELECT chat_id, data, updated_at, reminded FROM carts").fetchall()
    conn.close()
    result = []
    cutoff = datetime.now() - timedelta(hours=hours)
    for r in rows:
        try:
            data = json.loads(r["data"])
            if not data:  # bo'sh savat
                continue
            if r["reminded"]:  # allaqachon eslatilgan
                continue
            updated = r["updated_at"]
            if updated and datetime.fromisoformat(updated) < cutoff:
                result.append(r["chat_id"])
        except:
            continue
    return result

def mark_cart_reminded(chat_id):
    conn = get_conn()
    conn.execute("UPDATE carts SET reminded=1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def clear_cart(chat_id):
    conn = get_conn()
    conn.execute("DELETE FROM carts WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

# ─── ORDER ────────────────────────────────────────────────────────────────────

def get_order(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT data FROM orders WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else None

def set_order(chat_id, data):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO orders (chat_id, data) VALUES (?, ?)", (chat_id, json.dumps(data)))
    conn.commit()
    conn.close()

def update_order(chat_id, key, value):
    data = get_order(chat_id) or {}
    data[key] = value
    set_order(chat_id, data)

def delete_order(chat_id):
    conn = get_conn()
    conn.execute("DELETE FROM orders WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def order_exists(chat_id):
    return get_order(chat_id) is not None

# ─── PENDING ORDERS ───────────────────────────────────────────────────────────

def add_pending_order(order_id, chat_id):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO pending_orders (order_id, chat_id) VALUES (?, ?)", (order_id, chat_id))
    conn.commit()
    conn.close()

def get_pending_order(order_id):
    conn = get_conn()
    row = conn.execute("SELECT chat_id FROM pending_orders WHERE order_id=?", (order_id,)).fetchone()
    conn.close()
    return row["chat_id"] if row else None

def delete_pending_order(order_id):
    conn = get_conn()
    conn.execute("DELETE FROM pending_orders WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()

# ─── ORDER TRACKING ───────────────────────────────────────────────────────────

def add_tracking(order_id, chat_id, customer_name=""):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute("""INSERT OR REPLACE INTO order_tracking
                    (order_id, chat_id, status, customer_name, created_at, updated_at)
                    VALUES (?, ?, 'new', ?, ?, ?)""", (order_id, chat_id, customer_name, now, now))
    conn.commit()
    conn.close()

def update_tracking(order_id, status):
    conn = get_conn()
    conn.execute("UPDATE order_tracking SET status=?, updated_at=? WHERE order_id=?",
                 (status, datetime.now().isoformat(), order_id))
    conn.commit()
    conn.close()

def set_tracking_chat(order_id, chat_id, customer_name=""):
    """Saytdagi buyurtmani mijoz Telegramiga bog'laydi (statusni o'zgartirmasdan)."""
    conn = get_conn()
    if customer_name:
        conn.execute("UPDATE order_tracking SET chat_id=?, customer_name=? WHERE order_id=?",
                     (chat_id, customer_name, order_id))
    else:
        conn.execute("UPDATE order_tracking SET chat_id=? WHERE order_id=?",
                     (chat_id, order_id))
    conn.commit()
    conn.close()

def get_tracking(order_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM order_tracking WHERE order_id=?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_orders(chat_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM order_tracking WHERE chat_id=? ORDER BY created_at DESC LIMIT 50",
                        (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_web_users():
    """Saytdan ro'yxatdan o'tgan mijozlar (klient bazasi)."""
    try:
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, name, phone, created_at FROM web_users ORDER BY created_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def count_web_users():
    try:
        conn = get_conn()
        n = conn.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0

def _norm_phone_db(p):
    import re as _re
    d = _re.sub(r"\D", "", p or "")
    if len(d) == 9:
        d = "998" + d
    if d[:1] == "8" and len(d) == 10:
        d = "99" + d
    return d

def set_web_user_chat(phone, chat_id):
    """Sayt mijozini (telefon) Telegram chat_id ga bog'laydi — cashback/wallet uchun."""
    ph = _norm_phone_db(phone)
    if not ph:
        return
    try:
        conn = get_conn()
        conn.execute("UPDATE web_users SET chat_id=? WHERE phone=?", (chat_id, ph))
        conn.commit(); conn.close()
    except Exception:
        pass

def get_web_user_chat(phone):
    ph = _norm_phone_db(phone)
    try:
        conn = get_conn()
        row = conn.execute("SELECT chat_id FROM web_users WHERE phone=?", (ph,)).fetchone()
        conn.close()
        return int(row["chat_id"]) if row and row["chat_id"] else 0
    except Exception:
        return 0

def get_active_trackings():
    conn = get_conn()
    rows = conn.execute("""SELECT * FROM order_tracking
                           WHERE status NOT IN ('delivered', 'cancelled')
                           ORDER BY created_at DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ─── SETTINGS ─────────────────────────────────────────────────────────────────

def get_setting(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def next_order_number():
    """Keyingi tartib raqamini qaytaradi (#001, #002 ...). Atomik."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key='order_counter'").fetchone()
    current = int(row["value"]) if row else 0
    new_num = current + 1
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('order_counter', ?)", (str(new_num),))
    conn.commit()
    conn.close()
    return new_num

def get_all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}

# ─── CATALOG PAGE ─────────────────────────────────────────────────────────────

def get_catalog_page(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT page FROM catalog_pages WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row["page"] if row else 0

def set_catalog_page(chat_id, page):
    conn = get_conn()
    # category ni saqlab qolamiz
    row = conn.execute("SELECT category FROM catalog_pages WHERE chat_id=?", (chat_id,)).fetchone()
    cat = row["category"] if row else "all"
    conn.execute("INSERT OR REPLACE INTO catalog_pages (chat_id, page, category) VALUES (?, ?, ?)",
                 (chat_id, page, cat))
    conn.commit()
    conn.close()

def get_catalog_category(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT category FROM catalog_pages WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    cat = row["category"] if row else "all"
    return None if cat == "all" else cat

def set_catalog_category(chat_id, category):
    conn = get_conn()
    row = conn.execute("SELECT page FROM catalog_pages WHERE chat_id=?", (chat_id,)).fetchone()
    page = row["page"] if row else 0
    conn.execute("INSERT OR REPLACE INTO catalog_pages (chat_id, page, category) VALUES (?, ?, ?)",
                 (chat_id, page, category))
    conn.commit()
    conn.close()

# ─── ADMIN STEPS ──────────────────────────────────────────────────────────────

def get_admin_step(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT data FROM admin_steps WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else None

def set_admin_step(chat_id, data):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO admin_steps (chat_id, data) VALUES (?, ?)", (chat_id, json.dumps(data)))
    conn.commit()
    conn.close()

def update_admin_step(chat_id, key, value):
    data = get_admin_step(chat_id) or {}
    data[key] = value
    set_admin_step(chat_id, data)

def delete_admin_step(chat_id):
    conn = get_conn()
    conn.execute("DELETE FROM admin_steps WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def admin_step_exists(chat_id):
    return get_admin_step(chat_id) is not None

# ─── PROMO CODES ──────────────────────────────────────────────────────────────

def add_promo(code, discount, type_="percent", uses=-1):
    conn = get_conn()
    conn.execute("""INSERT OR REPLACE INTO promo_codes (code, discount, type, uses_left, active)
                    VALUES (?, ?, ?, ?, 1)""", (code.upper(), discount, type_, uses))
    conn.commit()
    conn.close()

def get_promo(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM promo_codes WHERE code=? AND active=1", (code.upper(),)).fetchone()
    conn.close()
    return dict(row) if row else None

def use_promo(code):
    conn = get_conn()
    promo = conn.execute("SELECT * FROM promo_codes WHERE code=?", (code.upper(),)).fetchone()
    if promo and promo["uses_left"] > 0:
        new_uses = promo["uses_left"] - 1
        if new_uses == 0:
            conn.execute("UPDATE promo_codes SET uses_left=0, active=0 WHERE code=?", (code.upper(),))
        else:
            conn.execute("UPDATE promo_codes SET uses_left=? WHERE code=?", (new_uses, code.upper()))
    conn.commit()
    conn.close()

def get_all_promos():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM promo_codes").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_promo(code):
    conn = get_conn()
    conn.execute("DELETE FROM promo_codes WHERE code=?", (code.upper(),))
    conn.commit()
    conn.close()

def toggle_promo(code):
    conn = get_conn()
    row = conn.execute("SELECT active FROM promo_codes WHERE code=?", (code.upper(),)).fetchone()
    if row:
        new_active = 0 if row["active"] else 1
        conn.execute("UPDATE promo_codes SET active=? WHERE code=?", (new_active, code.upper()))
        conn.commit()
    conn.close()
    return new_active if row else None

def update_promo(code, discount, uses_left):
    conn = get_conn()
    conn.execute("""UPDATE promo_codes SET discount=?, uses_left=? WHERE code=?""",
                 (discount, uses_left, code.upper()))
    conn.commit()
    conn.close()

# ─── WORK HOURS ───────────────────────────────────────────────────────────────

def get_work_hours():
    conn = get_conn()
    row = conn.execute("SELECT * FROM work_hours WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {"start_hour": 9, "end_hour": 22, "enabled": 0}

def set_work_hours(start_hour, end_hour, enabled):
    conn = get_conn()
    conn.execute("UPDATE work_hours SET start_hour=?, end_hour=?, enabled=? WHERE id=1",
                 (start_hour, end_hour, enabled))
    conn.commit()
    conn.close()

def is_working_hours():
    wh = get_work_hours()
    if not wh["enabled"]:
        return True
    now_hour = datetime.now().hour
    return wh["start_hour"] <= now_hour < wh["end_hour"]

# ─── STATISTIKA ───────────────────────────────────────────────────────────────

def get_stats(orders_file="orders.json"):
    try:
        with open(orders_file, "r", encoding="utf-8") as f:
            all_orders = json.load(f)
    except:
        return {"today": 0, "week": 0, "month": 0, "total": 0,
                "today_sum": 0, "week_sum": 0, "month_sum": 0, "total_sum": 0}

    now   = datetime.now()
    today = now.date()
    week  = today - timedelta(days=7)
    month = today - timedelta(days=30)

    stats = {"today": 0, "week": 0, "month": 0, "total": len(all_orders),
             "today_sum": 0, "week_sum": 0, "month_sum": 0, "total_sum": 0}

    for o in all_orders:
        total = o.get("total", 0) or 0
        stats["total_sum"] += total
        date_str = o.get("date", "")
        if date_str:
            try:
                order_date = datetime.fromisoformat(date_str).date()
                if order_date == today:
                    stats["today"] += 1
                    stats["today_sum"] += total
                if order_date >= week:
                    stats["week"] += 1
                    stats["week_sum"] += total
                if order_date >= month:
                    stats["month"] += 1
                    stats["month_sum"] += total
            except:
                pass

    return stats
