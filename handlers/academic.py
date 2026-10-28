"""Handlers for Today summary, Attendance (NB), Grades/Performance, Exams, and Study Resources."""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers.schedule import _hemis_connection_error, _split_telegram_text
from keyboards.inline import gpa_semesters_kb, resource_pages_kb, resource_subjects_kb
from keyboards.reply import main_menu_kb
from services.hemis_extra import (
    calculate_semester_gpas,
    fetch_attendance,
    fetch_exams,
    fetch_gpa_report,
    fetch_grades_overview,
    fetch_semester_subjects,
    fetch_subject_resources,
    format_attendance,
    format_exams,
    format_gpa_report,
    format_grades_overview,
    format_semester_gpa_details,
    format_subject_resources,
)
from services.hemis_schedule import HemisScheduleError, HemisSessionExpired
from services.hemis_session import decrypt_hemis_session, encrypt_hemis_session

router = Router()
logger = logging.getLogger(__name__)


async def _get_connected_user_and_session(
    telegram_id: int,
    session: AsyncSession,
    reply_target,
    *,
    is_callback: bool = False,
) -> tuple[User | None, dict[str, str] | None]:
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_id)
    )
    connection_error = _hemis_connection_error(user)
    if connection_error:
        if is_callback:
            await reply_target.answer(connection_error, show_alert=True)
        else:
            await reply_target.answer(connection_error, reply_markup=main_menu_kb())
        return None, None

    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
        return user, session_data
    except ValueError:
        logger.warning("Stored HEMIS session could not be decrypted.")
        msg = "Saqlangan HEMIS ulanishini o'qib bo'lmadi. 🎓 HEMIS ulash bo'limidan qayta kiring."
    except RuntimeError:
        logger.error("HEMIS session encryption is not configured correctly.")
        msg = "HEMIS ulanishi sozlamasida xato bor. Administratorga murojaat qiling."

    if is_callback and reply_target.message:
        await reply_target.message.answer(msg, reply_markup=main_menu_kb())
    elif not is_callback:
        await reply_target.answer(msg, reply_markup=main_menu_kb())
    return None, None


async def _save_updated_session(
    session: AsyncSession,
    user: User,
    original_session: dict[str, str],
    updated_session: dict[str, str],
) -> None:
    if updated_session != original_session:
        user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
        await session.commit()


async def _send_chunks(
    message: Message, text: str, reply_markup=None
) -> None:
    chunks = _split_telegram_text(text)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await message.answer(
            chunk, reply_markup=markup, disable_web_page_preview=True
        )


# --- 1. DAVOMAT (ATTENDANCE / NB) ---

@router.message(F.text == "📊 Davomat")
async def show_attendance(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user, session_data = await _get_connected_user_and_session(
        message.from_user.id, session, message
    )
    if not user or not session_data:
        return

    try:
        semester, records, updated_session = await fetch_attendance(
            user.hemis_api_url, session_data, user.timezone
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS attendance request failed: %s", error)
        await message.answer(
            "HEMIS davomat ma'lumotlarini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    text = format_attendance(
        records,
        user.timezone,
        semester_name=str(semester.get("name") or ""),
        subjects=semester.get("_subjects"),
    )
    await _send_chunks(message, text, reply_markup=main_menu_kb(user))


# --- 3. BAHOLAR VA O'ZLASHTIRISH (GRADES) ---

@router.message(F.text == "📈 Baholar")
async def show_grades(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user, session_data = await _get_connected_user_and_session(
        message.from_user.id, session, message
    )
    if not user or not session_data:
        return

    try:
        semester, subjects, tasks, updated_session = await fetch_grades_overview(
            user.hemis_api_url, session_data, user.timezone
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS grades request failed: %s", error)
        await message.answer(
            "HEMIS baholar ma'lumotini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    text = format_grades_overview(
        subjects,
        tasks,
        semester_name=str(semester.get("name") or ""),
    )
    await _send_chunks(message, text, reply_markup=main_menu_kb(user))


# --- 3. SEMESTR VA YILLIK GPA BALLARI (GPA) ---

@router.message(F.text == "🎯 GPA ballar")
async def show_gpa(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user, session_data = await _get_connected_user_and_session(
        message.from_user.id, session, message
    )
    if not user or not session_data:
        return

    try:
        semesters, gpa_list, subject_list, updated_session = await fetch_gpa_report(
            user.hemis_api_url, session_data
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS GPA request failed: %s", error)
        await message.answer(
            "HEMIS GPA ma'lumotini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    sem_summaries = calculate_semester_gpas(semesters, subject_list)
    text = format_gpa_report(gpa_list, sem_summaries)
    kb = gpa_semesters_kb(sem_summaries) if sem_summaries else main_menu_kb()
    await _send_chunks(message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("hemis_gpa_sem:"))
async def show_semester_gpa_callback(
    callback: CallbackQuery, session: AsyncSession
):
    _, _, sem_code = (callback.data or "").partition(":")
    if not sem_code or not sem_code.isalnum():
        await callback.answer("Semestr topilmadi.", show_alert=True)
        return
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("GPA bo'limini qayta oching.", show_alert=True)
        return

    user, session_data = await _get_connected_user_and_session(
        callback.from_user.id, session, callback, is_callback=True
    )
    if not user or not session_data:
        return

    await callback.answer("Semestr GPA yuklanmoqda…")
    try:
        semesters, _, subject_list, updated_session = await fetch_gpa_report(
            user.hemis_api_url, session_data
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await callback.message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError:
        await callback.message.answer(
            "HEMIS GPA ma'lumotini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
        )
        return

    sem_summaries = calculate_semester_gpas(semesters, subject_list)
    selected = next((s for s in sem_summaries if s["code"] == sem_code), None)
    if selected is None:
        await callback.message.answer("Tanlangan semestr bo'yicha fanlar topilmadi.")
        return

    text = format_semester_gpa_details(selected)
    kb = gpa_semesters_kb(sem_summaries)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await _send_chunks(callback.message, text, reply_markup=kb)


# --- 4. IMTIHONLAR JADVALI (EXAMS) ---

@router.message(F.text == "🎓 Imtihonlar")
async def show_exams(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user, session_data = await _get_connected_user_and_session(
        message.from_user.id, session, message
    )
    if not user or not session_data:
        return

    try:
        semester, exams, updated_session = await fetch_exams(
            user.hemis_api_url, session_data, user.timezone
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS exams request failed: %s", error)
        await message.answer(
            "HEMIS imtihonlar jadvalini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    text = format_exams(
        exams,
        user.timezone,
        semester_name=str(semester.get("name") or ""),
    )
    await _send_chunks(message, text, reply_markup=main_menu_kb(user))


# --- 5. FAN MATERIALLARI VA RESURSLAR (RESOURCES) ---

@router.message(F.text == "📚 Resurslar")
async def show_resources_menu(
    message: Message, session: AsyncSession, state: FSMContext
):
    await state.clear()
    user, session_data = await _get_connected_user_and_session(
        message.from_user.id, session, message
    )
    if not user or not session_data:
        return

    try:
        semester, subjects, updated_session = await fetch_semester_subjects(
            user.hemis_api_url, session_data, user.timezone
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS subjects for resources failed: %s", error)
        await message.answer(
            "HEMIS fanlar ro'yxatini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return

    sem_code = str(semester.get("code") or semester.get("id") or "")
    sem_name = str(semester.get("name") or "")
    if not subjects or not sem_code:
        await message.answer(
            "📭 Joriy semestr uchun fan resurslari topilmadi.",
            reply_markup=main_menu_kb(),
        )
        return

    await message.answer(
        f"📚 <b>HEMIS: Fan materiallari ({sem_name})</b>\n\n"
        "Kerakli fanni tanlang — ma'ruza va amaliyot fayllarini yuklab olishingiz mumkin:",
        reply_markup=resource_subjects_kb(sem_code, subjects),
    )


@router.callback_query(F.data == "hemis_res_back")
async def back_to_resource_subjects(
    callback: CallbackQuery, session: AsyncSession
):
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("Resurslar bo'limini qayta oching.", show_alert=True)
        return

    user, session_data = await _get_connected_user_and_session(
        callback.from_user.id, session, callback, is_callback=True
    )
    if not user or not session_data:
        return

    await callback.answer("Fanlar yuklanmoqda…")
    try:
        semester, subjects, updated_session = await fetch_semester_subjects(
            user.hemis_api_url, session_data, user.timezone
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except (HemisScheduleError, HemisSessionExpired):
        await callback.message.answer(
            "Fanlar ro'yxatini hozir olib bo'lmadi. 📚 Resurslar tugmasini qayta bosing.",
            reply_markup=main_menu_kb(),
        )
        return

    sem_code = str(semester.get("code") or semester.get("id") or "")
    sem_name = str(semester.get("name") or "")
    await callback.message.edit_text(
        f"📚 <b>HEMIS: Fan materiallari ({sem_name})</b>\n\n"
        "Kerakli fanni tanlang — ma'ruza va amaliyot fayllarini yuklab olishingiz mumkin:",
        reply_markup=resource_subjects_kb(sem_code, subjects),
    )


@router.callback_query(F.data.startswith("hemis_res:"))
async def show_subject_resources_page(
    callback: CallbackQuery, session: AsyncSession
):
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Noto'g'ri so'rov.", show_alert=True)
        return
    _, sem_code, subj_id, page_str = parts
    if not sem_code.isalnum() or not subj_id.isdecimal() or not page_str.isdecimal():
        await callback.answer("Noto'g'ri parametr.", show_alert=True)
        return
    page = int(page_str)

    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("Resurslar bo'limini qayta oching.", show_alert=True)
        return

    user, session_data = await _get_connected_user_and_session(
        callback.from_user.id, session, callback, is_callback=True
    )
    if not user or not session_data:
        return

    await callback.answer("Materiallar yuklanmoqda…")
    try:
        resources, updated_session = await fetch_subject_resources(
            user.hemis_api_url, session_data, sem_code, subj_id
        )
        await _save_updated_session(session, user, session_data, updated_session)
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await callback.message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError:
        await callback.message.answer(
            "HEMIS resurslarini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
        )
        return

    subject_name = ""
    if resources and isinstance(resources[0].get("subject"), dict):
        subject_name = str(resources[0]["subject"].get("name") or "")

    text, total_pages = format_subject_resources(
        resources, subject_name=subject_name, page=page
    )
    kb = resource_pages_kb(sem_code, subj_id, page, total_pages)
    try:
        await callback.message.edit_text(
            text, reply_markup=kb, disable_web_page_preview=True
        )
    except Exception:
        await callback.message.answer(
            text, reply_markup=kb, disable_web_page_preview=True
        )
