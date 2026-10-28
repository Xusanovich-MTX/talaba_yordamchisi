# Baza va migratsiyalar

## Identifikatorlar

- `User.id` — bazadagi ichki kalit. `Schedule.user_id` va `Task.user_id` shu qiymatga bog'lanadi.
- `User.telegram_id` — Telegram yuborgan raqamli foydalanuvchi IDsi. Xabar yuborish va kiruvchi foydalanuvchini aniqlash uchun ishlatiladi.
- `User.username` — ixtiyoriy Telegram username. `/start` orqali yangilanadi; akkauntning doimiy identifikatori yoki admin vakolati emas.
- `SUPPORT_USERNAME` sozlamasi yordam bo'limidagi aloqa manzilidir; odatiy qiymat `MTX_2477`.

Botdagi `/id` buyrug'i xabar yuborgan foydalanuvchining raqamli Telegram IDsini ko'rsatadi.

## V1 migratsiya

`init_db()` polling va scheduler ishga tushishidan oldin migratsiyani bajaradi.
Versiya SQLite `PRAGMA user_version` orqali saqlanadi. Ushbu migratsiya SQLite uchun yozilgan.

V1 quyidagilarni bitta tranzaksiyada bajaradi:

1. Eski `user_id` qiymatlarini `users.id` va `users.telegram_id` bilan solishtiradi.
2. Telegram ID bilan yozilgan bog'lanishlarni ichki IDga ko'chiradi; allaqachon to'g'ri bog'langan yozuvlarni saqlaydi.
3. `users.username` ustunini qo'shadi.
4. Foydalanuvchi/kun/vaqt va foydalanuvchi/holat/muddat indekslarini yaratadi.
5. Sxema va tashqi kalitlarni tekshiradi, so'ng versiyani 1 qiladi.

Mos foydalanuvchi topilmasa yoki bir qiymat ikki boshqa foydalanuvchini bildirsa, migratsiya yozuv IDlarini ko'rsatib to'xtaydi. Egasi taxmin bilan tanlanmaydi.
Tekshiruv xatosida ma'lumotlar, ustunlar va versiya o'zgarishlari qaytariladi.
V1 bazada ichki IDlar qayta Telegram ID sifatida talqin qilinmaydi.

Har bir yangi SQLite ulanishida `PRAGMA foreign_keys=ON` yoqiladi.

## Ishga tushirish

`C:\Dgango` papkasidan:

```powershell
& .\.venv\Scripts\python.exe .\talaba_bot\main.py --migrate-db
```

Bu buyruq migratsiyani bajarib chiqadi. Oddiy `main.py` ishga tushirilganda ham migratsiya avtomatik tekshiriladi.

Ishlab turgan eski bot jarayoni ma'lumot yozishni davom ettirmasligi uchun versiya yangilashdan oldin uni to'xtatish, migratsiyadan so'ng yangi kodni ishga tushirish kerak. Bir bot uchun bir vaqtning o'zida bitta polling jarayoni ishlatiladi.

## Zaxira nusxalar

Mavjud faylli bazani yangilashdan oldin `backups/` ichida SQLite backup API orqali izchil nusxa yaratiladi.
Migratsiya davomida yozish qulfi boshqa ulanishlar ma'lumotni o'zgartirishini to'xtatadi.
Yangi bo'sh bazaga va takroriy v1 tekshiruviga qo'shimcha backup yaratilmaydi.

21.10.2026 kuni qo'llangan v1 oldi nusxasi:

`backups/talaba_bot.before-v1.20261021T103032749236Z.a27fe2d7.sqlite3`

Nusxani qayta tiklashdan oldin botni to'xtatish va joriy bazaning ham alohida nusxasini olish kerak. Eski baza qaytarilganda yangi kod keyingi ishga tushishda migratsiyani yana bajaradi.
Backup fayllari, baza va `.env` Gitga qo'shilmaydi.

## Tekshiruvlar

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s talaba_bot/tests -v
```

Testlar alohida vaqtinchalik yoki xotiradagi bazalarda ishlaydi. Ular haqiqiy foydalanuvchi bazasini o'zgartirmaydi va Telegramga xabar yubormaydi.
