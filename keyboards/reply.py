from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def is_hemis_connected(user: object | None) -> bool:
    if isinstance(user, bool):
        return user
    if user is None:
        return False
    return bool(
        getattr(user, "hemis_api_url", None)
        and getattr(user, "hemis_session_ciphertext", None)
    )


def main_menu_kb(user_or_connected: object | None = None) -> ReplyKeyboardMarkup:
    connected = is_hemis_connected(user_or_connected)
    rows: list[list[KeyboardButton]] = []
    if not connected:
        rows.append([KeyboardButton(text="🎓 HEMIS ulash")])
    rows.extend([
        [KeyboardButton(text="📅 Dars jadvali"), KeyboardButton(text="📝 Topshiriqlar")],
        [KeyboardButton(text="📊 Davomat"), KeyboardButton(text="📈 Baholar")],
        [KeyboardButton(text="🎯 GPA ballar"), KeyboardButton(text="🎓 Imtihonlar")],
        [KeyboardButton(text="📚 Resurslar"), KeyboardButton(text="⚙️ Sozlamalar")],
    ])
    if connected:
        rows.append([KeyboardButton(text="ℹ️ Yordam"), KeyboardButton(text="🚪 Tizimdan chiqish")])
    else:
        rows.append([KeyboardButton(text="ℹ️ Yordam")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Bo'limni tanlang...",
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )
    return kb
