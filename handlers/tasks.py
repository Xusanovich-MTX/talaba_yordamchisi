from datetime import datetime
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Task, User
from handlers.schedule import _hemis_connection_error, _split_telegram_text
from keyboards.reply import main_menu_kb, cancel_kb
from keyboards.inline import task_actions_kb
from services.hemis_schedule import HemisScheduleError, HemisSessionExpired
from services.hemis_session import decrypt_hemis_session, encrypt_hemis_session
from services.hemis_tasks import fetch_upcoming_tasks, format_upcoming_tasks

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
        semester, upcoming_tasks, updated_session = await fetch_upcoming_tasks(
            user.hemis_api_url, session_data, user.timezone, limit=3
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

    text = format_upcoming_tasks(
        upcoming_tasks,
        user.timezone,
        semester_name=str(semester.get("name") or ""),
    )
    chunks = _split_telegram_text(text)
    for index, chunk in enumerate(chunks):
        markup = main_menu_kb(user) if index == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup, disable_web_page_preview=True)


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
