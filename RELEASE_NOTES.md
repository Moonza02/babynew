# BabyDiary real patch v1

Bu paket original live loyiha asosida haqiqatan tahrirlangan.

## Tuzatilganlar

- Productionda SMS tasdiqlash kodi API javobida ochiq qaytmaydi.
- SMS provider ulanmagan holatda ro‘yxatdan o‘tish xavfsiz tarzda to‘xtaydi.
- Parol minimumi 8 belgiga oshirildi.
- Sessiyalar 30 kundan keyin eskiradi va avtomatik tozalanadi.
- Login sessiyasi `HttpOnly`, `Secure`, `SameSite=Lax` cookie bilan ham saqlanadi.
- Parol hash tekshiruvida constant-time compare ishlatiladi.
- Productionda Telegram debug endpoint yopildi.
- Security headerlar va 2 MB request limiti qo‘shildi.
- `.gitignore`, `.env.example`, README va smoke test qo‘shildi.

## Hali qilinmagan

- Real SMS provider integratsiyasi.
- SQLite/JSON'dan MySQL'ga to‘liq migratsiya.
- GitHub'ni live database sifatida ishlatishni olib tashlash.
- Frontend va botni modullarga to‘liq ajratish.

Bu paket xavfsiz birinchi real release. Live deploydan oldin backup va preview test qiling.
