from datetime import datetime
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InaccessibleMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Task, User
from handlers.schedule import _hemis_connection_error, _split_telegram_text
from keyboards.reply import main_menu_kb, cancel_kb
from keyboards.inline import (
    task_actions_kb,
    task_subject_detail_kb,
    task_subjects_kb,
)
from services.hemis_extra import fetch_grades_overview
from services.hemis_schedule import HemisScheduleError, HemisSessionExpired
from services.hemis_session import decrypt_hemis_session, encrypt_hemis_session
from services.hemis_tasks import (
    fetch_upcoming_tasks,
    format_subject_tasks_detail,
    format_upcoming_tasks,
)

router = Router()
logger = logging.getLogger(__name__)


class AddTask(StatesGroup):
    subject = State()
    description = State()
    deadline = State()


async def _send_hemis_upcoming_tasks(
    message: Message, session: AsyncSession, user: User
) -> None:
    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
    except ValueError:
        logger.warning("Stored HEMIS session could not be decrypted.")
        await message.answer(
            "Saqlangan HEMIS ulanishini o'qib bo'lmadi. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except RuntimeError:
        logger.error("HEMIS session encryption is not configured correctly.")
        await message.answer(
            "HEMIS ulanishi sozlamasida xato bor. Administratorga murojaat qiling.",
            reply_markup=main_menu_kb(),
        )
        return

    try:
        semester, subjects, all_tasks, updated_session = await fetch_grades_overview(
            user.hemis_api_url, session_data, user.timezone
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        await message.answer(
            "HEMIS sessiyasi tugagan. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return
    except HemisScheduleError as error:
        logger.warning("HEMIS tasks request failed: %s", error)
        await message.answer(
            "HEMIS topshiriqlarini hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
        return
    except (RuntimeError, ValueError):
        logger.error("Refreshed HEMIS session could not be saved.")
        await message.answer(
            "HEMIS ulanishini saqlab bo'lmadi. 🎓 HEMIS ulash bo'limidan qayta kiring.",
            reply_markup=main_menu_kb(),
        )
        return

    sem_name = str(semester.get("name") or "")
    if not all_tasks:
        await message.answer(
            f"🎉 <b>HEMIS: Topshiriqlar ({sem_name})</b>\n\n"
            "✅ Sizda joriy semestr uchun berilgan topshiriqlar mavjud emas!",
            reply_markup=main_menu_kb(user),
        )
        return

    unsubmitted = [t for t in all_tasks if str((t.get("taskStatus") or {}).get("code") or "") == "11"]
    summary_lines = [
        f"📝 <b>HEMIS: Topshiriqlar ({sem_name})</b>",
        "",
        f"📌 <b>Jami topshiriqlar:</b> {len(all_tasks)} ta",
        f"   • 🔴 Bajarilmagan: <b>{len(unsubmitted)} ta</b>",
        f"   • 🟢 Bajarilgan/Baholangan: <b>{len(all_tasks) - len(unsubmitted)} ta</b>",
        "",
        "👇 <b>Kerakli fanni tanlang</b> — o'sha fandan berilgan topshiriqlar, ularning holati, muddati va fayllarini alohida ko'rasiz:",
    ]
    summary_text = "\n".join(summary_lines)
    kb = task_subjects_kb(subjects, all_tasks)
    await message.answer(summary_text, reply_markup=kb)


@router.callback_query(F.data == "hemis_tasks_back")
async def back_to_task_subjects(callback: CallbackQuery, session: AsyncSession):
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("Topshiriqlar bo'limini qayta oching.", show_alert=True)
        return

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    if not user or not user.hemis_session_ciphertext:
        await callback.answer("Avval HEMIS tizimiga kiring.", show_alert=True)
        return

    await callback.answer("Fanlar yuklanmoqda…")
    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
        semester, subjects, all_tasks, updated_session = await fetch_grades_overview(
            user.hemis_api_url, session_data, user.timezone
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
    except Exception:
        await callback.message.answer(
            "Topshiriqlarni hozir olib bo'lmadi. 📝 Topshiriqlar tugmasini qayta bosing.",
            reply_markup=main_menu_kb(),
        )
        return

    sem_name = str(semester.get("name") or "")
    if not all_tasks:
        try:
            await callback.message.edit_text(
                f"🎉 <b>HEMIS: Topshiriqlar ({sem_name})</b>\n\n"
                "✅ Sizda joriy semestr uchun berilgan topshiriqlar mavjud emas!",
            )
        except Exception:
            await callback.message.answer(
                f"🎉 <b>HEMIS: Topshiriqlar ({sem_name})</b>\n\n"
                "✅ Sizda joriy semestr uchun berilgan topshiriqlar mavjud emas!",
                reply_markup=main_menu_kb(user),
            )
        return

    unsubmitted = [t for t in all_tasks if str((t.get("taskStatus") or {}).get("code") or "") == "11"]
    summary_lines = [
        f"📝 <b>HEMIS: Topshiriqlar ({sem_name})</b>",
        "",
        f"📌 <b>Jami topshiriqlar:</b> {len(all_tasks)} ta",
        f"   • 🔴 Bajarilmagan: <b>{len(unsubmitted)} ta</b>",
        f"   • 🟢 Bajarilgan/Baholangan: <b>{len(all_tasks) - len(unsubmitted)} ta</b>",
        "",
        "👇 <b>Kerakli fanni tanlang</b> — o'sha fandan berilgan topshiriqlar, ularning holati, muddati va fayllarini alohida ko'rasiz:",
    ]
    summary_text = "\n".join(summary_lines)
    kb = task_subjects_kb(subjects, all_tasks)
    try:
        await callback.message.edit_text(summary_text, reply_markup=kb)
    except Exception:
        await callback.message.answer(summary_text, reply_markup=kb)


@router.callback_query(F.data.startswith("hemis_task_subj:"))
async def show_subject_tasks_callback(callback: CallbackQuery, session: AsyncSession):
    prefix, _, subj_id_str = (callback.data or "").partition(":")
    if prefix != "hemis_task_subj" or not subj_id_str.isdecimal():
        await callback.answer("Noto'g'ri so'rov.", show_alert=True)
        return
    subj_id = int(subj_id_str)

    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        await callback.answer("Topshiriqlar bo'limini qayta oching.", show_alert=True)
        return

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    if not user or not user.hemis_session_ciphertext:
        await callback.answer("Avval HEMIS tizimiga kiring.", show_alert=True)
        return

    await callback.answer("Topshiriqlar yuklanmoqda…")
    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
        semester, subjects, all_tasks, updated_session = await fetch_grades_overview(
            user.hemis_api_url, session_data, user.timezone
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
    except Exception:
        await callback.message.answer(
            "Topshiriqlarni hozir olib bo'lmadi. Birozdan keyin qayta urinib ko'ring."
        )
        return

    # Filter tasks and find subject name
    subj_tasks = [t for t in all_tasks if (t.get("subject") or {}).get("id") == subj_id]
    subject_name = "Fan"
    for s_item in subjects:
        s_obj = s_item.get("subject") or {}
        if s_obj.get("id") == subj_id:
            subject_name = str(s_obj.get("name") or "Fan")
            break
    if not subj_tasks and subject_name == "Fan":
        for t in all_tasks:
            s_obj = t.get("subject") or {}
            if s_obj.get("id") == subj_id:
                subject_name = str(s_obj.get("name") or "Fan")
                break

    text = format_subject_tasks_detail(
        subject_name,
        subj_tasks,
        timezone_name=user.timezone,
        semester_name=str(semester.get("name") or ""),
    )
    kb = task_subject_detail_kb()
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# --- Topshiriqlar bo'limiga kirish ---
@router.message(F.text == "📝 Topshiriqlar")
async def tasks_menu(message: Message, session: AsyncSession, state: FSMContext):
    user = await session.scalar(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    if user is None:
        await message.answer("Avval /start buyrug'ini yuboring.")
        return
    await state.clear()

    hemis_error = _hemis_connection_error(user)
    if hemis_error is None:
        await _send_hemis_upcoming_tasks(message, session, user)

    result = await session.execute(
        select(Task).where(
            Task.user_id == user.id,
            Task.is_completed == False
        ).order_by(Task.deadline)
    )
    tasks = result.scalars().all()

    if not tasks:
        if hemis_error is not None:
            await message.answer(
                hemis_error,
                reply_markup=main_menu_kb(),
            )
        return

    await message.answer(f"📝 <b>Faol topshiriqlar ({len(tasks)} ta):</b>")

    for task in tasks:
        # Qolgan vaqtni hisoblash
        now = datetime.now()
        diff = task.deadline - now

        if diff.total_seconds() < 0:
            time_left = "⏰ Muddati o'tgan!"
        elif diff.days > 0:
            time_left = f"⏳ {diff.days} kun qoldi"
        elif diff.seconds > 3600:
            time_left = f"⏳ {diff.seconds // 3600} soat qoldi"
        else:
            time_left = f"⏳ {diff.seconds // 60} daqiqa qoldi"

        text = (
            f"📚 <b>{task.subject}</b>\n"
            f"📄 {task.description}\n"
            f"📅 {task.deadline.strftime('%d.%m.%Y %H:%M')}\n"
            f"{time_left}"
        )

        await message.answer(
            text,
            reply_markup=task_actions_kb(task.id)
        )


# --- Yangi topshiriq qo'shish ---
@router.message(F.text == "/add_task")
async def add_task_start(message: Message, state: FSMContext):
    await state.set_state(AddTask.subject)
    await message.answer(
        "📚 Fan nomini kiriting:\n(masalan: Fizika)",
        reply_markup=cancel_kb()
    )


@router.message(AddTask.subject)
async def add_task_subject(message: Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
        return

    await state.update_data(subject=message.text)
    await state.set_state(AddTask.description)
    await message.answer("📄 Topshiriq tavsifini kiriting:\n(masalan: 5-masalani yechish)")


@router.message(AddTask.description)
async def add_task_description(message: Message, state: FSMContext):
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
        return

    await state.update_data(description=message.text)
    await state.set_state(AddTask.deadline)
    await message.answer(
        "📅 Deadline ni kiriting:\n"
        "Format: <code>DD.MM.YYYY HH:MM</code>\n"
        "Masalan: <code>25.12.2024 23:59</code>"
    )


@router.message(AddTask.deadline)
async def add_task_deadline(message: Message, state: FSMContext, session: AsyncSession):
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())
        return

    user = await session.scalar(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    if user is None:
        await message.answer("Avval /start buyrug'ini yuboring.")
        return

    try:
        deadline = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "❌ Xato format! Iltimos, quyidagi ko'rinishda kiriting:\n"
            "<code>25.12.2024 23:59</code>"
        )
        return

    data = await state.get_data()

    new_task = Task(
        user_id=user.id,
        subject=data["subject"],
        description=data["description"],
        deadline=deadline
    )
    session.add(new_task)
    await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Topshiriq qo'shildi!\n\n"
        f"📚 {data['subject']}\n"
        f"📄 {data['description']}\n"
        f"📅 {deadline.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=main_menu_kb()
    )


async def _owned_callback_task(
    callback: CallbackQuery, session: AsyncSession, expected_prefix: str
) -> Task | None:
    prefix, _, record_id = (callback.data or "").partition(":")
    if (
        prefix != expected_prefix
        or not record_id.isascii()
        or not record_id.isdecimal()
        or len(record_id) > 19
        or not 0 < int(record_id) <= 2**63 - 1
    ):
        await callback.answer("❌ Topilmadi!", show_alert=True)
        return None

    user = await session.scalar(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    if user is None:
        await callback.answer("Avval /start buyrug'ini yuboring.", show_alert=True)
        return None

    task = await session.scalar(
        select(Task).where(Task.id == int(record_id), Task.user_id == user.id)
    )
    if task is None:
        await callback.answer("❌ Topilmadi!", show_alert=True)
    return task


# --- Topshiriqni bajarildi deb belgilash ---
@router.callback_query(F.data.startswith("task_done:"))
async def mark_task_done(callback: CallbackQuery, session: AsyncSession):
    task = await _owned_callback_task(callback, session, "task_done")

    if task:
        task.is_completed = True
        await session.commit()
        await callback.answer("✅ Ajoyib! Bajarildi deb belgilandi.")
        if callback.message:
            await callback.message.edit_text(
                f"✅ <s>{task.subject}</s>\n\n"
                f"Bu topshiriq bajarilgan! 🎉"
            )


# --- Topshiriqni o'chirish ---
@router.callback_query(F.data.startswith("task_del:"))
async def delete_task(callback: CallbackQuery, session: AsyncSession):
    task = await _owned_callback_task(callback, session, "task_del")

    if task:
        await session.delete(task)
        await session.commit()
        await callback.answer("🗑 O'chirildi!")
        if callback.message:
            await callback.message.edit_text("🗑 Topshiriq o'chirildi.")
