from html import escape
from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from config import HEMIS_SESSION_KEY, SUPPORT_USERNAME
from database.models import User
from keyboards.reply import cancel_kb, main_menu_kb
from services.channel_sub import (
    get_channel_config,
    is_bot_admin,
    is_user_subscribed,
    subscription_prompt_kb,
    subscription_prompt_text,
    unsubscribed_notified_users,
)
from services.hemis_auth import HemisLoginError, authenticate_hemis
from services.hemis_catalog import get_hemis_institutions, match_hemis_institutions
from services.hemis_session import encrypt_hemis_session

router = Router()

BOT_LINK = "https://t.me/assistant_n1_bot"
BOT_SHARE_TEXT = (
    "🚀 HEMIS’ga har safar login-parol terib kirishdan charchadingizmi?\n\n"
    "🎓 «Talaba Yordamchisi» — har bir talabada bo'lishi shart bo'lgan shaxsiy HEMIS bot!\n\n"
    "⏰ Para boshlanishidan 10, 5 daqiqa oldin va dars boshlanganda avto-eslatma\n"
    "🔥 Topshiriq (deadline) muddati tugashidan oldin ogohlantirish\n"
    "📊 Dars jadvali, Davomat (NB), Baholar, Semestr/Yillik GPA va Fan resurslari — hammasi bitta joyda!\n\n"
    "👇 Hoziroq quyidagi havola orqali ulaning:\n"
    f"👉 {BOT_LINK}"
)
BOT_SHARE_URL = f"https://t.me/share/url?text={quote(BOT_SHARE_TEXT, safe='')}" 


class Registration(StatesGroup):
    first_name = State()
    last_name = State()
    phone = State()


class HemisConnect(StatesGroup):
    institution_search = State()
    login = State()
    password = State()


def _phone_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)],
            [KeyboardButton(text="❌ Bekor qilish")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()

    # Foydalanuvchini bazadan qidirish
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        session.add(user)
        await session.commit()
    else:
        user.username = message.from_user.username
        await session.commit()

    bot = getattr(message, "bot", None)
    cfg = get_channel_config()
    if cfg is not None and bot is not None:
        if not await is_user_subscribed(bot, message.from_user.id):
            await message.answer(
                subscription_prompt_text(left=False),
                reply_markup=subscription_prompt_kb(cfg[1]),
            )
            return

    display_name = user.full_name or message.from_user.full_name
    await message.answer(
        f"Assalomu alaykum, <b>{escape(display_name)}</b>! 👋\n"
        "🎓 <b>Talaba Yordamchisi</b> botiga xush kelibsiz!\n\n"
        "Bu bot orqali HEMIS tizimidagi barcha ma'lumotlaringizni qulay kuzatib borishingiz mumkin:\n"
        "• 📅 <b>Dars jadvali</b> va darsdan 10, 5 daqiqa oldin hamda dars boshlanganda avtomatik eslatma\n"
        "• 📝 <b>Topshiriqlar</b> (eng yaqin 3 ta vazifa) va deadline eslatmalari\n"
        "• 📊 <b>Davomat (NB)</b>, 📈 <b>Baholar</b>, 🎯 <b>GPA</b> va 📚 <b>Fan materiallari</b>\n\n"
        "👇 Quyidagi <b>Asosiy menyu</b>dan kerakli bo'limni tanlang:",
        reply_markup=main_menu_kb(user),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "check_channel_sub")
async def verify_channel_subscription(
    callback: CallbackQuery, session: AsyncSession
):
    cfg = get_channel_config()
    if cfg is not None and callback.bot is not None:
        if not await is_user_subscribed(
            callback.bot, callback.from_user.id, confirming=True
        ):
            await callback.answer(
                "❌ Siz hali kanalga a'zo bo'lmadingiz! Avval kanalga a'zo bo'lib, so'ng Tasdiqlashni bosing.",
                show_alert=True,
            )
            return

    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        session.add(user)
        await session.commit()

    await callback.answer("✅ Obuna tasdiqlandi!")
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        display_name = user.full_name or callback.from_user.full_name
        await callback.message.answer(
            f"Assalomu alaykum, <b>{escape(display_name)}</b>! 👋\n"
            "✅ Kanal obunasi tasdiqlandi. 🎓 <b>Talaba Yordamchisi</b> botiga xush kelibsiz!\n\n"
            "👇 Quyidagi <b>Asosiy menyu</b>dan kerakli bo'limni tanlang:",
            reply_markup=main_menu_kb(user),
        )


@router.chat_member()
async def on_channel_member_update(
    event: ChatMemberUpdated, session: AsyncSession
):
    cfg = get_channel_config()
    if cfg is None:
        return

    target_chat, channel_url = cfg
    chat_username = (event.chat.username or "").lstrip("@").casefold()
    matches_chat = False
    if isinstance(target_chat, int) and event.chat.id == target_chat:
        matches_chat = True
    elif isinstance(target_chat, str) and chat_username == target_chat.lstrip("@").casefold():
        matches_chat = True
    if not matches_chat:
        return

    member_user = event.new_chat_member.user
    if not member_user or member_user.is_bot:
        return

    new_status = event.new_chat_member.status
    if new_status in ("creator", "administrator", "member"):
        unsubscribed_notified_users.discard(member_user.id)
        return

    if new_status in ("left", "kicked"):
        result = await session.execute(
            select(User).where(User.telegram_id == member_user.id)
        )
        user = result.scalar_one_or_none()
        if user and member_user.id not in unsubscribed_notified_users:
            unsubscribed_notified_users.add(member_user.id)
            try:
                await event.bot.send_message(
                    chat_id=member_user.id,
                    text=subscription_prompt_text(left=True),
                    reply_markup=subscription_prompt_kb(channel_url),
                )
            except Exception:
                pass


def _valid_person_name(value: str) -> bool:
    return (
        1 <= len(value) <= 50
        and any(character.isalpha() for character in value)
        and all(character.isalpha() or character in " '-’" for character in value)
    )


@router.message(Registration.first_name)
async def registration_first_name(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not _valid_person_name(value):
        await message.answer("Ismingizni matn ko'rinishida yuboring.")
        return
    await state.update_data(first_name=value)
    await state.set_state(Registration.last_name)
    await message.answer("Familiyangizni yuboring.")


@router.message(Registration.last_name)
async def registration_last_name(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not _valid_person_name(value):
        await message.answer("Familiyangizni matn ko'rinishida yuboring.")
        return
    await state.update_data(last_name=value)
    await state.set_state(Registration.phone)
    await message.answer(
        "Telefon raqamingizni ro'yxatdan o'tkazish uchun quyidagi tugmani bosing.",
        reply_markup=_phone_request_kb(),
    )


@router.message(Registration.phone)
async def registration_phone(
    message: Message, session: AsyncSession, state: FSMContext
):
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer(
            "Ro'yxatdan o'tish bekor qilindi. Davom etish uchun /start yuboring.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    contact = message.contact
    if not contact or contact.user_id != message.from_user.id:
        await message.answer(
            "Iltimos, faqat o'zingizning telefon raqamingizni tugma orqali yuboring.",
            reply_markup=_phone_request_kb(),
        )
        return

    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await state.clear()
        await message.answer("Foydalanuvchi topilmadi. Qaytadan /start yuboring.")
        return

    data = await state.get_data()
    user.first_name = data["first_name"]
    user.last_name = data["last_name"]
    user.phone = contact.phone_number
    user.full_name = f"{user.first_name} {user.last_name}"
    await session.commit()
    await state.clear()
    await message.answer(
        f"Ro'yxatdan o'tdingiz, <b>{escape(user.full_name)}</b>! 🎓\n\n"
        "Asosiy menyu:",
        reply_markup=main_menu_kb(),
    )


def _institution_keyboard(institutions: list[dict], page: int) -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    visible = institutions[start:start + page_size]
    rows = [
        [InlineKeyboardButton(
            text=item["name"][:60],
            callback_data=f"hemis:pick:{item['code']}",
        )]
        for item in visible
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"hemis:page:{page - 1}"
        ))
    if start + page_size < len(institutions):
        navigation.append(InlineKeyboardButton(
            text="➡️", callback_data=f"hemis:page:{page + 1}"
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="hemis:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🎓 HEMIS ulash")
async def start_hemis_connect(
    message: Message, session: AsyncSession, state: FSMContext
):
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Avval /start buyrug'ini yuboring.")
        return

    await state.set_state(HemisConnect.institution_search)
    await message.answer(
        "Yozish davomida OTM takliflari chiqishi uchun <b>Jonli qidirish</b>ni bosing. "
        "Tugma javob bermasa, @BotFather'da /setinline ni yoqing. "
        "Oddiy qidirish uchun OTM nomini shu chatga yuboring.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔎 Jonli qidirish",
                switch_inline_query_current_chat="",
            )
        ]]),
    )


@router.message(HemisConnect.institution_search)
async def search_hemis_institutions(message: Message, state: FSMContext):
    if message.via_bot:
        return

    query = (message.text or "").strip()
    if query == "❌ Bekor qilish":
        await state.clear()
        await message.answer("HEMIS ulash bekor qilindi.", reply_markup=main_menu_kb())
        return
    if not query:
        await message.answer("OTM nomining kamida birinchi harfini yuboring.")
        return

    try:
        catalog = await get_hemis_institutions()
    except Exception:
        await message.answer(
            "HEMIS OTMlar ro'yxatini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
        )
        return

    matches = match_hemis_institutions(catalog, query)
    if not matches:
        await message.answer("Mos OTM topilmadi. Nomini boshqacha yozib ko'ring.")
        return

    await state.update_data(hemis_institutions=matches)
    pages = (len(matches) + 7) // 8
    await message.answer(
        f"Topildi: {len(matches)} ta OTM. Keraklisini tanlang "
        f"(1/{pages}); ko'p natijada keyingi sahifaga o'ting.",
        reply_markup=_institution_keyboard(matches, 0),
    )


@router.inline_query()
async def search_hemis_inline(inline_query: InlineQuery):
    query = (inline_query.query or "").strip()
    if not query:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    try:
        catalog = await get_hemis_institutions()
    except Exception:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    matches = match_hemis_institutions(catalog, query)
    results = [
        InlineQueryResultArticle(
            id=institution["code"],
            title=institution["name"],
            description="HEMIS OTMni tanlash",
            input_message_content=InputTextMessageContent(
                message_text=f"🎓 {institution['name']}"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Shu OTMni tanlash",
                    callback_data=f"hemis:inline:{institution['code']}",
                )
            ]]),
        )
        for institution in matches[:50]
    ]
    await inline_query.answer(results, cache_time=1, is_personal=True)


@router.callback_query(F.data.startswith("hemis:page:"))
async def hemis_institution_page(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    institutions = data.get("hemis_institutions", [])
    if not institutions:
        await callback.answer("Qidiruv muddati tugadi. Qaytadan qidiring.", show_alert=True)
        return

    page = int(callback.data.rsplit(":", 1)[1])
    page = max(0, min(page, (len(institutions) - 1) // 8))
    pages = (len(institutions) + 7) // 8
    if callback.message:
        await callback.message.edit_text(
            f"Topildi: {len(institutions)} ta OTM. Keraklisini tanlang "
            f"({page + 1}/{pages}).",
            reply_markup=_institution_keyboard(institutions, page),
        )
    await callback.answer()


@router.callback_query(F.data == "hemis:cancel")
async def cancel_hemis_connect(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.edit_text("HEMIS ulash bekor qilindi.")
    await callback.answer()


@router.callback_query(F.data.startswith("hemis:pick:"))
async def choose_hemis_institution(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    institution_code = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    institution = next(
        (
            item for item in data.get("hemis_institutions", [])
            if item["code"] == institution_code
        ),
        None,
    )
    if institution is None:
        await callback.answer("Tanlov eskirgan. OTM nomini qaytadan qidiring.", show_alert=True)
        return

    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Foydalanuvchi topilmadi. /start yuboring.", show_alert=True)
        return

    user.hemis_institution_code = institution["code"]
    user.hemis_institution_name = institution["name"]
    user.hemis_api_url = institution["api_url"]
    user.hemis_student_url = institution["student_url"]
    await session.commit()
    if HEMIS_SESSION_KEY:
        await state.set_state(HemisConnect.login)
    else:
        await state.clear()
    if callback.message:
        await callback.message.edit_text(
            f"Tanlandi: <b>{escape(user.hemis_institution_name)}</b>."
        )
        if HEMIS_SESSION_KEY:
            await callback.message.answer(
                "HEMIS login raqamingizni yuboring.", reply_markup=cancel_kb()
            )
        else:
            await callback.message.answer(
                "Login so'ralmadi: serverda HEMIS_SESSION_KEY sozlanmagan. "
                "Xavfsiz ulanish uchun avval administrator .env fayliga kalit qo'shishi kerak.",
                reply_markup=main_menu_kb(),
            )
    await callback.answer("OTM saqlandi")


@router.callback_query(F.data.startswith("hemis:inline:"))
async def choose_hemis_inline(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    institution_code = callback.data.rsplit(":", 1)[1]
    try:
        catalog = await get_hemis_institutions()
    except Exception:
        await callback.answer("HEMIS ro'yxatini olib bo'lmadi. Qayta urinib ko'ring.", show_alert=True)
        return

    institution = next(
        (item for item in catalog if item["code"] == institution_code),
        None,
    )
    if institution is None:
        await callback.answer("Bu OTM katalogda topilmadi. Qaytadan qidiring.", show_alert=True)
        return

    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Avval botga /start yuboring.", show_alert=True)
        return

    user.hemis_institution_code = institution["code"]
    user.hemis_institution_name = institution["name"]
    user.hemis_api_url = institution["api_url"]
    user.hemis_student_url = institution["student_url"]
    await session.commit()
    if HEMIS_SESSION_KEY:
        await state.set_state(HemisConnect.login)
        prompt = "HEMIS login raqamingizni yuboring."
        keyboard = cancel_kb()
    else:
        await state.clear()
        prompt = (
            "OTM saqlandi, lekin login so'ralmadi: serverda HEMIS_SESSION_KEY "
            "sozlanmagan. Xavfsiz ulanish uchun administrator avval .env fayliga kalit qo'shishi kerak."
        )
        keyboard = main_menu_kb()
    await callback.bot.send_message(
        callback.from_user.id,
        f"Tanlandi: <b>{escape(institution['name'])}</b>.\n\n{prompt}",
        reply_markup=keyboard,
    )
    await callback.answer("OTM saqlandi")


@router.message(HemisConnect.login)
async def receive_hemis_login(message: Message, state: FSMContext):
    if message.via_bot:
        return
    login = (message.text or "").strip()
    if login == "❌ Bekor qilish":
        await state.clear()
        await message.answer("HEMIS ulash bekor qilindi.", reply_markup=main_menu_kb())
        return
    if not login or len(login) > 128:
        await message.answer("HEMIS loginini matn ko'rinishida yuboring.")
        return
    if not HEMIS_SESSION_KEY:
        await state.clear()
        await message.answer(
            "Xavfsiz ulanish sozlanmagan. HEMIS parolini yubormang; administratorga murojaat qiling.",
            reply_markup=main_menu_kb(),
        )
        return

    await state.update_data(hemis_login=login)
    await state.set_state(HemisConnect.password)
    await message.answer(
        "Endi HEMIS parolingizni yuboring. Parol bazaga saqlanmaydi va xabar o'chiriladi.",
        reply_markup=cancel_kb(),
    )


@router.message(HemisConnect.password)
async def receive_hemis_password(
    message: Message, session: AsyncSession, state: FSMContext
):
    if message.via_bot:
        return
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("HEMIS ulash bekor qilindi.", reply_markup=main_menu_kb())
        return

    password = message.text
    if not password:
        await message.answer("HEMIS parolini matn ko'rinishida yuboring.")
        return
    if not HEMIS_SESSION_KEY:
        await state.clear()
        await message.answer(
            "Xavfsiz ulanish sozlanmagan. Parol ishlatilmadi; administratorga murojaat qiling.",
            reply_markup=main_menu_kb(),
        )
        return

    data = await state.get_data()
    hemis_login = data.get("hemis_login")
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass

    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user or not user.hemis_api_url or not hemis_login:
        await message.answer(
            "HEMIS ulash holati topilmadi. OTMni qaytadan tanlang.",
            reply_markup=main_menu_kb(),
        )
        return

    try:
        session_data = await authenticate_hemis(
            user.hemis_api_url, hemis_login, password
        )
        user.hemis_session_ciphertext = encrypt_hemis_session(session_data)
        await session.commit()
    except HemisLoginError as error:
        if error.reason == "invalid_credentials":
            reply = "HEMIS login yoki parol noto'g'ri. Qaytadan ulanishni boshlang."
        elif error.reason == "captcha":
            reply = "HEMIS CAPTCHA talab qildi. Xavfsizlik tekshiruvini chetlab o'tmaymiz; rasmiy HEMIS saytida kiring."
        elif error.reason == "rate_limited":
            reply = "HEMIS ko'p urinish sabab vaqtincha chekladi. Birozdan keyin qayta urinib ko'ring."
        else:
            reply = "HEMIS hozir javob bermadi. Birozdan keyin qayta urinib ko'ring."
        await message.answer(reply, reply_markup=main_menu_kb())
        return
    except (RuntimeError, ValueError):
        await message.answer(
            "Sessiyani xavfsiz saqlab bo'lmadi. Parol saqlanmadi; administrator sozlamasini tekshiring.",
            reply_markup=main_menu_kb(),
        )
        return

    await message.answer(
        f"HEMIS’ga <b>{escape(user.hemis_institution_name or '')}</b> orqali ulandingiz.",
        reply_markup=main_menu_kb(user),
    )


@router.message(F.text == "🚪 Tizimdan chiqish")
async def logout_hemis(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Avval /start buyrug'ini yuboring.")
        return

    user.hemis_session_ciphertext = None
    user.hemis_institution_code = None
    user.hemis_institution_name = None
    user.hemis_api_url = None
    user.hemis_student_url = None
    await session.commit()

    await message.answer(
        "🚪 <b>HEMIS tizimidan muvaffaqiyatli chiqdingiz.</b>\n\n"
        "Qayta ulanish uchun menyudagi <b>«🎓 HEMIS ulash»</b> tugmasini bosing.",
        reply_markup=main_menu_kb(user),
    )


@router.message(Command("id"))
async def show_telegram_id(message: Message):
    await message.answer(
        f"🆔 Sizning Telegram ID: <code>{message.from_user.id}</code>"
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Yordam")
async def help_handler(message: Message):
    await message.answer(
        "📖 <b>Botdan foydalanish va HEMIS tizimiga ulanish qo'llanmasi</b>\n\n"
        "🔐 <b>1. HEMIS tizimiga qanday ulaniladi?</b>\n"
        "1️⃣ Asosiy menyudan <b>«🎓 HEMIS ulash»</b> tugmasini bosing.\n"
        "2️⃣ <b>«🔎 Jonli qidirish»</b> tugmasini bosib OTM nomini yozing (masalan: <i>TATU, O'zMU, SamDU</i>) yoki OTM nomini oddiy xabar qilib yuboring va ro'yxatdan o'z oliygohingizni tanlang.\n"
        "3️⃣ <b>HEMIS Login (Talaba ID)</b> raqamingizni yuboring (masalan: <code>395231101234</code> — 12 xonali talaba ID raqamingiz).\n"
        "4️⃣ <b>HEMIS parolingizni</b> yuboring (xavfsizlik uchun parol yozilgan xabar darhol avtomatik o'chiriladi va shifrlanadi).\n\n"
        "❓ <b>2. HEMIS tizimiga kira olmasangiz nima qilish kerak?</b>\n"
        "🔹 <b>Login yoki parolni bilmasangiz / unutgan bo'lsangiz:</b>\n"
        "• Odatda HEMIS login — bu <b>12 xonali Talaba ID</b> raqamingiz, boshlang'ich parol esa pasport/ID-karta <b>seriya va raqamingiz</b> (masalan: <code>AD1234567</code>) bo'ladi.\n"
        "• Parolni unutgan bo'lsangiz, fakultetingiz <b>dekanati (tyutori)</b> yoki OTM <b>HEMIS administratoriga</b> murojaat qilib parolni tiklatishingiz mumkin.\n"
        "🔹 <b>«Login yoki parol noto'g'ri» xatosi chiqsa:</b>\n"
        "• Ro'yxatdan aynan o'zingiz o'qiydigan OTMni tanlaganingizni tekshiring.\n"
        "• Paroldagi katta-kichik harflarga (<code>A</code> va <code>a</code>) hamda bo'sh joy (probel) qolib ketmasligiga e'tibor bering.\n"
        "• Avval brauzer orqali OTMning rasmiy <code>student.hemis.uz</code> sahifasiga kirib, login-parolingiz ishlayotganini tekshirib ko'ring.\n"
        "🔹 <b>HEMIS CAPTCHA yoki vaqtinchalik cheklov chiqsa:</b>\n"
        "• Ko'p marta xato parol terilganda tizim vaqtincha cheklaydi. Rasmiy HEMIS saytiga brauzerdan bir marta kirib chiqing yoki 10–15 daqiqa kutib qayta urinib ko'ring.\n\n"
        "🧭 <b>3. Botning asosiy bo'limlari:</b>\n"
        "📅 <b>Dars jadvali</b> — Hafta kunlari bo'yicha darslar jadvali.\n"
        "📝 <b>Topshiriqlar</b> — Muddati eng yaqin 3 ta topshiriq va avtomatik deadline eslatmalari.\n"
        "📊 <b>Davomat</b> — Fanlar kesimida qoldirilgan soatlar, foiz (<b>%</b>) va <b>25% qizil chiziq</b> ogohlantirishi.\n"
        "📈 <b>Baholar</b> — Fanlar bo'yicha kreditlar va to'plangan ballar.\n"
        "🎯 <b>GPA ballar</b> — Semestr va yillik (kurslar bo'yicha) GPA ko'rsatkichlari.\n"
        "🎓 <b>Imtihonlar</b> — Oraliq va yakuniy nazorat imtihonlari jadvali.\n"
        "📚 <b>Resurslar</b> — Fanlar bo'yicha elektron ma'ruza va taqdimot fayllari.\n"
        "⚙️ <b>Sozlamalar</b> — Dars va topshiriq eslatmalarini boshqarish hamda botni do'stlarga ulashish.\n"
        "🚪 <b>Tizimdan chiqish</b> — Boshqa profilga ulanish uchun joriy HEMIS akkauntdan chiqish.\n\n"
        "🆔 /id — raqamli Telegram ID'ingizni ko'rish.\n"
        f"👨‍💻 Qo'shimcha yordam va murojaat uchun: @{escape(SUPPORT_USERNAME)}"
    )


def settings_kb(user: User) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if user.notify_class else '❌'} Dars eslatmalari",
            callback_data="toggle_class"
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if user.notify_deadline else '❌'} Deadline eslatmalari",
            callback_data="toggle_deadline"
        )],
        [InlineKeyboardButton(
            text="📤 Do'stlarga ulashish",
            callback_data="share_invite",
        )],
    ])


@router.message(F.text == "⚙️ Sozlamalar")
async def settings_handler(message: Message, session: AsyncSession):
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()

    if not user:
        await message.answer("Avval /start buyrug'ini yuboring.")
        return

    await message.answer(
        "⚙️ <b>Sozlamalar</b>\n\n"
        "Eslatmalarni yoqish yoki o'chirish hamda botni do'stlarga ulashish uchun quyidagi tugmalardan foydalaning:",
        reply_markup=settings_kb(user),
    )


@router.callback_query(F.data == "share_invite")
async def share_invite(callback: CallbackQuery):
    await callback.answer()
    share_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Botga ulanish", url=BOT_LINK),
            InlineKeyboardButton(text="📤 Ulashish", url=BOT_SHARE_URL),
        ]
    ])
    share_text = (
        "🚀 <b>HEMIS’ga har safar login-parol terib kirishdan charchadingizmi?</b>\n\n"
        "🎓 <b>«Talaba Yordamchisi»</b> — har bir talabada bo'lishi shart bo'lgan shaxsiy HEMIS bot!\n\n"
        "⏰ Para boshlanishidan 10, 5 daqiqa oldin va dars boshlanganda avto-eslatma\n"
        "🔥 Topshiriq (deadline) muddati tugashidan oldin ogohlantirish\n"
        "📊 Dars jadvali, Davomat (NB), Baholar, Semestr/Yillik GPA va Fan resurslari — hammasi bitta joyda!\n\n"
        "👇 <b>Hoziroq do'stlaringizga ulashing yoki quyidagi tugma orqali botga kiring:</b>"
    )
    if callback.message:
        await callback.message.answer(
            share_text,
            reply_markup=share_kb,
            disable_web_page_preview=True,
        )


async def _toggle_notification(
    callback: CallbackQuery, session: AsyncSession, field: str
):
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Avval /start buyrug'ini yuboring.", show_alert=True)
        return

    setattr(user, field, not getattr(user, field))
    await session.commit()
    await callback.answer("O'zgartirildi!")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=settings_kb(user))


@router.callback_query(F.data == "toggle_class")
async def toggle_class(callback: CallbackQuery, session: AsyncSession):
    await _toggle_notification(callback, session, "notify_class")


@router.callback_query(F.data == "toggle_deadline")
async def toggle_deadline(callback: CallbackQuery, session: AsyncSession):
    await _toggle_notification(callback, session, "notify_deadline")
