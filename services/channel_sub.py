import json
import logging
from pathlib import Path

import config
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

CONFIRMED_FILE = Path(__file__).resolve().parents[1] / "runtime" / "confirmed_channel_users.json"

# Kanaldan chiqib ketganda foydalanuvchiga qayta-qayta xabar yubormaslik uchun
unsubscribed_notified_users: set[int] = set()


def _load_confirmed_users() -> set[int]:
    try:
        if CONFIRMED_FILE.exists():
            data = json.loads(CONFIRMED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {int(x) for x in data}
    except Exception:
        pass
    return set()


def _save_confirmed_users(users: set[int]) -> None:
    try:
        CONFIRMED_FILE.parent.mkdir(exist_ok=True)
        CONFIRMED_FILE.write_text(
            json.dumps(sorted(users)), encoding="utf-8"
        )
    except Exception:
        pass


# Agar bot kanalda admin bo'lmasa (member list is inaccessible), Tasdiqlash bosganlarni saqlab qolish
confirmed_channel_users: set[int] = _load_confirmed_users()


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


async def is_user_subscribed(
    bot: Bot,
    user_id: int,
    *,
    confirming: bool = False,
    background_check: bool = False,
) -> bool:
    """Foydalanuvchi majburiy kanalga a'zo ekanligini tekshiradi.
    Bot admini (egasi) uchun doim True qaytaradi.
    """
    if is_bot_admin(user_id):
        return True

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
            if user_id not in confirmed_channel_users:
                confirmed_channel_users.add(user_id)
                _save_confirmed_users(confirmed_channel_users)
            return True
        if status == "restricted" and getattr(member, "is_member", False):
            unsubscribed_notified_users.discard(user_id)
            if user_id not in confirmed_channel_users:
                confirmed_channel_users.add(user_id)
                _save_confirmed_users(confirmed_channel_users)
            return True
        if user_id in confirmed_channel_users:
            confirmed_channel_users.discard(user_id)
            _save_confirmed_users(confirmed_channel_users)
        return False
    except Exception as exc:
        err_text = str(exc).lower()
        if "member list is inaccessible" in err_text or "chat not found" in err_text:
            if confirming:
                confirmed_channel_users.add(user_id)
                _save_confirmed_users(confirmed_channel_users)
                unsubscribed_notified_users.discard(user_id)
                return True
            if background_check:
                return True
            return user_id in confirmed_channel_users
        logger.warning("Kanal a'zoligini tekshirishda xatolik (%s): %s", chat_id, exc)
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
            "⚠️ <b>Siz kanalga ulanmagansiz!</b>\n\n"
            "Kanaldan chiqib ketganingiz sababli bot o'z ishini va avtomatik eslatmalarni to'xtatdi.\n"
            "Bot qayta ishlashi uchun quyidagi havola orqali kanalga a'zo bo'ling va "
            "<b>«✅ Tasdiqlash»</b> tugmasini bosing:"
        )
    return (
        "🔒 <b>Bot ishga tushishi uchun avval kanalimizga a'zo bo'ling!</b>\n\n"
        "Pastdagi <b>«📢 Kanalga a'zo bo'lish»</b> tugmasi orqali kanalga o'tib obuna bo'ling, "
        "so'ng <b>«✅ Tasdiqlash»</b> tugmasini bosing:"
    )
