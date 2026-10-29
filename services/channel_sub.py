import logging

import config
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# Kanaldan chiqib ketganda yoki obuna bo'lmaganda foydalanuvchiga qayta-qayta xabar yubormaslik uchun
unsubscribed_notified_users: set[int] = set()

# Agar bot hali kanalga admin qilinmagan bo'lsa, adminga bir marta eslatma yuborish uchun
_admin_warned_inaccessible: bool = False


def is_bot_admin(user_id: int) -> bool:
    admin_ids = getattr(config, "ADMIN_IDS", {6363461555})
    return user_id in admin_ids


def get_channel_config() -> tuple[str | int, str] | None:
    """(chat_id_or_username, channel_url) qaytaradi yoki kanal sozlanmagan bo'lsa None."""
    raw_channel = (getattr(config, "REQUIRED_CHANNEL", "") or "").strip()
    raw_url = (getattr(config, "REQUIRED_CHANNEL_URL", "") or "").strip()
    if not raw_channel:
        return None

    if raw_channel.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        slug = raw_channel.split("t.me/", 1)[1].strip("/").split("?")[0]
        if slug and not slug.startswith("+"):
            chat_id: str | int = f"@{slug.lstrip('@')}"
            url = raw_url or f"https://t.me/{slug.lstrip('@')}"
            return chat_id, url

    if raw_channel.lstrip("-").isdigit():
        chat_id = int(raw_channel)
        url = raw_url or "https://t.me/"
        return chat_id, url

    username = raw_channel.lstrip("@")
    chat_id = f"@{username}"
    url = raw_url or f"https://t.me/{username}"
    return chat_id, url


async def _warn_admin_bot_not_channel_admin(bot: Bot, chat_id: str | int) -> None:
    global _admin_warned_inaccessible
    if _admin_warned_inaccessible:
        return
    _admin_warned_inaccessible = True
    admin_ids = getattr(config, "ADMIN_IDS", {6363461555})
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Kanalga Admin qilish (1 bosishda)",
                    url="https://t.me/assistant_n1_bot?startchannel&admin=invite_users+manage_chat",
                )
            ]
        ]
    )
    text = (
        f"⚠️ <b>Muhim sozlama (Admin uchun):</b>\n\n"
        f"Telegram qoidasiga ko'ra, bot <b>{chat_id}</b> kanaliga a'zo bo'lgan yoki "
        f"chiqib ketgan foydalanuvchilarni haqiqiy tekshira olishi uchun "
        f"<b>@assistant_n1_bot</b> ushbu kanalda <b>Administrator</b> bo'lishi shart!\n\n"
        f"👇 Pastdagi tugmani bosib, <b>Oliy Ta'lim Live ({chat_id})</b> kanalini tanlang va "
        f"botni 1 bosishda Administrator qiling:"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=kb)
        except Exception:
            pass


async def is_user_subscribed(
    bot: Bot,
    user_id: int,
    *,
    confirming: bool = False,
    background_check: bool = False,
) -> bool:
    """Foydalanuvchi majburiy kanalga haqiqatan a'zo ekanligini qat'iy tekshiradi.
    Kanalga a'zo bo'lmagan hech kim (hatto Tasdiqlash tugmasini bossa ham) o'ta olmaydi.
    """
    cfg = get_channel_config()
    if cfg is None:
        return True

    chat_id, _ = cfg
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = getattr(member, "status", None)
        if not isinstance(status, str):
            return True
        if status in ("creator", "administrator", "member"):
            unsubscribed_notified_users.discard(user_id)
            return True
        if status == "restricted" and getattr(member, "is_member", False):
            unsubscribed_notified_users.discard(user_id)
            return True
        return False
    except Exception as exc:
        err_text = str(exc).lower()
        if "member list is inaccessible" in err_text or "chat_admin_required" in err_text:
            logger.warning(
                "Kanal (%s) a'zolar ro'yxati yopiq — botni kanalga Admin qilish kerak: %s",
                chat_id,
                exc,
            )
            await _warn_admin_bot_not_channel_admin(bot, chat_id)
            if background_check:
                return True
            return False
        logger.warning("Kanal a'zoligini tekshirishda xatolik (%s): %s", chat_id, exc)
        if background_check:
            return True
        return False


def subscription_prompt_kb(channel_url: str | None = None) -> InlineKeyboardMarkup:
    if not channel_url:
        cfg = get_channel_config()
        channel_url = cfg[1] if cfg else "https://t.me/"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Kanalga a'zo bo'lish",
                    url=channel_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash (A'zo bo'ldim)",
                    callback_data="check_channel_sub",
                )
            ],
        ]
    )


def subscription_prompt_text(*, left: bool = False) -> str:
    if left:
        return (
            "⚠️ <b>Siz kanalga ulanmagansiz yoki kanaldan chiqib ketdingiz!</b>\n\n"
            "Kanalga a'zo bo'lmasdan turib botdan foydalanish va avtomatik eslatmalarni olish mumkin emas.\n"
            "Bot ishlashi uchun quyidagi tugma orqali kanalga a'zo bo'ling va "
            "<b>«✅ Tasdiqlash (A'zo bo'ldim)»</b> tugmasini bosing:"
        )
    return (
        "🔒 <b>Bot ishga tushishi uchun avval kanalimizga a'zo bo'ling!</b>\n\n"
        "Kanalga a'zo bo'lmasdan turib botdan foydalanib bo'lmaydi.\n"
        "Pastdagi <b>«📢 Kanalga a'zo bo'lish»</b> tugmasi orqali kanalga o'tib obuna bo'ling, "
        "so'ng <b>«✅ Tasdiqlash (A'zo bo'ldim)»</b> tugmasini bosing:"
    )
