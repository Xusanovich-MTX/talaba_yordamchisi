# Talaba Yordamchisi — rivojlantirish rejasi

Sana: 21.10.2026
Holat: 1-bosqich 21.10.2026 kuni bajarildi. Navbatda 2-bosqich.

Maqsad: talaba darslari, topshiriqlari va yaqin muddatlarini bitta botda qulay boshqarsin.
Har bosqichda avval ma'lumotlar tuzilishi, keyin tugmalar va handlerlar, so'ng tegishli tekshiruvlar bajariladi.

## Reja tuzilgandagi imkoniyatlar va aniqlangan kamchiliklar

- `User`, `Schedule`, `Task` modellari mavjud.
- Dars va topshiriq qo'shish, topshiriqni bajarilgan deb belgilash va eslatmalar mavjud.
- `Schedule.user_id` va `Task.user_id` modellarda `users.id`ga bog'langan, handlerlar esa Telegram ID saqlaydi. Scheduler ham hozir Telegram ID orqali birlashtiradi.
- Dars qo'shishda hafta kuni so'ralmaydi: yozuv doim bugungi kunga saqlanadi.
- Darsni o'chirish handleri va klaviaturasi bor, lekin jadvalni ko'rsatishda bu tugma ulanmagan.
- Dars va topshiriqlarni tahrirlash yo'q; o'chirish/bajarilgan deb belgilash so'rovlarida yozuv egasi tekshirilmagan.
- `User.timezone` bor, lekin vaqt hisoblash butun bot bo'ylab ushbu sozlamaga bir xil tayanmaydi.
- Yuborilgan eslatmalar xotirada saqlanadi va bot qayta ishga tushganda bu holat yo'qoladi.

## 1-bosqich — baza va foydalanuvchi bog'lanishlarini to'g'rilash

Bajarildi: baza zaxiralandi va v1 migratsiya qo'llandi; 4 foydalanuvchi, 1 dars va 1 topshiriq saqlandi. Har ikki noto'g'ri tashqi kalit tuzatildi. Egalik tekshiruvlari, indekslar va har bir ulanishda SQLite FK nazorati ishlaydi. 41 test muvaffaqiyatli o'tdi; takror migratsiya ma'lumotlarni o'zgartirmadi.

`@MTX_2477` Telegram orqali tekshirilgan `6363461555` ID bilan mavjud foydalanuvchi yozuviga bog'landi. Username alohida saqlanadi, yordam bo'limidagi aloqa manzili sozlandi va `/id` buyrug'i qo'shildi. Username admin huquqini berish uchun ishlatilmaydi.

Natija: mavjud dars va topshiriqlar to'g'ri foydalanuvchiga bog'lanadi; har kim o'z yozuvlarini boshqaradi.

1. Bazaning zaxira nusxasini olish va mavjud bog'lanishlarni tekshirish.
2. Versiyali migratsiya tartibini kiritish: mavjud yozuvlarni saqlab, baza tuzilishi va qiymatlarini yangilash.
3. Eski `user_id` Telegram IDlarini tegishli `User.id`ga ko'chirish. Mos kelmaydigan yoki noaniq yozuvlarni taxmin bilan almashtirmaslik.
4. Saqlash, o'qish va scheduler joinlarini birgalikda ichki `User.id`ga o'tkazish.
5. Tahrirlash, o'chirish va bajarildi amallarida yozuv egasini tekshirish.
6. SQLite tashqi kalit nazoratini yoqish; foydalanuvchi va sana bo'yicha kerakli indekslarni qo'shish.

Asosiy fayllar: `database/models.py`, `database/engine.py`, `handlers/`, `scheduler/notifications.py`, yangi migratsiya fayllari.

Tayyorlik mezoni: mavjud yozuvlar saqlanadi, ORM bog'lanishlari ishlaydi, ikki foydalanuvchili tekshiruvda birining amallari boshqasining yozuviga ta'sir qilmaydi; migratsiya qayta bajarilganda qiymatlar yana almashtirilmaydi.

## 2-bosqich — to'liq dars jadvali

Natija: darsni istalgan hafta kuniga qo'shish va keyin o'zgartirish mumkin.

- Menyuda `➕ Dars qo'shish` tugmasi.
- Qo'shishda kun → fan → vaqt → xona yoki havola ketma-ketligi.
- Har bir darsda `✏️ Tahrirlash` va `🗑 O'chirish` tugmalari.
- Haftalik jadvalni tartibli ko'rish.
- Bo'sh matn, noto'g'ri vaqt va matn o'rniga yuborilgan boshqa xabarlar uchun tushunarli javoblar.
- Foydalanuvchi matnini HTML xabarlarda xavfsiz ko'rsatish.

Model: mavjud `Schedule` maydonlari birinchi versiya uchun yetadi. Tugash vaqti va o'qituvchi maydonlarini keyin ehtiyojga ko'ra qo'shish mumkin.

Tayyorlik mezoni: masalan, bugun chorshanba bo'lsa ham juma kuniga dars qo'shiladi; tahrir va o'chirish tanlangan yozuvga qo'llanadi.

## 3-bosqich — topshiriqlarni qulay boshqarish

Natija: talaba qaysi ishni avval bajarishi kerakligini ko'radi va muddatlarni yangilay oladi.

- `➕ Topshiriq qo'shish` tugmasi.
- Fan, tavsif va topshirish muddatini tahrirlash.
- Muhimlik darajasi: oddiy, muhim, shoshilinch.
- Faol, muddati o'tgan va bajarilgan topshiriqlar bo'limlari.
- Bajarildi holatini bekor qilish.
- Ko'p yozuvlar uchun sahifalash va fan bo'yicha qidirish.
- Yangi topshiriqda o'tgan muddat kiritilsa, qayta to'g'ri sana so'rash.

Model: `Task.priority`, `Task.created_at`, `Task.completed_at`. `is_completed` saqlanadi. Eski yozuvlarning noma'lum yaratilish/bajarilish sanalari to'qib yozilmaydi.

Tayyorlik mezoni: muddatni o'zgartirganda ro'yxat va eslatma yangi muddatga moslashadi; bajarilgan topshiriq arxivda ko'rinadi.

## 4-bosqich — sozlanadigan eslatmalar

Natija: talaba eslatma vaqtini tanlaydi, bot qayta ishga tushganda yuborish holati saqlanadi.

- Darsdan 15, 30 yoki 60 daqiqa oldin eslatish sozlamasi.
- Vaqt zonasini tanlash va barcha bo'limlarda bir xil qo'llash.
- Topshiriq eslatmasida `✅ Bajarildi` va `⏳ 10 daqiqadan keyin eslat` tugmalari.
- Yuborish va qayta urinish holatini bazada saqlash.
- O'chirilgan yoki bajarilgan yozuv uchun kutilayotgan eslatmani bekor qilish.
- Sana, muddat yoki eslatma sozlamasi o'zgarsa, eski eslatma holatini yangi hodisadan ajratish.

Model: yangi `Reminder` jadvali; foydalanuvchi, dars yoki topshiriq, hodisa sanasi, yuborish vaqti va holati. `User`ga eslatma oralig'i sozlamasi.

Tayyorlik mezoni: oddiy qayta ishga tushirish yuborilgan eslatmani qayta rejalashtirmaydi; vaqtincha yuborish xatosi qayta urinishga qoladi. Telegram va baza orasidagi uzilishlar sababli mutlaq bir martalik yetkazish kafolati va'da qilinmaydi.

## 5-bosqich — bugungi reja va ertalabki xulosa

Natija: `🏠 Bugun` tugmasida bugungi darslar, yaqin topshiriqlar va kechikkan ishlar birga ko'rinadi.

- Bugun, ertaga va shu hafta ko'rinishlari.
- Keyingi dars va eng yaqin topshirish muddati.
- Foydalanuvchi yoqsa, tanlangan vaqtda kunlik reja xabari.
- Kunlik xulosani sozlamalardan o'chirish.

Model: ko'rinishlar mavjud jadvallardan yig'iladi. Kunlik xulosa uchun `User.daily_summary_enabled` va `User.daily_summary_time`; yuborish holati 4-bosqichdagi mexanizmdan foydalanadi.

Tayyorlik mezoni: bo'sh kun va yarim tun holatlari to'g'ri ko'rsatiladi; xulosa faqat uni yoqqan foydalanuvchiga yuboriladi.

## 6-bosqich — statistika va ma'lumotlarni yuklab olish

Natija: talaba o'z natijalarini ko'radi va yozuvlarini fayl qilib oladi.

- Hafta/oy davomida bajarilgan topshiriqlar soni.
- Muddatida va kech bajarilgan topshiriqlar taqsimoti.
- Fanlar bo'yicha ishlar soni.
- O'z jadvali va topshiriqlarini CSV formatida yuklab olish.

Model: 3-bosqichdagi sanalar yetarli. Statistikada sana ma'lum bo'lgan yozuvlar hisoblanadi; eski yozuvlar alohida izohlanadi.

Tayyorlik mezoni: davr chegaralari foydalanuvchi vaqt zonasiga mos; eksportga boshqa foydalanuvchi ma'lumotlari kirmaydi.

## 7-bosqich — imtihonlar

Natija: imtihon, oraliq va yakuniy nazoratlarni alohida kuzatish.

- Fan, nazorat turi, sana/vaqt, xona va izoh.
- Imtihongacha qolgan vaqt.
- Oldindan eslatish va `Bugun` oynasiga qo'shish.

Model: yangi `Exam`, `User.id`ga bog'lanadi. Eslatma mexanizmi shu turdagi hodisalarni ham qamrab oladi.

Tayyorlik mezoni: imtihon tahrirlansa yoki o'chirilsa, uning eslatmalari ham mos ravishda yangilanadi.

## 8-bosqich — fan materiallari

Natija: konspekt, PDF, rasm va foydali havolalarni fan bo'yicha topish.

- Material yuklash, nom berish, qidirish va o'chirish.
- Fan bo'yicha materiallar ro'yxati.
- Materiallarni bot orqali qayta olish.

Model: yangi `StudyMaterial`: foydalanuvchi, fan, nom, material turi, Telegram `file_id` yoki havola, qo'shilgan sana.

Tayyorlik mezoni: fayl va havola alohida tekshiriladi; foydalanuvchi faqat o'z materiallarini oladi.

## Har bosqichni bajarish tartibi

1. Shu bosqichning kichik vazifalarini belgilash.
2. Zarur bo'lsa, model va mavjud baza uchun migratsiya tayyorlash.
3. Tugmalar, kiritish bosqichlari va handlerlarni qo'shish.
4. O'zgargan funksiyaga tegishli tekshiruvlarni bajarish.
5. Foydalanish misolini ko'rsatib, natijani qayd qilish.

Navbatdagi ish: 2-bosqichdagi hafta kunini tanlab dars qo'shish va jadvalni boshqarish.
Keyingi kengaytmalar: guruh uchun umumiy jadval, baholar, tashqi kalendar va AI yordamchi — asosiy bosqichlardan keyin alohida rejalashtiriladi.
