import os
import re
import json
import uuid
import random
import logging
import time
import base64
import sqlite3
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

# Toshkent vaqt zonasi (UTC+5) — global
TASHKENT_TZ = timezone(timedelta(hours=5))
from functools import wraps
import telebot
from telebot import types
import database as db

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── BOT ──────────────────────────────────────────────────────────────────────

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)
db.init_db()
_CARDS_DROPPED = False   # _drop_card_settings() log sozlangach chaqiriladi
log.info("BabyDiary bot ishga tushdi")

# ─── GitHub real-time backup & auto-restore ──────────────────────────────────
# Railway env'da qo'yish kerak:
#   GITHUB_TOKEN   = ghp_xxxx  (repo write huquqi bilan)
#   GITHUB_REPO    = Moonza02/babydiary   (sukut bo'yicha)
_GH_TOKEN  = os.getenv("GITHUB_TOKEN", "")
_GH_REPO   = os.getenv("GITHUB_REPO", "Moonza02/babydiary")
_GH_BRANCH = os.getenv("GITHUB_BRANCH", "main")
_GH_API    = "https://api.github.com"

def _gh_headers():
    return {"Authorization": f"Bearer {_GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json"}

def _gh_get_file(path):
    """GitHub'dan fayl meta (sha, content) ni oladi. Yo'q bo'lsa None."""
    if not _GH_TOKEN:
        return None
    try:
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/{path}?ref={_GH_BRANCH}"
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code != 404:                       # 404 = fayl hali yo'q, bu normal
            log.warning(f"_gh_get_file({path}): HTTP {e.code}")
        return None
    except Exception as e:
        log.warning(f"_gh_get_file({path}): {type(e).__name__}: {e}")
        return None

def _gh_push(path, content_bytes, message="backup"):
    """Faylni GitHub'ga push qiladi (create yoki update). Fon thread'da."""
    if not _GH_TOKEN:
        return
    import base64, threading
    def _push():
        try:
            b64 = base64.b64encode(content_bytes).decode()
            meta = _gh_get_file(path)
            payload = {"message": message, "content": b64, "branch": _GH_BRANCH}
            if meta and meta.get("sha"):
                payload["sha"] = meta["sha"]
            url = f"{_GH_API}/repos/{_GH_REPO}/contents/{path}"
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, headers=_gh_headers(), method="PUT")
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:
            log.warning(f"GitHub push ({path}): {e}")
    threading.Thread(target=_push, daemon=True).start()

def _gh_pull(path, local_path):
    """GitHub'dan faylni yuklab, local_path ga saqlaydi. Muvaffaqiyatda True."""
    if not _GH_TOKEN:
        return False
    import base64
    try:
        meta = _gh_get_file(path)
        if not meta or not meta.get("content"):
            return False
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(raw)
        log.info(f"GitHub'dan tiklandi: {path} → {local_path}")
        return True
    except Exception as e:
        log.warning(f"GitHub pull ({path}): {e}")
        return False

def gh_push_json(local_path, gh_path, label=""):
    """JSON faylni GitHub'ga nusxalaydi (har saqlashdan keyin chaqiriladi)."""
    try:
        raw = open(local_path, "rb").read()
        stamp = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M")
        _gh_push(gh_path, raw, f"backup: {label or gh_path} [{stamp}]")
    except Exception as e:
        log.warning(f"gh_push_json ({gh_path}): {e}")

def gh_push_json_sync(local_path, gh_path, label="", retries=3):
    """GitHub'ga SINXRON push. Qaytaradi: (ok: bool, sabab: str).
    Muhim amallar uchun — xato bo'lsa ANIQ sababini bilishimiz kerak."""
    if not _GH_TOKEN:
        return False, "GITHUB_TOKEN o'rnatilmagan"
    try:
        raw = open(local_path, "rb").read()
    except Exception as e:
        return False, f"faylni o'qib bo'lmadi: {e}"
    b64 = base64.b64encode(raw).decode()
    stamp = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M")
    url = f"{_GH_API}/repos/{_GH_REPO}/contents/{gh_path}"
    last = "noma'lum xato"
    for attempt in range(retries):
        try:
            meta = _gh_get_file(gh_path)          # har urinishda yangi SHA
            payload = {"message": f"{label or gh_path} [{stamp}]", "content": b64,
                       "branch": _GH_BRANCH}
            if meta and meta.get("sha"):
                payload["sha"] = meta["sha"]
            req = urllib.request.Request(url, json.dumps(payload).encode(),
                                         _gh_headers(), method="PUT")
            with urllib.request.urlopen(req, timeout=20):
                pass
            return True, "ok"
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
                msg = body.get("message", "")
            except Exception:
                msg = ""
            last = f"HTTP {e.code}: {msg}" if msg else f"HTTP {e.code}"
            log.warning(f"gh_push_sync ({gh_path}) urinish {attempt+1}: {last}")
            if e.code == 401:
                return False, "HTTP 401 — token yaroqsiz/muddati o'tgan"
            if e.code == 403:
                return False, "HTTP 403 — tokenda yozish huquqi yo'q (contents: write)"
            if e.code == 404:
                return False, f"HTTP 404 — repo/branch topilmadi ({_GH_REPO}@{_GH_BRANCH})"
            if e.code not in (409, 422):
                return False, last
            time.sleep(0.5)              # SHA konflikti — qayta urinamiz
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            log.warning(f"gh_push_sync ({gh_path}) urinish {attempt+1}: {last}")
            return False, last
    return False, last

def gh_matches(gh_path, expected, tries=3):
    """GitHub'dagi JSON kutilgan qiymatga tengmi? (tozalashni tasdiqlash uchun)
    GitHub API keshi tufayli darrov ko'rinmasligi mumkin — bir necha marta urinamiz."""
    for i in range(tries):
        meta = _gh_get_file(gh_path)
        if meta and meta.get("content"):
            try:
                raw = base64.b64decode(meta["content"].replace("\n", ""))
                if json.loads(raw.decode("utf-8")) == expected:
                    return True
            except Exception:
                pass
        if i < tries - 1:
            time.sleep(1.2)
    return False

def write_json_local(file, data):
    """Faqat lokal yozadi (GitHub push'siz). True = yozildi."""
    try:
        with open(file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"write_json_local({file}): {e}")
        return False

def gh_diagnose():
    """GitHub ulanishini tekshiradi: token, repo, branch, yozish huquqi."""
    if not _GH_TOKEN:
        return ("🔍 GitHub tekshiruvi\n\n"
                "❌ GITHUB_TOKEN o'rnatilmagan.\n\n"
                "Railway → bot servisi → Variables → GITHUB_TOKEN qo'shing.\n"
                "Tokenda repo'ga 'Contents: Read and write' huquqi bo'lsin.")
    out = ["🔍 GitHub tekshiruvi\n",
           f"Repo: {_GH_REPO}", f"Branch: {_GH_BRANCH}",
           f"Token: {_GH_TOKEN[:4]}…{_GH_TOKEN[-4:]} ({len(_GH_TOKEN)} belgi)\n"]

    # 1) Token kimga tegishli
    try:
        req = urllib.request.Request(f"{_GH_API}/user", headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            who = json.loads(r.read()).get("login", "?")
        out.append(f"✅ Token yaroqli (foydalanuvchi: {who})")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            out.append("❌ Token yaroqsiz yoki muddati o'tgan (401)")
            return "\n".join(out)
        out.append(f"⚠️ /user: HTTP {e.code}")
    except Exception as e:
        out.append(f"⚠️ /user: {type(e).__name__}")

    # 2) Repo ko'rinadimi va yozish huquqi bormi
    try:
        req = urllib.request.Request(f"{_GH_API}/repos/{_GH_REPO}", headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read())
        perms = info.get("permissions", {})
        out.append(f"✅ Repo topildi ({'private' if info.get('private') else 'public'})")
        out.append(("✅ Yozish huquqi bor" if perms.get("push")
                    else "❌ Yozish huquqi YO'Q — tokenga 'Contents: write' bering"))
    except urllib.error.HTTPError as e:
        out.append(f"❌ Repo: HTTP {e.code} — nom xato yoki token bu repoga ruxsatsiz")
        return "\n".join(out)
    except Exception as e:
        out.append(f"⚠️ Repo: {type(e).__name__}")

    # 3) Fayllar holati
    for f in ("data/products.json", "data/orders.json", "data/reviews.json"):
        meta = _gh_get_file(f)
        if meta:
            out.append(f"📄 {f}: {meta.get('size', '?')} bayt")
        else:
            out.append(f"📄 {f}: yo'q (yoki o'qib bo'lmadi)")

    # 4) Haqiqiy yozish sinovi
    ok, why = gh_push_json_sync(PRODUCTS_FILE, "data/products.json", "gh check")
    out.append("\n" + ("✅ Yozish sinovi: MUVAFFAQIYATLI\n(botdagi joriy mahsulotlar GitHub'ga yozildi)" if ok
                       else f"❌ Yozish sinovi: {why}"))
    return "\n".join(out)

def gh_sync_on_start(local_path, gh_path):
    """Startup sinxronizatsiyasi. GitHub — yagona ishonchli manba (har o'zgarishda
    sinxron yoziladi, bot ham sayt ham o'qiydi).

    - GitHub'da fayl bor  -> lokalni O'SHANDAN yozamiz (deploy'da eski nusxa qaytmasin)
    - GitHub'da yo'q      -> lokal borini GitHub'ga chiqaramiz
    Bu `/data` volume ulanmagan holatda ham (DATA_DIR=".") repo ichidagi eski
    fayl tirilib qolmasligini kafolatlaydi."""
    meta = _gh_get_file(gh_path)
    if meta and meta.get("content"):
        try:
            raw = base64.b64decode(meta["content"].replace("\n", ""))
            data = json.loads(raw.decode("utf-8"))       # faqat to'g'ri JSON bo'lsa
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(raw)
            n = len(data) if isinstance(data, (list, dict)) else "?"
            log.info(f"⬇️ GitHub'dan sinxronlandi: {gh_path} -> {local_path} ({n} ta)")
            return True
        except Exception as e:
            log.warning(f"gh_sync_on_start({gh_path}) o'qish xato: {e} — lokal qoldiriladi")
            return False
    # GitHub'da yo'q — lokalni yuqoriga chiqaramiz
    if os.path.exists(local_path):
        ok, why = gh_push_json_sync(local_path, gh_path, "startup sync")
        log.info(f"⬆️ GitHub'da yo'q edi, yuklandi: {gh_path} ({'ok' if ok else why})")
        return ok
    log.warning(f"⚠️ {gh_path} GitHub'da ham, lokalda ham yo'q — bo'sh boshlanadi")
    return False

def gh_restore_if_missing(local_path, gh_path):
    """Bot yoqilganda: lokal fayl yo'q bo'lsa GitHub'dan tiklaydi."""
    if os.path.exists(local_path):
        return False   # allaqachon bor
    log.warning(f"{local_path} topilmadi — GitHub'dan tiklanmoqda ({gh_path})...")
    ok = _gh_pull(gh_path, local_path)
    if ok:
        log.info(f"✅ Tiklandi: {local_path}")
    else:
        log.warning(f"⚠️ GitHub'dan ham topilmadi: {gh_path} — bo'sh ro'yxat bilan boshlanadi")
    return ok


DATA_DIR = "/data" if os.path.isdir("/data") else "."
PRODUCTS_FILE        = os.path.join(DATA_DIR, "products.json")
ADMINS_FILE          = "admins.json"  # bu kod bilan keladi (GitHub), Volume'da emas
REVIEWS_FILE         = os.path.join(DATA_DIR, "reviews.json")
PENDING_REVIEWS_FILE = os.path.join(DATA_DIR, "pending_reviews.json")
ORDERS_FILE          = os.path.join(DATA_DIR, "orders.json")

DEFAULT_PRODUCTS = []  # Namuna tovar yo'q — faqat admin qo'shganlari ko'rinadi

TEXT = {
    "uz": {
        "main": "Asosiy menu:", "catalog": "🛍 Katalog", "cart": "🛒 Savat",
        "reviews": "⭐ Fikrlar", "write_review": "✍️ Fikr yozish",
        "operator": "📞 Operator", "language": "🌐 Til", "home": "🏠 Asosiy menu",
        "my_orders": "📦 Buyurtmalarim",
        "cashback": "💰 Cashback",
        "choose_product": "🛍 Katalogdan mahsulot tanlang:",
        "next": "➡️ Keyingi sahifa", "prev": "⬅️ Oldingi sahifa",
        "cart_empty": "🛒 Savat bo'sh.", "cart_title": "🛒 Savat:",
        "delivery": "Yetkazib berish", "total": "Jami",
        "confirm_order": "✅ Buyurtmani tasdiqlash", "clear_cart": "🗑 Savatni tozalash",
        "back": "⬅️ Orqaga",
        "name": "Ismingizni yozing yoki tugmani bosing:",
        "phone": "Telefon raqamingizni yuboring yoki yozing:",
        "location": "Manzilni faqat joylashuv orqali yuboring:",
        "send_location": "📍 Joylashuvni yuborish", "location_ok": "✅ Joylashuv qabul qilindi.",
        "payment": "To'lov turini tanlang:", "receipt": "To'lovdan keyin chekni yuboring.",
        "thanks_review": "✅ Fikringiz uchun rahmat.", "no_reviews": "Hozircha fikrlar yo'q.",
        "operator_text": "Operator bilan bog'lanish:", "packaging": "Qadoqlash turini tanlang:",
        "gift_box": "🎁 Sovg'a qutisi", "brand_bag": "🛍 BabyDiary brend paketi",
        "packaging_line": "Qadoqlash",
        "order_received": "✅ Buyurtma qabul qilindi. Operator siz bilan bog'lanadi.",
        "check_wait": "⏳ Chek qabul qilindi. To'lov tekshirilmoqda.",
        "pay_confirmed": "✅ To'lov tasdiqlandi. Buyurtmangiz qabul qilindi.",
        "pay_rejected": "❌ To'lov tasdiqlanmadi. Iltimos, operator bilan bog'laning.",
        "closed": "🕐 Hozir ish vaqti emas.\n\nBiz {start}:00 - {end}:00 da ishlaymiz.\nKeyinroq murojaat qiling!",
        "promo_ask": "🎁 Promo kodingiz bo'lsa, jo'nating.\n\nBo'lmasa, quyidagi tugmani bosing 👇",
        "promo_ok": "✅ Promo kod qo'llanildi! {discount} chegirma.",
        "promo_no": "❌ Promo kod noto'g'ri yoki muddati o'tgan.",
        "promo_skip": "promo_skip",
        "search_ask": "🔍 Qidirish uchun mahsulot nomini yozing:",
        "search_empty": "🔍 Hech narsa topilmadi.",
        "delivered_thanks": "💝 {name}, buyurtmangiz yetkazildi!\n\nBabyDiary'ni tanlaganingiz uchun chin dildan rahmat. Sizning farzandingizga eng yaxshi narsalarni tanlashda yordam bera olganimizdan baxtiyormiz. 🤍\n\nFarzandingiz har doim shod, sog'lom va baxtli bo'lsin! Yana ko'rishguncha, BabyDiary oilasi sizni doimo kutadi. 👶✨",
        "product_not_found": "Mahsulot topilmadi.",
        "cart_cleared": "🗑 Savat tozalandi.",
        "no_my_orders": "📦 Hozircha buyurtmalaringiz yo'q.",
        "my_orders_title": "📦 Buyurtmalaringiz:",
        "search_found": "🔍 {count} ta topildi:",
        "promo_skip_btn": "➡️ Promo kodsiz davom etish",
        "status_update": "📦 Buyurtmangiz holati yangilandi!",
        "order_label": "Buyurtma",
        "status_label": "Holat",
        "date_label": "Sana",
    },
    "ru": {
        "main": "Главное меню:", "catalog": "🛍 Каталог", "cart": "🛒 Корзина",
        "reviews": "⭐ Отзывы", "write_review": "✍️ Оставить отзыв",
        "operator": "📞 Оператор", "language": "🌐 Язык", "home": "🏠 Главное меню",
        "my_orders": "📦 Мои заказы",
        "cashback": "💰 Кешбэк",
        "choose_product": "🛍 Выберите товар из каталога:",
        "next": "➡️ Следующая страница", "prev": "⬅️ Предыдущая страница",
        "cart_empty": "🛒 Корзина пустая.", "cart_title": "🛒 Корзина:",
        "delivery": "Доставка", "total": "Итого",
        "confirm_order": "✅ Оформить заказ", "clear_cart": "🗑 Очистить корзину",
        "back": "⬅️ Назад",
        "name": "Напишите имя или нажмите кнопку:",
        "phone": "Отправьте или напишите номер телефона:",
        "location": "Отправьте адрес только через локацию:",
        "send_location": "📍 Отправить локацию", "location_ok": "✅ Локация принята.",
        "payment": "Выберите способ оплаты:", "receipt": "После оплаты отправьте чек.",
        "thanks_review": "✅ Спасибо за ваш отзыв.", "no_reviews": "Пока отзывов нет.",
        "operator_text": "Связь с оператором:", "packaging": "Выберите упаковку:",
        "gift_box": "🎁 Подарочная коробка", "brand_bag": "🛍 Фирменный пакет BabyDiary",
        "packaging_line": "Упаковка",
        "order_received": "✅ Заказ принят. Оператор свяжется с вами.",
        "check_wait": "⏳ Чек принят. Оплата проверяется.",
        "pay_confirmed": "✅ Оплата подтверждена. Заказ принят.",
        "pay_rejected": "❌ Оплата не подтверждена. Пожалуйста, свяжитесь с оператором.",
        "closed": "🕐 Сейчас нерабочее время.\n\nМы работаем с {start}:00 до {end}:00.\nОбратитесь позже!",
        "promo_ask": "🎁 Если у вас есть промокод, отправьте его.\n\nЕсли нет, нажмите кнопку ниже 👇",
        "promo_ok": "✅ Промокод применён! Скидка {discount}.",
        "promo_no": "❌ Промокод неверный или истёк.",
        "promo_skip": "promo_skip",
        "search_ask": "🔍 Введите название товара для поиска:",
        "search_empty": "🔍 Ничего не найдено.",
        "delivered_thanks": "💝 {name}, ваш заказ доставлен!\n\nОт всего сердца благодарим вас за то, что выбрали BabyDiary. Мы счастливы, что смогли помочь вам выбрать самое лучшее для вашего малыша. 🤍\n\nПусть ваш ребёнок всегда будет радостным, здоровым и счастливым! До новых встреч, семья BabyDiary всегда вас ждёт. 👶✨",
        "product_not_found": "Товар не найден.",
        "cart_cleared": "🗑 Корзина очищена.",
        "no_my_orders": "📦 У вас пока нет заказов.",
        "my_orders_title": "📦 Ваши заказы:",
        "search_found": "🔍 Найдено: {count}",
        "promo_skip_btn": "➡️ Продолжить без промокода",
        "status_update": "📦 Статус вашего заказа обновлён!",
        "order_label": "Заказ",
        "status_label": "Статус",
        "date_label": "Дата",
    }
}

STATUS_TEXT = {
    "uz": {
        "new":        "🆕 Yangi",
        "confirmed":  "✅ Tasdiqlandi",
        "preparing":  "👨‍🍳 Tayyorlanmoqda",
        "delivering": "🚚 Yetkazilmoqda",
        "delivered":  "✅ Yetkazildi",
        "cancelled":  "❌ Bekor qilindi",
    },
    "ru": {
        "new":        "🆕 Новый",
        "confirmed":  "✅ Подтверждён",
        "preparing":  "👨‍🍳 Готовится",
        "delivering": "🚚 Доставляется",
        "delivered":  "✅ Доставлен",
        "cancelled":  "❌ Отменён",
    }
}

def status_text(chat_id, status):
    return STATUS_TEXT[lang(chat_id)].get(status, status)

# ─── Yetkazilganda tashakkurnoma (random) ─────────────────────────────────────

THANKS_MESSAGES = {
    "uz": [
        "💝 {name}, buyurtmangiz yetib keldi!\n\nBabyDiary'ni tanlaganingiz uchun chin dildan rahmat. Farzandingizga eng yaxshisini tanlashda yoningizda bo'lganimizdan baxtiyormiz. Kichkintoyingiz doim shod, sog'lom va mehr ichida ulg'aysin! 🤍",
        "🌸 {name}, mana buyurtmangiz qo'lingizda!\n\nBizga ishonganingiz uchun yurakdan minnatdormiz. Har bir kiyim farzandingizga iliqlik va g'amxo'rlik olib kelishini chin dildan tilaymiz. Kichkintoyingizning har kuni quvonchga to'lsin! 👶✨",
        "🤍 {name}, buyurtmangizni qabul qiling!\n\nSizdek g'amxo'r ota-onalar uchun ishlash biz uchun katta baxt. Farzandingiz sog'-salomat, baxtli va doim tabassum ichida bo'lsin. Yana ko'rishguncha, BabyDiary oilasi sizni doimo kutadi! 🌷",
        "💛 {name}, buyurtmangiz yetkazildi!\n\nBizni tanlaganingiz va ishonganingiz uchun chin yurakdan rahmat. Sizning xursandchiligingiz — bizning eng katta mukofotimiz. Kichkintoyingiz mehr, sog'lik va baxt ichida o'ssin! 🤍✨",
        "✨ {name}, mana sizning buyurtmangiz!\n\nHar bir buyurtmangiz biz uchun alohida qadrli. Farzandingizga tanlagan narsalaringiz unga iliqlik va quvonch ulashsin. Oilangizga farovonlik, kichkintoyingizga esa mustahkam salomatlik tilaymiz! 🌸",
        "🌷 {name}, buyurtmangiz yetib keldi!\n\nBabyDiary'ga ishonch bildirganingiz uchun chin dildan tashakkur. Farzandingizning kulgusi uyingizni doim yoritib tursin. Sog'lik, baxt va mehr sizning oilangizdan hech qachon arimasin! 👶🤍",
        "💖 {name}, buyurtmangizni qabul qiling!\n\nSizga xizmat qilish biz uchun sharaf. Tanlagan kiyimlaringiz kichkintoyingizga juda yarashishiga ishonamiz. Farzandingiz har kuni shodlik bilan uyg'onsin, hayoti quvonchga to'lsin! ✨🌸",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nBizning kichik do'konimizni tanlaб, ishonganingiz uchun yurakdan rahmat. Har bir tikuv, har bir mato farzandingiz uchun mehr bilan tanlangan. Kichkintoyingiz sog'lom va baxtli ulg'aysin! 🌷💝",
        "💐 {name}, mana buyurtmangiz!\n\nSizdek mijozlar tufayli biz har kuni yaxshilanishga harakat qilamiz. Rahmat sizga! Farzandingizga sog'lik, oilangizga esa tinchlik va mo'l-ko'lchilik tilaymiz. Yana kutib qolamiz! 👶🤍",
        "🌟 {name}, buyurtmangiz yetib keldi!\n\nBabyDiary'ni tanlaganingizdan chin dildan xursandmiz. Kichkintoyingizning kiyimlari unga qulaylik va iliqlik bersin. Farzandingiz mehr ichida, sog'-salomat va baxtli katta bo'lsin! ✨🌸",
        "💝 {name}, buyurtmangizni qabul qiling!\n\nIshonchingiz biz uchun bebaho. Har bir kiyim — bir xotira, deymiz biz. Farzandingiz bilan bog'liq har bir lahza go'zal xotiraga aylansin. Sog'lik, quvonch va baxt sizga yor bo'lsin! 🤍",
        "🌸 {name}, mana sizning buyurtmangiz!\n\nBizga vaqtingiz va ishonchingizni ajratganingiz uchun rahmat. Kichkintoyingiz tanlagan kiyimlarida o'zini qulay va baxtli his qilsin. Oilangizga mehr, sog'lik va farovonlik tilaymiz! 👶✨",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nSizning oilangizga xizmat qilganimizdan faxrlanamiz. Farzandingizning har bir kuni quyoshli va quvonchli o'tsin. BabyDiary doim siz va kichkintoyingiz yonida! Rahmat sizga! 🌷💖",
        "💛 {name}, buyurtmangiz yetib keldi!\n\nChin dildan rahmat sizga! Tanlagan narsalaringiz farzandingizga yoqishiga va unga iliqlik ulashishiga ishonamiz. Kichkintoyingiz sog'-salomat, kulib turadigan, baxtli bola bo'lib ulg'aysin! ✨🤍",
        "🌷 {name}, buyurtmangizni qabul qiling!\n\nBizni tanlaganingiz uchun yurakdan minnatdormiz. Har bir kiyim mehr va g'amxo'rlik bilan tayyorlangan. Farzandingiz baxtli, sog'lom va sevimli bo'lsin. Oilangizga tinchlik tilaymiz! 👶🌸",
        "💖 {name}, mana buyurtmangiz!\n\nSizdek ajoyib mijozlar bilan ishlash — chinakam quvonch. Rahmat ishonchingiz uchun! Kichkintoyingizning kulgusi hech qachon tinmasin, hayoti baxt va mehrga to'lsin. Yana ko'rishguncha! 🤍✨",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nBabyDiary oilasi sizga chin dildan minnatdor. Farzandingizga tanlagan har bir narsa unga quvonch keltirsin. Kichkintoyingiz sog'lik, mehr va baxt ichida ulg'aysin. Doim xushxabar bilan keling! 🌷",
        "💝 {name}, buyurtmangiz yetib keldi!\n\nVaqtingiz va ishonchingiz uchun rahmat. Sizning xursandchiligingizni ko'rish — bizning maqsadimiz. Farzandingiz har kuni yangi quvonch bilan uyg'onsin, hayoti go'zal xotiralarga boy bo'lsin! ✨🌸",
        "🌟 {name}, buyurtmangizni qabul qiling!\n\nKichik do'konimizni tanlaganingiz biz uchun katta ma'no kasb etadi. Yurakdan rahmat! Farzandingizga mustahkam salomatlik, oilangizga esa baxt va farovonlik tilaymiz. Yana kutamiz sizni! 👶🤍",
        "💐 {name}, mana sizning buyurtmangiz!\n\nHar bir mijozimiz biz uchun bir oila a'zosidek qadrli. Rahmat sizga ishonganingiz uchun! Kichkintoyingiz tanlagan kiyimlarida o'zini eng baxtli his qilsin. Sog'lik va quvonch sizga yor bo'lsin! 🌷✨",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nBizga ishonib, vaqt ajratganingiz uchun chin dildan tashakkur. Farzandingizning har bir tabassumi siz uchun eng katta baxt bo'lsin. Kichkintoyingiz mehr ichida, sog'-salomat ulg'aysin! 💝🌸",
        "💛 {name}, buyurtmangiz yetib keldi!\n\nSizdek g'amxo'r ota-onalarga xizmat qilish biz uchun sharaf. Rahmat! Tanlagan narsalaringiz farzandingizga iliqlik bersin, hayoti quvonch va sog'likka to'lsin. Yana ko'rishguncha! 👶✨",
        "🌷 {name}, buyurtmangizni qabul qiling!\n\nIshonchingiz uchun yurakdan rahmat. Har bir kiyim — kichkintoyingiz uchun bir xotira. Farzandingiz baxtli, sog'lom va sevgi ichida o'ssin. Oilangizga mehr va farovonlik tilaymiz! 🤍🌸",
        "💖 {name}, mana buyurtmangiz!\n\nBabyDiary'ni tanlaganingizdan chin dildan xursandmiz. Sizning quvonchingiz — bizning ilhomimiz. Kichkintoyingiz har kuni kulib o'ssin, hayoti baxtli xotiralarga boy bo'lsin! ✨🌷",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nSizning oilangizga xizmat qilganimizdan baxtiyormiz. Farzandingizga eng yaxshisini tilaymiz. Kichkintoyingiz sog'-salomat, mehr ichida va doim baxtli bo'lib ulg'aysin. Rahmat sizga! 💝🌸",
        "🌟 {name}, buyurtmangiz yetib keldi!\n\nBizni tanlaganingiz uchun chin dildan minnatdormiz. Har bir buyurtma — biz uchun yangi mas'uliyat va quvonch. Farzandingiz sog'lik, baxt va mehr ichida katta bo'lsin! Yana kutamiz! 👶🤍",
        "💝 {name}, buyurtmangizni qabul qiling!\n\nSizdek mijozlar bizning eng katta boyligimiz. Yurakdan rahmat ishonchingiz uchun! Kichkintoyingizning kulgusi uyingizni yoritsin, farzandingiz baxtli va sog'lom ulg'aysin! ✨🌷",
        "🌸 {name}, mana sizning buyurtmangiz!\n\nVaqtingiz va e'tiboringiz uchun rahmat. Tanlagan kiyimlaringiz farzandingizga juda yarashsin. Kichkintoyingiz mehr, quvonch va sog'lik ichida ulg'aysin. Oilangizga tinchlik tilaymiz! 🤍💖",
        "🤍 {name}, buyurtmangiz yetkazildi!\n\nBabyDiary oilasi sizga chin dildan rahmat aytadi. Farzandingizning har bir kuni baxtga to'lsin. Kichkintoyingiz sog'-salomat, sevimli va doim tabassum ichida bo'lib ulg'aysin! 🌷✨",
        "💛 {name}, buyurtmangiz yetib keldi!\n\nIshonchingiz va mehringiz uchun chin dildan tashakkur. Sizning xursandchiligingiz biz uchun bebaho. Farzandingiz baxtli, sog'lom va mehr ichida katta bo'lsin. Yana ko'rishguncha, aziz mijozimiz! 👶🤍",
    ],
    "ru": [
        "💝 {name}, ваш заказ доставлен!\n\nОт всего сердца благодарим, что выбрали BabyDiary. Мы счастливы быть рядом, помогая выбрать лучшее для вашего малыша. Пусть кроха растёт радостным, здоровым и окружённым любовью! 🤍",
        "🌸 {name}, ваш заказ у вас!\n\nИскренне благодарим за доверие. Желаем, чтобы каждая вещь дарила вашему малышу тепло и заботу. Пусть каждый день вашего крохи будет наполнен радостью и улыбками! 👶✨",
        "🤍 {name}, примите ваш заказ!\n\nРаботать для таких заботливых родителей — большое счастье. Пусть ваш малыш будет здоров, счастлив и всегда улыбается. До новых встреч, семья BabyDiary всегда вас ждёт! 🌷",
        "💛 {name}, ваш заказ доставлен!\n\nОт всей души спасибо, что выбрали и доверились нам. Ваша радость — наша лучшая награда. Пусть ваш малыш растёт в любви, здоровье и счастье! 🤍✨",
        "✨ {name}, вот ваш заказ!\n\nКаждый ваш заказ для нас особенно ценен. Пусть выбранные вещи дарят малышу тепло и радость. Желаем благополучия вашей семье и крепкого здоровья вашему крохе! 🌸",
        "🌷 {name}, ваш заказ доставлен!\n\nИскренне благодарим за доверие к BabyDiary. Пусть смех вашего малыша всегда наполняет дом светом. Пусть здоровье, счастье и любовь никогда не покидают вашу семью! 👶🤍",
        "💖 {name}, примите ваш заказ!\n\nСлужить вам — честь для нас. Уверены, выбранные вещи прекрасно подойдут вашему малышу. Пусть кроха просыпается с радостью, а жизнь будет полна счастья! ✨🌸",
        "🤍 {name}, ваш заказ доставлен!\n\nСпасибо, что выбрали наш маленький магазин и доверились нам. Каждый шов, каждая ткань выбраны с любовью для вашего малыша. Пусть кроха растёт здоровым и счастливым! 🌷💝",
        "💐 {name}, вот ваш заказ!\n\nБлагодаря таким клиентам мы стараемся становиться лучше каждый день. Спасибо вам! Желаем здоровья малышу, а вашей семье — мира и достатка. Будем рады видеть вас снова! 👶🤍",
        "🌟 {name}, ваш заказ доставлен!\n\nИскренне рады, что вы выбрали BabyDiary. Пусть одежда дарит малышу комфорт и тепло. Пусть ваш кроха растёт в любви, здоровым и счастливым! ✨🌸",
        "💝 {name}, примите ваш заказ!\n\nВаше доверие бесценно для нас. «Каждая вещь — это воспоминание», — говорим мы. Пусть каждый миг с вашим малышом станет прекрасным воспоминанием. Здоровья и счастья вам! 🤍",
        "🌸 {name}, вот ваш заказ!\n\nСпасибо, что уделили нам время и доверие. Пусть малыш чувствует себя уютно и счастливо в выбранных вещах. Желаем вашей семье любви, здоровья и благополучия! 👶✨",
        "🤍 {name}, ваш заказ доставлен!\n\nМы гордимся тем, что служим вашей семье. Пусть каждый день вашего малыша будет солнечным и радостным. BabyDiary всегда рядом с вами и крохой! Спасибо вам! 🌷💖",
        "💛 {name}, ваш заказ у вас!\n\nОт всего сердца спасибо! Уверены, выбранные вещи понравятся малышу и подарят ему тепло. Пусть кроха растёт здоровым, улыбчивым и счастливым ребёнком! ✨🤍",
        "🌷 {name}, примите ваш заказ!\n\nИскренне благодарим, что выбрали нас. Каждая вещь сделана с любовью и заботой. Пусть малыш будет счастлив, здоров и любим. Желаем вашей семье мира! 👶🌸",
        "💖 {name}, вот ваш заказ!\n\nРаботать с такими замечательными клиентами — настоящая радость. Спасибо за доверие! Пусть смех вашего крохи никогда не смолкает, а жизнь будет полна счастья и любви! 🤍✨",
        "🤍 {name}, ваш заказ доставлен!\n\nСемья BabyDiary искренне благодарна вам. Пусть всё выбранное приносит малышу радость. Пусть кроха растёт в здоровье, любви и счастье. Приходите всегда с хорошими новостями! 🌷",
        "💝 {name}, ваш заказ у вас!\n\nСпасибо за время и доверие. Видеть вашу радость — наша цель. Пусть малыш каждый день просыпается с новой радостью, а жизнь будет богата прекрасными воспоминаниями! ✨🌸",
        "🌟 {name}, примите ваш заказ!\n\nВыбор нашего маленького магазина много значит для нас. Спасибо от всего сердца! Желаем малышу крепкого здоровья, а вашей семье — счастья и благополучия. Ждём вас снова! 👶🤍",
        "💐 {name}, вот ваш заказ!\n\nКаждый клиент дорог нам как член семьи. Спасибо за доверие! Пусть малыш чувствует себя самым счастливым в выбранных вещах. Здоровья и радости вам! 🌷✨",
        "🤍 {name}, ваш заказ доставлен!\n\nОт всего сердца благодарим, что доверились и уделили нам время. Пусть каждая улыбка малыша станет для вас огромным счастьем. Пусть кроха растёт в любви и здоровье! 💝🌸",
        "💛 {name}, ваш заказ у вас!\n\nСлужить таким заботливым родителям — честь для нас. Спасибо! Пусть выбранные вещи дарят малышу тепло, а жизнь будет полна радости и здоровья. До новых встреч! 👶✨",
        "🌷 {name}, примите ваш заказ!\n\nОт всей души спасибо за доверие. Каждая вещь — это воспоминание для вашего крохи. Пусть малыш растёт счастливым, здоровым и любимым. Желаем вашей семье любви! 🤍🌸",
        "💖 {name}, вот ваш заказ!\n\nИскренне рады, что вы выбрали BabyDiary. Ваша радость — наше вдохновение. Пусть ваш кроха улыбается каждый день, а жизнь будет богата счастливыми моментами! ✨🌷",
        "🤍 {name}, ваш заказ доставлен!\n\nМы счастливы служить вашей семье. Желаем вашему малышу самого лучшего. Пусть кроха растёт здоровым, в любви и всегда счастливым. Спасибо вам! 💝🌸",
        "🌟 {name}, ваш заказ у вас!\n\nОт всего сердца благодарны, что выбрали нас. Каждый заказ — новая ответственность и радость для нас. Пусть малыш растёт в здоровье, счастье и любви! Ждём вас! 👶🤍",
        "💝 {name}, примите ваш заказ!\n\nТакие клиенты, как вы — наше главное богатство. Сердечное спасибо за доверие! Пусть смех вашего крохи освещает дом, а малыш растёт счастливым и здоровым! ✨🌷",
        "🌸 {name}, вот ваш заказ!\n\nСпасибо за время и внимание. Пусть выбранные вещи прекрасно подойдут малышу. Пусть кроха растёт в любви, радости и здоровье. Желаем вашей семье мира! 🤍💖",
        "🤍 {name}, ваш заказ доставлен!\n\nСемья BabyDiary от всего сердца благодарит вас. Пусть каждый день малыша наполнен счастьем. Пусть кроха растёт здоровым, любимым и всегда улыбается! 🌷✨",
        "💛 {name}, ваш заказ у вас!\n\nОт всей души спасибо за доверие и теплоту. Ваша радость для нас бесценна. Пусть малыш растёт счастливым, здоровым и в любви. До новых встреч, дорогой клиент! 👶🤍",
    ]
}

def random_thanks(chat_id, name):
    lng = lang(chat_id)
    messages = THANKS_MESSAGES.get(lng, THANKS_MESSAGES["uz"])
    return random.choice(messages).format(name=name)

# ─── SOZLAMALAR ───────────────────────────────────────────────────────────────

def get_delivery():       return int(db.get_setting("delivery") or 30000)
def get_min_delivery():   return int(db.get_setting("min_delivery") or 30000)
def get_gift_box_price(): return int(db.get_setting("gift_box_price") or 50000)
def get_gift_box_cost():  return int(db.get_setting("gift_box_cost") or 0)
def get_qadoq_oddiy_cost(): return int(db.get_setting("qadoq_oddiy_cost") or 0)
def get_operator():       return db.get_setting("operator") or "@babydiarysupport"

def order_qadoq_cost(packaging_price):
    """Buyurtmaga biriktiriladigan qadoq tannarxi (sotuv paytida fix qilinadi)."""
    return get_gift_box_cost() if int(packaging_price or 0) > 0 else get_qadoq_oddiy_cost()

# ─── Masofaga qarab yetkazib berish ───────────────────────────────────────────
def get_shop_location():
    """Do'kon koordinatasi (admin sozlaydi). (lat, lon) yoki None."""
    lat = db.get_setting("shop_lat")
    lon = db.get_setting("shop_lon")
    if lat and lon:
        try:
            return float(lat), float(lon)
        except:
            return None
    return None

def get_price_per_km():
    """1 km uchun narx (admin sozlaydi, default 3000)."""
    return int(db.get_setting("price_per_km") or 3000)

def haversine_km(lat1, lon1, lat2, lon2):
    """Ikki nuqta orasidagi to'g'ri chiziq masofasi (km)."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371  # Yer radiusi (km)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def calc_delivery(lat, lon):
    """Mijoz lokatsiyasiga qarab yetkazib berish summasini hisoblaydi (faqat masofa).
    Do'kon koordinatasi yo'q bo'lsa (0, None) qaytaradi."""
    shop = get_shop_location()
    if not shop or lat is None or lon is None:
        return 0, None  # do'kon o'rnatilmagan yoki lokatsiya yo'q
    dist = haversine_km(shop[0], shop[1], float(lat), float(lon))
    real_km = dist * 1.3  # yo'l egriligi koeffitsienti
    summa = real_km * get_price_per_km()
    summa = int(round(summa / 1000) * 1000)  # 1000 ga yaxlitlash
    minimal = get_min_delivery()
    if summa < minimal:
        summa = minimal                       # eng kam dastavka (sozlamadan)
    return summa, round(real_km, 1)

# ── To'lov kassalari (web.py bilan AYNAN bir xil env va shartlar) ──
# MUHIM: shartlar web.py bilan mos bo'lishi shart. Aks holda bot to'lov
# tugmasini ko'rsatadi, lekin web.py callback'ni rad etadi.
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_KEY         = os.getenv("PAYME_KEY", "")          # callback auth uchun — web.py ishlatadi
BOT_USERNAME = os.getenv("BOT_USERNAME", "babydiaryuz_bot")

CLICK_SERVICE_ID  = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SECRET_KEY  = os.getenv("CLICK_SECRET_KEY", "")   # callback imzosi — web.py ishlatadi
CLICK_PAY_URL     = "https://my.click.uz/services/pay"

def payme_enabled():
    return bool(PAYME_MERCHANT_ID and PAYME_KEY)

def click_enabled():
    return bool(CLICK_SERVICE_ID and CLICK_MERCHANT_ID and CLICK_SECRET_KEY)

def make_click_link(number, total_som):
    """Click to'lov sahifasi linki. transaction_param = buyurtma raqami —
    web.py dagi /click/prepare shu raqam bo'yicha buyurtmani topadi."""
    if not click_enabled():
        return ""
    q = urllib.parse.urlencode({
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "amount": int(total_som),
        "transaction_param": number,
        "return_url": "https://t.me/%s" % BOT_USERNAME,
    })
    return "%s?%s" % (CLICK_PAY_URL, q)

def make_payme_link(number, total_som):
    """Buyurtma raqami + summaga qarab Payme checkout linkini yasaydi.
    c = to'lovdan keyin QAYTISH manzili — botning o'zi (saytga emas)."""
    if not payme_enabled():
        return ""
    import base64 as _b64
    amount_tiyin = int(total_som) * 100
    # Diqqat: 'c' ichida '=' bo'lmasligi kerak (Payme parseri ';' va birinchi '=' bo'yicha bo'ladi)
    back = "https://t.me/%s" % BOT_USERNAME
    raw = "m=%s;ac.order_id=%s;a=%d;c=%s;l=uz" % (PAYME_MERCHANT_ID, number, amount_tiyin, back)
    encoded = _b64.b64encode(raw.encode()).decode()
    return "https://checkout.paycom.uz/%s" % encoded

# ─── JSON helpers ─────────────────────────────────────────────────────────────

def load_json(file, default):
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"load_json({file}): {e}")
        return default

def save_json(file, data):
    if not write_json_local(file, data):
        return
    fname = os.path.basename(file)
    # products/orders — GitHub yagona manba (startup'da o'shandan o'qiladi),
    # shuning uchun SINXRON yozamiz: deploy paytida fon thread uzilib qolmasin.
    if fname in ("orders.json", "products.json"):
        ok, why = gh_push_json_sync(file, f"data/{fname}", fname)
        if not ok:
            log.error(f"❌ {fname} GitHub'ga yozilmadi: {why}")
    elif fname == "reviews.json":
        gh_push_json(file, f"data/{fname}", fname)

# ─── Mahsulotlarni GitHub bilan yangilab turish ───────────────────────────────
# Sayt (web.py) sotuvda stokni GitHub'da kamaytiradi. Bot lokal nusxani ushlab
# tursa — keyingi yozuvida saytning kamaytirishini o'chirib yuboradi (oversell).
# Shuning uchun o'qishdan oldin GitHub'dan yangilaymiz (TTL bilan).
_prod_lock = threading.Lock()
_prod_pull_ts = 0.0
PROD_TTL = 15   # sekund

def refresh_products_from_gh(force=False):
    global _prod_pull_ts
    if not _GH_TOKEN:
        return
    now = time.time()
    if not force and (now - _prod_pull_ts) < PROD_TTL:
        return
    if not _prod_lock.acquire(blocking=False):
        return                      # boshqa oqim allaqachon tortyapti
    try:
        if not force and (time.time() - _prod_pull_ts) < PROD_TTL:
            return
        meta = _gh_get_file("data/products.json")
        if meta and meta.get("content"):
            raw = base64.b64decode(meta["content"].replace("\n", ""))
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                with open(PRODUCTS_FILE, "wb") as f:
                    f.write(raw)
        _prod_pull_ts = time.time()
    except Exception as e:
        log.warning(f"products GitHub pull: {e}")
        _prod_pull_ts = time.time()      # xatoda ham TTL (spam bo'lmasin)
    finally:
        try:
            _prod_lock.release()
        except Exception:
            pass

def get_products():
    refresh_products_from_gh()        # saytdagi stok o'zgarishlarini ham ko'ramiz
    data = load_json(PRODUCTS_FILE, [])
    # Self-heal: razmerli mahsulotlarda umumiy 'stock' = sizes yig'indisi bo'lsin
    for p in data:
        sizes = p.get("sizes")
        if sizes:
            p["stock"] = sum(int(s.get("stock", 0)) for s in sizes)
    return data

def save_products(data):
    global _prod_pull_ts
    save_json(PRODUCTS_FILE, data)
    _prod_pull_ts = time.time()       # endigina yozdik — darrov qayta tortmaymiz

# ─── Bazani tozalash yordamchilari ───────────────────────────────────────────
# database.py jadval nomlari shu faylda emas — sqlite_master orqali topamiz.
# Shu tufayli jadval nomi o'zgarsa ham tozalash ishlayveradi.

_DB_KEEP_TABLES = {"settings", "promo_codes", "sqlite_sequence"}  # konfiguratsiya — saqlanadi
# Hisoblagich jadvallari: qator o'chirilmaydi, faqat qiymati 0 ga tushiriladi
_COUNTER_TABLES = ("order_counter", "order_counters", "counters", "order_seq", "order_number")
_COUNTER_KEYS   = ("order_counter", "order_number", "last_order_number", "orders_counter")

def _db_tables():
    try:
        con = sqlite3.connect(db.DB_FILE)
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception as e:
        log.error(f"_db_tables: {e}")
        return set()

def _table_cols(con, table):
    try:
        return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]
    except Exception:
        return []

def _db_clear(tables):
    """Jadvallardagi barcha qatorlarni o'chiradi. [(jadval, nechta_edi)] qaytaradi."""
    have = _db_tables()
    out = []
    try:
        con = sqlite3.connect(db.DB_FILE)
        for t in tables:
            if t not in have or t in ("sqlite_sequence",):
                continue
            if t in _COUNTER_TABLES:
                continue          # qatorni o'chirmaymiz — _reset_order_counter() 0 ga tushiradi
            try:
                n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                con.execute('DELETE FROM "%s"' % t)
                if "sqlite_sequence" in have:
                    con.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
                out.append((t, int(n)))
            except Exception as e:
                log.warning(f"_db_clear {t}: {e}")
        con.commit(); con.close()
    except Exception as e:
        log.error(f"_db_clear: {e}")
    return out

def _tables_matching(*subs):
    return sorted(t for t in _db_tables()
                  if t not in _DB_KEEP_TABLES and any(s in t.lower() for s in subs))

def _reset_order_counter():
    """Buyurtma raqamini 0 ga tushiradi — keyingi buyurtma #00001 bo'ladi."""
    done = []
    have = _db_tables()
    try:
        con = sqlite3.connect(db.DB_FILE)
        for t in _COUNTER_TABLES:
            if t not in have:
                continue
            cols = _table_cols(con, t)
            num = [c for c in cols if c.lower() in
                   ("value", "val", "n", "num", "number", "counter", "current", "last")]
            if num:
                for c in num:
                    con.execute('UPDATE "%s" SET "%s"=0' % (t, c))
                done.append(f"{t} → 0")
            else:
                con.execute('DELETE FROM "%s"' % t)
                done.append(f"{t} (bo'shatildi)")
        con.commit(); con.close()
    except Exception as e:
        log.warning(f"_reset_order_counter: {e}")
    for k in _COUNTER_KEYS:
        try:
            if db.get_setting(k) not in (None, ""):
                db.set_setting(k, "0")
                done.append(f"settings.{k} → 0")
        except Exception:
            pass
    return done or ["hisoblagich topilmadi"]

def _drop_card_settings():
    """Bazada qolgan karta raqamlarini o'chiradi (bir martalik migratsiya).
    To'lov endi faqat Payme/Click kassasi orqali — karta saqlanmaydi."""
    try:
        con = sqlite3.connect(db.DB_FILE)
        cols = _table_cols(con, "settings")
        if not cols:
            con.close(); return
        keycol = "key" if "key" in cols else cols[0]
        cur = con.execute('DELETE FROM settings WHERE "%s" IN (?,?)' % keycol,
                          ("payme_card", "click_card"))
        n = cur.rowcount or 0
        con.commit(); con.close()
        if n:
            log.info(f"🔒 Karta raqamlari bazadan o'chirildi ({n} ta yozuv)")
    except Exception as e:
        log.warning(f"_drop_card_settings: {e}")


# ─── Konfliktsiz stok o'zgartirish (web.py bilan AYNAN bir xil mantiq) ───────
# Bot va sayt alohida jarayonlar. Agar bot butun ro'yxatni push qilsa, saytning
# endigina qilgan kamaytirishini o'chirib yuboradi (oversell). Shuning uchun
# stok o'zgarishi DELTA sifatida GitHub'dagi eng so'nggi ro'yxatga qo'llanadi.

def _gh_fetch_products():
    """GitHub'dan (ro'yxat, sha). Xato/yo'q bo'lsa (None, None)."""
    meta = _gh_get_file("data/products.json")
    if not meta or not meta.get("content"):
        return None, None
    try:
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        data = json.loads(raw.decode("utf-8"))
        return (data, meta.get("sha")) if isinstance(data, list) else (None, None)
    except Exception as e:
        log.warning(f"_gh_fetch_products: {e}")
        return None, None

def _gh_put_products(products_list, sha):
    """SHA bilan PUT. True=ok, False=konflikt(409/422) yoki xato."""
    if not _GH_TOKEN:
        return False
    try:
        raw = json.dumps(products_list, ensure_ascii=False, indent=2).encode("utf-8")
        stamp = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M")
        payload = {"message": f"stock update [{stamp}]",
                   "content": base64.b64encode(raw).decode(), "branch": _GH_BRANCH}
        if sha:
            payload["sha"] = sha
        url = f"{_GH_API}/repos/{_GH_REPO}/contents/data/products.json"
        req = urllib.request.Request(url, json.dumps(payload).encode(),
                                     _gh_headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=15):
            pass
        return True
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):
            return False              # kimdir o'zgartirdi — qayta urinamiz
        log.warning(f"_gh_put_products: HTTP {e.code}")
        return False
    except Exception as e:
        log.warning(f"_gh_put_products: {e}")
        return False

def _apply_deltas_to_list(products, deltas):
    """qty manfiy = kamaytirish (sotuv), musbat = qaytarish (bekor qilish)."""
    today = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    byid = {str(p.get("id")): p for p in products}
    for dl in deltas:
        p = byid.get(str(dl.get("product_id")))
        if not p:
            continue                  # o'chirilgan bo'lishi mumkin
        size = (dl.get("size") or "").strip()
        qd = int(dl.get("qty", 0))
        sizes = p.get("sizes") or []
        if sizes and size:
            for s in sizes:
                if s.get("label") == size:
                    s["stock"] = max(0, int(s.get("stock", 0)) + qd)
            p["stock"] = sum(int(s.get("stock", 0)) for s in sizes)
        else:
            p["stock"] = max(0, int(p.get("stock", 0)) + qd)
        if qd < 0 and int(p.get("stock", 0)) == 0:
            p["finished_date"] = today
        elif qd > 0 and int(p.get("stock", 0)) > 0:
            p.pop("finished_date", None)

def apply_stock_deltas(deltas):
    """Stok o'zgarishini GitHub'dagi ENG SO'NGGI ro'yxatga qo'llaydi (SHA + retry)."""
    global _prod_pull_ts
    deltas = [d for d in (deltas or []) if int(d.get("qty", 0)) != 0]
    if not deltas:
        return
    with _prod_lock:                  # /api refresh bilan aralashmasin
        for _ in range(5):
            gh_list, sha = _gh_fetch_products()
            if gh_list is None:
                # GitHub yo'q — lokal fallback
                products = load_json(PRODUCTS_FILE, [])
                _apply_deltas_to_list(products, deltas)
                write_json_local(PRODUCTS_FILE, products)
                _prod_pull_ts = time.time()
                return
            _apply_deltas_to_list(gh_list, deltas)
            write_json_local(PRODUCTS_FILE, gh_list)
            if _gh_put_products(gh_list, sha):
                _prod_pull_ts = time.time()
                return
            time.sleep(0.35)          # SHA konflikti — qayta
        log.error("apply_stock_deltas: GitHub konflikti, urinishlar tugadi")

# ─── Razmer (size) tizimi ─────────────────────────────────────────────────────
# Har mahsulotda bir nechta razmer, har biriga alohida ombor.
# products.json: "sizes": [{"label":"3 oy","stock":4}, ...], "stock" = yig'indi.
# Razmersiz eski mahsulotlar ham ishlaydi (yagona "stock").
SIZE_POOL = ["3 oy", "6 oy", "9 oy", "12 oy", "18 oy",
             "2 yosh", "3 yosh", "4 yosh", "5 yosh", "6 yosh", "7 yosh",
             "90 sm", "100 sm", "110 sm", "120 sm", "130 sm", "140 sm"]

def split_cart_key(key):
    """'pid|3 oy' -> ('pid','3 oy');  'pid' -> ('pid', None)."""
    s = str(key)
    if "|" in s:
        pid, size = s.split("|", 1)
        return pid, size
    return s, None

def make_cart_key(pid, size=None):
    return f"{pid}|{size}" if size else str(pid)

def size_stock(product, size):
    """Razmer ombori (razmersiz bo'lsa umumiy 'stock')."""
    sizes = product.get("sizes") or []
    if size and sizes:
        for s in sizes:
            if s.get("label") == size:
                return int(s.get("stock", 0))
        return 0
    return int(product.get("stock", 0))

def product_total_stock(product):
    sizes = product.get("sizes") or []
    if sizes:
        return sum(int(s.get("stock", 0)) for s in sizes)
    return int(product.get("stock", 0))

def dec_stock(product, size, qty):
    """Ombordan kamaytiradi va umumiy 'stock'ni yangilaydi."""
    sizes = product.get("sizes") or []
    if size and sizes:
        for s in sizes:
            if s.get("label") == size:
                s["stock"] = max(0, int(s.get("stock", 0)) - int(qty))
        product["stock"] = sum(int(x.get("stock", 0)) for x in sizes)
    else:
        product["stock"] = max(0, int(product.get("stock", 0)) - int(qty))

def sizes_to_ages(labels):
    """Razmerlardan yosh filtrini (AGE_RANGES) avtomatik chiqaradi."""
    m = {"3 oy": "0–1 yosh", "6 oy": "0–1 yosh", "9 oy": "0–1 yosh", "12 oy": "0–1 yosh",
         "18 oy": "1–2 yosh", "2 yosh": "1–2 yosh", "3 yosh": "2–3 yosh",
         "4 yosh": "3–5 yosh", "5 yosh": "3–5 yosh", "6 yosh": "5–7 yosh", "7 yosh": "5–7 yosh",
         "90 sm": "2–3 yosh", "100 sm": "3–5 yosh", "110 sm": "3–5 yosh",
         "120 sm": "5–7 yosh", "130 sm": "5–7 yosh", "140 sm": "5–7 yosh"}
    out = []
    for lb in labels:
        a = m.get(lb)
        if a and a not in out:
            out.append(a)
    return out

def size_pick_keyboard(selected):
    selected = selected or []
    kb = types.InlineKeyboardMarkup()
    row = []
    for i, s in enumerate(SIZE_POOL):
        mark = "✅ " if s in selected else "▫️ "
        row.append(types.InlineKeyboardButton(mark + s, callback_data=f"sizetog_{i}"))
        if len(row) == 3:
            kb.add(*row); row = []
    if row:
        kb.add(*row)
    kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="sizes_done"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="add_back"))
    return kb

# ─── Jins (gender) ─────────────────────────────────────────────────────────────
# Mahsulot: "boy" (o'g'il), "girl" (qiz), "unisex" (ikkisi ham).
# Saytda "o'g'il" filtri o'g'il+unisex, "qiz" filtri qiz+unisex ni ko'rsatadi.
GENDER_OPTS = [("boy", "👦 O'g'il bola"), ("girl", "👧 Qiz bola"), ("unisex", "👶 Ikkisi ham")]
GENDER_LABEL = {k: v for k, v in GENDER_OPTS}

def gender_keyboard():
    kb = types.InlineKeyboardMarkup()
    for k, label in GENDER_OPTS:
        kb.add(types.InlineKeyboardButton(label, callback_data=f"gender_{k}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="add_back"))
    return kb

# ─── Mahsulot qo'shish: bir qadam ortga qaytish ────────────────────────────────
def step_back_menu(chat_id):
    """Admin bosqichlari uchun: ⬅️ Orqaga + 🏠 Asosiy menu."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⬅️ Orqaga", tr(chat_id, "home"))
    return kb

def order_back_menu(chat_id):
    """Mijoz buyurtma bosqichlari uchun."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(tr(chat_id, "back"), tr(chat_id, "home"))
    return kb

def add_back_menu(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⬅️ Orqaga", tr(chat_id, "home"))
    return kb

ADD_FLOW = ["name_uz", "name_ru", "cost", "price_choose", "desc_uz", "desc_ru",
            "photo", "sizes_pick", "sizes_qty", "category", "gender"]

def prompt_step(chat_id, step):
    """Berilgan qo'shish-bosqichi savolini + klaviaturasini qayta chiqaradi (orqaga uchun)."""
    d = db.get_admin_step(chat_id) or {}
    if step == "name_uz":
        safe_send(chat_id, "Mahsulot nomini o'zbekcha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "name_ru":
        safe_send(chat_id, "Nomni ruscha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "cost":
        safe_send(chat_id, "💵 Tan narxini yozing (raqam):", reply_markup=add_back_menu(chat_id))
    elif step == "price_choose":
        cost = int(d.get("cost", 0) or 0)
        base, variants = suggest_prices(cost)
        kb = types.InlineKeyboardMarkup()
        for label, val in variants:
            kb.add(types.InlineKeyboardButton(f"{val:,} so'm", callback_data=f"setprice_{val}"))
        kb.add(types.InlineKeyboardButton("✏️ Boshqa narx yozaman", callback_data="setprice_custom"))
        safe_send(chat_id, f"💡 Tan narx: {cost:,} so'm\n\nSotuv narxini tanlang:", reply_markup=kb)
        safe_send(chat_id, "…yoki orqaga qaytish:", reply_markup=add_back_menu(chat_id))
    elif step == "desc_uz":
        safe_send(chat_id, "Tavsifni o'zbekcha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "desc_ru":
        safe_send(chat_id, "Tavsifni ruscha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "photo":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Rasmsiz davom etish", callback_data="photo_skip"))
        send_kb_prompt(chat_id, "📸 Rasm(lar)ni yuboring. Yuborgach '✅ Tayyor' chiqadi.", kb)
        safe_send(chat_id, "…yoki orqaga:", reply_markup=add_back_menu(chat_id))
    elif step == "sizes_pick":
        safe_send(chat_id, "📏 Bu mahsulotda qaysi razmerlar bor? Tanlang:",
                  reply_markup=size_pick_keyboard(d.get("size_labels", [])))
    elif step == "sizes_qty":
        labels = d.get("size_labels", [])
        example = " ".join(["4", "2", "0", "5", "3", "1", "2", "3", "1", "2", "1"][:len(labels)] or ["4"])
        safe_send(chat_id,
                  "Tanlangan razmerlar: " + " · ".join(labels) + "\n\n"
                  f"Har biriga sonini shu tartibda, probel bilan yozing.\nMasalan: {example}",
                  reply_markup=add_back_menu(chat_id))
    elif step == "category":
        all_cats = []
        for c in get_category_list() + list(get_categories().keys()):
            if c not in all_cats:
                all_cats.append(c)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row = []
        for cat in all_cats:
            row.append(cat)
            if len(row) == 2:
                kb.add(*row); row = []
        if row: kb.add(*row)
        kb.add("⬅️ Orqaga", tr(chat_id, "home"))
        safe_send(chat_id, "📂 Kategoriyani tanlang yoki yangi nom yozing:", reply_markup=kb)
    elif step == "gender":
        safe_send(chat_id, "👶 Bu kim uchun? (jinsni tanlang)", reply_markup=gender_keyboard())

def add_step_back(chat_id):
    d = db.get_admin_step(chat_id) or {}
    step = d.get("step", "")
    if step not in ADD_FLOW:
        return False
    idx = ADD_FLOW.index(step)
    if idx == 0:
        db.delete_admin_step(chat_id)
        safe_send(chat_id, "❌ Bekor qilindi.", reply_markup=admin_menu())
        return True
    prev = ADD_FLOW[idx - 1]
    d["step"] = prev
    db.set_admin_step(chat_id, d)
    prompt_step(chat_id, prev)
    return True

def get_admins():
    data = load_json(ADMINS_FILE, {"admins": [5285940949, 512101064]})
    return data.get("admins", [5285940949, 512101064])

# Cheklangan adminlar (faqat mahsulot/buyurtma/fikr/sozlama, qolgani ko'rinmaydi)
LIMITED_ADMINS = [8733385729]

def is_admin(uid):
    """To'liq YOKI cheklangan admin (umumiy ruxsat)."""
    return int(uid) in [int(x) for x in get_admins()] or int(uid) in LIMITED_ADMINS
def is_full_admin(uid):
    """Faqat to'liq admin (Ibrohim, Rustam)."""
    return int(uid) in [int(x) for x in get_admins()]
def is_limited_admin(uid):
    """Faqat cheklangan admin."""
    return int(uid) in LIMITED_ADMINS

def get_all_admins():
    """To'liq + cheklangan adminlar (buyurtma/fikr xabarlari uchun)."""
    result = [int(x) for x in get_admins()]
    for la in LIMITED_ADMINS:
        if la not in result:
            result.append(la)
    return result

def can_manage_status(uid):
    """Buyurtma holatini FAQAT cheklangan admin o'zgartiradi.
    Cheklangan admin yo'q bo'lsa — to'liq adminlarga o'tadi (yo'qolmasligi uchun)."""
    if LIMITED_ADMINS:
        return is_limited_admin(uid)
    return is_full_admin(uid)

def _order_number(order_id):
    try:
        for o in load_json(ORDERS_FILE, []):
            if str(o.get("id")) == str(order_id):
                return o.get("number")
    except Exception:
        pass
    return None
def get_reviews():         return load_json(REVIEWS_FILE, [])
def save_reviews(d):       save_json(REVIEWS_FILE, d)
def get_pending_reviews(): return load_json(PENDING_REVIEWS_FILE, [])
def save_pending_reviews(d): save_json(PENDING_REVIEWS_FILE, d)

def suggest_prices(cost):
    """Tan narxdan sotuv narxlarini taklif qiladi.
    Formula: (tan narx * 2) + 30%. Keyin turli yaxlitlash."""
    base = cost * 2 * 1.3  # (tan narx × 2) + 30%
    exact = int(round(base))
    # Turli yaxlitlash
    def round_to(val, step):
        return int(round(val / step) * step)
    r1000  = round_to(base, 1000)
    r5000  = round_to(base, 5000)
    r10000 = round_to(base, 10000)
    # Variantlar (takrorlanmasligi uchun dict)
    variants = []
    seen = set()
    for label, val in [("aniq", exact), ("1000", r1000), ("5000", r5000), ("10000", r10000)]:
        if val not in seen and val > 0:
            variants.append((label, val))
            seen.add(val)
    return base, variants

def build_order_items(chat_id):
    """Savatdagi mahsulotlarni ro'yxat qiladi (tahlil uchun): nom, soni, narx."""
    items = []
    cart = db.get_cart(chat_id)
    products = get_products()
    for key, qty in cart.items():
        pid, size = split_cart_key(key)
        p = next((x for x in products if str(x["id"]) == str(pid)), None)
        if p:
            items.append({
                "product_id": str(pid),
                "name": p.get("name_uz", ""),
                "name_ru": p.get("name_ru", ""),
                "category": p.get("category", "Boshqa"),
                "size": size or "",
                "price": int(p.get("price", 0)),
                "cost": int(p.get("cost", 0)),
                "qty": int(qty),
                "subtotal": int(p.get("price", 0)) * int(qty),
            })
    return items

def _gh_load_orders():
    """GitHub'dagi orders.json (bo'lmasa None)."""
    meta = _gh_get_file("data/orders.json")
    if not meta or not meta.get("content"):
        return None
    try:
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, list) else None
    except Exception:
        return None

def save_order(data):
    data["date"] = datetime.now().isoformat()
    # Yangi model: tovar buyurtma yaratilganda EMAS, to'lov/tasdiq bo'lganda yechiladi
    data.setdefault("stock_model", "on_payment")
    data.setdefault("stock_taken", False)

    all_orders = load_json(ORDERS_FILE, [])

    # MUHIM: sayt (web.py) ham shu faylni yozadi. Butunlay ustidan yozsak,
    # saytdan kelgan buyurtmalar o'chib ketadi. Shuning uchun GitHub'dagi
    # nusxa bilan birlashtiramiz (id bo'yicha).
    try:
        gh_orders = _gh_load_orders()
        if gh_orders:
            have = {str(o.get("id")) for o in all_orders if o.get("id")}
            added = [o for o in gh_orders if str(o.get("id")) not in have]
            if added:
                all_orders = added + all_orders
                all_orders.sort(key=lambda o: str(o.get("date", "")))
                log.info(f"orders merge: GitHub'dan {len(added)} ta buyurtma qo'shildi")
    except Exception as e:
        log.warning(f"orders merge xato: {e}")

    # Tartib raqami. Sayt max+1, bot SQLite hisoblagichini ishlatadi — ikkisi
    # to'qnashmasligi kerak (Payme buyurtmani AYNAN raqam bo'yicha topadi!).
    used = set()
    for o in all_orders:
        try:
            used.add(int(o.get("number", 0)))
        except Exception:
            pass
    num = data.get("number") or db.next_order_number()
    try:
        num = int(num)
    except Exception:
        num = 1
    if num in used:
        old = num
        num = (max(used) + 1) if used else 1
        log.warning(f"Buyurtma raqami band edi (#{old}) -> #{num} berildi")
    data["number"] = num

    all_orders.append(data)
    save_json(ORDERS_FILE, all_orders)
    return num

def _save_orders_merged(orders):
    """orders ro'yxatini GitHub'dagi nusxa bilan birlashtirib yozadi."""
    try:
        gh_orders = _gh_load_orders()
        if gh_orders:
            have = {str(o.get("id")) for o in orders if o.get("id")}
            added = [o for o in gh_orders if str(o.get("id")) not in have]
            if added:
                orders = added + orders
                orders.sort(key=lambda o: str(o.get("date", "")))
    except Exception as e:
        log.warning(f"_save_orders_merged: {e}")
    save_json(ORDERS_FILE, orders)

def _order_deltas(order, sign):
    return [{"product_id": str(it.get("product_id")),
             "size": it.get("size", ""),
             "qty": sign * int(it.get("qty", 1))} for it in (order.get("items") or [])]

def _is_stock_taken(order):
    """Bu buyurtma uchun tovar hozir ombordan yechilganmi?"""
    if "stock_taken" in order:
        return bool(order["stock_taken"])
    # Eski (deploy'dan oldingi) buyurtmalar: tovar yaratilganda yechilgan edi
    return order.get("stock_model") != "on_payment" and not order.get("stock_restored")

def order_shortages(order):
    """Buyurtma pozitsiyalaridan ombordan oshganlari: [(nom, kerak, bor)]."""
    refresh_products_from_gh(force=True)
    byid = {str(p.get("id")): p for p in load_json(PRODUCTS_FILE, [])}
    out = []
    for it in (order.get("items") or []):
        need = int(it.get("qty", 1))
        nm = it.get("name") or str(it.get("product_id"))
        size = (it.get("size") or "").strip()
        if size:
            nm = f"{nm} ({size})"
        p = byid.get(str(it.get("product_id")))
        if not p:
            out.append((nm, need, 0))          # mahsulot o'chirilgan
            continue
        avail = size_stock(p, size) if size else product_total_stock(p)
        if need > avail:
            out.append((nm, need, avail))
    return out

def take_order_stock(order_id, reason=""):
    """Tovarni ombordan YECHADI — faqat to'lov yakunlanganda yoki admin
    tasdiqlaganda chaqiriladi. Idempotent (`stock_taken` bayrog'i).
    Qaytaradi: (ok, shortages). ok=False faqat ombor yetmaganda."""
    orders = load_json(ORDERS_FILE, [])
    order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
    if not order:
        log.warning(f"take_order_stock: buyurtma topilmadi ({order_id})")
        return True, []                        # buyurtma yo'q — bloklamaymiz
    if _is_stock_taken(order):
        return True, []                        # allaqachon yechilgan

    short = order_shortages(order)
    if short:
        log.warning(f"take_order_stock: ombor yetarli emas ({order_id}) -> {short}")
        return False, short

    apply_stock_deltas(_order_deltas(order, -1))
    order["stock_taken"] = True
    order.pop("stock_restored", None)
    _save_orders_merged(orders)
    log.info(f"📦 Ombordan yechildi: {order_id} ({reason})")

    # Ombor kam qoldimi?
    try:
        threshold = int(db.get_setting("low_stock_threshold") or 3)
    except Exception:
        threshold = 3
    low = []
    byid = {str(p.get("id")): p for p in load_json(PRODUCTS_FILE, [])}
    for it in (order.get("items") or []):
        p = byid.get(str(it.get("product_id")))
        if not p:
            continue
        size = (it.get("size") or "").strip()
        left = size_stock(p, size) if size else product_total_stock(p)
        if left <= threshold:
            nm = p.get("name_uz") or ""
            if size:
                nm = f"{nm} ({size})"
            low.append((nm, left))
    if low:
        try:
            notify_low_stock(low, threshold)
        except Exception as e:
            log.error(f"low stock alert xato: {e}")
    return True, []

def _chat_by_phone(phone):
    """Sayt akkaunti (web_users) orqali Telegram chat_id."""
    try:
        con = sqlite3.connect(db.DB_FILE)
        row = con.execute("SELECT chat_id FROM web_users WHERE phone=?",
                          (_norm_phone(phone),)).fetchone()
        con.close()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0

def _order_chat_id(order):
    """Buyurtma egasining chat_id si (bot / tracking / sayt akkaunti)."""
    cid = order.get("tg_chat_id") or 0
    if not cid:
        t = db.get_tracking(str(order.get("id"))) or {}
        cid = t.get("chat_id") or 0
    if not cid and order.get("phone"):
        cid = _chat_by_phone(order.get("phone"))
    try:
        return int(cid or 0)
    except Exception:
        return 0

def _refund_cashback(order):
    """Bekor qilinganda ishlatilgan cashback'ni mijozga qaytaradi (bir marta)."""
    amt = int(order.get("cashback_used", 0) or 0)
    if amt <= 0 or order.get("cashback_refunded"):
        return
    cid = _order_chat_id(order)
    if not cid:
        log.warning(f"cashback qaytarilmadi: chat_id yo'q ({order.get('id')})")
        return
    try:
        bal = db.add_cashback(cid, amt)
        order["cashback_refunded"] = True
        if lang(cid) == "ru":
            safe_send(cid, f"↩️ Заказ отменён. Возвращено {amt:,} сум кешбэка.\n"
                           f"💰 Баланс: {bal:,} сум")
        else:
            safe_send(cid, f"↩️ Buyurtma bekor qilindi. {amt:,} so'm cashback qaytarildi.\n"
                           f"💰 Balans: {bal:,} so'm")
        log.info(f"↩️ Cashback qaytarildi: {amt} -> {cid}")
    except Exception as e:
        log.error(f"cashback qaytarish xato: {e}")

def _refund_promo_code(code):
    """Promo kodning bitta ishlatilishini qaytaradi (cheksiz kodga tegmaydi)."""
    code = (code or "").strip().upper()
    if not code:
        return
    try:
        con = sqlite3.connect(db.DB_FILE)
        row = con.execute("SELECT uses_left FROM promo_codes WHERE code=?", (code,)).fetchone()
        if row and row[0] is not None and int(row[0]) >= 0:
            con.execute("UPDATE promo_codes SET uses_left=uses_left+1, active=1 WHERE code=?", (code,))
            con.commit()
        con.close()
    except Exception as e:
        log.warning(f"promo qaytarish xato ({code}): {e}")

def _refund_promo(order):
    """Bekor qilinganda promo kodning ishlatilishini qaytaradi (bir marta)."""
    code = (order.get("promo_code") or "").strip()
    if not code or order.get("promo_refunded"):
        return
    _refund_promo_code(code)
    order["promo_refunded"] = True

def restore_order_stock(order_id, reason="", cancel=False):
    """Buyurtmani orqaga qaytaradi. Tovar FAQAT yechilgan bo'lsa qaytariladi
    (`stock_taken`). Cashback va promo esa har doim qaytariladi — ular
    buyurtma yaratilganda yechilgan.
    cancel=True -> buyurtma 'cancelled' bo'ladi (Payme qayta to'lay olmaydi)."""
    orders = load_json(ORDERS_FILE, [])
    order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
    if not order:
        log.warning(f"restore_order_stock: buyurtma topilmadi ({order_id})")
        return False

    if _is_stock_taken(order):
        apply_stock_deltas(_order_deltas(order, +1))
        order["stock_taken"] = False
        order["stock_restored"] = True
        log.info(f"♻️ Ombor qaytarildi: {order_id} ({reason})")
    else:
        order["stock_taken"] = False

    _refund_cashback(order)
    _refund_promo(order)

    if cancel:
        order["status"] = "cancelled"
        pm = order.get("payme") or {}
        if pm.get("state") != 2:      # to'langanni -1 qilmaymiz
            order["payme"] = dict(pm, state=-1,
                                  cancel_time=int(time.time() * 1000),
                                  reason=pm.get("reason") or 4)
    _save_orders_merged(orders)
    try:
        db.delete_pending_cashback(str(order_id))
    except Exception:
        pass
    return True

# ─── Lang & translate ─────────────────────────────────────────────────────────

def lang(chat_id): return db.get_lang(chat_id)
def tr(chat_id, key, **kwargs):
    text = TEXT[lang(chat_id)].get(key, key)
    return text.format(**kwargs) if kwargs else text

# ─── Decorators ───────────────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    def wrapper(message):
        if not is_admin(message.from_user.id):
            return
        return func(message)
    return wrapper

def full_admin_only(func):
    """Faqat to'liq admin (Ibrohim, Rustam). Cheklangan admin uchun yopiq (jim)."""
    @wraps(func)
    def wrapper(message):
        if not is_full_admin(message.from_user.id):
            return
        return func(message)
    return wrapper

# ─── Safe send ────────────────────────────────────────────────────────────────

def _is_full_admin_menu(kb):
    """reply_markup to'liq admin menyusimi (cheklangan adminga mos kelmaydigan)?"""
    try:
        flat = []
        for row in getattr(kb, "keyboard", []):
            for btn in row:
                t = btn.get("text") if isinstance(btn, dict) else getattr(btn, "text", "")
                flat.append(t)
        # To'liq menyuda bo'lib, cheklanganda bo'lmaydigan tugma bormi
        return any(b in flat for b in ("📄 Hisobot (PDF)", "📣 Broadcast", "⚙️ Buyruqlar", "💰 Cashback boshqaruvi"))
    except Exception:
        return False

# Inline tugmali "so'rov" xabarlari: foydalanuvchi tugma bosmay, rasm/matn
# yuborsa ham o'sha xabarni keyin o'chirib tashlaymiz (chat toza qolsin).
_prompt_msgs = {}

def send_kb_prompt(chat_id, text, kb):
    """Inline tugmali so'rov yuboradi va id sini eslab qoladi."""
    drop_kb_prompt(chat_id)
    m = safe_send(chat_id, text, reply_markup=kb)
    try:
        if m:
            _prompt_msgs[chat_id] = m.message_id
    except Exception:
        pass
    return m

def drop_kb_prompt(chat_id):
    """Eslab qolingan tugmali so'rovni chatdan o'chiradi."""
    mid = _prompt_msgs.pop(chat_id, None)
    if not mid:
        return
    try:
        bot.delete_message(chat_id, mid)
    except Exception as e:
        log.debug(f"drop_kb_prompt: {e}")

def kill_kb(call, delete=True):
    """Tanlangandan keyin tugmalarni yo'q qiladi.
    delete=True  -> xabarni butunlay o'chiradi (chat toza qoladi)
    delete=False -> matn qoladi, faqat tugmalar olinadi
    O'chirib bo'lmasa (48 soatdan eski xabar) — hech bo'lmasa tugmalarni olib tashlaydi."""
    cid, mid = call.message.chat.id, call.message.message_id
    if _prompt_msgs.get(cid) == mid:
        _prompt_msgs.pop(cid, None)
    if delete:
        try:
            bot.delete_message(cid, mid)
            return
        except Exception as e:
            log.debug(f"kill_kb delete: {e}")
    try:
        bot.edit_message_reply_markup(cid, mid, reply_markup=None)
    except Exception as e:
        log.debug(f"kill_kb markup: {e}")

def safe_send(chat_id, text, **kwargs):
    # Cheklangan adminga to'liq admin menyusi yuborilsa -> cheklangan menyuga almashtiramiz
    rm = kwargs.get("reply_markup")
    if rm is not None and is_limited_admin(chat_id) and not is_full_admin(chat_id):
        if _is_full_admin_menu(rm):
            kwargs["reply_markup"] = admin_menu(chat_id)
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        log.error(f"safe_send({chat_id}): {e}")

def safe_photo(chat_id, photo, caption="", **kwargs):
    try:
        return bot.send_photo(chat_id, photo, caption=caption[:1024], **kwargs)
    except Exception as e:
        log.error(f"safe_photo({chat_id}): {e}")

# ─── Ish vaqti tekshiruvi ─────────────────────────────────────────────────────

def check_working_hours(chat_id):
    if db.is_working_hours():
        return True
    wh = db.get_work_hours()
    safe_send(chat_id, tr(chat_id, "closed", start=wh["start_hour"], end=wh["end_hour"]))
    return False

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu(chat_id, user_id=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(tr(chat_id, "catalog"), tr(chat_id, "cart"))
    kb.add(tr(chat_id, "reviews"), tr(chat_id, "write_review"))
    kb.add(tr(chat_id, "my_orders"), tr(chat_id, "operator"))
    kb.add(tr(chat_id, "cashback"), tr(chat_id, "language"))
    if user_id and is_admin(user_id):
        kb.add("👑 Admin panel")
    return kb

def back_menu(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(tr(chat_id, "home"))
    return kb

def admin_menu(user_id=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Mahsulot qo'shish")
    kb.add("📦 Mahsulotlar ro'yxati", "🗑 Mahsulot o'chirish")
    kb.add("✏️ Mahsulot tahrirlash")
    kb.add("📂 Kategoriyalar")
    kb.add("💬 Fikrlar navbati", "📋 Buyurtmalar")
    # Cheklangan admin uchun: faqat Sozlamalar va Buyurtma holati qo'shimcha
    if user_id is not None and is_limited_admin(user_id) and not is_full_admin(user_id):
        kb.add("⚙️ Sozlamalar", "🚚 Buyurtma holati")
        kb.add("🏠 Asosiy menu")
        return kb
    # To'liq admin uchun hamma narsa
    kb.add("📄 Hisobot (PDF)", "⚙️ Sozlamalar")
    kb.add("📣 Broadcast", "🎁 Promo kodlar")
    kb.add("🚚 Buyurtma holati", "🕐 Ish vaqti")
    kb.add("💰 Cashback boshqaruvi", "⚙️ Buyruqlar")
    kb.add("👥 Mijozlar (sayt)")
    kb.add("🏠 Asosiy menu")
    return kb

def settings_menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🚚 Delivery", callback_data="open_delivery"))
    kb.add(types.InlineKeyboardButton("🎁 Sovg'a qutisi narxi (sotuv)", callback_data="set_gift_box"))
    kb.add(types.InlineKeyboardButton("📦 Oddiy qadoq tannarxi", callback_data="set_qadoq_oddiy_cost"))
    kb.add(types.InlineKeyboardButton("🎁 Sovg'a qutisi tannarxi", callback_data="set_gift_box_cost"))
    kb.add(types.InlineKeyboardButton("📉 Ombor ogohlantirish chegarasi", callback_data="set_low_stock"))
    kb.add(types.InlineKeyboardButton("📞 Operator username",   callback_data="set_operator"))
    return kb

def delivery_menu():
    """Delivery sozlamalari — faqat do'kon lokatsiyasi + km narxi."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📍 Do'kon lokatsiyasi (A nuqta)", callback_data="set_shop_loc"))
    kb.add(types.InlineKeyboardButton("🚗 1 km narxi",                 callback_data="set_per_km"))
    kb.add(types.InlineKeyboardButton("💵 Minimal yetkazish summasi",    callback_data="set_min_delivery"))
    return kb

def lang_buttons():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🇺🇿 Uzbek",   callback_data="lang_uz"))
    kb.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"))
    return kb

DEFAULT_CATEGORIES = ["Kiyim", "Bodi", "Pijama", "Kombinezon", "Poyabzal", "Aksessuar", "Boshqa"]

def get_category_list():
    """Admin belgilagan kategoriyalar ro'yxati (qo'shish/o'chirish mumkin).
    Admin barchasini o'chirsa — BO'SH qaytadi (default qaytmaydi, o'zidan o'zi paydo bo'lmaydi)."""
    raw = db.get_setting("category_list")
    if raw is not None:
        try:
            lst = json.loads(raw)
            if isinstance(lst, list):
                return lst          # bo'sh bo'lsa ham aynan shuni qaytaramiz
        except:
            pass
        return []
    # Hech qachon sozlanmagan bo'lsa ham — bo'sh (admin o'zi qo'shadi)
    return []

def save_category_list(lst):
    db.set_setting("category_list", json.dumps(lst, ensure_ascii=False))

def get_category_ru():
    """Kategoriya (uz) -> ruscha nomi. Sayt va bot RU rejimida shuni ko'rsatadi."""
    raw = db.get_setting("category_ru")
    try:
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def set_category_ru(name, ru):
    d = get_category_ru()
    if ru:
        d[name] = ru
    else:
        d.pop(name, None)
    db.set_setting("category_ru", json.dumps(d, ensure_ascii=False))

def cat_label(name, lg="uz"):
    """Foydalanuvchi tiliga mos kategoriya nomi (ruschasi yo'q bo'lsa — uzcha)."""
    if lg == "ru":
        return get_category_ru().get(name) or name
    return name

def cat_from_label(label):
    """Tugma matnidan haqiqiy (uzcha) kategoriya nomini topadi."""
    label = (label or "").replace("📂 ", "").strip()
    if label in get_category_list():
        return label
    for uz, ru in get_category_ru().items():
        if ru == label:
            return uz
    return label

def get_category_images():
    """Kategoriya -> rasm file_id (saytda ko'rsatish uchun)."""
    raw = db.get_setting("category_images")
    try:
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def set_category_image(name, file_id):
    imgs = get_category_images()
    if file_id:
        imgs[name] = file_id
    else:
        imgs.pop(name, None)
    db.set_setting("category_images", json.dumps(imgs, ensure_ascii=False))

def prompt_category_ru(chat_id, cat):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➡️ Ruschasiz o'tkazish", callback_data="catru_skip"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="catadd_back"))
    send_kb_prompt(chat_id,
                   f"🇷🇺 '{cat}' kategoriyasining RUSCHA nomini yozing.\n"
                   f"(Masalan: Ko'ylak → Платье)\n\n"
                   f"Ruscha tildagi mijozlar shu nomni ko'radi.", kb)

def prompt_category_photo(chat_id, cat):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➡️ Rasmsiz qo'shish", callback_data="catphoto_skip"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="catphoto_back"))
    send_kb_prompt(chat_id,
                   f"📸 '{cat}' kategoriyasi uchun rasm yuboring.\n"
                   f"(Saytda shu rasm ko'rinadi.) Yoki rasmsiz qo'shing:", kb)

def finalize_category(chat_id, cat, file_id, cat_ru=""):
    cats = get_category_list()
    if cat and cat not in cats:
        cats.append(cat)
        save_category_list(cats)
    set_category_image(cat, file_id)
    if cat_ru:
        set_category_ru(cat, cat_ru)
    db.delete_admin_step(chat_id)
    log.info(f"Kategoriya qo'shildi: {cat} / {cat_ru or '—'} (rasm={'bor' if file_id else 'yoq'})")
    extra = "🖼 rasm bilan" if file_id else "(rasmsiz)"
    ru_txt = f"\n🇷🇺 Ruscha: {cat_ru}" if cat_ru else "\n🇷🇺 Ruscha: yo'q (keyin qo'shsangiz bo'ladi)"
    safe_send(chat_id, f"✅ '{cat}' kategoriyasi qo'shildi {extra}.{ru_txt}",
              reply_markup=admin_menu())

def rename_category(old, new):
    """Kategoriya nomini o'zgartiradi: ro'yxat + rasm + ruscha nom + mahsulotlar."""
    cats = get_category_list()
    if old in cats:
        save_category_list([new if c == old else c for c in cats])
    imgs = get_category_images()
    if old in imgs:
        imgs[new] = imgs.pop(old)
        db.set_setting("category_images", json.dumps(imgs, ensure_ascii=False))
    ru = get_category_ru()
    if old in ru:
        ru[new] = ru.pop(old)
        db.set_setting("category_ru", json.dumps(ru, ensure_ascii=False))
    products = get_products()
    changed = False
    for p in products:
        if p.get("category") == old:
            p["category"] = new
            changed = True
    if changed:
        save_products(products)

def get_categories():
    """Mijoz katalogi uchun kategoriyalar — FAQAT admin qo'shganlar (category_list),
    va shu kategoriyada omborda (stock>0) mahsuloti borlari. Avtomatik paydo bo'lmaydi."""
    managed = get_category_list()
    counts = {}
    for p in get_products():
        if int(p.get("stock", 0)) > 0:
            c = p.get("category", "") or ""
            if c in managed:
                counts[c] = counts.get(c, 0) + 1
    # Admin tartibida, faqat mahsuloti borlarini ko'rsatamiz
    return {c: counts[c] for c in managed if counts.get(c, 0) > 0}

def category_keyboard(chat_id):
    """Kategoriya tanlash tugmalari."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    cats = get_categories()
    # Ikkitadan kam kategoriya bo'lsa, to'g'ridan-to'g'ri mahsulotlar
    if len(cats) <= 1:
        return None
    all_txt = "📋 Hammasi" if lang(chat_id) == "uz" else "📋 Все"
    kb.add(all_txt)
    row = []
    lg = lang(chat_id)
    for cat in cats:
        row.append(f"📂 {cat_label(cat, lg)}")
        if len(row) == 2:
            kb.add(*row); row = []
    if row: kb.add(*row)
    kb.add("🔍 Qidirish")
    kb.add(tr(chat_id, "home"))
    return kb

def catalog_keyboard(chat_id, page=0, category=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # Faqat omborda bor (stock > 0) mahsulotlar
    products = [p for p in get_products() if int(p.get("stock", 0)) > 0]
    # Kategoriya filtri
    if category and category not in ("Hammasi", "Все"):
        products = [p for p in products if (p.get("category", "Boshqa") or "Boshqa") == category]
    per_page = 5
    start, end = page * per_page, page * per_page + per_page
    for p in products[start:end]:
        name = p["name_ru"] if lang(chat_id) == "ru" else p["name_uz"]
        kb.add(f"🛍 {name}")
    nav = []
    if page > 0:            nav.append(tr(chat_id, "prev"))
    if end < len(products): nav.append(tr(chat_id, "next"))
    if nav: kb.add(*nav)
    kb.add("🔍 Qidirish")
    kb.add(tr(chat_id, "home"))
    return kb

def product_buttons(chat_id, pid, sel_size=None):
    kb = types.InlineKeyboardMarkup()
    product = next((p for p in get_products() if str(p["id"]) == str(pid)), None)
    cart = db.get_cart(chat_id)
    sizes = (product.get("sizes") if product else None) or []
    if sizes:
        # Razmer chiplari
        row = []
        for i, s in enumerate(sizes):
            label = s.get("label", "")
            st = int(s.get("stock", 0))
            q = cart.get(make_cart_key(pid, label), 0)
            if st <= 0:
                txt = f"🚫 {label}"
            elif label == sel_size:
                txt = f"🔘 {label}" + (f" ·{q}" if q else "")
            else:
                txt = label + (f" ·{q}" if q else "")
            row.append(types.InlineKeyboardButton(txt, callback_data=f"selsz_{pid}_{i}"))
            if len(row) == 3:
                kb.add(*row); row = []
        if row:
            kb.add(*row)
        # Tanlangan razmer uchun +/- (agar omborda bor bo'lsa)
        if sel_size:
            st = size_stock(product, sel_size)
            si = next((i for i, s in enumerate(sizes) if s.get("label") == sel_size), 0)
            q = cart.get(make_cart_key(pid, sel_size), 0)
            if st > 0:
                kb.add(
                    types.InlineKeyboardButton("➖", callback_data=f"minus_{pid}_{si}"),
                    types.InlineKeyboardButton(f"{sel_size}: {q}", callback_data="none"),
                    types.InlineKeyboardButton("➕", callback_data=f"plus_{pid}_{si}"),
                )
    else:
        qty = cart.get(str(pid), 0)
        kb.add(
            types.InlineKeyboardButton("➖", callback_data=f"minus_{pid}"),
            types.InlineKeyboardButton(str(qty), callback_data="none"),
            types.InlineKeyboardButton("➕", callback_data=f"plus_{pid}")
        )
    kb.add(types.InlineKeyboardButton(tr(chat_id, "cart"), callback_data="open_cart"))
    return kb

def cart_buttons(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(tr(chat_id, "confirm_order"), callback_data="confirm_order"))
    kb.add(types.InlineKeyboardButton(tr(chat_id, "clear_cart"),    callback_data="clear_cart"))
    return kb

def packaging_buttons(chat_id):
    gp = get_gift_box_price()
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"{tr(chat_id,'gift_box')} (+{gp:,} so'm)", callback_data="pack_gift"))
    kb.add(types.InlineKeyboardButton(f"{tr(chat_id,'brand_bag')} (0 so'm)",      callback_data="pack_bag"))
    kb.add(types.InlineKeyboardButton(tr(chat_id, "back"), callback_data="pack_back"))
    return kb

def payment_buttons(chat_id):
    """Onlayn kassa ulanmagan bo'lsa — o'sha tugma umuman ko'rsatilmaydi.
    Karta raqami bilan qo'lda o'tkazma endi yo'q."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💵 Naqd / Наличные", callback_data="pay_Naqd"))
    if payme_enabled():
        kb.add(types.InlineKeyboardButton("💳 Payme", callback_data="pay_Payme"))
    if click_enabled():
        kb.add(types.InlineKeyboardButton("🔵 Click", callback_data="pay_Click"))
    kb.add(types.InlineKeyboardButton(back_label(chat_id), callback_data="pay_back"))
    return kb

def back_label(chat_id):
    return "⬅️ Назад" if lang(chat_id) == "ru" else "⬅️ Ortga"

def cancel_label(chat_id):
    return "❌ Отменить заказ" if lang(chat_id) == "ru" else "❌ Buyurtmani bekor qilish"

def name_button(chat_id, user):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(f"👤 {user.first_name or 'Telegram'}")
    kb.add(tr(chat_id, "back"), tr(chat_id, "home"))
    return kb

def phone_button(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📱 Telefon / Телефон", request_contact=True))
    kb.add(tr(chat_id, "back"), tr(chat_id, "home"))
    return kb

def location_button(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(tr(chat_id, "send_location"), request_location=True))
    kb.add(tr(chat_id, "back"), tr(chat_id, "home"))
    return kb

def admin_confirm_buttons(order_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"admin_ok_{order_id}"))
    kb.add(types.InlineKeyboardButton("❌ Rad etish",  callback_data=f"admin_no_{order_id}"))
    return kb

def review_confirm_buttons(review_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"review_ok_{review_id}"))
    kb.add(types.InlineKeyboardButton("❌ Rad etish",  callback_data=f"review_no_{review_id}"))
    return kb

def edit_field_buttons(pid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📝 Nom (uz)",    callback_data=f"edit_name_uz_{pid}"))
    kb.add(types.InlineKeyboardButton("📝 Nom (ru)",    callback_data=f"edit_name_ru_{pid}"))
    kb.add(types.InlineKeyboardButton("💰 Narx",        callback_data=f"edit_price_{pid}"))
    kb.add(types.InlineKeyboardButton("📦 Ombor (soni)", callback_data=f"edit_stock_{pid}"))
    kb.add(types.InlineKeyboardButton("📏 Razmerlar",    callback_data=f"edit_sizes_{pid}"))
    kb.add(types.InlineKeyboardButton("👶 Jins",         callback_data=f"edit_gender_{pid}"))
    kb.add(types.InlineKeyboardButton("💵 Tan narx", callback_data=f"edit_cost_{pid}"))
    kb.add(types.InlineKeyboardButton("📂 Kategoriya",   callback_data=f"edit_category_{pid}"))
    kb.add(types.InlineKeyboardButton("📄 Tavsif (uz)", callback_data=f"edit_desc_uz_{pid}"))
    kb.add(types.InlineKeyboardButton("📄 Tavsif (ru)", callback_data=f"edit_desc_ru_{pid}"))
    kb.add(types.InlineKeyboardButton("🖼 Rasm",         callback_data=f"edit_photo_{pid}"))
    return kb

def tracking_status_buttons(order_id):
    kb = types.InlineKeyboardMarkup()
    statuses = [
        ("✅ Tasdiqlandi",     f"track_confirmed_{order_id}"),
        ("👨‍🍳 Tayyorlanmoqda", f"track_preparing_{order_id}"),
        ("🚚 Yetkazilmoqda",  f"track_delivering_{order_id}"),
        ("✅ Yetkazildi",     f"track_delivered_{order_id}"),
        ("❌ Bekor qilindi",  f"track_cancelled_{order_id}"),
    ]
    for label, data in statuses:
        kb.add(types.InlineKeyboardButton(label, callback_data=data))
    return kb

def promo_skip_button(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(tr(chat_id, "promo_skip_btn"))
    kb.add(tr(chat_id, "home"))
    return kb

# ─── Cart helpers ─────────────────────────────────────────────────────────────

def cart_total(chat_id):
    total = 0
    products = get_products()
    for key, qty in db.get_cart(chat_id).items():
        pid, _ = split_cart_key(key)
        for p in products:
            if str(p["id"]) == str(pid):
                total += int(p["price"]) * qty
                break
    return total

def packaging_price(chat_id):
    return int((db.get_order(chat_id) or {}).get("packaging_price", 0))

def promo_discount(chat_id):
    order = db.get_order(chat_id) or {}
    return int(order.get("promo_discount", 0))

def get_order_delivery(chat_id):
    """Buyurtmaga saqlangan (masofa bo'yicha) delivery. Lokatsiya yo'q bo'lsa 0."""
    o = db.get_order(chat_id) or {}
    if o.get("delivery_summa") is not None:
        return int(o["delivery_summa"])
    return 0

def final_total(chat_id):
    subtotal = cart_total(chat_id) + get_order_delivery(chat_id) + packaging_price(chat_id)
    discount = promo_discount(chat_id)
    return max(0, subtotal - discount)

def cart_text(chat_id, include_packaging=False):
    text = f"{tr(chat_id,'cart_title')}\n\n"
    products = get_products()
    for key, qty in db.get_cart(chat_id).items():
        pid, size = split_cart_key(key)
        for p in products:
            if str(p["id"]) == str(pid):
                name = p["name_ru"] if lang(chat_id) == "ru" else p["name_uz"]
                sz = f" ({size})" if size else ""
                text += f"• {name}{sz} x{qty} — {int(p['price'])*qty:,} so'm\n"
                break
    text += f"\n🛒 {tr(chat_id,'total')}: {cart_total(chat_id):,} so'm"
    # Yetkazib berish — faqat lokatsiyaga qarab
    o = db.get_order(chat_id) or {}
    if o.get("delivery_summa") is not None:
        # Lokatsiya berilgan -> aniq summa
        text += f"\n🚚 {tr(chat_id,'delivery')}: {int(o['delivery_summa']):,} so'm"
        if include_packaging:
            text += f"\n🎁 {tr(chat_id,'packaging_line')}: {o.get('packaging_name','')} — {packaging_price(chat_id):,} so'm"
        disc = promo_discount(chat_id)
        if disc > 0:
            text += f"\n🎁 Chegirma: -{disc:,} so'm"
        text += f"\n💰 {tr(chat_id,'total')}: {final_total(chat_id):,} so'm"
    else:
        # Lokatsiya hali yo'q
        if lang(chat_id) == "ru":
            text += f"\n🚚 Доставка: рассчитается по локации при заказе"
        else:
            text += f"\n🚚 Yetkazib berish: lokatsiya yuborilganda hisoblanadi"
    return text

def order_text(chat_id, order_id=None, number=None):
    o = db.get_order(chat_id) or {}
    num_str = f" #{number:05d}" if number else ""
    text = (
        f"🛒 Yangi buyurtma{num_str} — BabyDiary\n\n"
        f"👤 Ism: {o.get('name')}\n"
        f"📱 Telefon: {o.get('phone')}\n"
        f"💳 To'lov: {o.get('pay_type')}\n"
        f"🎁 Qadoqlash: {o.get('packaging_name')} — {o.get('packaging_price',0):,} so'm\n"
    )
    if o.get("promo_code"):
        text += f"🏷 Promo: {o.get('promo_code')} (-{o.get('promo_discount',0):,} so'm)\n"
    text += (
        f"\n{cart_text(chat_id, include_packaging=True)}\n\n"
        f"📍 Joylashuv:\nhttps://maps.google.com/?q={o.get('lat')},{o.get('lon')}"
    )
    if order_id:
        text += f"\n\n🆔 Order ID: {order_id}"
    return text

# ─── Order helpers ────────────────────────────────────────────────────────────

def notify_low_stock(low_stock, threshold):
    """Ombor kam qolgan tovarlar haqida to'liq adminlarni ogohlantiradi."""
    # Diqqat: Markdown ISHLATILMAYDI — mahsulot nomida `_` yoki `*` bo'lsa
    # Telegram xato beradi va ogohlantirish umuman kelmaydi.
    lines = ["⚠️ Ombor kam qoldi!", ""]
    for name, stock in low_stock:
        if stock <= 0:
            lines.append(f"🔴 {name} — tugadi (0 dona)")
        else:
            lines.append(f"🟡 {name} — {stock} dona qoldi")
    lines.append(f"\nChegara: {threshold} dona. Tovarni to'ldirishni unutmang.")
    text = "\n".join(lines)
    for aid in get_admins():
        try:
            bot.send_message(aid, text)
        except Exception as e:
            log.error(f"low stock xabar {aid}: {e}")


def cart_shortages(chat_id, refresh=False):
    """Savatdagi miqdor ombordan oshganlarini qaytaradi: [(key, nom, savatda, omborda)]."""
    if refresh:
        refresh_products_from_gh(force=True)
    products = get_products()
    problems = []
    for key, qty in db.get_cart(chat_id).items():
        pid, size = split_cart_key(key)
        p = next((x for x in products if str(x["id"]) == str(pid)), None)
        if not p:
            problems.append((key, "—", qty, 0))       # mahsulot o'chirilgan
            continue
        stock = size_stock(p, size)
        if qty > stock:
            name = p["name_ru"] if lang(chat_id) == "ru" else p["name_uz"]
            if size:
                name = f"{name} ({size})"
            problems.append((key, name, qty, stock))
    return problems

def fix_cart_to_stock(chat_id, problems):
    """Savatni ombordagi haqiqiy miqdorga moslaydi va matn qaytaradi."""
    cart = db.get_cart(chat_id)
    if lang(chat_id) == "ru":
        txt = "⚠️ Недостаточно товара на складе:\n\n"
        for key, name, qty, stock in problems:
            txt += f"• {name}: в корзине {qty}, в наличии {stock}\n"
        txt += "\nКорзина обновлена под наличие. Проверьте и попробуйте снова."
    else:
        txt = "⚠️ Omborda yetarli emas:\n\n"
        for key, name, qty, stock in problems:
            txt += f"• {name}: savatda {qty}, omborda {stock}\n"
        txt += "\nSavat ombordagi miqdorga moslandi. Tekshirib, qayta urinib ko'ring."
    for key, name, qty, stock in problems:
        if stock > 0:
            cart[key] = stock
        else:
            cart.pop(key, None)
    db.set_cart(chat_id, cart)
    return txt

def register_pending_cashback(chat_id, order_id):
    """Buyurtma yaratilganda cashback summasini hisoblab, KUTISHGA qo'yadi.
    DIQQAT: bu yerda ombor TEGILMAYDI — tovar to'lov yakunlanganda
    (Payme/Click) yoki admin tasdiqlaganda take_order_stock() bilan yechiladi."""
    if not db.get_cart(chat_id):
        return
    try:
        if db.get_setting("cashback_on") == "1":
            percent = int(db.get_setting("cashback_percent") or "5")
            total   = final_total(chat_id)
            bonus   = int(total * percent / 100)
            if bonus > 0:
                db.set_pending_cashback(order_id, chat_id, bonus)
    except Exception as e:
        log.error(f"pending cashback xato: {e}")

def award_cashback(order_id):
    """Kutilayotgan cashback'ni mijozga beradi va xabar yuboradi."""
    pending = db.get_pending_cashback(order_id)
    if not pending:
        return
    chat_id = pending["chat_id"]
    bonus   = pending["amount"]
    if bonus > 0:
        new_balance = db.add_cashback(chat_id, bonus)
        if lang(chat_id) == "ru":
            msg = (f"🎁 Вам начислено {bonus:,} сум кешбэка!\n"
                   f"💰 Ваш баланс: {new_balance:,} сум\n\n"
                   f"Бонусы накапливаются — используете в будущем.")
        else:
            msg = (f"🎁 Sizga {bonus:,} so'm cashback berildi!\n"
                   f"💰 Balansingiz: {new_balance:,} so'm\n\n"
                   f"Bonuslar yig'ilib boradi — kelajakda ishlatasiz.")
        safe_send(chat_id, msg)
    db.delete_pending_cashback(order_id)

def award_web_cashback(order_id, chat_id):
    """Sayt buyurtmasi tasdiqlanganda cashback beradi (botdagi % bilan, faqat bir marta)."""
    if not chat_id:
        return
    if db.get_setting("cashback_on") != "1":
        return
    try:
        orders = load_json(ORDERS_FILE, [])
        for o in orders:
            if str(o.get("id")) != str(order_id):
                continue
            if o.get("source") != "sayt" or o.get("cashback_awarded"):
                return
            percent = int(db.get_setting("cashback_percent") or "5")
            bonus = int(int(o.get("total", 0)) * percent / 100)
            if bonus > 0:
                new_bal = db.add_cashback(chat_id, bonus)
                o["cashback_awarded"] = True
                save_json(ORDERS_FILE, orders)
                if lang(chat_id) == "ru":
                    safe_send(chat_id, f"🎁 Вам начислено {bonus:,} сум кешбэка!\n💰 Баланс: {new_bal:,} сум")
                else:
                    safe_send(chat_id, f"🎁 Sizga {bonus:,} so'm cashback berildi!\n💰 Balans: {new_bal:,} so'm")
            return
    except Exception as e:
        log.error(f"web cashback xato: {e}")

def send_order_to_admin(chat_id, pay_type, order_id):
    db.update_order(chat_id, "pay_type", pay_type)
    o = db.get_order(chat_id) or {}
    pkg_price = int(o.get("packaging_price", 0) or 0)
    lat, lon = o.get("lat"), o.get("lon")
    number = save_order({
        "id": order_id, "name": o.get("name"), "phone": o.get("phone"),
        "pay_type": pay_type, "packaging": o.get("packaging_name"),
        "packaging_price": pkg_price, "qadoq_cost": order_qadoq_cost(pkg_price),
        "subtotal": cart_total(chat_id),
        "delivery": get_order_delivery(chat_id),
        "promo_code": o.get("promo_code", ""),
        "promo_discount": promo_discount(chat_id),
        "cashback_used": 0,
        "total": final_total(chat_id), "lat": lat, "lon": lon,
        "maps": (f"https://maps.google.com/?q={lat},{lon}" if lat and lon else ""),
        "district": "",
        "items": build_order_items(chat_id),
        "source": "bot", "status": "new",
        "tg_chat_id": chat_id,  # to'lov tasdiqlanganda mijozga xabar uchun
    })
    register_pending_cashback(chat_id, order_id)
    db.add_tracking(order_id, chat_id, o.get("name", ""))
    # Eslatma: admin'ga xabar bu yerda yuborilmaydi —
    # chaqiruvchi (payment) tugma bilan yuboradi (takror bo'lmasligi uchun)
    return number

def order_photos(chat_id):
    """Savatdagi mahsulotlar uchun (file_id, izoh) — admin ko'rishi uchun."""
    products = get_products()
    out = []
    for key, qty in db.get_cart(chat_id).items():
        pid, size = split_cart_key(key)
        p = next((x for x in products if str(x["id"]) == str(pid)), None)
        if not p:
            continue
        photos = p.get("photos") or ([p["photo_id"]] if p.get("photo_id") else [])
        if not photos:
            continue
        sz = f"\n📏 Razmer: {size}" if size else ""
        cap = (f"📌 {p.get('name_uz','')}{sz}\n"
               f"🔢 {qty} ta · {int(p.get('price',0))*int(qty):,} so'm")
        out.append((photos[0], cap))
    return out

def send_admin_order(admin_id, text, kb=None, photos=None):
    """Buyurtmani adminga rasm + IZOH ko'rinishida yuboradi (caption limiti 1024)."""
    photos = photos or []
    if photos and len(text) <= 1024:
        try:
            bot.send_photo(admin_id, photos[0][0], caption=text, reply_markup=kb)
        except Exception as e:
            log.warning(f"send_photo (admin {admin_id}): {e}")
            safe_send(admin_id, text, reply_markup=kb)
    else:
        safe_send(admin_id, text, reply_markup=kb)
        if photos:
            try:
                bot.send_photo(admin_id, photos[0][0], caption=photos[0][1])
            except Exception:
                pass
    for fid, cap in photos[1:]:
        try:
            bot.send_photo(admin_id, fid, caption=cap)
        except Exception:
            pass

# ─── /start ───────────────────────────────────────────────────────────────────

def web_order_by_token(token):
    """orders.json dan saytdagi buyurtmani token (id) bo'yicha topadi."""
    try:
        for o in load_json(ORDERS_FILE, []):
            if str(o.get("id")) == str(token):
                return o
    except Exception as e:
        log.warning(f"web_order_by_token xato: {e}")
    return None

def _norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 9:
        d = "998" + d
    if d[:1] == "8" and len(d) == 10:
        d = "99" + d
    return d

def link_web_orders(phone, chat_id, name=""):
    """Mijozning shu telefondagi BARCHA sayt buyurtmalarini chat_id ga bog'laydi
    (bot va sayt tarixini bir xil qilish uchun)."""
    target = _norm_phone(phone)
    if not target:
        return
    try:
        for o in load_json(ORDERS_FILE, []):
            oid = str(o.get("id", ""))
            if not oid.startswith("w"):       # faqat sayt buyurtmalari
                continue
            if _norm_phone(o.get("phone")) != target:
                continue
            if db.get_tracking(oid):
                db.set_tracking_chat(oid, chat_id, o.get("name") or name)
            else:
                db.add_tracking(oid, chat_id, o.get("name") or name)
    except Exception as e:
        log.warning(f"link_web_orders xato: {e}")

@bot.message_handler(commands=["start"])
def start(message):
    db.register_user(message.chat.id, message.from_user.username, message.from_user.first_name)
    log.info(f"Foydalanuvchi: {message.from_user.id} @{message.from_user.username}")
    name = message.from_user.first_name or ""

    # ── Deep-link: saytdan kelgan buyurtmani mijoz Telegramiga bog'lash ──
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload:
        wo = web_order_by_token(payload)
        if wo:
            try:
                db.set_tracking_chat(payload, message.chat.id, wo.get("name") or name)
                link_web_orders(wo.get("phone"), message.chat.id, wo.get("name") or name)
                db.set_web_user_chat(wo.get("phone"), message.chat.id)  # cashback/wallet bog'lash
            except Exception as e:
                log.warning(f"tracking bog'lashda xato: {e}")
            num = wo.get("number", 0)
            lines = []
            for it in wo.get("items", []):
                lines.append(f"• {it.get('name','')} x{it.get('qty',1)}")
            items_txt = "\n".join(lines)
            total = wo.get("total", 0)
            t = db.get_tracking(payload) or {}
            status = t.get("status", "new")
            if status in ("confirmed", "preparing", "delivering"):
                state_line = "✅ Buyurtmangiz <b>tasdiqlandi</b> va tayyorlanmoqda."
            elif status == "delivered":
                state_line = "✅ Buyurtmangiz <b>yetkazildi</b>. Rahmat!"
            elif status == "cancelled":
                state_line = "❌ Buyurtmangiz bekor qilindi. Operator bilan bog'laning."
            else:
                state_line = "⏳ Buyurtmangiz qabul qilindi va <b>tasdiqlanishi kutilmoqda</b>."
            greet = (
                f"🧸 Assalomu alaykum, {name}!\n\n"
                f"BabyDiary'ni tanlaganingiz uchun chin dildan <b>rahmat</b> 🤍\n\n"
                f"🌐 Saytdan bergan buyurtmangiz qabul qilindi:\n"
                f"🧾 Buyurtma <b>#{num:03d}</b>\n{items_txt}\n"
                f"💰 Jami: <b>{total:,} so'm</b>\n\n"
                f"{state_line}\n\n"
                f"📦 Holat o'zgarganda shu yerda xabar beramiz. "
                f"Buyurtmalaringizni «🚚 Buyurtma holati» bo'limidan kuzatishingiz mumkin."
            )
            try:
                bot.send_message(message.chat.id, greet, parse_mode="HTML")
            except Exception:
                safe_send(message.chat.id, greet.replace("<b>", "").replace("</b>", ""))
            # Til tanlash menyusi ham chiqsin (xaridni davom ettirish uchun)
            safe_send(message.chat.id, "Tilni tanlang / Выберите язык 👇🏻", reply_markup=lang_buttons())
            return

    welcome = (
        f"🧸 *Xush kelibsiz!*\n\n"
        f"BabyDiary — 0–3 yosh kichkintoylar uchun premium kiyimlar olami\n\n"
        f"🤍 Har bir kiyim mehr bilan,\n"
        f"har bir buyurtma e'tibor bilan tayyorlanadi.\n"
        f"Tilni tanlang 👇🏻\n\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🧸 *Добро пожаловать!*\n\n"
        f"BabyDiary — мир премиальной одежды для малышей от 0 до 3 лет\n\n"
        f"🤍 Каждая вещь создана с любовью,\n"
        f"а каждый заказ собирается с особым вниманием.\n"
        f"Выберите язык 👇🏻"
    )
    try:
        bot.send_message(message.chat.id, welcome, reply_markup=lang_buttons(), parse_mode="Markdown")
    except Exception:
        # Markdown xato bersa, oddiy matn
        safe_send(message.chat.id, welcome.replace("*", "").replace("_", ""), reply_markup=lang_buttons())

@bot.message_handler(commands=["id"])
def my_id(message):
    safe_send(message.chat.id, f"Telegram ID: {message.from_user.id}")

@bot.message_handler(commands=["debug"])
@admin_only
def debug_info(message):
    """Volume va saqlash holatini tekshiradi."""
    import os as _os
    info = "🔧 Debug ma'lumot:\n\n"
    # /data mavjudmi?
    data_exists = _os.path.isdir("/data")
    info += "📁 /data papka bor: " + ("✅ HA" if data_exists else "❌ YOQ") + "\n"
    info += f"📂 DATA_DIR: {DATA_DIR}\n\n"
    # Fayllar qayerda va bormi?
    info += f"DB: {db.DB_FILE}\n"
    info += "  mavjud: " + ("✅" if _os.path.exists(db.DB_FILE) else "❌") + "\n"
    info += f"Orders: {ORDERS_FILE}\n"
    info += "  mavjud: " + ("✅" if _os.path.exists(ORDERS_FILE) else "❌") + "\n"
    # Nechta buyurtma bor?
    orders = load_json(ORDERS_FILE, [])
    info += f"\n📦 Saqlangan buyurtmalar: {len(orders)} ta\n"
    # /data ichida nima bor?
    if data_exists:
        try:
            files = _os.listdir("/data")
            info += "\n/data ichida: " + (", ".join(files) if files else "(bosh)")
        except Exception as e:
            info += f"\n/data o'qishda xato: {e}"
    safe_send(message.chat.id, info)

# Maxsus shriftlar yo'li (GitHub'dan keladi, bot.py yonida "fonts/" papkada)
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

def _register_fonts(pdf):
    """Caveat (qo'lyozma) va Oswald (qalin) shriftlarini ulaydi.
    Agar fayllar bo'lmasa, False qaytaradi (Helvetica ishlatiladi)."""
    ok = True
    try:
        caveat = os.path.join(FONTS_DIR, "Caveat-Bold.ttf")
        oswald = os.path.join(FONTS_DIR, "Oswald-Bold.ttf")
        oswald_r = os.path.join(FONTS_DIR, "Oswald-Regular.ttf")
        if os.path.exists(caveat):
            pdf.add_font("Caveat", "", caveat)
        else:
            ok = False
        if os.path.exists(oswald):
            pdf.add_font("Oswald", "B", oswald)
        else:
            ok = False
        if os.path.exists(oswald_r):
            pdf.add_font("Oswald", "", oswald_r)
    except Exception as e:
        log.warning(f"Shrift ulashda xato: {e}")
        ok = False
    return ok

_DAILY_FONTS = {"ready": False, "font": "Helvetica", "fontb": "Helvetica-Bold", "uni": False}

def _daily_setup_fonts():
    """DejaVu (Unicode) shriftini bir marta ulaydi — oʻ, gʻ kabi harflar to'g'ri chiqishi uchun.
    Tizimda yoki repo fonts/ ichida bo'lsa ishlatadi, aks holda Helvetica."""
    if _DAILY_FONTS["ready"]:
        return _DAILY_FONTS["font"], _DAILY_FONTS["fontb"], _DAILY_FONTS["uni"]
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        _DAILY_FONTS["ready"] = True
        return "Helvetica", "Helvetica-Bold", False
    reg = bold = None
    reg_paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 os.path.join(FONTS_DIR, "DejaVuSans.ttf")]
    bold_paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")]
    for p in reg_paths:
        if os.path.exists(p):
            try: pdfmetrics.registerFont(TTFont("BDSans", p)); reg = "BDSans"; break
            except Exception: pass
    for p in bold_paths:
        if os.path.exists(p):
            try: pdfmetrics.registerFont(TTFont("BDSans-Bold", p)); bold = "BDSans-Bold"; break
            except Exception: pass
    if reg and bold:
        _DAILY_FONTS.update(ready=True, font=reg, fontb=bold, uni=True)
    else:
        _DAILY_FONTS.update(ready=True, font="Helvetica", fontb="Helvetica-Bold", uni=False)
    return _DAILY_FONTS["font"], _DAILY_FONTS["fontb"], _DAILY_FONTS["uni"]


def generate_pdf_report(period_label="Kunlik"):
    """Qattiy/rasmiy 1-sahifali PDF hisobot (reportlab, BabyDiary ranglari, Unicode-safe)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                        TableStyle, Flowable)
        from reportlab.platypus.flowables import HRFlowable
    except ImportError:
        log.error("reportlab o'rnatilmagan")
        return None
    try:
        # ── Ma'lumot yig'ish ──
        s  = db.get_stats(ORDERS_FILE)
        cb = db.get_cashback_totals()
        users = db.get_user_count()
        orders = load_json(ORDERS_FILE, [])
        products = get_products()

        product_sales = {}
        for o in orders:
            for it in o.get("items", []):
                nm = it.get("name", "?")
                product_sales[nm] = product_sales.get(nm, 0) + int(it.get("qty", 0))
        top_products = sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:5]

        pay_counts = {}
        for o in orders:
            pt = o.get("pay_type", "?")
            pay_counts[pt] = pay_counts.get(pt, 0) + 1

        bugun = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
        in_stock = []; finished_today = []; ombor_qiymat = 0
        for p in products:
            st = int(p.get("stock", 0))
            if st > 0:
                in_stock.append((p.get("name_uz", "?"), st, int(p.get("cost", 0))))
                ombor_qiymat += int(p.get("cost", 0)) * st
            elif p.get("finished_date") == bugun:
                finished_today.append(p.get("name_uz", "?"))
        avg = int(s["total_sum"] / s["total"]) if s["total"] else 0

        # ── Ranglar / shriftlar ──
        MOCHA=colors.HexColor("#6B4226"); GOLD=colors.HexColor("#B8860B")
        DARK=colors.HexColor("#3A2A1E");  SOFT=colors.HexColor("#8A7864")
        CREAM=colors.HexColor("#FAF6EF"); LINE=colors.HexColor("#E5D9C8")
        RED=colors.HexColor("#B5503F");   GREEN=colors.HexColor("#3E7D5A")
        WHITE=colors.white
        FONT, FONTB, UNI = _daily_setup_fonts()

        def cl(t):
            t = str(t)
            for a,b in (("\u02bb","'"),("\u2018","'"),("\u2019","'"),("\u02bc","'"),
                        ("\u2013","-"),("\u2014","-"),("\u2026","...")):
                t = t.replace(a,b)
            if not UNI:
                t = t.encode("latin-1","replace").decode("latin-1")
            return t

        def smm(n): return f"{int(n):,}".replace(",", " ") + " so'm"

        class KPICards(Flowable):
            def __init__(self, items, width, height=23*mm):
                super().__init__(); self.items=items; self.width=width; self.height=height
            def wrap(self,*a): return (self.width,self.height)
            def draw(self):
                c=self.canv; n=len(self.items); gap=5*mm; cw=(self.width-gap*(n-1))/n
                for i,(label,big,sub) in enumerate(self.items):
                    x=i*(cw+gap)
                    c.setFillColor(WHITE); c.setStrokeColor(LINE); c.setLineWidth(0.8)
                    c.roundRect(x,0,cw,self.height,2*mm,stroke=1,fill=1)
                    c.setFillColor(GOLD); c.rect(x,self.height-1.2*mm,cw,1.2*mm,stroke=0,fill=1)
                    c.setFillColor(SOFT); c.setFont(FONTB,7.5)
                    c.drawCentredString(x+cw/2,self.height-6.5*mm,cl(label))
                    c.setFillColor(MOCHA); c.setFont(FONTB,15)
                    c.drawCentredString(x+cw/2,self.height-14*mm,cl(big))
                    c.setFillColor(SOFT); c.setFont(FONT,7.5)
                    c.drawCentredString(x+cw/2,3.5*mm,cl(sub))

        SEC=ParagraphStyle("Sec",fontName=FONTB,fontSize=9,textColor=MOCHA,leading=11)
        def section(title):
            t=Table([[Paragraph(cl(title.upper()),SEC)]],colWidths=[170*mm])
            t.setStyle(TableStyle([("LINEBELOW",(0,0),(-1,-1),0.8,GOLD),
                ("BOTTOMPADDING",(0,0),(-1,-1),1.5),("TOPPADDING",(0,0),(-1,-1),4),
                ("LEFTPADDING",(0,0),(-1,-1),0)]))
            return t

        def kv(rows, val_color=None):
            data=[]
            for label,value in rows:
                data.append([Paragraph(cl(label),ParagraphStyle("l",fontName=FONT,fontSize=9,textColor=DARK,leading=11)),
                             Paragraph(cl(value),ParagraphStyle("v",fontName=FONTB,fontSize=9,textColor=val_color or MOCHA,alignment=TA_RIGHT,leading=11))])
            t=Table(data,colWidths=[120*mm,50*mm])
            style=[("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("TOPPADDING",(0,0),(-1,-1),2.3),("BOTTOMPADDING",(0,0),(-1,-1),2.3),
                ("LEFTPADDING",(0,0),(0,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
                ("LINEBELOW",(0,0),(-1,-2),0.4,LINE)]
            for i in range(len(data)):
                if i%2==0: style.append(("BACKGROUND",(0,i),(-1,i),CREAM))
            t.setStyle(TableStyle(style)); return t

        today_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y")
        now_str   = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M")

        out_path = os.path.join(DATA_DIR, "hisobot.pdf")
        doc=SimpleDocTemplate(out_path,pagesize=A4,topMargin=15*mm,bottomMargin=13*mm,
            leftMargin=20*mm,rightMargin=20*mm,title=f"BabyDiary {period_label}")
        CW=doc.width; el=[]

        H1=ParagraphStyle("H1",fontName=FONTB,fontSize=21,textColor=MOCHA,leading=23)
        TAG=ParagraphStyle("Tag",fontName=FONT,fontSize=8.5,textColor=SOFT,leading=11)
        RH=ParagraphStyle("RHead",fontName=FONTB,fontSize=12,textColor=DARK,alignment=TA_RIGHT,leading=14)
        RD=ParagraphStyle("RDate",fontName=FONT,fontSize=9,textColor=SOFT,alignment=TA_RIGHT,leading=11)
        head=Table([[
            [Paragraph("BabyDiary",H1),Paragraph(cl("Bolalar kiyimlari · moliya hisoboti"),TAG)],
            [Paragraph(cl(f"{period_label.upper()} HISOBOT"),RH),Paragraph(today_str,RD)],
        ]],colWidths=[CW*0.6,CW*0.4])
        head.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0)]))
        el.append(head); el.append(Spacer(1,3))
        el.append(HRFlowable(width="100%",thickness=1.4,color=GOLD,spaceAfter=8))

        el.append(KPICards([("BUYURTMA",f"{s['total']}","ta jami"),
            ("UMUMIY TUSHUM",smm(s['total_sum']),""),("MIJOZLAR",f"{users}","ta")],CW))
        el.append(Spacer(1,7))

        el.append(section("Tushum"))
        el.append(kv([("Bugungi tushum",smm(s["today_sum"])),("So'nggi 7 kun",smm(s["week_sum"])),
            ("So'nggi 30 kun",smm(s["month_sum"])),("Jami tushum",smm(s["total_sum"])),
            ("O'rtacha chek",smm(avg))]))
        el.append(Spacer(1,4))

        el.append(section("Buyurtmalar"))
        el.append(kv([("Bugun / 7 kun / 30 kun",f"{s['today']} / {s['week']} / {s['month']} ta")]))
        el.append(Spacer(1,4))

        if top_products:
            el.append(section("Eng ko'p sotilgan"))
            el.append(kv([(f"{i+1}.  {nm}",f"{qty} dona") for i,(nm,qty) in enumerate(top_products[:5])]))
            el.append(Spacer(1,4))

        el.append(section("Ombor (joriy ostatka)"))
        if in_stock:
            el.append(kv([(nm,f"{st} dona · {smm(cost*st)}") for nm,st,cost in in_stock[:6]]))
            el.append(kv([("OMBOR QIYMATI (tan narx)",smm(ombor_qiymat))],val_color=GREEN))
        else:
            el.append(kv([("Omborda mahsulot","yo'q")]))
        if finished_today:
            el.append(Spacer(1,2))
            el.append(Paragraph(cl("Bugun tugagan tovar: "+", ".join(finished_today)),
                ParagraphStyle("ft",fontName=FONTB,fontSize=8.5,textColor=RED,leading=11)))
        el.append(Spacer(1,4))

        el.append(section("To'lov turlari va cashback"))
        pr=[]
        if pay_counts:
            pr.append(("To'lov turlari","   ".join(f"{pt}: {cnt}" for pt,cnt in pay_counts.items())))
        pr.append(("Cashback - jami qaytarilgan",smm(cb['total_given'])))
        pr.append(("Cashback - mijozlarda turgan",smm(cb['current_balance'])))
        el.append(kv(pr)); el.append(Spacer(1,8))

        el.append(HRFlowable(width="100%",thickness=0.6,color=LINE,spaceAfter=5))
        el.append(Paragraph(cl("Hurmatli Ibrohim va Rustam - biznesingizga omad!"),
            ParagraphStyle("FB",fontName=FONTB,fontSize=10,textColor=MOCHA,leading=12.5)))
        el.append(Paragraph(cl(f"BabyDiary · avtomatik hisobot tizimi · {now_str}"),
            ParagraphStyle("F",fontName=FONT,fontSize=8,textColor=SOFT,leading=10.5)))

        doc.build(el)
        return out_path
    except Exception as e:
        log.error(f"PDF hisobot xato: {e}")
        return None





def do_export(chat_id, silent=False):
    """Barcha ma'lumot fayllarini chat_id ga yuboradi. silent=True bo'lsa kam xabar."""
    files = [
        (ORDERS_FILE,          "orders.json"),
        (PRODUCTS_FILE,        "products.json"),
        (REVIEWS_FILE,         "reviews.json"),
        (PENDING_REVIEWS_FILE, "pending_reviews.json"),
    ]
    sent = 0
    for path, name in files:
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, "rb") as f:
                    bot.send_document(chat_id, f, visible_file_name=name)
                    sent += 1
        except Exception as e:
            log.error(f"export {name} xato: {e}")
    try:
        if os.path.exists(db.DB_FILE) and os.path.getsize(db.DB_FILE) > 0:
            with open(db.DB_FILE, "rb") as f:
                bot.send_document(chat_id, f, visible_file_name="babydiary.db")
                sent += 1
    except Exception as e:
        log.error(f"export db xato: {e}")
    return sent

@bot.message_handler(commands=["export"])
@full_admin_only
def export_data(message):
    """Admin barcha ma'lumotni Telegram orqali yuklab oladi (backup/tahlil uchun)."""
    chat_id = message.chat.id
    safe_send(chat_id, "📤 Ma'lumotlar tayyorlanmoqda...")
    sent = do_export(chat_id)
    if sent:
        safe_send(chat_id, f"✅ {sent} ta fayl yuborildi. Saqlab oling (backup / Obsidian tahlili uchun).")
    else:
        safe_send(chat_id, "⚠️ Hozircha ma'lumot yo'q (bot endi ishlatilyapti).")

@bot.message_handler(commands=["report"])
@full_admin_only
def report_cmd(message):
    _send_report(message.chat.id)

@bot.message_handler(commands=["reset_products"])
@full_admin_only
def reset_products_cmd(message):
    """Barcha mahsulotlarni o'chiradi (nol'dan boshlash uchun)."""
    save_products([])
    log.info("Mahsulotlar tozalandi (reset)")
    safe_send(message.chat.id,
              "🗑 Barcha mahsulotlar o'chirildi.\n\nEndi katalog bo'sh. Yangi mahsulot qo'shing.",
              reply_markup=admin_menu())

@bot.message_handler(commands=["reset_stats"])
@full_admin_only
def reset_stats_cmd(message):
    """Statistikani + buyurtmalar tarixini nol'dan boshlaydi."""
    n = _wipe_orders_history()
    log.info("Statistika tozalandi (reset)")
    safe_send(message.chat.id,
              f"🗑 Statistika tozalandi ({n} ta buyurtma).\n"
              f"Buyurtmalar tarixi ham o'chdi, raqam #00001 dan boshlanadi.",
              reply_markup=admin_menu())


def _wipe_orders_history():
    """orders.json + SQLite'dagi buyurtma tarixi + raqam hisoblagichi.
    Buyurtmalar soni (o'chirilgan) qaytariladi."""
    before = len(load_json(ORDERS_FILE, []))
    save_json(ORDERS_FILE, [])
    _db_clear(_tables_matching("tracking", "pending_order", "pending_cashback"))
    _reset_order_counter()
    db.set_setting("cashback_total_given", "0")
    return before

@bot.message_handler(commands=["restart"])
@full_admin_only
def restart_cmd(message):
    """Botni qayta ishga tushiradi (Railway avtomatik tiklaydi)."""
    safe_send(message.chat.id, "🔄 Bot qayta ishga tushyapti... (bir necha soniya)")
    log.info("Admin /restart buyrug'i")
    import threading, time as _t
    def _restart():
        _t.sleep(2)
        os._exit(0)
    threading.Thread(target=_restart, daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "📄 Hisobot (PDF)")
@full_admin_only
def report_button(message):
    _send_report(message.chat.id)

# ─── Kategoriyalar boshqaruvi ─────────────────────────────────────────────────
def category_manage_kb():
    kb = types.InlineKeyboardMarkup()
    cats = get_category_list()
    for c in cats:
        kb.add(
            types.InlineKeyboardButton(f"✏️ {c}", callback_data=f"catedit_{c}"),
            types.InlineKeyboardButton("🗑", callback_data=f"catdel_{c}"),
        )
    kb.add(types.InlineKeyboardButton("➕ Yangi kategoriya qo'shish", callback_data="catadd"))
    return kb

@bot.message_handler(func=lambda m: m.text == "📂 Kategoriyalar")
@admin_only
def categories_menu(message):
    show_categories(message.chat.id)

def show_categories(chat_id):
    cats = get_category_list()
    ru = get_category_ru()
    if not cats:
        txt = "📂 Kategoriya yo'q.\n\n➕ tugmasini bosib yangi qo'shing:"
    else:
        txt = "📂 Kategoriyalar ro'yxati:\n\n"
        for c in cats:
            r = ru.get(c)
            txt += f"• {c}" + (f"  🇷🇺 {r}\n" if r else "  ⚠️ ruscha nomi yo'q\n")
        txt += "\n✏️ — tahrirlash (nom uz/ru, rasm),  🗑 — o'chirish.  Yoki yangi qo'shing:"
    safe_send(chat_id, txt, reply_markup=category_manage_kb())

CMD_TXT = ("⚙️ Buyruqlar\n\n"
           "Tugmalar orqali boshqaring. Tozalash buyruqlari (🗑) ehtiyotkorlik bilan — "
           "ma'lumot qaytmaydi!")

def commands_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Botni qayta ishga tushirish", callback_data="cmd_restart"))
    kb.add(types.InlineKeyboardButton("📤 Ma'lumot eksport (backup)", callback_data="cmd_export"))
    kb.add(types.InlineKeyboardButton("📄 Hisobot (PDF)", callback_data="cmd_report"))
    kb.add(types.InlineKeyboardButton("🗑 Mahsulotlarni tozalash", callback_data="cmd_reset_products"))
    kb.add(types.InlineKeyboardButton("🗑 Statistika + buyurtmalar tarixi", callback_data="cmd_reset_stats"))
    kb.add(types.InlineKeyboardButton("🧹 TO'LIQ TOZALASH (hammasi)", callback_data="cmd_reset_all"))
    kb.add(types.InlineKeyboardButton("🔍 GitHub tekshiruvi", callback_data="cmd_ghcheck"))
    kb.add(types.InlineKeyboardButton("🆔 Mening ID raqamim", callback_data="cmd_id"))
    kb.add(types.InlineKeyboardButton("⬅️ Ortga", callback_data="cmd_close"))
    return kb

def back_to_cmds_kb():
    """Natija ostidagi '⬅️ Ortga' — buyruqlar menyusini qayta ochadi."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Ortga", callback_data="cmd_menu"))
    return kb

@bot.message_handler(func=lambda m: m.text == "⚙️ Buyruqlar")
@full_admin_only
def commands_menu(message):
    safe_send(message.chat.id, CMD_TXT, reply_markup=commands_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("cmd_"))
def commands_callback(call):
    if not is_full_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    action = call.data.replace("cmd_", "")

    if action == "menu":                 # ⬅️ Ortga — menyuni qayta ochamiz
        kill_kb(call)
        bot.answer_callback_query(call.id)
        safe_send(chat_id, CMD_TXT, reply_markup=commands_kb())
        return
    if action == "close":                # menyudagi ⬅️ Ortga — yopamiz
        kill_kb(call)
        bot.answer_callback_query(call.id)
        safe_send(chat_id, "Asosiy menu:", reply_markup=admin_menu())
        return

    kill_kb(call)                        # buyruq bosildi -> menyu chatdan yo'qoladi

    if action == "restart":
        bot.answer_callback_query(call.id, "🔄 Qayta ishga tushyapti...")
        safe_send(chat_id, "🔄 Bot qayta ishga tushyapti... (bir necha soniya)")
        log.info("Admin /restart bosdi")
        # Railway jarayonni qayta ishga tushiradi
        import threading, time as _t
        def _restart():
            _t.sleep(2)
            os._exit(0)  # Railway avtomatik qayta ishga tushiradi
        threading.Thread(target=_restart, daemon=True).start()
    elif action == "export":
        bot.answer_callback_query(call.id, "📤 Tayyorlanmoqda...")
        sent = do_export(chat_id)
        if sent:
            safe_send(chat_id, f"✅ {sent} ta fayl yuborildi.", reply_markup=admin_menu())
        else:
            safe_send(chat_id, "⚠️ Ma'lumot yo'q.", reply_markup=admin_menu())
    elif action == "report":
        bot.answer_callback_query(call.id, "📄 Tayyorlanmoqda...")
        _send_report(chat_id)
    elif action == "reset_products":
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Ha, tozalash", callback_data="confirm_reset_products"))
        kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="cmd_cancel"))
        safe_send(chat_id, "⚠️ BARCHA mahsulotlar o'chiriladi. Ishonchingiz komilmi?", reply_markup=kb)
    elif action == "reset_stats":
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Ha, tozalash", callback_data="confirm_reset_stats"))
        kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="cmd_cancel"))
        safe_send(chat_id,
                  "⚠️ Quyidagilar o'chiriladi:\n"
                  "• Barcha buyurtmalar (orders.json + GitHub)\n"
                  "• Bazadagi buyurtma tarixi va holatlari\n"
                  "• Buyurtma raqami hisoblagichi (#00001 dan)\n\n"
                  "Mahsulotlar, mijozlar va cashback balanslari QOLADI.\n\n"
                  "Ishonchingiz komilmi?", reply_markup=kb)
    elif action == "reset_all":
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Ha, HAMMASINI tozalash", callback_data="confirm_reset_all"))
        kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="cmd_cancel"))
        safe_send(chat_id,
                  "🧨 ZAVOD HOLATIGA QAYTARISH\n\n"
                  "Butunlay o'chadi (bot + sayt + GitHub + SQLite):\n"
                  "• Barcha mahsulotlar\n"
                  "• Barcha buyurtmalar, tarix va holatlar\n"
                  "• Barcha sharhlar\n"
                  "• Barcha kategoriyalar va rasmlari\n"
                  "• Mijozlar, savatlar, sessiyalar\n"
                  "• Cashback balanslari (hammasi 0)\n"
                  "• Buyurtma raqami — #00001 dan\n\n"
                  "Saqlanadi: sozlamalar (delivery, operator, ish vaqti) va promo kodlar.\n\n"
                  "⚠️ Bu amal QAYTMAYDI. Avval '📤 Ma'lumot eksport' bilan zaxira oling!\n\n"
                  "Ishonchingiz komilmi?", reply_markup=kb)
    elif action == "ghcheck":
        bot.answer_callback_query(call.id, "🔍 Tekshirilmoqda...")
        safe_send(chat_id, gh_diagnose(), reply_markup=back_to_cmds_kb())
    elif action == "id":
        bot.answer_callback_query(call.id)
        safe_send(chat_id, f"🆔 Sizning ID: {call.from_user.id}", reply_markup=back_to_cmds_kb())
    elif action == "cancel":
        bot.answer_callback_query(call.id, "Bekor qilindi")
        safe_send(chat_id, "❌ Bekor qilindi.", reply_markup=back_to_cmds_kb())

@bot.callback_query_handler(func=lambda c: c.data == "confirm_reset_products")
def confirm_reset_products_cb(call):
    if not is_full_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Faqat to'liq admin", show_alert=True)
        return
    chat_id = call.message.chat.id
    kill_kb(call)
    bot.answer_callback_query(call.id, "⏳ Tozalanmoqda...")
    before = len(get_products())

    # 1) Lokal faylni bo'shatamiz va TEKSHIRAMIZ
    if not write_json_local(PRODUCTS_FILE, []) or len(get_products()) != 0:
        safe_send(chat_id,
                  "❌ Tozalanmadi: faylga yozib bo'lmadi.\n"
                  f"Mahsulotlar joyida ({before} ta). Railway loglarini tekshiring.",
                  reply_markup=admin_menu())
        return

    # 2) GitHub'ga sinxron push (bu bo'lmasa qayta ishga tushganda tovarlar QAYTADI)
    pushed, why = gh_push_json_sync(PRODUCTS_FILE, "data/products.json", "products reset")
    ok = pushed and gh_matches("data/products.json", [])
    log.info(f"Mahsulotlar tozalandi: {before} ta, github={ok} ({why})")

    if ok:
        safe_send(chat_id,
                  f"🗑 Barcha mahsulotlar o'chirildi ({before} ta).\n"
                  f"✅ GitHub zaxirasi ham tozalandi.\n"
                  f"🌐 Saytdan ~20 soniyada yo'qoladi.",
                  reply_markup=admin_menu())
    else:
        safe_send(chat_id,
                  f"⚠️ Botda o'chirildi ({before} ta), lekin GitHub zaxirasi tozalanmadi.\n\n"
                  f"Sabab: {why}\n\n"
                  f"Bot qayta ishga tushsa mahsulotlar QAYTIB KELADI.",
                  reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "confirm_reset_all")
def confirm_reset_all_cb(call):
    if not is_full_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Faqat to'liq admin", show_alert=True)
        return
    chat_id = call.message.chat.id
    kill_kb(call)
    bot.answer_callback_query(call.id, "⏳ Tozalanmoqda...")

    before_p = len(get_products())
    before_o = len(load_json(ORDERS_FILE, []))
    before_r = len(load_json(REVIEWS_FILE, []))

    # (lokal fayl, GitHub yo'li yoki None, nomi)
    targets = [
        (PRODUCTS_FILE,        "data/products.json", "Mahsulotlar"),
        (ORDERS_FILE,          "data/orders.json",   "Buyurtmalar"),
        (REVIEWS_FILE,         "data/reviews.json",  "Sharhlar"),
        (PENDING_REVIEWS_FILE, None,                 "Kutilayotgan sharhlar"),
    ]
    lines, all_ok = [], True
    for local_path, gh_path, nom in targets:
        if not write_json_local(local_path, []):
            lines.append(f"❌ {nom}: faylga yozib bo'lmadi")
            all_ok = False
            continue
        if not gh_path:
            lines.append(f"✅ {nom}: tozalandi")
            continue
        pushed, why = gh_push_json_sync(local_path, gh_path, f"reset {os.path.basename(local_path)}")
        if pushed and gh_matches(gh_path, []):
            lines.append(f"✅ {nom}: tozalandi (GitHub ham)")
        else:
            lines.append(f"⚠️ {nom}: botda tozalandi, GitHub QOLDI — {why}")
            all_ok = False

    # Kategoriyalar va rasmlari (SQLite settings)
    try:
        save_category_list([])
        db.set_setting("category_images", json.dumps({}, ensure_ascii=False))
        db.set_setting("category_ru", json.dumps({}, ensure_ascii=False))
        db.set_setting("cashback_total_given", "0")
        lines.append("✅ Kategoriyalar (uz/ru), rasmlar va cashback hisobi: tozalandi")
    except Exception as e:
        log.error(f"reset_all kategoriya: {e}")
        lines.append("❌ Kategoriyalar: tozalanmadi")
        all_ok = False

    # SQLite: KONFIGURATSIYADAN tashqari hamma jadval nolga
    # (settings va promo_codes saqlanadi — do'kon ishlashda davom etsin)
    _drop_card_settings()
    wiped = _db_clear(sorted(_db_tables() - _DB_KEEP_TABLES - set(_COUNTER_TABLES)))
    counter = _reset_order_counter()
    if wiped:
        total_rows = sum(n for _, n in wiped)
        lines.append(f"✅ Baza: {len(wiped)} ta jadval, {total_rows} ta yozuv o'chirildi")
        lines.append("   " + ", ".join(f"{t}({n})" for t, n in wiped))
    else:
        lines.append("⚠️ Baza: tozalanadigan jadval topilmadi")
    lines.append("✅ Buyurtma raqami: " + ", ".join(counter))

    log.info(f"ZAVOD HOLATI: {before_p} mahsulot, {before_o} buyurtma, {before_r} sharh, "
             f"db={len(wiped)} jadval, ok={all_ok}")

    head = "🧨 ZAVOD HOLATIGA QAYTARILDI\n\n" if all_ok else "🧨 ZAVOD HOLATI — qisman\n\n"
    tail = ("\n\n🌐 Saytdan ~20 soniyada yo'qoladi.\nKeyingi buyurtma — #00001"
            if all_ok else
            "\n\n⚠️ GitHub zaxirasi tozalanmagan fayllar bor — bot qayta ishga tushsa ular QAYTIB KELADI.\n"
            "GITHUB_TOKEN va uning 'contents: write' huquqini tekshiring, so'ng qayta bosing.")
    safe_send(chat_id,
              head + f"O'chirildi: {before_p} mahsulot · {before_o} buyurtma · {before_r} sharh\n\n"
              + "\n".join(lines) + tail,
              reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "confirm_reset_stats")
def confirm_reset_stats_cb(call):
    if not is_full_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Faqat to'liq admin", show_alert=True)
        return
    chat_id = call.message.chat.id
    kill_kb(call)
    bot.answer_callback_query(call.id, "⏳ Tozalanmoqda...")
    before = len(load_json(ORDERS_FILE, []))

    # 1) orders.json
    if not write_json_local(ORDERS_FILE, []) or len(load_json(ORDERS_FILE, [])) != 0:
        safe_send(chat_id, "❌ Tozalanmadi: faylga yozib bo'lmadi.", reply_markup=admin_menu())
        return

    # 2) SQLite: buyurtma tarixi (order_tracking / pending_orders / pending_cashback)
    cleared = _db_clear(_tables_matching("tracking", "pending_order", "pending_cashback"))
    # 3) Raqam hisoblagichi -> keyingi buyurtma #00001
    counter = _reset_order_counter()
    db.set_setting("cashback_total_given", "0")

    # 4) GitHub zaxirasi
    pushed, why = gh_push_json_sync(ORDERS_FILE, "data/orders.json", "orders reset")
    ok = pushed and gh_matches("data/orders.json", [])
    log.info(f"Statistika tozalandi: {before} ta buyurtma, db={cleared}, github={ok} ({why})")

    db_lines = "\n".join(f"  • {t}: {n} ta" for t, n in cleared) or "  • (tarix jadvali topilmadi)"
    msg = (f"🗑 Statistika tozalandi\n\n"
           f"📦 Buyurtmalar: {before} ta o'chirildi\n"
           f"🗂 Bazadan:\n{db_lines}\n"
           f"🔢 Hisoblagich: {', '.join(counter)}\n"
           f"💰 Berilgan cashback hisobi: 0\n\n")
    msg += ("✅ GitHub zaxirasi ham tozalandi.\n\nKeyingi buyurtma — #00001"
            if ok else f"⚠️ GitHub zaxirasi tozalanmadi: {why}\nBot qayta ishga tushsa buyurtmalar QAYTADI.")
    safe_send(chat_id, msg, reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "catadd")
def category_add_cb(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    db.set_admin_step(call.message.chat.id, {"step": "category_add"})
    safe_send(call.message.chat.id, "➕ Yangi kategoriya nomini yozing:",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "catphoto_skip")
def cat_photo_skip_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id)
    if not d or d.get("step") != "category_add_photo":
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "✅")
    kill_kb(call)          # tugma yo'qoladi
    finalize_category(chat_id, d.get("cat_name", ""), "", d.get("cat_ru", ""))


@bot.callback_query_handler(func=lambda c: c.data == "catru_skip")
def cat_ru_skip_cb(call):
    """Ruscha nomsiz davom etish."""
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id) or {}
    if d.get("step") != "category_add_ru":
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "✅")
    kill_kb(call)
    d["cat_ru"] = ""
    d["step"] = "category_add_photo"
    db.set_admin_step(chat_id, d)
    prompt_category_photo(chat_id, d.get("cat_name", ""))


@bot.callback_query_handler(func=lambda c: c.data == "catadd_back")
def cat_add_back_cb(call):
    """Ruscha nom bosqichidan nom bosqichiga qaytish."""
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    kill_kb(call)
    db.set_admin_step(chat_id, {"step": "category_add"})
    safe_send(chat_id, "➕ Yangi kategoriya nomini yozing (o'zbekcha):",
              reply_markup=step_back_menu(chat_id))


@bot.callback_query_handler(func=lambda c: c.data == "catphoto_back")
def cat_photo_back_cb(call):
    """Rasm bosqichidan ruscha nom bosqichiga qaytish."""
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id) or {}
    bot.answer_callback_query(call.id)
    kill_kb(call)
    d["step"] = "category_add_ru"
    db.set_admin_step(chat_id, d)
    prompt_category_ru(chat_id, d.get("cat_name", ""))

@bot.callback_query_handler(func=lambda c: c.data.startswith("catedit_"))
def category_edit_cb(call):
    if not is_admin(call.from_user.id):
        return
    cat = call.data.replace("catedit_", "")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📝 Nomi (uz)", callback_data=f"catren_{cat}"))
    kb.add(types.InlineKeyboardButton("🇷🇺 Nomi (ru)", callback_data=f"catrenru_{cat}"))
    kb.add(types.InlineKeyboardButton("🖼 Rasmini o'zgartirish", callback_data=f"catimg_{cat}"))
    kb.add(types.InlineKeyboardButton("⬅️ Ortga", callback_data="catback"))
    try:
        bot.edit_message_text(f"✏️ '{cat}' — nimani o'zgartiramiz?",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id, f"✏️ '{cat}' — nimani o'zgartiramiz?", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "catback")
def category_back_cb(call):
    if not is_admin(call.from_user.id):
        return
    cats = get_category_list()
    txt = "📂 Kategoriyalar ro'yxati:\n\n" + "\n".join(f"• {c}" for c in cats)
    txt += "\n\n✏️ — tahrirlash (nom/rasm),  🗑 — o'chirish.  Yoki yangi qo'shing:"
    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=category_manage_kb())
    except Exception:
        safe_send(call.message.chat.id, txt, reply_markup=category_manage_kb())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("catrenru_"))
def category_rename_ru_cb(call):
    if not is_admin(call.from_user.id):
        return
    cat = call.data.replace("catrenru_", "")
    kill_kb(call)
    cur = get_category_ru().get(cat, "")
    db.set_admin_step(call.message.chat.id, {"step": "cat_rename_ru", "cat_name": cat})
    safe_send(call.message.chat.id,
              f"🇷🇺 '{cat}' — ruscha nomini yozing.\n"
              f"Hozirgi: {cur or 'yo\'q'}\n\n"
              f"(Bo'sh xabar — bitta '-' belgisi yuborsangiz, ruscha nom olib tashlanadi.)",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("catren_"))
def category_rename_cb(call):
    if not is_admin(call.from_user.id):
        return
    cat = call.data.replace("catren_", "")
    kill_kb(call)
    db.set_admin_step(call.message.chat.id, {"step": "cat_rename", "cat_old": cat})
    safe_send(call.message.chat.id, f"📝 '{cat}' — yangi nomni yozing:",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("catimg_"))
def category_reimage_cb(call):
    if not is_admin(call.from_user.id):
        return
    cat = call.data.replace("catimg_", "")
    kill_kb(call)
    db.set_admin_step(call.message.chat.id, {"step": "cat_reimage", "cat_name": cat})
    safe_send(call.message.chat.id, f"🖼 '{cat}' uchun yangi rasm yuboring:",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("catdel_"))
def category_del_cb(call):
    if not is_admin(call.from_user.id):
        return
    cat = call.data.replace("catdel_", "")
    set_category_ru(cat, "")            # ruscha nomi ham o'chsin
    cats = get_category_list()
    if cat in cats:
        cats.remove(cat)
        save_category_list(cats)
        set_category_image(cat, "")
        log.info(f"Kategoriya o'chirildi: {cat}")
    bot.answer_callback_query(call.id, f"🗑 {cat} o'chirildi")
    # Ro'yxatni yangilaymiz
    if cats:
        txt = "📂 Kategoriyalar ro'yxati:\n\n" + "\n".join(f"• {c}" for c in cats)
        txt += "\n\nO'chirish uchun 🗑 bosing, yoki yangi qo'shing:"
        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                  reply_markup=category_manage_kb())
        except Exception:
            safe_send(call.message.chat.id, txt, reply_markup=category_manage_kb())
    else:
        try:
            bot.edit_message_text("📂 Kategoriya qolmadi. Yangi qo'shing:",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=category_manage_kb())
        except Exception:
            pass

def _send_report(chat_id):
    """Admin xohlagan vaqt chiroyli PDF hisobot oladi."""
    safe_send(chat_id, "📊 Hisobot tayyorlanmoqda...")
    pdf_path = generate_pdf_report("Joriy")
    if pdf_path and os.path.exists(pdf_path):
        try:
            today_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y")
            with open(pdf_path, "rb") as f:
                bot.send_document(chat_id, f,
                    visible_file_name=f"BabyDiary_hisobot_{today_str}.pdf",
                    caption=f"📊 Hisobot — {today_str}")
        except Exception as e:
            log.error(f"report yuborish xato: {e}")
            safe_send(chat_id, "⚠️ Hisobot yuborishda xato.")
    else:
        safe_send(chat_id, "⚠️ Hisobot tayyorlab bo'lmadi (fpdf2 kerak yoki ma'lumot yo'q).")

# ─── Home ─────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ["🏠 Asosiy menu", "🏠 Главное меню"])
def home(message):
    db.delete_order(message.chat.id)
    db.delete_admin_step(message.chat.id)
    safe_send(message.chat.id, tr(message.chat.id, "main"),
              reply_markup=main_menu(message.chat.id, message.from_user.id))

# ─── Language ─────────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def set_lang(call):
    chosen = call.data.split("_")[1]
    db.set_lang(call.message.chat.id, chosen)
    text = "Добро пожаловать в BabyDiary 👶" if chosen == "ru" else "BabyDiary botiga xush kelibsiz 👶"
    safe_send(call.message.chat.id, text,
              reply_markup=main_menu(call.message.chat.id, call.from_user.id))
    kill_kb(call)

@bot.message_handler(func=lambda m: m.text in ["🌐 Til", "🌐 Язык"])
def change_lang(message):
    safe_send(message.chat.id, "Til / Язык:", reply_markup=lang_buttons())

# ─── Catalog & Search ─────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ["🛍 Katalog", "🛍 Каталог"])
def catalog(message):
    cid = message.chat.id
    db.set_catalog_page(cid, 0)
    # Agar bir nechta kategoriya bo'lsa — avval kategoriya tanlatamiz
    cat_kb = category_keyboard(cid)
    if cat_kb:
        txt = "📂 Kategoriyani tanlang:" if lang(cid) == "uz" else "📂 Выберите категорию:"
        safe_send(cid, txt, reply_markup=cat_kb)
    else:
        safe_send(cid, tr(cid, "choose_product"),
                  reply_markup=catalog_keyboard(cid, 0))

@bot.message_handler(func=lambda m: m.text and m.text.startswith("📂 ") or m.text in ["📋 Hammasi", "📋 Все"])
def catalog_by_category(message):
    cid = message.chat.id
    if message.text in ["📋 Hammasi", "📋 Все"]:
        category = None
    else:
        # Tugmada ruscha nom bo'lishi mumkin — kanonik (uzcha) nomga o'giramiz
        category = cat_from_label(message.text)
    db.set_catalog_page(cid, 0)
    db.set_catalog_category(cid, category or "all")
    safe_send(cid, tr(cid, "choose_product"),
              reply_markup=catalog_keyboard(cid, 0, category))

@bot.message_handler(func=lambda m: m.text in [
    "➡️ Keyingi sahifa", "⬅️ Oldingi sahifa",
    "➡️ Следующая страница", "⬅️ Предыдущая страница"
])
def catalog_page_nav(message):
    cid = message.chat.id
    page = db.get_catalog_page(cid)
    if message.text in ["➡️ Keyingi sahifa", "➡️ Следующая страница"]:
        page += 1
    else:
        page = max(0, page - 1)
    db.set_catalog_page(cid, page)
    category = db.get_catalog_category(cid)
    safe_send(cid, tr(cid, "choose_product"),
              reply_markup=catalog_keyboard(cid, page, category))

@bot.message_handler(func=lambda m: m.text == "🔍 Qidirish")
def search_start(message):
    db.set_order(message.chat.id, {"step": "search"})
    safe_send(message.chat.id, tr(message.chat.id, "search_ask"), reply_markup=back_menu(message.chat.id))

@bot.message_handler(func=lambda m: m.text and m.text.startswith("🛍 "))
def open_product(message):
    product_name = message.text.replace("🛍 ", "").strip()
    product = next((p for p in get_products()
                    if (p["name_ru"] if lang(message.chat.id) == "ru" else p["name_uz"]) == product_name), None)
    if not product:
        safe_send(message.chat.id, tr(message.chat.id, "product_not_found"))
        return
    send_product_card(message.chat.id, product)

def send_product_card(chat_id, product):
    name    = product["name_ru"] if lang(chat_id) == "ru" else product["name_uz"]
    desc    = product["desc_ru"] if lang(chat_id) == "ru" else product["desc_uz"]
    qty     = db.get_cart(chat_id).get(str(product["id"]), 0)
    caption = f"🛍 {name}\n\n{desc}\n\n💰 {int(product['price']):,} so'm"
    # Rasmlarni yig'amiz: yangi "photos" ro'yxati yoki eski "photo_id"
    photos = product.get("photos") or ([product["photo_id"]] if product.get("photo_id") else [])
    photos = [p for p in photos if p]  # bo'shlarni olib tashlaymiz

    if len(photos) > 1:
        # Galereya: bir nechta rasmni media group bilan ko'rsatamiz
        try:
            media = [types.InputMediaPhoto(photos[0], caption=caption)]
            for ph in photos[1:10]:  # Telegram limiti 10 ta
                media.append(types.InputMediaPhoto(ph))
            bot.send_media_group(chat_id, media)
        except Exception as e:
            log.error(f"media_group xato: {e}")
            safe_photo(chat_id, photos[0], caption=caption)
        # Tugmalarni alohida xabar bilan (media group tugma qo'llab-quvvatlamaydi)
        safe_send(chat_id, f"🛍 {name} — {int(product['price']):,} so'm",
                  reply_markup=product_buttons(chat_id, product["id"]))
    elif len(photos) == 1:
        safe_photo(chat_id, photos[0], caption=caption,
                   reply_markup=product_buttons(chat_id, product["id"]))
    else:
        safe_send(chat_id, caption[:4096],
                  reply_markup=product_buttons(chat_id, product["id"]))

# ─── Cart ─────────────────────────────────────────────────────────────────────

def _resolve_size_from_call(call):
    """plus_/minus_/selsz_ callback -> (pid, sel_size, size_stock, cart_key)."""
    parts = call.data.split("_")          # uuid da '_' yo'q, xavfsiz
    pid = parts[1]
    product = next((p for p in get_products() if str(p["id"]) == str(pid)), None)
    sizes = (product.get("sizes") if product else None) or []
    if len(parts) > 2 and sizes:
        i = int(parts[2])
        if 0 <= i < len(sizes):
            sel = sizes[i].get("label")
            return pid, product, sel, int(sizes[i].get("stock", 0)), make_cart_key(pid, sel)
    st = int(product.get("stock", 0)) if product else 0
    return pid, product, None, st, str(pid)

@bot.callback_query_handler(func=lambda c: c.data.startswith("plus_"))
def plus(call):
    chat_id = call.message.chat.id
    pid, product, sel, stock, key = _resolve_size_from_call(call)
    if not product:
        bot.answer_callback_query(call.id); return
    cart = db.get_cart(chat_id)
    current = cart.get(key, 0)
    if current >= stock:
        msg = (f"⚠️ На складе всего {stock} шт" if lang(chat_id) == "ru"
               else f"⚠️ Omborda faqat {stock} ta bor")
        bot.answer_callback_query(call.id, msg, show_alert=True)
        return
    cart[key] = current + 1
    db.set_cart(chat_id, cart)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id,
                                      reply_markup=product_buttons(chat_id, pid, sel_size=sel))
    except Exception as e:
        log.warning(f"edit_markup: {e}")
    bot.answer_callback_query(call.id, "✅")

@bot.callback_query_handler(func=lambda c: c.data.startswith("minus_"))
def minus(call):
    chat_id = call.message.chat.id
    pid, product, sel, stock, key = _resolve_size_from_call(call)
    cart = db.get_cart(chat_id)
    if cart.get(key, 0) > 1:
        cart[key] -= 1
    elif key in cart:
        del cart[key]
    db.set_cart(chat_id, cart)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id,
                                      reply_markup=product_buttons(chat_id, pid, sel_size=sel))
    except Exception as e:
        log.warning(f"edit_markup: {e}")
    bot.answer_callback_query(call.id, "✅")

@bot.callback_query_handler(func=lambda c: c.data.startswith("selsz_"))
def select_size_cb(call):
    chat_id = call.message.chat.id
    pid, product, sel, stock, key = _resolve_size_from_call(call)
    if not product or sel is None:
        bot.answer_callback_query(call.id); return
    if stock <= 0:
        msg = "Этого размера сейчас нет" if lang(chat_id) == "ru" else "Bu razmer hozir yo'q"
        bot.answer_callback_query(call.id, msg, show_alert=True); return
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id,
                                      reply_markup=product_buttons(chat_id, pid, sel_size=sel))
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"✅ {sel}")

@bot.callback_query_handler(func=lambda c: c.data == "none")
def none_cb(call):
    # Miqdor ko'rsatkichi (➖ 3 ➕) — bosilganda hech narsa qilmaydi
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "open_cart")
def open_cart_cb(call):
    show_cart(call.message.chat.id)

@bot.message_handler(func=lambda m: m.text in ["🛒 Savat", "🛒 Корзина"])
def cart_handler(message):
    show_cart(message.chat.id)

def show_cart(chat_id):
    if not db.get_cart(chat_id):
        safe_send(chat_id, tr(chat_id, "cart_empty"))
        return
    safe_send(chat_id, cart_text(chat_id), reply_markup=cart_buttons(chat_id))

@bot.callback_query_handler(func=lambda c: c.data == "clear_cart")
def clear_cart_cb(call):
    db.clear_cart(call.message.chat.id)
    kill_kb(call)
    safe_send(call.message.chat.id, tr(call.message.chat.id, "cart_cleared"))

@bot.callback_query_handler(func=lambda c: c.data == "confirm_order")
def confirm_order(call):
    cid = call.message.chat.id
    # Ish vaqti faqat BUYURTMA berishda tekshiriladi
    if not db.is_working_hours():
        wh = db.get_work_hours()
        if lang(cid) == "ru":
            msg = (f"🕐 Извините, сейчас нерабочее время.\n\n"
                   f"Мы принимаем заказы с {wh['start_hour']}:00 до {wh['end_hour']}:00.\n\n"
                   f"Вы можете спокойно посмотреть каталог и добавить товары в корзину — "
                   f"оформите заказ в рабочее время, корзина сохранится! 🛒")
        else:
            msg = (f"🕐 Kechirasiz, hozir ish vaqti emas.\n\n"
                   f"Buyurtmalarni {wh['start_hour']}:00 dan {wh['end_hour']}:00 gacha qabul qilamiz.\n\n"
                   f"Katalogni bemalol ko'rib, savatga mahsulot qo'shishingiz mumkin — "
                   f"buyurtmani ish vaqtida rasmiylashtirasiz, savat saqlanib qoladi! 🛒")
        safe_send(cid, msg)
        return
    kill_kb(call)
    if not db.get_cart(cid):
        safe_send(cid, tr(cid, "cart_empty"))
        return
    # Ostatka tekshiruvi — savatdagi miqdor ombordan oshmasin
    problems = cart_shortages(cid)
    if problems:
        txt = fix_cart_to_stock(cid, problems)
        safe_send(cid, txt, reply_markup=main_menu(cid, call.from_user.id))
        return
    db.set_order(cid, {"step": "name"})
    safe_send(cid, tr(cid, "name"),
              reply_markup=name_button(cid, call.from_user))

# ─── Mijozning buyurtmalari ───────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ["📦 Buyurtmalarim", "📦 Мои заказы"])
def my_orders(message):
    cid = message.chat.id
    rows = db.get_user_orders(cid)
    if not rows:
        safe_send(cid, tr(cid, "no_my_orders"))
        return
    all_orders = {str(o.get("id")): o for o in load_json(ORDERS_FILE, [])}
    is_ru = lang(cid) == "ru"
    cur = "сум" if is_ru else "so'm"
    text = tr(cid, "my_orders_title") + "\n\n"
    for r in rows:
        o = all_orders.get(str(r.get("order_id")), {})
        status = status_text(cid, r.get("status", "new"))
        date = (r.get("created_at") or o.get("date") or "")[:10] or "—"
        num = o.get("number")
        head = f"#{int(num):03d}" if num else (str(r.get("order_id"))[:8] + "...")
        items = o.get("items", [])
        if is_ru:
            iline = ", ".join(f"{it.get('name_ru') or it.get('name','')} ×{it.get('qty',1)}" for it in items)
        else:
            iline = ", ".join(f"{it.get('name','')} ×{it.get('qty',1)}" for it in items)
        text += f"🆔 {head}\n📅 {tr(cid,'date_label')}: {date}\n"
        if iline:
            text += f"📦 {iline}\n"
        if o.get("total") is not None:
            text += f"💰 {int(o.get('total',0)):,} {cur}\n"
        text += f"📌 {tr(cid,'status_label')}: {status}\n\n"
    safe_send(cid, text)

@bot.message_handler(func=lambda m: m.text in ["💰 Cashback", "💰 Кешбэк"])
def show_cashback(message):
    cid = message.chat.id
    balance = db.get_cashback(cid)
    percent = db.get_setting("cashback_percent") or "5"
    if lang(cid) == "ru":
        txt = (f"💰 Ваш кешбэк: {balance:,} сум\n\n"
               f"С каждой покупки вы получаете {percent}% кешбэка. "
               f"Бонусы накапливаются на вашем балансе.")
    else:
        txt = (f"💰 Sizning cashback: {balance:,} so'm\n\n"
               f"Har bir xariddan {percent}% cashback olasiz. "
               f"Bonuslar balansingizda yig'ilib boradi.")
    safe_send(cid, txt)

@bot.message_handler(func=lambda m: m.text in ["👥 Mijozlar (sayt)", "👥 Клиенты (сайт)"])
@full_admin_only
def web_clients(message):
    """Saytdan ro'yxatdan o'tgan mijozlar bazasi (faqat Ibrohim va Rustam)."""
    cid = message.chat.id
    users = db.get_web_users()
    if not users:
        safe_send(cid, "👥 Saytdan ro'yxatdan o'tgan mijoz hali yo'q.")
        return
    orders = load_json(ORDERS_FILE, [])
    agg = {}
    for o in orders:
        ph = _norm_phone(o.get("phone"))
        if not ph:
            continue
        a = agg.setdefault(ph, {"count": 0, "total": 0})
        a["count"] += 1
        try:
            a["total"] += int(o.get("total", 0))
        except Exception:
            pass
    total_clients = len(users)
    total_orders = sum(v["count"] for v in agg.values())
    msg = (f"👥 <b>Sayt mijozlari (klient bazasi)</b>\n"
           f"Jami: <b>{total_clients}</b> mijoz\n\n")
    for i, u in enumerate(users, 1):
        ph = _norm_phone(u.get("phone"))
        st = agg.get(ph, {"count": 0, "total": 0})
        date = (u.get("created_at") or "")[:10]
        block = (f"{i}. <b>{u.get('name','')}</b>\n"
                 f"   📱 +{u.get('phone','')}\n"
                 f"   📅 {date}  •  🛒 {st['count']} ta  •  {st['total']:,} so'm\n\n")
        if len(msg) + len(block) > 3500:
            safe_send(cid, msg)
            msg = ""
        msg += block
    if msg.strip():
        safe_send(cid, msg)

# ─── Operator & Reviews ───────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ["📞 Operator", "📞 Оператор"])
def operator(message):
    safe_send(message.chat.id, tr(message.chat.id, "operator_text"), reply_markup=back_menu(message.chat.id))
    safe_send(message.chat.id, f"📞 {get_operator()}")

@bot.message_handler(func=lambda m: m.text in ["⭐ Fikrlar", "⭐ Отзывы"])
def show_reviews(message):
    reviews = get_reviews()
    if not reviews:
        safe_send(message.chat.id, tr(message.chat.id, "no_reviews"))
        return
    # O'rtacha reyting (Yandex kabi yuqorida ko'rsatamiz)
    rated = [int(r.get("stars", 0)) for r in reviews if r.get("stars")]
    if rated:
        avg = sum(rated) / len(rated)
        full = int(round(avg))
        header = f"{'⭐' * full} {avg:.1f} / 5  ({len(rated)} ta baho)\n"
        safe_send(message.chat.id, header)
    for r in reviews[-10:]:
        stars = int(r.get("stars", 0))
        star_line = ("⭐" * stars + f"  ({stars}/5)\n") if stars else ""
        text = f"{star_line}👤 {r.get('name','Mijoz')}\n\n{r.get('text','')}"
        if r.get("photo_id"):
            safe_photo(message.chat.id, r["photo_id"], caption=text)
        else:
            safe_send(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text in ["✍️ Fikr yozish", "✍️ Оставить отзыв"])
def review_start(message):
    cid = message.chat.id
    db.set_order(cid, {"step": "review_stars"})
    text = "Mahsulotni necha yulduzga baholaysiz?" if lang(cid) == "uz" \
        else "На сколько звёзд оцениваете товар?"
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("⭐", callback_data="star_1"),
        types.InlineKeyboardButton("⭐⭐", callback_data="star_2"),
        types.InlineKeyboardButton("⭐⭐⭐", callback_data="star_3"),
    )
    kb.add(
        types.InlineKeyboardButton("⭐⭐⭐⭐", callback_data="star_4"),
        types.InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="star_5"),
    )
    safe_send(cid, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("star_"))
def review_star_chosen(call):
    cid = call.message.chat.id
    stars = int(call.data.replace("star_", ""))
    db.set_order(cid, {"step": "review", "stars": stars})
    bot.answer_callback_query(call.id, "⭐" * stars)
    text = (f"{'⭐' * stars}\n\nEndi fikringizni matn yoki rasm orqali yozing:"
            if lang(cid) == "uz"
            else f"{'⭐' * stars}\n\nТеперь напишите отзыв текстом или фото:")
    safe_send(cid, text, reply_markup=back_menu(cid))
    kill_kb(call)

# ─── Admin panel ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "👑 Admin panel")
@admin_only
def admin_panel(message):
    safe_send(message.chat.id, "👑 Admin panel", reply_markup=admin_menu(message.from_user.id))

# Statistika
@bot.message_handler(func=lambda m: m.text == "📊 Statistika")
@admin_only
def admin_stats(message):
    try:
        s = db.get_stats(ORDERS_FILE)
        users = db.get_user_count()
        cb = db.get_cashback_totals()
        text = (
            f"📊 Statistika\n\n"
            f"👥 Foydalanuvchilar: {users} ta\n\n"
            f"📅 Bugun:  {s['today']} ta — {s['today_sum']:,} so'm\n"
            f"📆 7 kun:  {s['week']} ta — {s['week_sum']:,} so'm\n"
            f"🗓 30 kun: {s['month']} ta — {s['month_sum']:,} so'm\n"
            f"📦 Jami:   {s['total']} ta — {s['total_sum']:,} so'm\n\n"
            f"💰 Cashback:\n"
            f"  • Jami qaytarilgan: {cb['total_given']:,} so'm\n"
            f"  • Hozir balanslarda: {cb['current_balance']:,} so'm"
        )
        safe_send(message.chat.id, text, reply_markup=admin_menu())
    except Exception as e:
        log.error(f"Statistika xato: {e}")
        safe_send(message.chat.id, f"⚠️ Statistikada xato: {e}", reply_markup=admin_menu())

# Sozlamalar
@bot.message_handler(func=lambda m: m.text == "⚙️ Sozlamalar")
@admin_only
def admin_settings(message):
    show_settings(message.chat.id)

def show_settings(chat_id):
    s = db.get_all_settings()
    text = (
        f"⚙️ Sozlamalar:\n\n"
        f"🚚 Delivery: {int(s.get('delivery', 30000)):,} so'm\n"
        f"💵 Minimal yetkazish: {get_min_delivery():,} so'm\n"
        f"🎁 Sovg'a qutisi narxi (sotuv): {int(s.get('gift_box_price', 50000)):,} so'm\n"
        f"📦 Oddiy qadoq tannarxi: {int(s.get('qadoq_oddiy_cost', 0)):,} so'm\n"
        f"🎁 Sovg'a qutisi tannarxi: {int(s.get('gift_box_cost', 0)):,} so'm\n"
        f"📉 Ombor chegarasi: {int(s.get('low_stock_threshold', 3))} dona\n"
        f"📞 Operator: {s.get('operator','')}\n"
        f"💳 Payme kassa: {'✅ ulangan' if payme_enabled() else '❌ ulanmagan'}\n"
        f"🔵 Click kassa: {'✅ ulangan' if click_enabled() else '❌ ulanmagan'}\n\n"
        f"To'lov tizimlari env orqali ulanadi (karta raqami saqlanmaydi).\n\n"
        f"Qadoq tannarxi har buyurtmaga sotuv paytida biriktiriladi "
        f"(keyin o'zgartirsangiz, eski oylar o'zgarmaydi).\n\n"
        f"O'zgartirish uchun tugmani bosing:"
    )
    safe_send(chat_id, text, reply_markup=settings_menu())

@bot.callback_query_handler(func=lambda c: c.data == "open_delivery")
def open_delivery_cb(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    cur = get_shop_location()
    loc_txt = f"✅ {cur[0]:.4f}, {cur[1]:.4f}" if cur else "⚠️ o'rnatilmagan"
    txt = (f"🚚 Delivery sozlamalari\n\n"
           f"📍 Do'kon manzili: {loc_txt}\n"
           f"🚗 1 km narxi: {get_price_per_km():,} so'm\n"
           f"💵 Minimal yetkazish: {get_min_delivery():,} so'm\n\n"
           f"Yetkazib berish mijoz lokatsiyasiga qarab avtomatik hisoblanadi "
           f"(masofa × 1 km narxi). Natija minimaldan kam bo'lsa — minimal olinadi.\n\n"
           f"⚠️ Do'kon manzili o'rnatilmasa, yetkazib berish hisoblanmaydi!")
    safe_send(call.message.chat.id, txt, reply_markup=delivery_menu())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_") and not c.data.startswith("setprice_"))
def settings_callback(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    chat_id = call.message.chat.id
    # Do'kon lokatsiyasi — alohida (lokatsiya yuborish)
    if call.data == "set_shop_loc":
        db.set_admin_step(chat_id, {"step": "setting_shop_loc"})
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(types.KeyboardButton("📍 Do'kon lokatsiyasini yuborish", request_location=True))
        kb.add("⬅️ Orqaga", "🏠 Asosiy menu")
        cur = get_shop_location()
        cur_txt = f"\n\nHozirgi: {cur[0]:.5f}, {cur[1]:.5f}" if cur else "\n\nHozircha o'rnatilmagan."
        safe_send(chat_id,
                  "📍 Do'kon turgan joyga borib (yoki xaritadan), lokatsiyani yuboring." + cur_txt,
                  reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if call.data == "set_per_km":
        db.set_admin_step(chat_id, {"step": "setting_price_per_km"})
        safe_send(chat_id, f"🚗 1 km uchun narxni yozing (raqam).\nHozirgi: {get_price_per_km():,} so'm",
                  reply_markup=step_back_menu(chat_id))
        bot.answer_callback_query(call.id)
        return
    key_map = {
        "set_delivery": ("delivery",       "Yangi delivery narxini yozing (raqam):"),
        "set_min_delivery": ("min_delivery",
            "💵 Minimal yetkazish summasini yozing (raqam, masalan 30000):"),
        "set_gift_box": ("gift_box_price", "Yangi sovg'a qutisi narxini yozing:"),
        "set_qadoq_oddiy_cost": ("qadoq_oddiy_cost",
            "📦 Oddiy qadoq TANNARXINI yozing (tissue+vizitka+thank you+nakleyka+sumka jami, raqam):"),
        "set_gift_box_cost": ("gift_box_cost",
            "🎁 Sovg'a qutisi TANNARXINI yozing (tissue+vizitka+thank you+nakleyka+quti jami, raqam):"),
        "set_low_stock": ("low_stock_threshold",
            "📉 Ombor ogohlantirish chegarasini yozing (necha dona qolganda signal kelsin, masalan 3):"),
        "set_operator": ("operator",       "Yangi operator username:"),
    }
    if call.data in key_map:
        db_key, prompt = key_map[call.data]
        db.set_admin_step(chat_id, {"step": f"setting_{db_key}"})
        safe_send(chat_id, prompt, reply_markup=step_back_menu(chat_id))
    bot.answer_callback_query(call.id)

# ─── Cashback boshqaruvi (admin) ──────────────────────────────────────────────

def cashback_admin_menu():
    on = db.get_setting("cashback_on") == "1"
    percent = db.get_setting("cashback_percent") or "5"
    kb = types.InlineKeyboardMarkup()
    if on:
        kb.add(types.InlineKeyboardButton("🟢 Yoqilgan (o'chirish)", callback_data="cb_toggle"))
    else:
        kb.add(types.InlineKeyboardButton("🔴 O'chirilgan (yoqish)", callback_data="cb_toggle"))
    kb.add(types.InlineKeyboardButton(f"✏️ Foizni o'zgartirish (hozir {percent}%)", callback_data="cb_percent"))
    return kb

@bot.message_handler(func=lambda m: m.text == "💰 Cashback boshqaruvi")
@full_admin_only
def cashback_admin(message):
    on = db.get_setting("cashback_on") == "1"
    percent = db.get_setting("cashback_percent") or "5"
    status = "🟢 Yoqilgan" if on else "🔴 O'chirilgan"
    txt = (f"💰 Cashback boshqaruvi\n\n"
           f"Holat: {status}\n"
           f"Foiz: {percent}%\n\n"
           f"Har bir tasdiqlangan buyurtmadan mijozga {percent}% cashback beriladi.")
    safe_send(message.chat.id, txt, reply_markup=cashback_admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "cb_toggle")
def cashback_toggle(call):
    if not is_admin(call.from_user.id):
        return
    current = db.get_setting("cashback_on") == "1"
    db.set_setting("cashback_on", "0" if current else "1")
    new_status = "🔴 O'chirildi" if current else "🟢 Yoqildi"
    bot.answer_callback_query(call.id, new_status)
    on = db.get_setting("cashback_on") == "1"
    percent = db.get_setting("cashback_percent") or "5"
    status = "🟢 Yoqilgan" if on else "🔴 O'chirilgan"
    txt = (f"💰 Cashback boshqaruvi\n\nHolat: {status}\nFoiz: {percent}%")
    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=cashback_admin_menu())
    except:
        safe_send(call.message.chat.id, txt, reply_markup=cashback_admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "cb_percent")
def cashback_percent_start(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    db.set_admin_step(call.message.chat.id, {"step": "cashback_percent"})
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id,
              "Yangi cashback foizini yozing (faqat raqam, masalan: 5):",
              reply_markup=step_back_menu(call.message.chat.id))

# Broadcast
@bot.message_handler(func=lambda m: m.text == "📣 Broadcast")
@full_admin_only
def broadcast_start(message):
    db.set_admin_step(message.chat.id, {"step": "broadcast"})
    users = db.get_user_count()
    safe_send(message.chat.id,
              f"📣 Broadcast\n\n👥 {users} ta foydalanuvchiga xabar yuboriladi.\n\nXabarni yozing (matn, rasm, yoki rasm+matn):",
              reply_markup=step_back_menu(message.chat.id))

@bot.callback_query_handler(func=lambda c: c.data == "bc_cancel")
def broadcast_cancel(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    db.delete_admin_step(call.message.chat.id)
    bot.answer_callback_query(call.id, "Bekor qilindi")
    safe_send(call.message.chat.id, "❌ Broadcast bekor qilindi.", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "bc_send")
def broadcast_send(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    chat_id = call.message.chat.id
    step_data = db.get_admin_step(chat_id)
    if not step_data or step_data.get("step") != "broadcast_confirm":
        bot.answer_callback_query(call.id, "Xabar topilmadi")
        return
    bc = step_data.get("bc", {})
    bot.answer_callback_query(call.id, "Yuborilmoqda...")
    users   = db.get_all_users()
    success = 0
    failed  = 0
    safe_send(chat_id, f"📣 Yuborilmoqda... ({len(users)} ta)")
    for user_id in users:
        try:
            if bc.get("photo"):
                bot.send_photo(user_id, bc["photo"], caption=bc.get("text", "")[:1024])
            else:
                bot.send_message(user_id, bc.get("text", ""))
            success += 1
            time.sleep(0.05)
        except Exception as e:
            failed += 1
            log.warning(f"Broadcast xato ({user_id}): {e}")
    db.delete_admin_step(chat_id)
    log.info(f"Broadcast: {success} muvaffaqiyat, {failed} xato")
    safe_send(chat_id, f"✅ Broadcast tugadi!\n✅ {success} ta yuborildi\n❌ {failed} ta xato",
              reply_markup=admin_menu())


# ─── PROMO KODLAR (to'liq admin panel) ───────────────────────────────────────

def promo_list_keyboard():
    promos = db.get_all_promos()
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Yangi promo qo'shish", callback_data="promo_add"))
    for p in promos:
        icon = "✅" if p["active"] else "❌"
        uses = "∞" if p["uses_left"] == -1 else f"{p['uses_left']}ta"
        label = f"{icon} {p['code']} — {p['discount']}% | {uses}"
        kb.add(
            types.InlineKeyboardButton(label, callback_data=f"promo_info_{p['code']}"),
        )
    return kb

def promo_detail_keyboard(code, active):
    kb = types.InlineKeyboardMarkup()
    toggle_label = "❌ To'xtatish" if active else "✅ Yoqish"
    kb.add(types.InlineKeyboardButton(toggle_label,     callback_data=f"promo_toggle_{code}"))
    kb.add(types.InlineKeyboardButton("✏️ Tahrirlash",  callback_data=f"promo_edit_{code}"))
    kb.add(types.InlineKeyboardButton("🗑 O'chirish",   callback_data=f"promo_del_{code}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga",      callback_data="promo_back"))
    return kb

@bot.message_handler(func=lambda m: m.text == "🎁 Promo kodlar")
@full_admin_only
def promo_menu(message):
    show_promo_list(message.chat.id)

def show_promo_list(chat_id):
    promos = db.get_all_promos()
    if not promos:
        text = "🎁 Promo kodlar yo'q.\n\n➕ tugmasini bosib yangi qo'shing:"
    else:
        text = f"🎁 Promo kodlar ({len(promos)} ta):\n\n"
        for p in promos:
            status = "✅ Faol" if p["active"] else "❌ To'xtatilgan"
            uses   = "∞ Cheksiz" if p["uses_left"] == -1 else f"{p['uses_left']} ta qoldi"
            text  += f"{status}\n📌 Kod: {p['code']}\n💰 Chegirma: {p['discount']}%\n🔢 Foydalanish: {uses}\n\n"
    safe_send(chat_id, text, reply_markup=promo_list_keyboard())

@bot.callback_query_handler(func=lambda c: c.data == "promo_back")
def promo_back(call):
    if not is_admin(call.from_user.id): return
    kill_kb(call)
    show_promo_list(call.message.chat.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "promo_add")
def promo_add_start(call):
    if not is_admin(call.from_user.id): return
    db.set_admin_step(call.message.chat.id, {"step": "promo_add_code"})
    safe_send(call.message.chat.id,
              "➕ Yangi promo kod\n\nKodni yozing (lotin harflar va raqamlar):\nMasalan: SALE20",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)
    kill_kb(call)

@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_info_"))
def promo_info(call):
    if not is_admin(call.from_user.id): return
    code  = call.data.replace("promo_info_", "")
    promo = db.get_promo(code) or next((p for p in db.get_all_promos() if p["code"] == code), None)
    if not promo:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    status = "✅ Faol" if promo["active"] else "❌ To'xtatilgan"
    uses   = "∞ Cheksiz" if promo["uses_left"] == -1 else f"{promo['uses_left']} ta qoldi"
    text = (
        f"🎁 Promo kod: {promo['code']}\n\n"
        f"📌 Holat: {status}\n"
        f"💰 Chegirma: {promo['discount']}%\n"
        f"🔢 Foydalanish: {uses}\n\n"
        f"Quyidan amal tanlang:"
    )
    safe_send(call.message.chat.id, text, reply_markup=promo_detail_keyboard(code, promo["active"]))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_toggle_"))
def promo_toggle(call):
    if not is_admin(call.from_user.id): return
    code      = call.data.replace("promo_toggle_", "")
    new_state = db.toggle_promo(code)
    status    = "✅ Yoqildi" if new_state else "❌ To'xtatildi"
    log.info(f"Promo {code} holati: {status}")
    bot.answer_callback_query(call.id, f"{status}: {code}")
    kill_kb(call)
    show_promo_list(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_del_"))
def promo_delete(call):
    if not is_admin(call.from_user.id): return
    code = call.data.replace("promo_del_", "")
    db.delete_promo(code)
    log.info(f"Promo o'chirildi: {code}")
    bot.answer_callback_query(call.id, f"🗑 {code} o'chirildi")
    kill_kb(call)
    show_promo_list(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_edit_"))
def promo_edit_start(call):
    if not is_admin(call.from_user.id): return
    code = call.data.replace("promo_edit_", "")
    db.set_admin_step(call.message.chat.id, {"step": "promo_edit_discount", "promo_code": code})
    safe_send(call.message.chat.id,
              f"✏️ {code} ni tahrirlash\n\nYangi chegirmani yozing (faqat raqam %):\nMasalan: 30",
              reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)
    kill_kb(call)

# ─── Faol buyurtmalar: har biri alohida tugma ────────────────────────────────

_ACTIVE_SCAN = 300          # oxirgi shuncha buyurtma ichidan qidiramiz

def _order_paid(o):
    return (o.get("status") == "paid"
            or (o.get("payme") or {}).get("state") == 2
            or (o.get("click") or {}).get("state") == 2)

def active_orders(limit=20):
    """Faol (yetkazilmagan va bekor qilinmagan) buyurtmalar: [(order, status)]."""
    orders = load_json(ORDERS_FILE, [])[-_ACTIVE_SCAN:]
    out = []
    for o in reversed(orders):                       # yangilari yuqorida
        if o.get("status") == "cancelled":
            continue
        oid = str(o.get("id"))
        st = (db.get_tracking(oid) or {}).get("status", "new")
        if st in ("delivered", "cancelled"):
            continue
        out.append((o, st))
        if len(out) >= limit:
            break
    return out

def _order_btn_label(o, st):
    num = o.get("number")
    head = f"#{int(num):05d}" if num else str(o.get("id"))[:6]
    money = f"{int(o.get('total', 0) or 0):,}".replace(",", " ")
    mark = "💰" if _order_paid(o) else ("💵" if (o.get("pay_type") == "Naqd") else "⏳")
    nm = (o.get("name") or "")[:12]
    return f"{mark} {head} · {nm} · {money}"

def active_orders_kb(orders):
    kb = types.InlineKeyboardMarkup()
    for o, st in orders:
        kb.add(types.InlineKeyboardButton(_order_btn_label(o, st),
                                          callback_data=f"ord_{o.get('id')}"))
    kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data="ordlist"))
    return kb

def order_card_text(chat_id, o, st):
    num = o.get("number")
    head = f"#{int(num):05d}" if num else str(o.get("id"))[:8]
    lines = [f"🧾 Buyurtma {head}", ""]
    lines.append(f"👤 {o.get('name','')}")
    lines.append(f"📱 {o.get('phone','')}")
    lines.append(f"💳 {o.get('pay_type','')} — " + ("✅ TO'LANGAN" if _order_paid(o) else "⏳ to'lanmagan"))
    lines.append(f"📌 Holat: {status_text(chat_id, st)}")
    lines.append(f"🌐 Manba: {'sayt' if o.get('source') == 'sayt' else 'bot'}")
    if o.get("date"):
        lines.append(f"📅 {str(o['date'])[:16].replace('T', ' ')}")
    lines.append("")
    for it in (o.get("items") or []):
        size = (it.get("size") or "").strip()
        nm = it.get("name", "")
        if size:
            nm = f"{nm} ({size})"
        lines.append(f"• {nm} x{it.get('qty', 1)} — {int(it.get('subtotal', 0)):,} so'm")
    lines.append("")
    if int(o.get("delivery", 0) or 0):
        lines.append(f"🚚 Yetkazish: {int(o['delivery']):,} so'm")
    if o.get("packaging"):
        lines.append(f"🎁 Qadoq: {o['packaging']}")
    if int(o.get("promo_discount", 0) or 0):
        lines.append(f"🏷 Promo ({o.get('promo_code','')}): -{int(o['promo_discount']):,} so'm")
    if int(o.get("cashback_used", 0) or 0):
        lines.append(f"💰 Cashback: -{int(o['cashback_used']):,} so'm")
    lines.append(f"💵 Jami: {int(o.get('total', 0) or 0):,} so'm")
    maps = o.get("maps") or ""
    if maps:
        lines.append(f"\n📍 {maps}")
    return "\n".join(lines)

def order_card_kb(o, st, user_id):
    oid = o.get("id")
    kb = types.InlineKeyboardMarkup()
    if can_manage_status(user_id):
        if not _order_paid(o):
            kb.add(types.InlineKeyboardButton("✅ To'lovni tasdiqlash", callback_data=f"paycfm_{oid}"))
        if st in ("new", "confirmed"):
            kb.add(types.InlineKeyboardButton("👨‍🍳 Tayyorlanmoqda", callback_data=f"track_preparing_{oid}"))
        if st in ("confirmed", "preparing"):
            kb.add(types.InlineKeyboardButton("🚚 Yetkazilmoqda", callback_data=f"track_delivering_{oid}"))
        if st in ("preparing", "delivering"):
            kb.add(types.InlineKeyboardButton("✅ Yetkazildi", callback_data=f"track_delivered_{oid}"))
        kb.add(types.InlineKeyboardButton("❌ Buyurtmani bekor qilish", callback_data=f"ordcan_{oid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data="ordlist"))
    return kb

def _edit_or_send(call, text, kb):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id, text, reply_markup=kb)

def _show_active_list(call=None, chat_id=None):
    orders = active_orders()
    if not orders:
        txt = "📭 Faol buyurtma yo'q.\n\nHamma buyurtma yetkazilgan yoki bekor qilingan."
        if call:
            _edit_or_send(call, txt, types.InlineKeyboardMarkup(
                [[types.InlineKeyboardButton("🔄 Yangilash", callback_data="ordlist")]]))
        else:
            safe_send(chat_id, txt, reply_markup=admin_menu())
        return
    paid = sum(1 for o, _ in orders if _order_paid(o))
    txt = (f"🚚 Faol buyurtmalar: {len(orders)} ta\n"
           f"💰 To'langan: {paid} · ⏳ Kutilmoqda: {len(orders) - paid}\n\n"
           f"Batafsil ko'rish uchun tugmani bosing:")
    kb = active_orders_kb(orders)
    if call:
        _edit_or_send(call, txt, kb)
    else:
        safe_send(chat_id, txt, reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "🚚 Buyurtma holati")
@admin_only
def tracking_list(message):
    _show_active_list(chat_id=message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "ordlist")
def orders_list_cb(call):
    if not is_admin(call.from_user.id):
        return
    bot.answer_callback_query(call.id)
    _show_active_list(call=call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ord_"))
def order_card_cb(call):
    if not is_admin(call.from_user.id):
        return
    oid = call.data.replace("ord_", "")
    o = next((x for x in load_json(ORDERS_FILE, []) if str(x.get("id")) == oid), None)
    if not o:
        bot.answer_callback_query(call.id, "Buyurtma topilmadi", show_alert=True)
        return
    st = (db.get_tracking(oid) or {}).get("status", "new")
    bot.answer_callback_query(call.id)
    _edit_or_send(call, order_card_text(call.message.chat.id, o, st),
                  order_card_kb(o, st, call.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("paycfm_"))
def order_pay_confirm_cb(call):
    """To'lovni qo'lda tasdiqlash (Naqd yoki onlayn kelmagan holat).
    Tovar shu yerda ombordan yechiladi, qolganini paid_watch_loop bajaradi."""
    if not can_manage_status(call.from_user.id):
        bot.answer_callback_query(call.id, "Faqat mas'ul admin", show_alert=True)
        return
    oid = call.data.replace("paycfm_", "")
    o = next((x for x in load_json(ORDERS_FILE, []) if str(x.get("id")) == oid), None)
    if not o:
        bot.answer_callback_query(call.id, "Buyurtma topilmadi", show_alert=True)
        return
    if _order_paid(o):
        bot.answer_callback_query(call.id, "Allaqachon to'langan", show_alert=True)
        return

    ok, short = take_order_stock(oid, f"admin {call.from_user.id} to'lovni tasdiqladi")
    if not ok:
        bot.answer_callback_query(call.id, "Omborda yetarli emas", show_alert=True)
        safe_send(call.message.chat.id,
                  "⚠️ Omborda yetarli emas — tasdiqlanmadi:\n\n"
                  + "\n".join(f"• {n}: kerak {q}, bor {a}" for n, q, a in short))
        return

    orders = load_json(ORDERS_FILE, [])
    o = next((x for x in orders if str(x.get("id")) == oid), None)
    o["status"] = "paid"
    o["paid_by"] = f"admin:{call.from_user.id}"
    _save_orders_merged(orders)
    try:
        db.update_tracking(oid, "confirmed")
    except Exception:
        pass
    # cashback + mijozga xabar -> paid_watch_loop (5 soniya ichida)
    bot.answer_callback_query(call.id, "✅ To'lov tasdiqlandi")
    st = (db.get_tracking(oid) or {}).get("status", "confirmed")
    _edit_or_send(call, order_card_text(call.message.chat.id, o, st),
                  order_card_kb(o, st, call.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("ordcan_"))
def order_cancel_cb(call):
    if not can_manage_status(call.from_user.id):
        bot.answer_callback_query(call.id, "Faqat mas'ul admin", show_alert=True)
        return
    oid = call.data.replace("ordcan_", "")
    o = next((x for x in load_json(ORDERS_FILE, []) if str(x.get("id")) == oid), None)
    if not o:
        bot.answer_callback_query(call.id, "Buyurtma topilmadi", show_alert=True)
        return

    try:
        restore_order_stock(oid, f"admin {call.from_user.id} bekor qildi", cancel=True)
        db.update_tracking(oid, "cancelled")
        db.delete_pending_order(oid)
    except Exception as e:
        log.error(f"order_cancel_cb ({oid}): {e}")

    cid = _order_chat_id(o)
    if cid:
        num = o.get("number")
        head = f"#{int(num):05d}" if num else oid[:8]
        if lang(cid) == "ru":
            safe_send(cid, f"❌ Заказ {head} отменён.\n\nПо вопросам: {get_operator()}")
        else:
            safe_send(cid, f"❌ Buyurtma {head} bekor qilindi.\n\nSavollar bo'lsa: {get_operator()}")
    bot.answer_callback_query(call.id, "❌ Bekor qilindi")
    _show_active_list(call=call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("track_"))
def update_tracking_cb(call):
    # Holatni FAQAT mas'ul (cheklangan) admin o'zgartiradi
    if not can_manage_status(call.from_user.id):
        try: bot.answer_callback_query(call.id, "Buyurtma holatini faqat mas'ul admin o'zgartiradi.")
        except Exception: pass
        return
    parts    = call.data.split("_")
    status   = parts[1]
    order_id = "_".join(parts[2:])
    t = db.get_tracking(order_id)
    if not t:
        bot.answer_callback_query(call.id, "Buyurtma topilmadi.")
        return
    # ── Ombor: 'cancelled' -> qaytaramiz, tasdiqlash/keyingilari -> yechamiz ──
    if status == "cancelled":
        try:
            restore_order_stock(order_id, "holat: bekor qilindi", cancel=True)
        except Exception as e:
            log.error(f"stock restore xato ({order_id}): {e}")
    elif status in ("confirmed", "preparing", "delivering", "delivered"):
        ok, short = take_order_stock(order_id, f"holat: {status}")
        if not ok:
            txt = "⚠️ Omborda yetarli emas — holat o'zgartirilmadi:\n\n"
            txt += "\n".join(f"• {n}: kerak {q}, bor {a}" for n, q, a in short)
            txt += "\n\nOmborni to'ldiring yoki buyurtmani bekor qiling."
            try:
                bot.answer_callback_query(call.id, "Omborda yetarli emas", show_alert=True)
            except Exception:
                pass
            safe_send(call.message.chat.id, txt)
            return

    db.update_tracking(order_id, status)
    user_id = t["chat_id"]
    status_label = status_text(user_id, status)

    if status == "confirmed":
        try: award_web_cashback(order_id, user_id)
        except Exception: pass

    # Mijozga xabar (agar botga bog'langan bo'lsa)
    if user_id:
        if status == "delivered":
            name = t.get("customer_name") or ("Aziz mijoz" if lang(user_id) == "uz" else "Дорогой клиент")
            safe_send(user_id, random_thanks(user_id, name))
            # 3 kundan keyin so'rovnoma uchun sanalaydi
            try:
                review_at = (datetime.now(TASHKENT_TZ) + timedelta(days=3)).isoformat()
                pending = get_pending_reviews()
                # Avvalgi yozuv bo'lsa yangilaymiz
                pending = [r for r in pending if r.get("order_id") != order_id]
                pending.append({
                    "order_id": order_id, "chat_id": user_id,
                    "name": name, "review_at": review_at
                })
                save_pending_reviews(pending)
            except Exception as e:
                log.error(f"review schedule xato: {e}")
        elif status == "cancelled":
            safe_send(user_id, f"{tr(user_id,'status_update')}\n\n📌 {status_label}")
        else:
            safe_send(user_id, f"{tr(user_id,'status_update')}\n\n📌 {status_label}\n🆔 {order_id[:8]}...")

    log.info(f"Tracking yangilandi: {order_id} → {status}")
    bot.answer_callback_query(call.id, f"✅ {status_label}")

    if status in ("delivered", "cancelled"):
        # Yakunlandi — holat tugmalarini olib tashlaymiz va 3 adminni xabardor qilamiz
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        num = _order_number(order_id)
        head = f"#{int(num):03d}" if num else (order_id[:8] + "...")
        fin = "✅ Yakunlandi (yetkazildi)" if status == "delivered" else "❌ Bekor qilindi"
        for aid in get_all_admins():
            try: safe_send(aid, f"📦 Buyurtma {head} — {fin}")
            except Exception: pass
    else:
        safe_send(call.message.chat.id, f"✅ Holat yangilandi: {status_label}")

# Ish vaqti
@bot.message_handler(func=lambda m: m.text == "🕐 Ish vaqti")
@full_admin_only
def work_hours_menu(message):
    wh = db.get_work_hours()
    status = "✅ Yoqilgan" if wh["enabled"] else "❌ O'chirilgan"
    text = (
        f"🕐 Ish vaqti sozlamasi\n\n"
        f"Holat: {status}\n"
        f"Vaqt: {wh['start_hour']}:00 — {wh['end_hour']}:00\n\n"
        f"O'zgartirish uchun:\n"
        f"/workon — Yoqish\n"
        f"/workoff — O'chirish\n"
        f"/workhours 9 22 — Vaqtni o'zgartirish"
    )
    safe_send(message.chat.id, text, reply_markup=admin_menu())

@bot.message_handler(commands=["workon"])
@full_admin_only
def work_on(message):
    wh = db.get_work_hours()
    db.set_work_hours(wh["start_hour"], wh["end_hour"], 1)
    safe_send(message.chat.id, f"✅ Ish vaqti yoqildi: {wh['start_hour']}:00 — {wh['end_hour']}:00")

@bot.message_handler(commands=["workoff"])
@full_admin_only
def work_off(message):
    wh = db.get_work_hours()
    db.set_work_hours(wh["start_hour"], wh["end_hour"], 0)
    safe_send(message.chat.id, "❌ Ish vaqti o'chirildi. Bot 24/7 ishlaydi.")

@bot.message_handler(commands=["workhours"])
@full_admin_only
def set_work_hours_cmd(message):
    parts = message.text.split()
    if len(parts) < 3:
        safe_send(message.chat.id, "❌ Format: /workhours 9 22")
        return
    try:
        start = int(parts[1])
        end   = int(parts[2])
        wh    = db.get_work_hours()
        db.set_work_hours(start, end, wh["enabled"])
        safe_send(message.chat.id, f"✅ Ish vaqti: {start}:00 — {end}:00")
    except:
        safe_send(message.chat.id, "❌ Xato. Format: /workhours 9 22")

# Mahsulotlar
@bot.message_handler(func=lambda m: m.text == "➕ Mahsulot qo'shish")
@admin_only
def add_product_start(message):
    db.set_admin_step(message.chat.id, {"step": "name_uz"})
    safe_send(message.chat.id, "Mahsulot nomini o'zbekcha yozing:", reply_markup=add_back_menu(message.chat.id))

def _prod_categories():
    """Mahsulotlardagi papkalar (kategoriyalar), tartiblangan + guruhlangan."""
    by_cat = {}
    for p in get_products():
        c = (p.get("category") or "Boshqa").strip() or "Boshqa"
        by_cat.setdefault(c, []).append(p)
    return sorted(by_cat.keys()), by_cat

def _cat_folders_kb(cb_prefix):
    """Papka (kategoriya) tugmalari — o'chirish/tahrirlash uchun umumiy."""
    cats, by_cat = _prod_categories()
    kb = types.InlineKeyboardMarkup()
    for i, c in enumerate(cats):
        kb.add(types.InlineKeyboardButton(
            f"📂 {c} ({len(by_cat[c])} ta)", callback_data=f"{cb_prefix}{i}"))
    return kb

def _prod_folders_kb():
    cats, by_cat = _prod_categories()
    kb = types.InlineKeyboardMarkup()
    for i, c in enumerate(cats):
        dona = sum(product_total_stock(p) for p in by_cat[c])
        kb.add(types.InlineKeyboardButton(
            f"📂 {c} — {len(by_cat[c])} ta ({dona} dona)", callback_data=f"prodcat_{i}"))
    kb.add(types.InlineKeyboardButton("📊 Umumiy ombor hisoboti", callback_data="prodall"))
    return kb

def _sklad_summary_text():
    products = get_products()
    text = "📦 OMBOR (SKLAD) — umumiy\n\n"
    total_cost = total_sale = total_items = 0
    for p in products:
        stock = product_total_stock(p)
        if stock <= 0:
            continue
        cost = int(p.get("cost", 0)); price = int(p.get("price", 0))
        total_cost += cost * stock; total_sale += price * stock; total_items += stock
    text += f"📊 Jami: {total_items} dona\n"
    text += f"💰 Ombor qiymati (tan narx): {total_cost:,} so'm\n"
    if total_sale > total_cost:
        text += f"📈 Sotuv qiymati: {total_sale:,} so'm\n"
        text += f"💵 Kutilayotgan foyda: {total_sale - total_cost:,} so'm"
    return text

@bot.message_handler(func=lambda m: m.text == "📦 Mahsulotlar ro'yxati")
@admin_only
def admin_products_list(message):
    products = get_products()
    if not products:
        safe_send(message.chat.id, "📦 Mahsulotlar yo'q.", reply_markup=admin_menu())
        return
    total_items = sum(product_total_stock(p) for p in products)
    safe_send(message.chat.id,
              f"📦 OMBOR — papkalar\n\nJami {len(products)} ta mahsulot · {total_items} dona.\n"
              f"Ko'rish uchun papkani tanlang:",
              reply_markup=_prod_folders_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("prodcat_"))
def prod_cat_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        idx = int(call.data.replace("prodcat_", ""))
    except ValueError:
        bot.answer_callback_query(call.id); return
    cats, by_cat = _prod_categories()
    if idx < 0 or idx >= len(cats):
        bot.answer_callback_query(call.id, "Papka topilmadi")
        return
    c = cats[idx]
    ps = by_cat[c]
    text = f"📂 {c}\n\n"
    cat_cost = cat_items = 0
    # Bir xil nom BARCHA papkalarda nechta bor (chalkashlik/tarqoqlikni ko'rish uchun)
    name_counts = {}
    for allp in get_products():
        nm = allp.get("name_uz", "")
        name_counts[nm] = name_counts.get(nm, 0) + 1
    for p in ps:
        total = product_total_stock(p)
        cost = int(p.get("cost", 0)); price = int(p.get("price", 0))
        cat_cost += cost * total; cat_items += total
        dup = "  ⚠️ takror" if name_counts.get(p.get("name_uz", ""), 0) > 1 else ""
        text += f"📌 {p['name_uz']} — {total} ta{dup}\n"
        _g = p.get("gender")
        if _g in GENDER_LABEL:
            text += f"   {GENDER_LABEL[_g]}\n"
        _sizes = p.get("sizes") or []
        if _sizes:
            text += "   📏 " + " · ".join(f"{s.get('label')}:{int(s.get('stock',0))}" for s in _sizes) + "\n"
        text += f"   Narx: {price:,} so'm\n\n"
    text += f"━━━━━━━━━━\n📊 {len(ps)} ta mahsulot · {cat_items} dona\n💰 Tan narx qiymati: {cat_cost:,} so'm"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Papkalar", callback_data="prodback"))
    try:
        if len(text) > 3800:
            bot.answer_callback_query(call.id)
            for i in range(0, len(text), 3800):
                safe_send(call.message.chat.id, text[i:i+3800])
            safe_send(call.message.chat.id, "⬆️ Ro'yxat", reply_markup=kb)
        else:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id)
    except Exception:
        safe_send(call.message.chat.id, text[:4000], reply_markup=kb)
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "prodback")
def prod_back_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.edit_message_text("📦 OMBOR — papkalar\n\nKo'rish uchun papkani tanlang:",
                              call.message.chat.id, call.message.message_id, reply_markup=_prod_folders_kb())
    except Exception:
        safe_send(call.message.chat.id, "📦 Papkalar:", reply_markup=_prod_folders_kb())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "prodall")
def prod_all_cb(call):
    if not is_admin(call.from_user.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Papkalar", callback_data="prodback"))
    try:
        bot.edit_message_text(_sklad_summary_text(), call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id, _sklad_summary_text(), reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "🗑 Mahsulot o'chirish")
@admin_only
def delete_start(message):
    if not get_products():
        safe_send(message.chat.id, "Mahsulotlar yo'q.", reply_markup=admin_menu())
        return
    safe_send(message.chat.id, "🗑 O'chirish — papkani tanlang:",
              reply_markup=_cat_folders_kb("delfold_"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("delfold_"))
def del_fold_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        idx = int(call.data.replace("delfold_", ""))
    except ValueError:
        bot.answer_callback_query(call.id); return
    cats, by_cat = _prod_categories()
    if idx < 0 or idx >= len(cats):
        bot.answer_callback_query(call.id, "Papka topilmadi"); return
    c = cats[idx]
    kb = types.InlineKeyboardMarkup()
    for p in by_cat[c]:
        kb.add(types.InlineKeyboardButton(
            f"🗑 {p['name_uz']} ({product_total_stock(p)} ta)",
            callback_data=f"delprod_{p['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Papkalar", callback_data="delfoldback"))
    try:
        bot.edit_message_text(f"📂 {c} — o'chirish uchun mahsulotni tanlang:",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id, f"📂 {c}:", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "delfoldback")
def del_fold_back_cb(call):
    if not is_admin(call.from_user.id):
        return
    if not get_products():
        try:
            bot.edit_message_text("📦 Mahsulot qolmadi.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id); return
    try:
        bot.edit_message_text("🗑 O'chirish — papkani tanlang:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=_cat_folders_kb("delfold_"))
    except Exception:
        safe_send(call.message.chat.id, "🗑 Papkalar:", reply_markup=_cat_folders_kb("delfold_"))
    bot.answer_callback_query(call.id)

AGE_RANGES = ["0–1 yosh", "1–2 yosh", "2–3 yosh", "3–5 yosh", "5–7 yosh"]

def age_keyboard(selected):
    selected = selected or []
    kb = types.InlineKeyboardMarkup()
    row = []
    for i, a in enumerate(AGE_RANGES):
        mark = "✅ " if a in selected else "▫️ "
        row.append(types.InlineKeyboardButton(mark + a, callback_data=f"agetog_{i}"))
        if len(row) == 2:
            kb.add(*row); row = []
    if row:
        kb.add(*row)
    kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="age_done"))
    return kb

def finalize_product(chat_id):
    d = db.get_admin_step(chat_id) or {}
    category = d.get("category", "Boshqa")
    sizes = d.get("sizes", [])
    if sizes:
        total = sum(int(s.get("stock", 0)) for s in sizes)
        ages = sizes_to_ages([s.get("label", "") for s in sizes])
    else:
        total = int(d.get("stock", 0) or 0)
        ages = d.get("ages", [])
    ps = get_products()
    ps.append({
        "id": str(uuid.uuid4()), "name_uz": d.get("name_uz"), "name_ru": d.get("name_ru"),
        "price": d.get("price"), "cost": d.get("cost", 0),
        "desc_uz": d.get("desc_uz"), "desc_ru": d.get("desc_ru"),
        "photo_id": d.get("photo_id", ""), "photos": d.get("photos", []),
        "stock": total, "sizes": sizes, "category": category,
        "gender": d.get("gender", "unisex"),
        "age": ages,
        "added_at": datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d"),
    })
    save_products(ps)
    db.delete_admin_step(chat_id)
    log.info(f"Yangi mahsulot: {d.get('name_uz')} (razmer={[s.get('label') for s in sizes]}, jins={d.get('gender')}, sotuv={d.get('price')})")
    if sizes:
        sz_txt = "\n".join(f"   {s.get('label')} — {int(s.get('stock',0))} ta" for s in sizes)
    else:
        sz_txt = f"   {total} ta"
    safe_send(chat_id,
              f"✅ Mahsulot qo'shildi!\n"
              f"📂 Kategoriya: {category}\n"
              f"👶 Jins: {GENDER_LABEL.get(d.get('gender','unisex'), '👶 Ikkisi ham')}\n"
              f"📏 Razmerlar:\n{sz_txt}\n"
              f"💵 Tan narx: {int(d.get('cost',0)):,} so'm\n"
              f"🏷 Sotuv narx: {int(d.get('price',0)):,} so'm\n"
              f"📦 Jami: {total} ta",
              reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data.startswith("agetog_") or c.data == "age_done")
def age_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id)
    if not d or d.get("step") != "age":
        bot.answer_callback_query(call.id)
        return
    ages = d.get("ages", [])
    if call.data == "age_done":
        if not ages:
            bot.answer_callback_query(call.id, "Kamida bittasini tanlang")
            return
        bot.answer_callback_query(call.id, "✅")
        finalize_product(chat_id)
        return
    idx = int(call.data.replace("agetog_", ""))
    if 0 <= idx < len(AGE_RANGES):
        a = AGE_RANGES[idx]
        if a in ages:
            ages.remove(a)
        else:
            ages.append(a)
        d["ages"] = ages
        db.set_admin_step(chat_id, d)
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=age_keyboard(ages))
        except Exception:
            pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data in ("photo_done", "photo_more", "photo_skip"))
def photo_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id)
    if not d or d.get("step") != "photo":
        bot.answer_callback_query(call.id)
        return
    photos = d.get("photos", [])
    if call.data == "photo_more":
        bot.answer_callback_query(call.id, "Yana rasm yuboring")
        safe_send(chat_id, "🖼 Keyingi rasmni yuboring:")
        return
    kill_kb(call)          # tanlangach tugmalar chatdan yo'qoladi
    if call.data == "photo_skip":
        d["photo_id"] = ""
        d["photos"] = []
    else:  # photo_done
        d["photo_id"] = photos[0] if photos else ""
        d["photos"] = photos
    d["step"] = "sizes_pick"
    d["size_labels"] = []
    db.set_admin_step(chat_id, d)
    bot.answer_callback_query(call.id, "✅")
    cnt = len(d.get("photos", []))
    msg = f"📦 {cnt} ta rasm saqlandi.\n\n" if cnt else "📦 Rasmsiz davom etamiz.\n\n"
    safe_send(chat_id, msg + "📏 Bu mahsulotda qaysi razmerlar bor? Tanlang:",
              reply_markup=size_pick_keyboard([]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sizetog_") or c.data == "sizes_done")
def size_pick_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    d = db.get_admin_step(chat_id)
    if not d or d.get("step") not in ("sizes_pick", "edit_sizes_pick"):
        bot.answer_callback_query(call.id)
        return
    editing = d.get("step") == "edit_sizes_pick"
    picked = d.get("size_labels", [])
    if call.data == "sizes_done":
        if not picked:
            bot.answer_callback_query(call.id, "Kamida bitta razmer tanlang")
            return
        picked = [s for s in SIZE_POOL if s in picked]   # SIZE_POOL tartibida
        d["size_labels"] = picked
        d["step"] = "edit_sizes_qty" if editing else "sizes_qty"
        db.set_admin_step(chat_id, d)
        bot.answer_callback_query(call.id, "✅")
        kill_kb(call)      # razmer chiplari yo'qoladi
        if editing:
            # Mavjud sonlarni namuna qilib ko'rsatamiz
            product = next((p for p in get_products() if str(p["id"]) == str(d.get("edit_pid"))), None)
            old = {s.get("label"): int(s.get("stock", 0)) for s in ((product or {}).get("sizes") or [])}
            example = " ".join(str(old.get(lb, 0)) for lb in picked)
            safe_send(chat_id,
                      "Tanlangan razmerlar: " + " · ".join(picked) + "\n\n"
                      "Har biriga nechta borligini shu tartibda, probel bilan yozing.\n"
                      f"Hozirgi sonlar: {example}",
                      reply_markup=back_menu(chat_id))
            return
        example = " ".join(["4", "2", "0", "5", "3", "1", "2", "3", "1", "2", "1"][:len(picked)])
        safe_send(chat_id,
                  "Tanlangan razmerlar: " + " · ".join(picked) + "\n\n"
                  "Har biriga nechta borligini shu tartibda, probel bilan yozing.\n"
                  f"Masalan: {example}",
                  reply_markup=add_back_menu(chat_id))
        return
    idx = int(call.data.replace("sizetog_", ""))
    if 0 <= idx < len(SIZE_POOL):
        s = SIZE_POOL[idx]
        if s in picked:
            picked.remove(s)
        else:
            picked.append(s)
        d["size_labels"] = picked
        db.set_admin_step(chat_id, d)
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id,
                                          reply_markup=size_pick_keyboard(picked))
        except Exception:
            pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("gender_"))
def gender_pick_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    key = call.data.replace("gender_", "")
    if key not in GENDER_LABEL:
        bot.answer_callback_query(call.id); return
    d = db.get_admin_step(chat_id) or {}
    step = d.get("step", "")
    if step == "gender":
        d["gender"] = key
        db.set_admin_step(chat_id, d)
        bot.answer_callback_query(call.id, "✅")
        kill_kb(call)      # jins tugmalari yo'qoladi
        finalize_product(chat_id)
        return
    if step == "edit_gender":
        pid = d.get("edit_pid")
        products = get_products()
        for p in products:
            if str(p["id"]) == str(pid):
                p["gender"] = key
                break
        save_products(products)
        db.delete_admin_step(chat_id)
        bot.answer_callback_query(call.id, "✅")
        kill_kb(call)
        safe_send(chat_id, f"✅ Jins yangilandi: {GENDER_LABEL[key]}", reply_markup=admin_menu())
        return
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "add_back")
def add_back_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    kill_kb(call)          # eski tugmalar yo'qoladi
    chat_id = call.message.chat.id
    if not add_step_back(chat_id):
        # Qo'shish oqimi emas (masalan jins tahriri) — bekor qilamiz
        if db.admin_step_exists(chat_id):
            db.delete_admin_step(chat_id)
        safe_send(chat_id, "❌ Bekor qilindi.", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data.startswith("setprice_"))
def set_price_cb(call):
    if not is_admin(call.from_user.id):
        return
    chat_id = call.message.chat.id
    val = call.data.replace("setprice_", "")
    if val == "custom":
        bot.answer_callback_query(call.id)
        kill_kb(call)      # narx tugmalari yo'qoladi
        safe_send(chat_id, "✏️ Sotuv narxini o'zingiz yozing (raqam):",
                  reply_markup=add_back_menu(chat_id))
        # price_choose step'ida qoladi, qo'lda yozadi
        return
    try:
        price = int(val)
    except:
        bot.answer_callback_query(call.id, "Xato")
        return
    kill_kb(call)          # narx tanlandi -> tugmalar yo'qoladi
    db.update_admin_step(chat_id, "price", price)
    db.update_admin_step(chat_id, "step", "desc_uz")
    bot.answer_callback_query(call.id, f"✅ {price:,} so'm")
    safe_send(chat_id, f"✅ Sotuv narxi: {price:,} so'm\n\nTavsifni o'zbekcha yozing:",
              reply_markup=add_back_menu(chat_id))

@bot.callback_query_handler(func=lambda c: c.data.startswith("delprod_"))
def delete_product_cb(call):
    if not is_admin(call.from_user.id):
        return
    pid = call.data.replace("delprod_", "")
    product = next((p for p in get_products() if str(p["id"]) == pid), None)
    if not product:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    total = product_total_stock(product)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Ha, o'chir", callback_data=f"delyes_{pid}"))
    kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="delno"))
    try:
        bot.edit_message_text(
            f"🗑 '{product['name_uz']}' ({total} ta) — butunlay o'chirilsinmi?\n"
            f"Bu amal qaytmaydi.",
            call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id,
                  f"🗑 '{product['name_uz']}' — o'chirilsinmi?", reply_markup=kb)
    bot.answer_callback_query(call.id)

def _del_list_kb():
    kb = types.InlineKeyboardMarkup()
    for p in get_products():
        total = product_total_stock(p)
        kb.add(types.InlineKeyboardButton(f"🗑 {p['name_uz']} ({total} ta)",
                                          callback_data=f"delprod_{p['id']}"))
    return kb

@bot.callback_query_handler(func=lambda c: c.data.startswith("delyes_"))
def delete_product_yes_cb(call):
    if not is_admin(call.from_user.id):
        return
    pid = call.data.replace("delyes_", "")
    products = get_products()
    product = next((p for p in products if str(p["id"]) == pid), None)
    if not product:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    name = product.get("name_uz", "")
    remaining_list = [p for p in products if str(p["id"]) != pid]
    write_json_local(PRODUCTS_FILE, remaining_list)
    pushed, why = gh_push_json_sync(PRODUCTS_FILE, "data/products.json", f"delete {name}")
    log.info(f"Mahsulot to'liq o'chirildi: {pid} ({name}), github={pushed} ({why})")
    bot.answer_callback_query(call.id, "🗑 O'chirildi")
    warn = "" if pushed else f"\n\n⚠️ GitHub zaxirasi yangilanmadi ({why}) — bot qayta ishga tushsa qaytishi mumkin."
    remaining = get_products()
    if remaining:
        try:
            bot.edit_message_text(f"✅ '{name}' o'chirildi.{warn}\n\n🗑 Yana o'chirish — papkani tanlang:",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=_cat_folders_kb("delfold_"))
        except Exception:
            safe_send(call.message.chat.id, f"✅ '{name}' o'chirildi.{warn}", reply_markup=admin_menu())
    else:
        try:
            bot.edit_message_text(f"✅ '{name}' o'chirildi.\n\nBoshqa mahsulot qolmadi.",
                                  call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        safe_send(call.message.chat.id, "📦 Mahsulot qolmadi.", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda c: c.data == "delno")
def delete_product_no_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.edit_message_text("🗑 O'chirish — papkani tanlang:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=_cat_folders_kb("delfold_"))
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Bekor qilindi")

@bot.message_handler(func=lambda m: m.text == "✏️ Mahsulot tahrirlash")
@admin_only
def edit_product_start(message):
    if not get_products():
        safe_send(message.chat.id, "Mahsulotlar yo'q.", reply_markup=admin_menu())
        return
    safe_send(message.chat.id, "✏️ Tahrirlash — papkani tanlang:",
              reply_markup=_cat_folders_kb("editfold_"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("editfold_"))
def edit_fold_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        idx = int(call.data.replace("editfold_", ""))
    except ValueError:
        bot.answer_callback_query(call.id); return
    cats, by_cat = _prod_categories()
    if idx < 0 or idx >= len(cats):
        bot.answer_callback_query(call.id, "Papka topilmadi"); return
    c = cats[idx]
    kb = types.InlineKeyboardMarkup()
    for p in by_cat[c]:
        kb.add(types.InlineKeyboardButton(
            f"✏️ {p['name_uz']} — {int(p['price']):,} so'm",
            callback_data=f"editprod_{p['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Papkalar", callback_data="editfoldback"))
    try:
        bot.edit_message_text(f"📂 {c} — tahrirlash uchun mahsulotni tanlang:",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        safe_send(call.message.chat.id, f"📂 {c}:", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "editfoldback")
def edit_fold_back_cb(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.edit_message_text("✏️ Tahrirlash — papkani tanlang:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=_cat_folders_kb("editfold_"))
    except Exception:
        safe_send(call.message.chat.id, "✏️ Papkalar:", reply_markup=_cat_folders_kb("editfold_"))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("editprod_"))
def edit_product_choose_cb(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    pid = call.data.replace("editprod_", "")
    product = next((p for p in get_products() if str(p["id"]) == pid), None)
    if not product:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    db.set_admin_step(call.message.chat.id, {"step": "edit_choose_field", "edit_pid": pid})
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id, f"✏️ {product['name_uz']}\n\nQaysi maydonni tahrirlaysiz?",
              reply_markup=edit_field_buttons(pid))

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_") and not c.data.startswith("editprod_"))
def edit_field_callback(call):
    if not is_admin(call.from_user.id):
        return
    kill_kb(call)
    parts  = call.data.split("_")
    action = "_".join(parts[1:-1])
    pid    = parts[-1]
    prompts = {
        "name_uz": "Yangi nomni o'zbekcha yozing:",
        "name_ru": "Yangi nomni ruscha yozing:",
        "price":   "Yangi narxni yozing (raqam):",
        "stock":   "Yangi ombor sonini yozing (raqam):",
        "cost":    "Yangi tan narxni yozing (raqam):",
        "category": "Yangi kategoriya nomini yozing:",
        "desc_uz": "Yangi tavsifni o'zbekcha yozing:",
        "desc_ru": "Yangi tavsifni ruscha yozing:",
        "photo":   "Yangi rasmni yuboring:",
    }
    if action == "sizes":
        product = next((p for p in get_products() if str(p["id"]) == str(pid)), None)
        if not product:
            bot.answer_callback_query(call.id, "Topilmadi"); return
        cur = [s.get("label") for s in (product.get("sizes") or [])]
        db.set_admin_step(call.message.chat.id,
                          {"step": "edit_sizes_pick", "edit_pid": pid, "size_labels": cur})
        have = (" · ".join(f"{s.get('label')}:{int(s.get('stock',0))}" for s in (product.get("sizes") or []))
                or "hozircha yo'q")
        safe_send(call.message.chat.id,
                  f"📏 '{product['name_uz']}'\nHozirgi razmerlar: {have}\n\n"
                  f"Kerakli razmerlarni belgilang, so'ng '✅ Tayyor':",
                  reply_markup=size_pick_keyboard(cur))
        bot.answer_callback_query(call.id)
        return
    if action == "gender":
        db.set_admin_step(call.message.chat.id, {"step": "edit_gender", "edit_pid": pid})
        safe_send(call.message.chat.id, "👶 Yangi jinsni tanlang:", reply_markup=gender_keyboard())
        bot.answer_callback_query(call.id)
        return
    if action == "stock":
        product = next((p for p in get_products() if str(p["id"]) == str(pid)), None)
        sizes = (product.get("sizes") if product else None) or []
        if sizes:
            db.set_admin_step(call.message.chat.id, {"step": "edit_restock", "edit_pid": pid})
            cur = " · ".join(f"{s.get('label')}:{int(s.get('stock',0))}" for s in sizes)
            labels = [s.get("label") for s in sizes]
            example = " ".join(["6", "3", "0", "8", "4", "2", "3", "1", "2", "1", "1"][:len(labels)])
            safe_send(call.message.chat.id,
                      f"📏 Hozirgi ombor: {cur}\n\n"
                      f"Har razmerga YANGI sonini shu tartibda yozing (probel bilan):\n"
                      f"{' · '.join(labels)}\n"
                      f"Masalan: {example}",
                      reply_markup=step_back_menu(call.message.chat.id))
            bot.answer_callback_query(call.id)
            return
    if action in prompts:
        db.set_admin_step(call.message.chat.id, {"step": f"edit_save_{action}", "edit_pid": pid})
        safe_send(call.message.chat.id, prompts[action], reply_markup=step_back_menu(call.message.chat.id))
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "💬 Fikrlar navbati")
@admin_only
def pending_reviews_list(message):
    pending = get_pending_reviews()
    if not pending:
        safe_send(message.chat.id, "Tasdiqlanmagan fikrlar yo'q.", reply_markup=admin_menu())
        return
    for r in pending:
        text = f"Yangi fikr:\n\n{r.get('text','')}\n\nID: {r['id']}"
        if r.get("photo_id"):
            safe_photo(message.chat.id, r["photo_id"], caption=text,
                       reply_markup=review_confirm_buttons(r["id"]))
        else:
            safe_send(message.chat.id, text, reply_markup=review_confirm_buttons(r["id"]))

# ─── Buyurtmalar ro'yxati: kunlik / haftalik / oylik / butun davr ────────────

_PERIODS = {
    "day":   ("📅 Bugun",       lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0)),
    "week":  ("🗓 7 kun",        lambda now: now - timedelta(days=7)),
    "month": ("📆 30 kun",       lambda now: now - timedelta(days=30)),
    "all":   ("♾ Butun davr",   lambda now: None),
}
_PERIOD_ORDER = ["day", "week", "month", "all"]

def _orders_in_period(period):
    """Sana bo'yicha filtr. Sanalar naive yoziladi — naive now bilan solishtiramiz."""
    since = _PERIODS[period][1](datetime.now())
    out = []
    for o in load_json(ORDERS_FILE, []):
        raw = str(o.get("date", ""))
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
        except Exception:
            continue
        if since is None or dt >= since:
            out.append((dt, o))
    out.sort(key=lambda x: x[0], reverse=True)
    return [o for _, o in out]

def orders_filter_kb(active):
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = []
    for p in _PERIOD_ORDER:
        label = _PERIODS[p][0]
        if p == active:
            label = "• " + label + " •"
        btns.append(types.InlineKeyboardButton(label, callback_data=f"ordf_{p}"))
    kb.add(*btns)
    return kb

def orders_report(chat_id, period):
    orders = _orders_in_period(period)
    title = _PERIODS[period][0]
    if not orders:
        return f"📋 Buyurtmalar — {title}\n\n📭 Bu davrda buyurtma yo'q."

    live = [o for o in orders if o.get("status") != "cancelled"]
    paid = [o for o in live if _order_paid(o)]
    revenue = sum(int(o.get("total", 0) or 0) for o in paid)
    pending = len(live) - len(paid)
    cancelled = len(orders) - len(live)

    txt = (f"📋 Buyurtmalar — {title}\n\n"
           f"🧾 Jami: {len(orders)} ta\n"
           f"💰 To'langan: {len(paid)} ta — {revenue:,} so'm\n"
           f"⏳ Kutilmoqda: {pending} ta\n"
           f"❌ Bekor qilingan: {cancelled} ta\n\n")

    for o in orders[:15]:
        num = o.get("number")
        head = f"#{int(num):05d}" if num else str(o.get("id"))[:6]
        d = str(o.get("date", ""))[:10]
        if o.get("status") == "cancelled":
            mark = "❌"
        elif _order_paid(o):
            mark = "💰"
        else:
            mark = "⏳"
        txt += (f"{mark} {head} · {(o.get('name') or '')[:14]} · "
                f"{int(o.get('total', 0) or 0):,} so'm · {d}\n")
    if len(orders) > 15:
        txt += f"\n… va yana {len(orders) - 15} ta"
    return txt


@bot.message_handler(func=lambda m: m.text == "📋 Buyurtmalar")
@admin_only
def orders_list(message):
    safe_send(message.chat.id, orders_report(message.chat.id, "day"),
              reply_markup=orders_filter_kb("day"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("ordf_"))
def orders_filter_cb(call):
    if not is_admin(call.from_user.id):
        return
    period = call.data.replace("ordf_", "")
    if period not in _PERIODS:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    _edit_or_send(call, orders_report(call.message.chat.id, period), orders_filter_kb(period))

# ─── Admin steps ──────────────────────────────────────────────────────────────

def handle_admin_steps(message):
    chat_id   = message.chat.id
    step_data = db.get_admin_step(chat_id)
    if not step_data:
        return
    step = step_data.get("step", "")

    # ⬅️ Orqaga — qo'shish oqimida bir qadam ortga
    if (message.text or "") == "⬅️ Orqaga":
        if add_step_back(chat_id):
            return

    # Yangi kategoriya qo'shish
    if step == "category_add":
        cat = (message.text or "").strip()
        if not cat or len(cat) > 30:
            safe_send(chat_id, "❌ Kategoriya nomi 1-30 belgi bo'lsin.")
            return
        if cat in get_category_list():
            safe_send(chat_id, f"⚠️ '{cat}' allaqachon bor.", reply_markup=admin_menu())
            db.delete_admin_step(chat_id)
            return
        db.set_admin_step(chat_id, {"step": "category_add_ru", "cat_name": cat})
        prompt_category_ru(chat_id, cat)
        return

    if step == "category_add_ru":
        d = db.get_admin_step(chat_id) or {}
        ru = (message.text or "").strip()
        if len(ru) > 30:
            safe_send(chat_id, "❌ Ruscha nom 1-30 belgi bo'lsin.")
            return
        d["cat_ru"] = ru
        d["step"] = "category_add_photo"
        db.set_admin_step(chat_id, d)
        prompt_category_photo(chat_id, d.get("cat_name", ""))
        return

    if step == "category_add_photo":
        d = db.get_admin_step(chat_id) or {}
        if message.photo:
            drop_kb_prompt(chat_id)          # tugmali so'rov chatdan ketsin
            finalize_category(chat_id, d.get("cat_name", ""), message.photo[-1].file_id,
                              d.get("cat_ru", ""))
        else:
            prompt_category_photo(chat_id, d.get("cat_name", ""))
        return

    if step == "cat_rename_ru":
        ru = (message.text or "").strip()
        if len(ru) > 30:
            safe_send(chat_id, "❌ Ruscha nom 1-30 belgi bo'lsin.")
            return
        cat = step_data.get("cat_name", "")
        if ru in ("-", "—"):
            ru = ""
        set_category_ru(cat, ru)
        db.delete_admin_step(chat_id)
        safe_send(chat_id, f"✅ '{cat}' → 🇷🇺 '{ru or 'olib tashlandi'}'", reply_markup=admin_menu())
        return

    if step == "cat_rename":
        new = (message.text or "").strip()
        if not new or len(new) > 30:
            safe_send(chat_id, "❌ Nom 1-30 belgi bo'lsin.")
            return
        old = step_data.get("cat_old", "")
        if new != old and new in get_category_list():
            safe_send(chat_id, f"⚠️ '{new}' allaqachon bor.", reply_markup=admin_menu())
            db.delete_admin_step(chat_id)
            return
        rename_category(old, new)
        db.delete_admin_step(chat_id)
        log.info(f"Kategoriya nomi o'zgartirildi: {old} -> {new}")
        safe_send(chat_id,
                  f"✅ '{old}' → '{new}' ga o'zgartirildi.\n"
                  f"(Shu kategoriyadagi barcha mahsulotlar ham yangilandi.)",
                  reply_markup=admin_menu())
        return

    if step == "cat_reimage":
        if not message.photo:
            safe_send(chat_id, "📸 Rasm yuboring (yoki 🏠 Asosiy menu).")
            return
        cat = step_data.get("cat_name", "")
        set_category_image(cat, message.photo[-1].file_id)
        db.delete_admin_step(chat_id)
        log.info(f"Kategoriya rasmi yangilandi: {cat}")
        safe_send(chat_id, f"✅ '{cat}' rasmi yangilandi.", reply_markup=admin_menu())
        return

    # Promo kod qo'shish
    if step == "promo_add_code":
        code = (message.text or "").strip().upper()
        if not code.isalnum():
            safe_send(chat_id, "❌ Kod faqat lotin harf va raqamlardan iborat bo'lishi kerak.")
            return
        db.update_admin_step(chat_id, "promo_code", code)
        db.update_admin_step(chat_id, "step", "promo_add_discount")
        safe_send(chat_id, f"Kod: {code}\n\nEndi chegirmani yozing (faqat raqam %):\nMasalan: 20")
        return

    if step == "promo_add_discount":
        try:
            discount = int((message.text or "").strip())
            if not 1 <= discount <= 100:
                raise ValueError
        except:
            safe_send(chat_id, "❌ 1 dan 100 gacha raqam kiriting.")
            return
        db.update_admin_step(chat_id, "promo_discount", discount)
        db.update_admin_step(chat_id, "step", "promo_add_uses")
        safe_send(chat_id, f"Chegirma: {discount}%\n\nNecha marta ishlatilsin?\n0 yozing = cheksiz")
        return

    if step == "promo_add_uses":
        try:
            uses = int((message.text or "").strip())
            uses = -1 if uses == 0 else uses
        except:
            safe_send(chat_id, "❌ Faqat raqam kiriting.")
            return
        d    = db.get_admin_step(chat_id)
        code = d.get("promo_code")
        disc = d.get("promo_discount")
        db.add_promo(code, disc, "percent", uses)
        db.delete_admin_step(chat_id)
        uses_text = "Cheksiz" if uses == -1 else f"{uses} ta"
        log.info(f"Promo qo'shildi: {code} {disc}% {uses_text}")
        safe_send(chat_id,
                  f"✅ Promo kod qo'shildi!\n\n📌 Kod: {code}\n💰 Chegirma: {disc}%\n🔢 Foydalanish: {uses_text}",
                  reply_markup=admin_menu())
        return

    # Promo kod tahrirlash
    if step == "promo_edit_discount":
        try:
            discount = int((message.text or "").strip())
            if not 1 <= discount <= 100:
                raise ValueError
        except:
            safe_send(chat_id, "❌ 1 dan 100 gacha raqam kiriting.")
            return
        db.update_admin_step(chat_id, "promo_discount", discount)
        db.update_admin_step(chat_id, "step", "promo_edit_uses")
        safe_send(chat_id, f"Yangi chegirma: {discount}%\n\nYangi foydalanish sonini yozing:\n0 = cheksiz")
        return

    if step == "promo_edit_uses":
        try:
            uses = int((message.text or "").strip())
            uses = -1 if uses == 0 else uses
        except:
            safe_send(chat_id, "❌ Faqat raqam kiriting.")
            return
        d    = db.get_admin_step(chat_id)
        code = d.get("promo_code")
        disc = d.get("promo_discount")
        db.update_promo(code, disc, uses)
        db.delete_admin_step(chat_id)
        uses_text = "Cheksiz" if uses == -1 else f"{uses} ta"
        log.info(f"Promo tahrirlandi: {code} {disc}% {uses_text}")
        safe_send(chat_id,
                  f"✅ Promo yangilandi!\n\n📌 Kod: {code}\n💰 Chegirma: {disc}%\n🔢 Foydalanish: {uses_text}",
                  reply_markup=admin_menu())
        return

    if step == "cashback_percent":
        try:
            percent = int((message.text or "").strip())
            if percent < 0 or percent > 100:
                safe_send(chat_id, "Foiz 0 dan 100 gacha bo'lishi kerak.")
                return
        except ValueError:
            safe_send(chat_id, "Iltimos, faqat raqam yozing (masalan: 5).")
            return
        db.set_setting("cashback_percent", str(percent))
        db.delete_admin_step(chat_id)
        safe_send(chat_id, f"✅ Cashback foizi {percent}% ga o'zgartirildi.", reply_markup=admin_menu())
        return

    if step == "broadcast":
        # Xabarni saqlaymiz, tasdiq so'raymiz (darrov yubormaymiz)
        bc = {"text": message.text or message.caption or "",
              "photo": message.photo[-1].file_id if message.photo else ""}
        db.set_admin_step(chat_id, {"step": "broadcast_confirm", "bc": bc})
        users = db.get_user_count()
        # Oldindan ko'rsatish (preview)
        preview = "📣 Xabar tayyor. Quyidagicha yuboriladi:\n\n— — —"
        safe_send(chat_id, preview)
        if bc["photo"]:
            safe_photo(chat_id, bc["photo"], caption=bc["text"])
        else:
            safe_send(chat_id, bc["text"])
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"✅ Yuborish ({users} kishiga)", callback_data="bc_send"))
        kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="bc_cancel"))
        safe_send(chat_id, "— — —\nYuborilsinmi?", reply_markup=kb)
        return

    # Sozlama
    if step == "setting_shop_loc":
        if message.location:
            lat = message.location.latitude
            lon = message.location.longitude
        else:
            # Qo'lda koordinata yozish (masalan: 41.3111, 69.2797)
            txt = (message.text or "").replace(" ", "")
            try:
                parts = txt.split(",")
                lat = float(parts[0])
                lon = float(parts[1])
            except:
                safe_send(chat_id, "📍 Lokatsiya yuboring (tugma orqali) yoki koordinata yozing.\nMasalan: 41.3111, 69.2797")
                return
        db.set_setting("shop_lat", str(lat))
        db.set_setting("shop_lon", str(lon))
        db.delete_admin_step(chat_id)
        log.info(f"Do'kon lokatsiyasi: {lat}, {lon}")
        safe_send(chat_id,
                  f"✅ Do'kon lokatsiyasi saqlandi!\n"
                  f"📍 {lat:.5f}, {lon:.5f}\n\n"
                  f"Endi mijoz lokatsiya yuborsa, masofaga qarab yetkazib berish hisoblanadi.",
                  reply_markup=admin_menu())
        return

    if step.startswith("setting_"):
        db_key = step.replace("setting_", "")
        value  = (message.text or "").strip()
        if db_key in ("delivery", "gift_box_price", "price_per_km", "qadoq_oddiy_cost", "gift_box_cost", "low_stock_threshold"):
            try:
                value = str(int(value.replace(" ", "")))
            except:
                safe_send(chat_id, "❌ Faqat raqam kiriting.")
                return
        db.set_setting(db_key, value)
        db.delete_admin_step(chat_id)
        log.info(f"Sozlama: {db_key} = {value}")
        safe_send(chat_id, f"✅ Saqlandi: {value}", reply_markup=admin_menu())
        return

    # Mahsulot tahrirlash: ID tanlash
    if step == "edit_choose_id":
        pid     = (message.text or "").strip()
        product = next((p for p in get_products() if str(p["id"]) == pid), None)
        if not product:
            safe_send(chat_id, "❌ Mahsulot topilmadi.")
            return
        db.update_admin_step(chat_id, "edit_pid", pid)
        db.update_admin_step(chat_id, "step", "edit_choose_field")
        safe_send(chat_id, f"✏️ {product['name_uz']}\n\nQaysi maydonni tahrirlaysiz?",
                  reply_markup=edit_field_buttons(pid))
        return

    # Mahsulot tahrirlash: qiymat saqlash
    if step == "edit_sizes_qty":
        pid = step_data.get("edit_pid")
        labels = step_data.get("size_labels", [])
        products = get_products()
        p = next((x for x in products if str(x["id"]) == str(pid)), None)
        if not p:
            db.delete_admin_step(chat_id)
            safe_send(chat_id, "❌ Mahsulot topilmadi.", reply_markup=admin_menu())
            return
        raw = (message.text or "").replace(",", " ").split()
        nums, ok = [], True
        for x in raw:
            try:
                nums.append(max(0, int(x)))
            except ValueError:
                ok = False
                break
        if not ok or len(nums) != len(labels):
            safe_send(chat_id,
                      f"❌ Aynan {len(labels)} ta son kerak (probel bilan).\n"
                      f"{' · '.join(labels)}")
            return
        p["sizes"] = [{"label": lb, "stock": nums[i]} for i, lb in enumerate(labels)]
        p["stock"] = sum(nums)
        p["age"] = sizes_to_ages(labels)      # yosh filtri razmerdan yangilanadi
        save_products(products)
        db.delete_admin_step(chat_id)
        log.info(f"Razmerlar yangilandi: {pid} -> {labels}")
        br = "\n".join(f"   {lb} — {nums[i]} ta" for i, lb in enumerate(labels))
        safe_send(chat_id,
                  f"✅ '{p['name_uz']}' razmerlari saqlandi!\n📏 Razmerlar:\n{br}\n"
                  f"📦 Jami: {sum(nums)} ta\n\nSaytda ~20 soniyada ko'rinadi.",
                  reply_markup=admin_menu())
        return

    if step == "edit_restock":
        pid = step_data.get("edit_pid")
        products = get_products()
        p = next((x for x in products if str(x["id"]) == str(pid)), None)
        if not p:
            db.delete_admin_step(chat_id)
            safe_send(chat_id, "❌ Mahsulot topilmadi.", reply_markup=admin_menu())
            return
        sizes = p.get("sizes") or []
        labels = [s.get("label") for s in sizes]
        raw = (message.text or "").replace(",", " ").split()
        nums, ok = [], True
        for x in raw:
            try:
                nums.append(max(0, int(x)))
            except ValueError:
                ok = False
                break
        if not ok or len(nums) != len(labels):
            example = " ".join(["6", "3", "0", "8", "4", "2", "3", "1", "2", "1", "1"][:len(labels)] or ["6"])
            safe_send(chat_id,
                      f"❌ Aynan {len(labels)} ta son kerak (probel bilan).\n"
                      f"{' · '.join(labels)}\nMasalan: {example}")
            return
        for i, s in enumerate(sizes):
            s["stock"] = nums[i]
        p["stock"] = sum(nums)
        save_products(products)
        db.delete_admin_step(chat_id)
        log.info(f"Ombor to'ldirildi (razmer): {pid}")
        br = "\n".join(f"   {s.get('label')} — {int(s.get('stock',0))} ta" for s in sizes)
        safe_send(chat_id, f"✅ Ombor yangilandi!\n📏 Razmerlar:\n{br}\n📦 Jami: {sum(nums)} ta",
                  reply_markup=admin_menu())
        return

    if step.startswith("edit_save_"):
        field    = step.replace("edit_save_", "")
        pid      = step_data.get("edit_pid")
        products = get_products()
        for p in products:
            if str(p["id"]) == str(pid):
                if field == "photo":
                    if message.photo:
                        # Galereyani ham yangilaymiz (bitta rasm bilan almashtiradi)
                        p["photo_id"] = message.photo[-1].file_id
                        p["photos"] = [message.photo[-1].file_id]
                    else:
                        safe_send(chat_id, "❌ Rasm yuboring.")
                        return
                elif field == "price":
                    try:
                        p["price"] = int((message.text or "").replace(" ", ""))
                    except:
                        safe_send(chat_id, "❌ Faqat raqam.")
                        return
                elif field == "stock":
                    try:
                        p["stock"] = max(0, int((message.text or "").strip()))
                    except:
                        safe_send(chat_id, "❌ Faqat raqam (masalan: 10).")
                        return
                elif field == "cost":
                    try:
                        p["cost"] = max(0, int((message.text or "").replace(" ", "")))
                    except:
                        safe_send(chat_id, "❌ Faqat raqam.")
                        return
                else:
                    p[field] = message.text or ""
                break
        save_products(products)
        db.delete_admin_step(chat_id)
        log.info(f"Mahsulot tahrirlandi: {pid}, {field}")
        safe_send(chat_id, "✅ Yangilandi.", reply_markup=admin_menu())
        return

    # Mahsulot qo'shish
    if step == "name_uz":
        db.update_admin_step(chat_id, "name_uz", message.text)
        db.update_admin_step(chat_id, "step", "name_ru")
        safe_send(chat_id, "Nomni ruscha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "name_ru":
        db.update_admin_step(chat_id, "name_ru", message.text)
        db.update_admin_step(chat_id, "step", "cost")
        safe_send(chat_id, "💵 Tan narxini yozing (sizga necha pulga tushgan, raqam):",
                  reply_markup=add_back_menu(chat_id))
    elif step == "cost":
        try:
            cost = int((message.text or "").replace(" ", ""))
            if cost <= 0:
                safe_send(chat_id, "❌ Narx 0 dan katta bo'lsin.")
                return
        except:
            safe_send(chat_id, "❌ Faqat raqam yozing.")
            return
        db.update_admin_step(chat_id, "cost", cost)
        # Formula bo'yicha narx variantlarini taklif qilamiz
        base, variants = suggest_prices(cost)
        kb = types.InlineKeyboardMarkup()
        for label, val in variants:
            kb.add(types.InlineKeyboardButton(f"{val:,} so'm", callback_data=f"setprice_{val}"))
        kb.add(types.InlineKeyboardButton("✏️ Boshqa narx yozaman", callback_data="setprice_custom"))
        txt = (f"💡 Tan narx: {cost:,} so'm\n"
               f"📐 Formula: (tan narx × 2) + 30% = {int(round(base)):,} so'm\n\n"
               f"Sotuv narxini tanlang:")
        safe_send(chat_id, txt, reply_markup=kb)
        db.update_admin_step(chat_id, "step", "price_choose")
    elif step == "price_choose":
        # Agar admin "boshqa narx" tanlab, qo'lda yozsa
        try:
            price = int((message.text or "").replace(" ", ""))
            db.update_admin_step(chat_id, "price", price)
            db.update_admin_step(chat_id, "step", "desc_uz")
            safe_send(chat_id, f"✅ Sotuv narxi: {price:,} so'm\n\nTavsifni o'zbekcha yozing:",
                      reply_markup=add_back_menu(chat_id))
        except:
            safe_send(chat_id, "❌ Narxni tugmadan tanlang yoki raqam yozing.")
    elif step == "price":
        try:
            db.update_admin_step(chat_id, "price", int((message.text or "").replace(" ", "")))
        except:
            safe_send(chat_id, "❌ Faqat raqam.")
            return
        db.update_admin_step(chat_id, "step", "desc_uz")
        safe_send(chat_id, "Tavsifni o'zbekcha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "desc_uz":
        db.update_admin_step(chat_id, "desc_uz", message.text)
        db.update_admin_step(chat_id, "step", "desc_ru")
        safe_send(chat_id, "Tavsifni ruscha yozing:", reply_markup=add_back_menu(chat_id))
    elif step == "desc_ru":
        db.update_admin_step(chat_id, "desc_ru", message.text)
        db.update_admin_step(chat_id, "step", "photo")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Rasmsiz davom etish", callback_data="photo_skip"))
        send_kb_prompt(chat_id, "📸 Rasm(lar)ni yuboring (bittalab yuborishingiz mumkin).\n\nRasm yuborgach, '✅ Tayyor' tugmasi chiqadi.", kb)
    elif step == "photo":
        d = db.get_admin_step(chat_id)
        photos = d.get("photos", [])
        txt = (message.text or "").lower().strip()

        if message.photo:
            # Rasm qo'shamiz
            photos.append(message.photo[-1].file_id)
            d["photos"] = photos
            db.set_admin_step(chat_id, d)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="photo_done"))
            kb.add(types.InlineKeyboardButton("🖼 Yana rasm yuboraman", callback_data="photo_more"))
            send_kb_prompt(chat_id, f"✅ {len(photos)}-rasm qabul qilindi.\n\nYana rasm yuborishingiz yoki 'Tayyor' bosishingiz mumkin.", kb)
            return
        elif txt in ("tayyor", "готово", "done"):
            # Matn bilan ham ishlaydi (eski usul)
            d["photo_id"] = photos[0] if photos else ""
            d["photos"] = photos
            d["step"] = "sizes_pick"
            d["size_labels"] = []
            db.set_admin_step(chat_id, d)
            safe_send(chat_id, f"📦 {len(photos)} ta rasm saqlandi.\n\n📏 Qaysi razmerlar bor? Tanlang:",
                      reply_markup=size_pick_keyboard([]))
            return
        elif txt == "skip":
            d["photo_id"] = ""
            d["photos"] = []
            d["step"] = "sizes_pick"
            d["size_labels"] = []
            db.set_admin_step(chat_id, d)
            safe_send(chat_id, "📏 Qaysi razmerlar bor? Tanlang:",
                      reply_markup=size_pick_keyboard([]))
            return
        else:
            kb = types.InlineKeyboardMarkup()
            if photos:
                kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="photo_done"))
            kb.add(types.InlineKeyboardButton("➡️ Rasmsiz davom etish", callback_data="photo_skip"))
            safe_send(chat_id, "Rasm yuboring yoki tugmani bosing:", reply_markup=kb)
            return
    elif step == "sizes_qty":
        d = db.get_admin_step(chat_id) or {}
        labels = d.get("size_labels", [])
        raw = (message.text or "").replace(",", " ").split()
        nums, ok = [], True
        for x in raw:
            try:
                nums.append(max(0, int(x)))
            except ValueError:
                ok = False
                break
        if not ok or len(nums) != len(labels):
            example = " ".join(["4", "2", "0", "5", "3", "1", "2", "3", "1", "2", "1"][:len(labels)] or ["4"])
            safe_send(chat_id,
                      f"❌ Aynan {len(labels)} ta son kerak (probel bilan).\n"
                      f"Razmerlar: {' · '.join(labels)}\n"
                      f"Masalan: {example}")
            return
        d["sizes"] = [{"label": lb, "stock": nums[i]} for i, lb in enumerate(labels)]
        d["stock"] = sum(nums)
        d["step"] = "category"
        db.set_admin_step(chat_id, d)
        all_cats = []
        for c in get_category_list() + list(get_categories().keys()):
            if c not in all_cats:
                all_cats.append(c)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row = []
        for cat in all_cats:
            row.append(cat)
            if len(row) == 2:
                kb.add(*row); row = []
        if row: kb.add(*row)
        kb.add(tr(chat_id, "home"))
        safe_send(chat_id, "📂 Kategoriyani tugmadan tanlang yoki yangi nom yozing:", reply_markup=kb)
    elif step == "stock":
        try:
            stock = int((message.text or "0").strip())
            if stock < 0:
                stock = 0
        except ValueError:
            safe_send(chat_id, "Iltimos, raqam yozing (masalan: 10).")
            return
        d = db.get_admin_step(chat_id)
        d["stock"] = stock
        d["step"] = "category"
        db.set_admin_step(chat_id, d)
        # Admin belgilagan kategoriyalar + mavjudlar (takrorsiz)
        all_cats = []
        for c in get_category_list() + list(get_categories().keys()):
            if c not in all_cats:
                all_cats.append(c)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row = []
        for cat in all_cats:
            row.append(cat)
            if len(row) == 2:
                kb.add(*row); row = []
        if row: kb.add(*row)
        kb.add("⬅️ Orqaga", tr(chat_id, "home"))
        safe_send(chat_id, "📂 Kategoriyani tugmadan tanlang yoki yangi nom yozing:",
                  reply_markup=kb)
    elif step == "category":
        category = (message.text or "Boshqa").strip()
        d = db.get_admin_step(chat_id)
        d["category"] = category
        d["step"] = "gender"
        db.set_admin_step(chat_id, d)
        safe_send(chat_id, "👶 Bu kim uchun? (jinsni tanlang)", reply_markup=gender_keyboard())
    elif step == "gender":
        safe_send(chat_id, "👆 Yuqoridagi tugmalardan jinsni tanlang.", reply_markup=gender_keyboard())
    elif step == "age":
        d = db.get_admin_step(chat_id) or {}
        safe_send(chat_id, "👆 Yoshni yuqoridagi tugmalardan belgilang, so'ng '✅ Tayyor'.",
                  reply_markup=age_keyboard(d.get("ages", [])))
    elif step == "delete_id":
        pid = (message.text or "").strip()
        save_products([p for p in get_products() if str(p["id"]) != pid])
        db.delete_admin_step(chat_id)
        log.info(f"Mahsulot o'chirildi: {pid}")
        safe_send(chat_id, "✅ O'chirildi.", reply_markup=admin_menu())

# ─── Order steps ──────────────────────────────────────────────────────────────

def handle_order_steps(message):
    chat_id = message.chat.id
    order   = db.get_order(chat_id)
    step    = order.get("step")

    # Qidiruv
    if step == "search":
        query    = (message.text or "").strip().lower()
        results  = [p for p in get_products()
                    if query in p["name_uz"].lower() or query in p["name_ru"].lower()
                    or query in p.get("desc_uz","").lower() or query in p.get("desc_ru","").lower()]
        db.delete_order(chat_id)
        if not results:
            safe_send(chat_id, tr(chat_id, "search_empty"),
                      reply_markup=catalog_keyboard(chat_id, db.get_catalog_page(chat_id)))
            return
        safe_send(chat_id, tr(chat_id, "search_found", count=len(results)),
                  reply_markup=catalog_keyboard(chat_id, db.get_catalog_page(chat_id)))
        for p in results[:5]:
            send_product_card(chat_id, p)
        return

    if step == "review":
        review_id = str(uuid.uuid4())
        stars = db.get_order(chat_id).get("stars", 5)
        review = {
            "id": review_id, "user_id": message.from_user.id,
            "name": message.from_user.first_name or "Mijoz",
            "text": message.caption or message.text or "",
            "photo_id": message.photo[-1].file_id if message.photo else "",
            "stars": stars
        }
        pending = get_pending_reviews()
        pending.append(review)
        save_pending_reviews(pending)
        for admin_id in get_all_admins():
            txt = f"Yangi fikr: {'⭐' * stars} ({stars}/5)\n\n{review['text']}\n\nID: {review_id}"
            if review["photo_id"]:
                safe_photo(admin_id, review["photo_id"], caption=txt,
                           reply_markup=review_confirm_buttons(review_id))
            else:
                safe_send(admin_id, txt, reply_markup=review_confirm_buttons(review_id))
        db.delete_order(chat_id)
        safe_send(chat_id, tr(chat_id, "thanks_review"), reply_markup=back_menu(chat_id))

    elif step == "name":
        db.update_order(chat_id, "name", (message.text or "").replace("👤 ", ""))
        db.update_order(chat_id, "step", "phone")
        safe_send(chat_id, tr(chat_id, "phone"), reply_markup=phone_button(chat_id))

    elif step == "phone":
        phone = message.contact.phone_number if message.contact else message.text
        db.update_order(chat_id, "phone", phone)
        db.update_order(chat_id, "step", "location")
        safe_send(chat_id, tr(chat_id, "location"), reply_markup=location_button(chat_id))

    elif step == "location":
        if not message.location:
            safe_send(chat_id, tr(chat_id, "location"), reply_markup=location_button(chat_id))
            return
        lat = message.location.latitude
        lon = message.location.longitude
        db.update_order(chat_id, "lat", lat)
        db.update_order(chat_id, "lon", lon)
        # Masofaga qarab yetkazib berish summasini hisoblaymiz
        delivery_summa, km = calc_delivery(lat, lon)
        db.update_order(chat_id, "delivery_summa", delivery_summa)
        if km is not None:
            if lang(chat_id) == "ru":
                msg = (f"📍 Локация принята!\n"
                       f"📏 Расстояние: ~{km} км\n"
                       f"🚚 Доставка: {delivery_summa:,} сум")
            else:
                msg = (f"📍 Lokatsiya qabul qilindi!\n"
                       f"📏 Masofa: ~{km} km\n"
                       f"🚚 Yetkazib berish: {delivery_summa:,} so'm")
            safe_send(chat_id, msg, reply_markup=back_menu(chat_id))
        else:
            safe_send(chat_id, tr(chat_id, "location_ok"), reply_markup=back_menu(chat_id))
        db.update_order(chat_id, "step", "packaging")
        safe_send(chat_id, tr(chat_id, "packaging"), reply_markup=packaging_buttons(chat_id))

    elif step == "promo":
        text = (message.text or "").strip()
        skip_words = ["yoq", "yo'q", "нет", "no", "skip",
                      "promo kodsiz davom etish", "продолжить без промокода",
                      "➡️ promo kodsiz davom etish", "➡️ продолжить без промокода"]
        if text.lower() in skip_words:
            db.update_order(chat_id, "step", "payment")
            safe_send(chat_id, cart_text(chat_id, include_packaging=True))
            safe_send(chat_id, tr(chat_id, "payment"), reply_markup=payment_buttons(chat_id))
        else:
            promo = db.get_promo(text)
            if promo:
                subtotal = cart_total(chat_id) + get_order_delivery(chat_id) + packaging_price(chat_id)
                discount = int(subtotal * promo["discount"] / 100)
                db.update_order(chat_id, "promo_code",     promo["code"])
                db.update_order(chat_id, "promo_discount", discount)
                db.use_promo(promo["code"])
                db.update_order(chat_id, "step", "payment")
                disc_text = f"{promo['discount']}% (-{discount:,} so'm)"
                safe_send(chat_id, tr(chat_id, "promo_ok", discount=disc_text))
                safe_send(chat_id, cart_text(chat_id, include_packaging=True))
                safe_send(chat_id, tr(chat_id, "payment"), reply_markup=payment_buttons(chat_id))
                log.info(f"Promo ishlatildi: {promo['code']} user={chat_id}")
            else:
                safe_send(chat_id, tr(chat_id, "promo_no"))


# ─── Universal handler ────────────────────────────────────────────────────────

# ─── Universal «⬅️ Orqaga» ───────────────────────────────────────────────────
# Har qanday bosqichda bir qadam ortga qaytaradi. Qaytadigan joy bo'lmasa —
# tegishli menyuga chiqaradi. all_steps'dan OLDIN ro'yxatdan o'tishi shart.

BACK_TEXTS = ("⬅️ Orqaga", "⬅️ Назад", "⬅️ Ortga")
ORDER_FLOW = ["name", "phone", "location"]

def prompt_order_step(chat_id, step):
    if step == "name":
        safe_send(chat_id, tr(chat_id, "name"), reply_markup=order_back_menu(chat_id))
    elif step == "phone":
        safe_send(chat_id, tr(chat_id, "phone"), reply_markup=phone_button(chat_id))
    elif step == "location":
        safe_send(chat_id, tr(chat_id, "location"), reply_markup=location_button(chat_id))

def order_step_back(chat_id, user_id):
    """Mijoz buyurtma oqimida bir qadam ortga. Savat saqlanadi."""
    o = db.get_order(chat_id) or {}
    step = o.get("step", "")
    if step not in ORDER_FLOW or step == ORDER_FLOW[0]:
        _refund_promo_code(o.get("promo_code", ""))
        db.delete_order(chat_id)
        msg = ("↩️ Оформление отменено. Корзина сохранена."
               if lang(chat_id) == "ru" else
               "↩️ Rasmiylashtirish to'xtatildi. Savatingiz saqlandi.")
        safe_send(chat_id, msg, reply_markup=main_menu(chat_id, user_id))
        return
    prev = ORDER_FLOW[ORDER_FLOW.index(step) - 1]
    db.update_order(chat_id, "step", prev)
    prompt_order_step(chat_id, prev)

def admin_step_back(chat_id):
    """Admin bosqichini bekor qilib, kelib chiqqan menyusiga qaytaradi."""
    d = db.get_admin_step(chat_id) or {}
    step = str(d.get("step", ""))
    db.delete_admin_step(chat_id)
    try:
        drop_kb_prompt(chat_id)
    except Exception:
        pass
    if step.startswith("setting_") or step == "cashback_percent":
        show_settings(chat_id)
    elif step.startswith("cat"):            # category_add / category_add_ru / cat_rename / cat_reimage
        show_categories(chat_id)
    elif step.startswith("promo"):
        show_promo_list(chat_id)
    elif step.startswith("edit_"):
        safe_send(chat_id, "✏️ Tahrirlash bekor qilindi.", reply_markup=admin_menu())
    elif step.startswith("broadcast"):
        safe_send(chat_id, "📣 Broadcast bekor qilindi.", reply_markup=admin_menu())
    else:
        safe_send(chat_id, "❌ Bekor qilindi.", reply_markup=admin_menu())


@bot.message_handler(func=lambda m: (m.text or "") in BACK_TEXTS)
def universal_back(message):
    chat_id = message.chat.id
    uid = message.from_user.id
    try:
        if db.admin_step_exists(chat_id) and is_admin(uid):
            if add_step_back(chat_id):      # mahsulot qo'shish oqimi — o'z qadamiga
                return
            admin_step_back(chat_id)
            return
        if db.order_exists(chat_id):
            order_step_back(chat_id, uid)
            return
    except Exception as e:
        log.error(f"universal_back({chat_id}): {e}")
    safe_send(chat_id, tr(chat_id, "home"), reply_markup=main_menu(chat_id, uid))


@bot.message_handler(content_types=["text", "contact", "location", "photo", "document"])
def all_steps(message):
    chat_id = message.chat.id
    # Asosiy menyu tugmalari bosilsa, "yopishib qolgan" step'ni tozalaymiz
    menu_buttons = [
        "📊 Statistika", "⚙️ Sozlamalar", "➕ Mahsulot qo'shish",
        "📦 Mahsulotlar ro'yxati", "🗑 Mahsulot o'chirish", "✏️ Mahsulot tahrirlash",
        "📣 Broadcast", "🎁 Promo kodlar", "🚚 Buyurtma holati", "🕐 Ish vaqti",
        "💰 Cashback boshqaruvi", "📋 Buyurtmalar", "👑 Admin panel",
        "📄 Hisobot (PDF)", "📂 Kategoriyalar", "⚙️ Buyruqlar",
        "🏠 Asosiy menu", "🏠 Главное меню",
        "🛍 Katalog", "🛍 Каталог", "🛒 Savat", "🛒 Корзина",
        "⭐ Fikrlar", "⭐ Отзывы", "📞 Operator", "📞 Оператор",
        "🌐 Til", "🌐 Язык", "📦 Buyurtmalarim", "📦 Мои заказы",
        "💰 Cashback", "💰 Кешбэк",
    ]
    if message.text in menu_buttons:
        if db.admin_step_exists(chat_id):
            db.delete_admin_step(chat_id)
        if db.order_exists(chat_id):
            db.delete_order(chat_id)
        return  # menyu tugmasi o'z handleriga o'tadi
    try:
        if db.admin_step_exists(chat_id) and is_admin(message.from_user.id):
            handle_admin_steps(message)
            return
        if db.order_exists(chat_id):
            handle_order_steps(message)
    except Exception as e:
        log.error(f"all_steps({chat_id}): {e}")

# ─── Packaging & Payment ──────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "pack_back")
def packaging_back(call):
    """Qadoq bosqichidan lokatsiya bosqichiga qaytish."""
    chat_id = call.message.chat.id
    kill_kb(call)
    db.update_order(chat_id, "step", "location")
    safe_send(chat_id, tr(chat_id, "location"), reply_markup=location_button(chat_id))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("pack_") and c.data != "pack_back")
def packaging(call):
    chat_id = call.message.chat.id
    if call.data == "pack_gift":
        db.update_order(chat_id, "packaging_name",  tr(chat_id, "gift_box"))
        db.update_order(chat_id, "packaging_price", get_gift_box_price())
    else:
        db.update_order(chat_id, "packaging_name",  tr(chat_id, "brand_bag"))
        db.update_order(chat_id, "packaging_price", 0)
    db.update_order(chat_id, "step", "promo")
    kill_kb(call)          # qadoq tanlandi -> tugmalar yo'qoladi
    safe_send(chat_id, tr(chat_id, "promo_ask"), reply_markup=promo_skip_button(chat_id))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_") and c.data != "pay_back")
def payment(call):
    chat_id  = call.message.chat.id
    # Ish vaqti tekshiruvi — yakuniy tasdiqda ham
    if not db.is_working_hours():
        wh = db.get_work_hours()
        if lang(chat_id) == "ru":
            msg = (f"🕐 Извините, сейчас нерабочее время.\n\n"
                   f"Мы принимаем заказы с {wh['start_hour']}:00 до {wh['end_hour']}:00.\n\n"
                   f"Ваша корзина сохранится — оформите заказ в рабочее время! 🛒")
        else:
            msg = (f"🕐 Kechirasiz, hozir ish vaqti emas.\n\n"
                   f"Buyurtmalarni {wh['start_hour']}:00 dan {wh['end_hour']}:00 gacha qabul qilamiz.\n\n"
                   f"Savatingiz saqlanib qoladi — buyurtmani ish vaqtida rasmiylashtiring! 🛒")
        safe_send(chat_id, msg, reply_markup=main_menu(chat_id, call.from_user.id))
        bot.answer_callback_query(call.id)
        return
    # OXIRGI ombor tekshiruvi — savat to'ldirilgandan beri 5-10 daqiqa o'tgan
    # bo'lishi mumkin, boshqa mijoz (yoki sayt) shu tovarni olib ketgan bo'lishi mumkin.
    problems = cart_shortages(chat_id, refresh=True)
    if problems:
        kill_kb(call)
        txt = fix_cart_to_stock(chat_id, problems)
        db.delete_order(chat_id)
        safe_send(chat_id, txt, reply_markup=main_menu(chat_id, call.from_user.id))
        bot.answer_callback_query(call.id)
        return
    if not db.get_cart(chat_id):
        kill_kb(call)
        db.delete_order(chat_id)
        safe_send(chat_id, tr(chat_id, "cart_empty"), reply_markup=main_menu(chat_id, call.from_user.id))
        bot.answer_callback_query(call.id)
        return

    pay_type = call.data.replace("pay_", "")
    order_id = str(uuid.uuid4())
    kill_kb(call)          # to'lov tugmalari yo'qoladi
    db.update_order(chat_id, "pay_type", pay_type)

    # ── Onlayn to'lov (Payme / Click) ──
    # Ikkalasi ham server-server integratsiya: to'lov o'tsa buyurtma O'ZI tasdiqlanadi.
    # Shuning uchun chek so'ralmaydi va adminga tasdiq tugmasi YUBORILMAYDI —
    # to'lov kelganda web.py `_notify_admins_paid()` to'liq xabarni bir marta yuboradi.
    if pay_type in ("Payme", "Click"):
        db.update_order(chat_id, "order_id", order_id)
        number = send_order_to_admin(chat_id, pay_type, order_id)
        total  = final_total(chat_id)
        db.add_pending_order(order_id, chat_id)

        link = make_payme_link(number, total) if pay_type == "Payme" else make_click_link(number, total)
        if link:
            btn = "💳 Payme bilan to'lash" if pay_type == "Payme" else "🔵 Click bilan to'lash"
            icon = "💳" if pay_type == "Payme" else "🔵"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(btn, url=link))
            # Mijoz fikridan qaytsa — to'lov sahifasiga bormasdan bekor qila oladi
            kb.add(types.InlineKeyboardButton(cancel_label(chat_id),
                                              callback_data=f"cancelpay_{order_id}"))
            if lang(chat_id) == "ru":
                txt = (f"{icon} Оплата — {pay_type}\n"
                       f"🧾 Заказ #{number:05d}\n"
                       f"💰 {tr(chat_id,'total')}: {total:,} so'm\n\n"
                       f"Нажмите кнопку и оплатите безопасно. После оплаты заказ "
                       f"подтвердится автоматически — чек отправлять не нужно 👇")
            else:
                txt = (f"{icon} To'lov — {pay_type}\n"
                       f"🧾 Buyurtma #{number:05d}\n"
                       f"💰 {tr(chat_id,'total')}: {total:,} so'm\n\n"
                       f"Tugmani bosib xavfsiz to'lang. To'lovdan keyin buyurtmangiz "
                       f"avtomatik tasdiqlanadi — chek yuborish shart emas 👇")
            safe_send(chat_id, txt, reply_markup=kb)
        else:
            # Kassa ulanmagan — buyurtmani ushlab turmaymiz, bekor qilamiz.
            # (Tugma ham ko'rsatilmasligi kerak edi; bu — himoya qatlami.)
            log.error(f"{pay_type} kassasi sozlanmagan — buyurtma #{number} bekor qilindi")
            restore_order_stock(order_id, f"{pay_type} kassasi sozlanmagan", cancel=True)
            db.delete_pending_order(order_id)
            db.update_tracking(order_id, "cancelled")
            if lang(chat_id) == "ru":
                msg = (f"⚠️ Онлайн-оплата {pay_type} временно недоступна.\n\n"
                       f"Выберите «Наличные» или свяжитесь с оператором: {get_operator()}")
            else:
                msg = (f"⚠️ {pay_type} orqali onlayn to'lov vaqtincha ishlamayapti.\n\n"
                       f"«Naqd» ni tanlang yoki operator bilan bog'laning: {get_operator()}")
            safe_send(chat_id, msg, reply_markup=main_menu(chat_id, call.from_user.id))
            db.delete_order(chat_id)      # savat qoladi — «Naqd» bilan qayta urinsin
            bot.answer_callback_query(call.id)
            return

        db.clear_cart(chat_id)
        db.delete_order(chat_id)
        bot.answer_callback_query(call.id)
        return

    # ── Naqd: admin tasdiqlaydi (o'shanda tovar ombordan yechiladi) ──
    db.update_order(chat_id, "order_id", order_id)
    photos = order_photos(chat_id)
    number = send_order_to_admin(chat_id, "Naqd", order_id)
    db.add_pending_order(order_id, chat_id)
    safe_send(chat_id, tr(chat_id, "order_received"),
              reply_markup=main_menu(chat_id, call.from_user.id))
    text = order_text(chat_id, order_id, number)
    for admin_id in get_all_admins():
        try:
            kb = admin_confirm_buttons(order_id) if can_manage_status(admin_id) else None
            send_admin_order(admin_id, text, kb, photos)
        except Exception as e:
            log.error(f"Admin ({admin_id}) ga yuborishda xato: {e}")
    db.clear_cart(chat_id)
    db.delete_order(chat_id)
    bot.answer_callback_query(call.id)


# ─── To'lov bosqichida ortga / bekor qilish ──────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "pay_back")
def pay_back(call):
    """To'lov usuli tanlashdagi «⬅️ Ortga» — rasmiylashtirishni to'xtatadi,
    savat saqlanadi, ishlatilgan promo kod qaytariladi."""
    chat_id = call.message.chat.id
    kill_kb(call)
    o = db.get_order(chat_id) or {}
    _refund_promo_code(o.get("promo_code", ""))
    db.delete_order(chat_id)
    if lang(chat_id) == "ru":
        msg = "↩️ Оформление отменено. Корзина сохранена — можно продолжить в любой момент."
    else:
        msg = "↩️ Rasmiylashtirish to'xtatildi. Savatingiz saqlandi — istalgan vaqtda davom eting."
    safe_send(chat_id, msg, reply_markup=main_menu(chat_id, call.from_user.id))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancelpay_"))
def cancel_pay(call):
    """To'lov linki ostidagi «❌ Bekor qilish». To'lov o'tib bo'lgan bo'lsa — rad etadi."""
    chat_id  = call.message.chat.id
    order_id = call.data.replace("cancelpay_", "")

    o = next((x for x in load_json(ORDERS_FILE, []) if str(x.get("id")) == str(order_id)), None)
    paid = bool(o and (o.get("status") == "paid"
                       or (o.get("payme") or {}).get("state") == 2
                       or (o.get("click") or {}).get("state") == 2))
    if paid:
        alert = "To'lov allaqachon amalga oshdi" if lang(chat_id) != "ru" else "Оплата уже прошла"
        try:
            bot.answer_callback_query(call.id, alert, show_alert=True)
        except Exception:
            pass
        return

    kill_kb(call)
    try:
        restore_order_stock(order_id, "mijoz bekor qildi", cancel=True)
        db.update_tracking(order_id, "cancelled")
        db.delete_pending_order(order_id)
    except Exception as e:
        log.error(f"cancel_pay ({order_id}): {e}")

    if lang(chat_id) == "ru":
        msg = "❌ Заказ отменён. Деньги не списаны."
    else:
        msg = "❌ Buyurtma bekor qilindi. Pul yechilmadi."
    safe_send(chat_id, msg, reply_markup=main_menu(chat_id, call.from_user.id))
    bot.answer_callback_query(call.id)

# ─── Admin order confirm/reject ───────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_ok_"))
def admin_ok(call):
    if not can_manage_status(call.from_user.id):
        try: bot.answer_callback_query(call.id, "Faqat mas'ul admin tasdiqlaydi.")
        except Exception: pass
        return
    order_id = call.data.replace("admin_ok_", "")
    # Tovar AYNAN shu yerda ombordan yechiladi.
    # Bu tugma faqat Naqd va karta-fallback buyurtmalarida chiqadi —
    # onlayn Payme/Click da tovar to'lov o'tganda avtomatik yechiladi.
    ok, short = take_order_stock(order_id, "admin tasdiqladi")
    if not ok:
        txt = "⚠️ Omborda yetarli emas — buyurtma TASDIQLANMADI:\n\n"
        txt += "\n".join(f"• {n}: kerak {q}, bor {a}" for n, q, a in short)
        txt += "\n\nOmborni to'ldiring va qayta tasdiqlang, yoki «❌ Rad etish» bosing."
        try:
            bot.answer_callback_query(call.id, "Omborda yetarli emas", show_alert=True)
        except Exception:
            pass
        safe_send(call.message.chat.id, txt)
        return

    pending = db.get_pending_cashback(order_id)
    tracking = db.get_tracking(order_id)
    # Mijoz chat_id ni topamiz (pending_cashback yoki tracking'dan)
    user_id = None
    if pending:
        user_id = pending["chat_id"]
    elif tracking:
        user_id = tracking.get("chat_id")
    else:
        user_id = db.get_pending_order(order_id)

    db.update_tracking(order_id, "confirmed")
    if user_id:
        safe_send(user_id, tr(user_id, "pay_confirmed"), reply_markup=back_menu(user_id))
        award_cashback(order_id)  # Tasdiqlandi — cashback beriladi
        # Agar order hali ochiq bo'lsa, tozalaymiz (chek to'lov uchun)
        if db.order_exists(user_id):
            db.clear_cart(user_id)
            db.delete_order(user_id)
    db.delete_pending_order(order_id)
    log.info(f"Buyurtma tasdiqlandi: {order_id}")
    # Tasdiqlangач — saytdagidek buyurtma holati tugmalari chiqadi
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    safe_send(call.message.chat.id, "✅ Tasdiqlandi. Mijozga xabar yuborildi.\n\n📦 Endi buyurtma holatini boshqaring:",
              reply_markup=tracking_status_buttons(order_id))

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_no_"))
def admin_no(call):
    if not can_manage_status(call.from_user.id):
        try: bot.answer_callback_query(call.id, "Faqat mas'ul admin.")
        except Exception: pass
        return
    order_id = call.data.replace("admin_no_", "")
    user_id  = db.get_pending_order(order_id)
    db.update_tracking(order_id, "cancelled")
    try:
        restore_order_stock(order_id, "admin rad etdi", cancel=True)
    except Exception as e:
        log.error(f"stock restore xato ({order_id}): {e}")
    if user_id:
        safe_send(user_id, tr(user_id, "pay_rejected"), reply_markup=back_menu(user_id))
        db.delete_pending_order(order_id)
        db.delete_pending_cashback(order_id)  # rad etilganda cashback bekor
    log.info(f"Buyurtma rad: {order_id}")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    num = _order_number(order_id)
    head = f"#{int(num):03d}" if num else (order_id[:8] + "...")
    for aid in get_all_admins():
        try: safe_send(aid, f"📦 Buyurtma {head} — ❌ Bekor qilindi")
        except Exception: pass
    safe_send(call.message.chat.id, "❌ Rad etildi.")

# ─── Review confirm/reject ────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("review_ok_"))
def review_ok(call):
    if not is_admin(call.from_user.id):
        return
    review_id   = call.data.replace("review_ok_", "")
    pending     = get_pending_reviews()
    approved    = get_reviews()
    found, new_pending = None, []
    for r in pending:
        if str(r["id"]) == review_id:
            found = r
        else:
            new_pending.append(r)
    if found:
        approved.append(found)
        save_reviews(approved)
        save_pending_reviews(new_pending)
        log.info(f"Fikr tasdiqlandi: {review_id}")
        # Tugmalarni o'chirib, natijani ko'rsatamiz
        try:
            bot.edit_message_text(
                f"✅ Fikr tasdiqlandi\n\n⭐ {found.get('stars',5)}/5\n👤 {found.get('name','')}\n{found.get('text','')}",
                call.message.chat.id, call.message.message_id
            )
        except Exception:
            bot.answer_callback_query(call.id, "✅ Tasdiqlandi")
    else:
        try:
            bot.edit_message_text(
                "⚠️ Fikr allaqachon ko'rib chiqilgan.",
                call.message.chat.id, call.message.message_id
            )
        except Exception:
            bot.answer_callback_query(call.id, "Allaqachon ko'rib chiqilgan")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("review_no_"))
def review_no(call):
    if not is_admin(call.from_user.id):
        return
    review_id = call.data.replace("review_no_", "")
    pending = get_pending_reviews()
    found = next((r for r in pending if str(r["id"]) == review_id), None)
    save_pending_reviews([r for r in pending if str(r["id"]) != review_id])
    log.info(f"Fikr rad: {review_id}")
    # Tugmalarni o'chirib, natijani ko'rsatamiz
    try:
        txt = f"❌ Fikr rad etildi"
        if found:
            txt += f"\n\n👤 {found.get('name','')}\n{found.get('text','')[:100]}..."
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id)
    except Exception:
        bot.answer_callback_query(call.id, "❌ Rad etildi")
    bot.answer_callback_query(call.id)

# ─── Savatni eslatish ─────────────────────────────────────────────────────────

def review_loop():
    """Har soatda: 3 kun o'tgan delivered buyurtmalar uchun so'rovnoma yuboradi."""
    while True:
        time.sleep(60 * 60)
        try:
            now = datetime.now(TASHKENT_TZ)
            pending = get_pending_reviews()
            remaining = []
            for r in pending:
                try:
                    review_at = datetime.fromisoformat(r["review_at"])
                    if review_at.tzinfo is None:
                        from zoneinfo import ZoneInfo
                        review_at = review_at.replace(tzinfo=ZoneInfo("Asia/Tashkent"))
                    if now < review_at:
                        remaining.append(r); continue
                    chat_id = r["chat_id"]
                    name = r.get("name") or "mijoz"
                    if not chat_id:
                        continue
                    if lang(chat_id) == "ru":
                        msg = (f"💬 {name}, надеемся, что ваш заказ дошёл в отличном состоянии!\n\n"
                               f"Как вам понравились товары BabyDiary? Пожалуйста, поделитесь своим мнением — "
                               f"это помогает нам становиться лучше. 🌸\n\n"
                               f"Нажмите «✍️ Оставить отзыв» в меню.")
                    else:
                        msg = (f"💬 {name}, buyurtmangiz yetib borgani umid qilamiz!\n\n"
                               f"BabyDiary mahsulotlari sizga yoqdimi? Fikringizni qoldiring — "
                               f"bu bizni yaxshilashga yordam beradi. 🌸\n\n"
                               f"Menyudagi «✍️ Fikr yozish» tugmasini bosing.")
                    safe_send(chat_id, msg)
                    log.info(f"So'rovnoma yuborildi: {chat_id}")
                except Exception as e:
                    log.error(f"review_loop item xato: {e}")
                    remaining.append(r)
            save_pending_reviews(remaining)
        except Exception as e:
            log.error(f"review_loop xato: {e}")


def stale_order_loop():
    """Har 6 soatda: 2 kundan ko'p 'new' holatida qolgan buyurtmalarni adminlarga bildiradi."""
    while True:
        time.sleep(6 * 60 * 60)
        try:
            orders = load_json(ORDERS_FILE, [])
            now = datetime.now(TASHKENT_TZ)
            stale = []
            for o in orders:
                # faqat bot buyurtmalari (sayt buyurtmasi tracking orqali boshqariladi)
                date_str = (o.get("date") or "")[:10]
                if not date_str:
                    continue
                try:
                    order_date = datetime.fromisoformat(date_str).replace(tzinfo=TASHKENT_TZ)
                except Exception:
                    continue
                age_hours = (now - order_date).total_seconds() / 3600
                if age_hours < 48:
                    continue
                # tracking statusini tekshiramiz
                t = db.get_tracking(o.get("id"))
                if not t:
                    continue
                status = t.get("status", "new")
                if status in ("delivered", "cancelled"):
                    continue
                # 48+ soat, hali yakunlanmagan
                num = o.get("number")
                head = f"#{int(num):03d}" if num else (o.get("id", "")[:8] + "...")
                stale.append((head, status, date_str))

            if stale:
                lines = ["⏰ *Kutib turgan buyurtmalar (2 kundan ortiq):*\n"]
                for head, status, date in stale[:10]:
                    status_label = STATUS_TEXT["uz"].get(status, status)
                    lines.append(f"• {head} — {status_label} ({date})")
                text = "\n".join(lines)
                for aid in get_admins():
                    try: safe_send(aid, text, parse_mode="Markdown")
                    except Exception: pass
                log.info(f"Stale order alert: {len(stale)} ta")
        except Exception as e:
            log.error(f"stale_order_loop xato: {e}")


def cart_reminder_loop():
    """Har 1 soatda tashlab ketilgan savatlarni tekshirib, eslatma yuboradi."""
    while True:
        time.sleep(60 * 60)  # har soatda tekshiradi
        try:
            abandoned = db.get_abandoned_carts(hours=6)  # 6 soatdan oldingi
            for chat_id in abandoned:
                try:
                    if lang(chat_id) == "ru":
                        msg = ("🛒 Ваша корзина ждёт вас!\n\n"
                               "Вы добавили товары, но не завершили заказ. "
                               "Загляните — возможно, они вам всё ещё нужны? 😊")
                    else:
                        msg = ("🛒 Savatingiz sizni kutyapti!\n\n"
                               "Mahsulot qo'shgansiz, lekin buyurtmani yakunlamagansiz. "
                               "Bir ko'rib qo'ying — balki hali kerakdir? 😊")
                    safe_send(chat_id, msg)
                    db.mark_cart_reminded(chat_id)
                    time.sleep(0.1)
                except Exception as e:
                    log.error(f"Savat eslatish ({chat_id}) xato: {e}")
            if abandoned:
                log.info(f"Savat eslatma: {len(abandoned)} ta yuborildi")
        except Exception as e:
            log.error(f"cart_reminder_loop xato: {e}")

# ─── Buyurtma kuzatuvchisi: to'lov -> tasdiq -> cashback, va timeout ─────────
# 1) To'langan buyurtma (status="paid", web.py Payme/Click qo'yadi) avtomatik
#    tasdiqlanadi va cashback beriladi — admin tugma bosishini kutmaydi.
# 2) Payme tanlangan, lekin belgilangan vaqtda to'lanmagan buyurtma bekor
#    qilinadi: ombor, cashback va promo qaytariladi.
PAYME_HOLD_MIN = 5       # db sozlamasi: payme_hold_min

def _order_age_seconds(order):
    """Buyurtma yoshi (sekund). Sana naive yoziladi — o'sha soat bilan solishtiramiz."""
    raw = str(order.get("date", ""))
    if not raw:
        return -1
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return -1
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
        return (datetime.utcnow() - dt).total_seconds()
    return (datetime.now() - dt).total_seconds()

def _process_paid_orders():
    """status='paid' bo'lgan, hali ishlanmagan buyurtmalarni yakunlaydi."""
    for _ in range(20):                       # bir siklda ko'pi bilan 20 ta
        orders = load_json(ORDERS_FILE, [])
        o = next((x for x in orders
                  if x.get("status") == "paid" and not x.get("payment_processed")), None)
        if not o:
            return
        oid = str(o.get("id"))
        # Belgilaymiz — cashback ikki marta berilmasin
        o["payment_processed"] = True
        _save_orders_merged(orders)

        # Xavfsizlik: web.py to'lovda tovarni yechgan bo'lishi kerak. Yechilmagan
        # bo'lsa (xato/uzilish) shu yerda yechamiz.
        try:
            ok, short = take_order_stock(oid, "to'lov yakunlandi")
            if not ok:
                msg = ("🚨 DIQQAT: to'langan buyurtma, lekin omborda yetarli emas!\n\n"
                       + "\n".join(f"• {n}: kerak {q}, bor {a}" for n, q, a in short))
                for aid in get_all_admins():
                    try:
                        safe_send(aid, msg)
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"take_order_stock ({oid}): {e}")

        try:
            db.update_tracking(oid, "confirmed")
        except Exception as e:
            log.warning(f"tracking confirm ({oid}): {e}")
        cid = _order_chat_id(o)
        try:
            if o.get("source") == "sayt":
                if cid:
                    award_web_cashback(oid, cid)   # 'cashback_awarded' bayrog'i bilan
            else:
                award_cashback(oid)                # pending_cashback jadvalidan
        except Exception as e:
            log.error(f"cashback ({oid}): {e}")
        try:
            db.delete_pending_order(oid)
        except Exception:
            pass

        # Mijozga xabar + ASOSIY MENYUGA qaytaramiz (pastdagi «🏠» klaviaturasi)
        if cid:
            num = int(o.get("number") or 0)
            total = int(o.get("total", 0) or 0)
            if lang(cid) == "ru":
                msg = (f"✅ Оплата принята!\n\n"
                       f"🧾 Заказ #{num:05d} — {total:,} сум\n"
                       f"Заказ подтверждён автоматически. Скоро приготовим! 🧸")
            else:
                msg = (f"✅ To'lov qabul qilindi!\n\n"
                       f"🧾 Buyurtma #{num:05d} — {total:,} so'm\n"
                       f"Buyurtmangiz avtomatik tasdiqlandi. Tez orada tayyorlaymiz! 🧸")
            try:
                safe_send(cid, msg, reply_markup=main_menu(cid, cid))
            except Exception as e:
                log.error(f"paid xabar ({cid}): {e}")

        head = f"#{int(o.get('number')):03d}" if o.get("number") else (oid[:8] + "...")
        log.info(f"✅ To'lov yakunlandi: {head} ({oid})")

def _expire_unpaid_online():
    """Muddati o'tgan, to'lanmagan ONLAYN (Payme/Click) buyurtmalarini bekor qiladi.
    Tovar band emas (u to'lovda yechiladi) — bu yerda cashback/promo qaytariladi
    va buyurtma yopiladi, mijoz osilib qolmasin."""
    try:
        hold = int(db.get_setting("payme_hold_min") or PAYME_HOLD_MIN)
    except Exception:
        hold = PAYME_HOLD_MIN

    for o in load_json(ORDERS_FILE, []):
        pt = (o.get("pay_type") or "")
        if pt == "Payme" and not payme_enabled():
            continue                                    # karta rejimi — admin qo'lda tasdiqlaydi
        if pt == "Click" and not click_enabled():
            continue                                    # karta rejimi
        if pt not in ("Payme", "Click"):
            continue                                    # Naqd — tegmaymiz
        if o.get("status") in ("paid", "cancelled"):
            continue
        if (o.get("payme") or {}).get("state") == 2:    # to'langan
            continue
        if (o.get("click") or {}).get("state") == 2:    # to'langan
            continue

        # Mijoz AYNAN HOZIR to'lov ekranida (tranzaksiya yaratilgan) — vaqt beramiz.
        # Aks holda karta ma'lumotini kiritayotgan odamning buyurtmasi uzilib qoladi.
        pm_state = (o.get("payme") or {}).get("state")
        ck_state = (o.get("click") or {}).get("state")
        if pm_state == 1 or ck_state == 1:
            ct = int((o.get("payme") or {}).get("create_time") or 0) / 1000.0
            if not ct or (time.time() - ct) < hold * 60:
                continue

        if _order_age_seconds(o) < hold * 60:
            continue
        oid = str(o.get("id"))
        # Admin allaqachon qo'lda tasdiqlagan bo'lsa — tegmaymiz
        st = (db.get_tracking(oid) or {}).get("status", "new")
        if st != "new":
            continue

        if not restore_order_stock(oid, f"{pt}: to'lov {hold} daq. ichida kelmadi", cancel=True):
            continue
        try:
            db.update_tracking(oid, "cancelled")
            db.delete_pending_order(oid)
        except Exception:
            pass

        num = o.get("number")
        head = f"#{int(num):03d}" if num else (oid[:8] + "...")
        cid = _order_chat_id(o)
        if cid:
            if lang(cid) == "ru":
                msg = (f"⏳ Заказ {head} отменён — оплата не поступила в течение {hold} мин.\n\n"
                       f"Пожалуйста, оформите заказ заново.")
            else:
                msg = (f"⏳ Buyurtma {head} bekor qilindi — {hold} daqiqa ichida to'lov kelmadi.\n\n"
                       f"Iltimos, buyurtmani qaytadan bering.")
            safe_send(cid, msg)
        for aid in get_all_admins():
            try:
                safe_send(aid, f"⏳ Buyurtma {head} — to'lovsiz bekor qilindi.")
            except Exception:
                pass

_paid_scan_mtime = 0.0

def paid_watch_loop():
    """Har 5 soniyada to'langan buyurtmalarni yakunlaydi.
    Faqat orders.json o'zgargan bo'lsa ishlaydi (mtime) — bekorga yuk bermaydi.
    Shu tufayli mijoz Payme/Click'dan qaytganda bot uni allaqachon kutib turadi."""
    global _paid_scan_mtime
    while True:
        time.sleep(5)
        try:
            m = os.path.getmtime(ORDERS_FILE) if os.path.exists(ORDERS_FILE) else 0.0
            if m == _paid_scan_mtime:
                continue
            _paid_scan_mtime = m
            _process_paid_orders()
        except Exception as e:
            log.error(f"paid_watch_loop xato: {e}")

def expire_loop():
    """Har daqiqada muddati o'tgan onlayn buyurtmalarni yopadi.
    (Interval hold vaqtidan ancha kichik bo'lishi shart.)"""
    while True:
        time.sleep(60)
        try:
            _expire_unpaid_online()
        except Exception as e:
            log.error(f"expire_loop xato: {e}")

# ─── Avtomatik kunlik backup (har kuni soat 00:00, Toshkent) ──────────────────

def auto_backup_loop():
    """Har kuni soat 00:00 (Toshkent vaqti) da barcha ma'lumotni adminlarga yuboradi."""
    while True:
        # Toshkent vaqti bilan keyingi 00:00 gacha qancha qolganini hisoblaymiz
        now = datetime.now(TASHKENT_TZ)
        # Ertangi kun 00:00:00
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        log.info(f"Keyingi backup {wait_seconds/3600:.1f} soatdan keyin (00:00 Toshkent)")
        time.sleep(wait_seconds)
        # 00:00 bo'ldi — backup + PDF hisobot yuboramiz
        try:
            today_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y")
            admins = get_admins()
            # Asosiy admin — backup fayllari faqat shunga boradi
            MAIN_ADMIN_ID = 5285940949
            main_admin = MAIN_ADMIN_ID if MAIN_ADMIN_ID in [int(a) for a in admins] else (admins[0] if admins else None)

            # Chiroyli PDF hisobot — HAMMA adminlarga
            pdf_path = generate_pdf_report("Kunlik")
            for admin_id in admins:
                if pdf_path and os.path.exists(pdf_path):
                    try:
                        with open(pdf_path, "rb") as f:
                            bot.send_document(admin_id, f,
                                visible_file_name=f"BabyDiary_hisobot_{today_str}.pdf",
                                caption=f"📊 Kunlik hisobot — {today_str}")
                    except Exception as e:
                        log.error(f"PDF yuborish xato ({admin_id}): {e}")

            # Backup fayllari (JSON, baza) — FAQAT asosiy adminga
            if main_admin:
                sent = do_export(main_admin)
                if sent:
                    safe_send(main_admin, f"💾 Backup fayllari — {today_str}, soat 00:00")
            log.info("Avtomatik PDF (hamma) + backup (asosiy admin) yuborildi (00:00)")
        except Exception as e:
            log.error(f"Avtomatik backup xato: {e}")
        # Bir oz kutamiz (00:00 ni ikki marta o'tkazib yubormaslik uchun)
        time.sleep(60)

# ─── Run ──────────────────────────────────────────────────────────────────────

print("BabyDiary bot ishga tushdi...")
# ── Startup: yo'q bo'lgan fayllarni GitHub'dan tiklaymiz (fon thread'da) ──
def _startup_restore():
    """Startup'da GitHub bilan sinxronlaymiz — fon thread'da (bot bloklanmaydi)."""
    try:
        vol = os.path.isdir("/data")
        log.info(f"📁 DATA_DIR={DATA_DIR}  (/data volume: {'BOR' if vol else 'YO`Q'})")
        if not vol:
            log.warning("⚠️ /data volume ulanmagan — lokal ma'lumot har deploy'da yo'qoladi. "
                        "GitHub yagona manba bo'lib qoladi.")
        if not _GH_TOKEN:
            log.error("❌ GITHUB_TOKEN yo'q — sinxronizatsiya ishlamaydi!")
            return
        gh_sync_on_start(ORDERS_FILE,   "data/orders.json")
        gh_sync_on_start(PRODUCTS_FILE, "data/products.json")
        gh_sync_on_start(REVIEWS_FILE,  "data/reviews.json")
        log.info(f"✅ Startup sinxron: {len(get_products())} mahsulot, "
                 f"{len(load_json(ORDERS_FILE, []))} buyurtma, "
                 f"{len(load_json(REVIEWS_FILE, []))} sharh")
    except Exception as e:
        log.warning(f"Startup sync xato: {e}")
# Fon thread'lari
import threading
try:
    _drop_card_settings()          # bazada karta raqami qolmasin
except Exception as _e:
    log.warning(f"karta migratsiyasi: {_e}")

threading.Thread(target=_startup_restore, daemon=True).start()
threading.Thread(target=auto_backup_loop, daemon=True).start()
threading.Thread(target=cart_reminder_loop, daemon=True).start()
threading.Thread(target=review_loop, daemon=True).start()
threading.Thread(target=stale_order_loop, daemon=True).start()
threading.Thread(target=paid_watch_loop, daemon=True).start()
threading.Thread(target=expire_loop, daemon=True).start()


def _notify_crash(exc):
    """Bot to'xtab qolsa/xato bersa asosiy adminni ogohlantiradi."""
    try:
        import traceback
        tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        MAIN_ADMIN_ID = 5285940949
        bot.send_message(MAIN_ADMIN_ID,
            f"🚨 *Bot xatosi!*\n\n`{tb[:500]}`\n\nBot qayta ishga tushishga urinmoqda.",
            parse_mode="Markdown")
    except Exception as e:
        log.error(f"crash alert yuborilmadi: {e}")


# Xato bo'lsa ham bot o'chmasin — ogohlantirib, qayta ishga tushadi
while True:
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
        break  # toza to'xtatilsa chiqamiz
    except Exception as e:
        log.error(f"Polling xatosi: {e}")
        _notify_crash(e)
        time.sleep(15)  # biroz kutib, qayta urinamiz
