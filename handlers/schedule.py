from datetime import date, timedelta
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InaccessibleMessage
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User, Schedule
from keyboards.inline import hemis_weekdays_kb
from keyboards.reply import main_menu_kb
from services.hemis_schedule import (
    HemisScheduleError,
    HemisSessionExpired,
    fetch_day_schedule,
    format_day_schedule,
    schedule_week_dates,
)
from services.hemis_session import decrypt_hemis_session, encrypt_hemis_session

router = Router()
logger = logging.getLogger(__name__)


def _hemis_connection_error(user: User | None) -> str | None:
    if user is None:
        return "Avval /start buyrug'ini yuboring."
    if not user.hemis_institution_name or not user.hemis_api_url:
        return "Avval 🎓 HEMIS ulash bo'limidan o'qish joyingizni tanlang."
    if not user.hemis_session_ciphertext:
        return "HEMIS hali ulanmagan. 🎓 HEMIS ulash bo'limidan login/parol bilan kiring."
    return None

def _split_telegram_text(text: str, limit: int = 3800) -> list[str]:
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@router.message(F.text.in_({"📅 Dars jadvali", "📆 HEMIS jadvali"}))
async def show_hemis_schedule(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user = await session.scalar(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    connection_error = _hemis_connection_error(user)
    if connection_error:
        await message.answer(connection_error, reply_markup=main_menu_kb())
        return

    week_dates = schedule_week_dates(user.timezone)
    monday = week_dates[0]
    saturday = week_dates[-1]
    await message.answer(
        "📅 <b>HEMIS dars jadvali</b>\n"
        f"🗓 <b>Hafta:</b> {monday:%d.%m.%Y} — {saturday:%d.%m.%Y}\n\n"
        "Kunni tanlang. Tanlangan kun jadvali HEMISdan olinadi.",
        reply_markup=hemis_weekdays_kb(week_dates),
    )



@router.callback_query(F.data.startswith("hemis_week:"))
async def switch_hemis_week(callback: CallbackQuery, session: AsyncSession):
    prefix, _, date_text = (callback.data or "").partition(":")
    try:
        monday = date.fromisoformat(date_text)
        if monday.weekday() != 0:
            monday = monday - timedelta(days=monday.weekday())
        week_dates = [monday + timedelta(days=offset) for offset in range(6)]
    except (ValueError, OverflowError):
        await callback.answer("Hafta sanasi noto'g'ri.", show_alert=True)
        return

    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    connection_error = _hemis_connection_error(user)
    if connection_error:
        await callback.answer(connection_error, show_alert=True)
        return

    await callback.answer()
    saturday = week_dates[-1]
    text = (
        "📅 <b>HEMIS dars jadvali</b>\n"
        f"🗓 <b>Hafta:</b> {monday:%d.%m.%Y} — {saturday:%d.%m.%Y}\n\n"
        "Kunni tanlang. Tanlangan kun jadvali HEMISdan olinadi."
    )
    markup = hemis_weekdays_kb(week_dates)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

@router.callback_query(F.data.startswith("hemis_day:"))
async def show_hemis_day(callback: CallbackQuery, session: AsyncSession):
    prefix, _, date_text = (callback.data or "").partition(":")
    try:
        selected_date = date.fromisoformat(date_text)
        if prefix != "hemis_day" or date_text != selected_date.isoformat():
            raise ValueError
        monday = selected_date - timedelta(days=selected_date.weekday())
        week_dates = [monday + timedelta(days=offset) for offset in range(6)]
    except (ValueError, OverflowError):
        await callback.answer("Kun noto'g'ri. Dars jadvalini qayta oching.", show_alert=True)
        return
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("Dars jadvalini qayta oching.", show_alert=True)
        return

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    connection_error = _hemis_connection_error(user)
    if connection_error:
        await callback.answer(connection_error, show_alert=True)
        return

    await callback.answer("HEMISdan jadval olinmoqda…")
    day_keyboard = hemis_weekdays_kb(week_dates)
    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
    except ValueError:
        logger.warning("Stored HEMIS session could not be decrypted.")
        await callback.message.answer(
            "Saqlangan HEMIS ulanishini o'qib bo'lmadi. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except RuntimeError:
        logger.error("HEMIS session encryption is not configured correctly.")
        await callback.message.answer(
            "HEMIS ulanishi sozlamasida xato bor. Administratorga murojaat qiling.",
            reply_markup=main_menu_kb(),
        )
        return

    try:
        semester, lessons, updated_session = await fetch_day_schedule(
            user.hemis_api_url, session_data, selected_date, user.timezone
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await callback.message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS daily schedule request failed: %s", error)
        await callback.message.answer(
            f"\u26a0\ufe0f HEMIS jadvalini olib bo'lmadi.\n\n"
            f"<b>Sabab:</b> {error}\n\n"
            "Birozdan keyin kunni qayta tanlang.",
            reply_markup=day_keyboard,
        )
        return
    except (RuntimeError, ValueError):
        logger.error("Refreshed HEMIS session could not be saved.")
        await callback.message.answer(
            "HEMIS ulanishini saqlab bo'lmadi. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return

    schedule_text = format_day_schedule(
        lessons, selected_date, user.timezone,
        semester_name=str(semester.get("name") or ""),
    )
    chunks = _split_telegram_text(schedule_text)
    for index, chunk in enumerate(chunks):
        markup = day_keyboard if index == len(chunks) - 1 else None
        await callback.message.answer(chunk, reply_markup=markup)


# --- Darsni o'chirish ---
@router.callback_query(F.data.startswith("sched_del:"))
async def delete_schedule(callback: CallbackQuery, session: AsyncSession):
    prefix, _, record_id = (callback.data or "").partition(":")
    if (
        prefix != "sched_del"
        or not record_id.isascii()
        or not record_id.isdecimal()
        or len(record_id) > 19
        or not 0 < int(record_id) <= 2**63 - 1
    ):
        await callback.answer("❌ Topilmadi!", show_alert=True)
        return
    sched_id = int(record_id)

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    if user is None:
        await callback.answer("Avval /start buyrug'ini yuboring.", show_alert=True)
        return

    result = await session.execute(
        select(Schedule).where(Schedule.id == sched_id, Schedule.user_id == user.id)
    )
    schedule = result.scalar_one_or_none()

    if schedule:
        await session.delete(schedule)
        await session.commit()
        await callback.answer("✅ O'chirildi!")
        if callback.message:
            await callback.message.edit_text("🗑 Dars o'chirildi.")
    else:
        await callback.answer("❌ Topilmadi!", show_alert=True)


# --- Orqaga qaytish ---
@router.message(F.text == "⬅️ Orqaga")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Asosiy menyu:", reply_markup=main_menu_kb())
