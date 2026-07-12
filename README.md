# BabyDiary

BabyDiary sayti, asosiy Telegram bot va moliya boti bitta Railway service ichida ishlaydi.

## Ishga tushirish

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python launcher.py
```

Railway start command:

```bash
python launcher.py
```

## Muhim

- `.env`, tokenlar va to‘lov kalitlarini GitHub'ga commit qilmang.
- Productionda `APP_ENV=production` va `SMS_MODE=disabled` qoldiring, real SMS provider ulanmaguncha.
- `/data` uchun Railway Volume ulang. Buyurtmalar va SQLite baza shu yerda saqlanadi.
- `data/orders.json` va `data/babydiary.db` GitHub'ga yuborilmaydi.

## Health check

`GET /health`
