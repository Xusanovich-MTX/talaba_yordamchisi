import ctypes
import logging
import os
import time as time_module
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import async_session
from database.models import User, Schedule, Task
from services.hemis_schedule import (
    HemisScheduleError,
    HemisSessionExpired,
    _local_date,
    _timestamp,
    fetch_day_schedule,
)
from services.hemis_session import decrypt_hemis_session, encrypt_hemis_session
from services.hemis_tasks import fetch_upcoming_tasks
from services.hemis_extra import format_morning_motivation
from services.channel_sub import (
    get_channel_config,
    is_user_subscribed,
    subscription_prompt_kb,
    subscription_prompt_text,
    unsubscribed_notified_users,
)

logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Tashkent")

# Global scheduler obyekti
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Eslatilgan narsalarni takror yubormaslik uchun (xotirada saqlaymiz)
notified_schedules: set[str] = set()
notified_tasks: set[str] = set()

# HEMIS ma'lumotlarini har daqiqada qayta so'ramaslik uchun kesh (15 daqiqa)
HEMIS_CACHE_TTL_SECONDS = 900
_hemis_day_cache: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}
_hemis_tasks_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}

# Dars boshlanishidan aniq 10 daqiqa, 5 daqiqa oldin va dars boshlanganda eslatma oynalari
# Masalan, 12:20 dagi dars uchun:
# - 10 daqiqalik eslatma: faqat 12:10:00 .. 12:11:00 oralig'ida
# - 5 daqiqalik eslatma: faqat 12:15:00 .. 12:16:00 oralig'ida (12:13 yoki 12:14 da emas!)
# - 0 daqiqalik (boshlandi) eslatma: faqat 12:20:00 .. 12:21:00 oralig'ida
CLASS_REMINDER_WINDOWS: tuple[tuple[int, timedelta, timedelta], ...] = (
    (10, timedelta(minutes=9), timedelta(minutes=10)),
    (5, timedelta(minutes=4), timedelta(minutes=5)),
    (0, timedelta(minutes=-1), timedelta(minutes=0)),
)

# Topshiriq muddati tugashidan 3 kun, 1 kun va 3 soat oldin eslatma oynalari (daqiqalarda)
DEADLINE_REMINDER_WINDOWS: tuple[tuple[str, float, float, str], ...] = (
    ("task_3d", 4315.0, 4325.0, "⏰ 3 kun qoldi!"),
    ("task_1d", 1435.0, 1445.0, "⏰ 1 kun qoldi!"),
    ("task_3h", 175.0, 185.0, "⚠️ 3 soat qoldi!"),
)


def _parse_lesson_time(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


async def _get_cached_hemis_day_lessons(
    session: AsyncSession, user: User, target_date: date
) -> list[dict[str, Any]]:
    if not user.hemis_api_url or not user.hemis_session_ciphertext:
        return []

    cache_key = (user.id, target_date.isoformat())
    now_mono = time_module.monotonic()
    cached = _hemis_day_cache.get(cache_key)
    if cached is not None and now_mono - cached[0] < HEMIS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
        _, lessons, updated_session = await fetch_day_schedule(
            user.hemis_api_url, session_data, target_date, user.timezone
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
        _hemis_day_cache[cache_key] = (now_mono, lessons)
        return lessons
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        return []
    except (HemisScheduleError, ValueError, RuntimeError) as error:
        logger.warning("HEMIS schedule reminder fetch failed for user %s: %s", user.id, error)
        return cached[1] if cached is not None else []


async def _get_cached_hemis_tasks(
    session: AsyncSession, user: User
) -> list[dict[str, Any]]:
    if not user.hemis_api_url or not user.hemis_session_ciphertext:
        return []

    now_mono = time_module.monotonic()
    cached = _hemis_tasks_cache.get(user.id)
    if cached is not None and now_mono - cached[0] < HEMIS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
        _, tasks, updated_session = await fetch_upcoming_tasks(
            user.hemis_api_url, session_data, user.timezone, limit=50
        )
        if updated_session != session_data:
            user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
            await session.commit()
        _hemis_tasks_cache[user.id] = (now_mono, tasks)
        return tasks
    except HemisSessionExpired:
        user.hemis_session_ciphertext = None
        await session.commit()
        return []
    except (HemisScheduleError, ValueError, RuntimeError) as error:
        logger.warning("HEMIS task reminder fetch failed for user %s: %s", user.id, error)
        return cached[1] if cached is not None else []


async def _check_user_channel_or_stop(bot: Bot, telegram_id: int) -> bool:
    """Foydalanuvchi kanalga a'zo bo'lmasa eslatmani to'xtatib, ogohlantirish yuboradi."""
    cfg = get_channel_config()
    if cfg is None:
        return True
    if await is_user_subscribed(bot, telegram_id, background_check=True):
        return True
    if telegram_id not in unsubscribed_notified_users:
        unsubscribed_notified_users.add(telegram_id)
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=subscription_prompt_text(left=True),
                reply_markup=subscription_prompt_kb(cfg[1]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("Obuna uzilgani xabari xatosi: %s", e)
    return False


async def _send_class_reminder(
    bot: Bot,
    telegram_id: int,
    subject: str,
    sched_time: time,
    room_or_link: str | None,
    minutes_before: int,
    dedup_key: str,
) -> None:
    if dedup_key in notified_schedules:
        return
    status_line = (
        "🚀 <b>Dars boshlandi!</b>"
        if minutes_before == 0
        else f"⏰ {minutes_before} daqiqadan keyin boshlanadi!"
    )
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🔔 <b>Dars eslatmasi!</b>\n\n"
                f"📚 <b>{escape(subject)}</b>\n"
                f"🕐 Boshlanish: <b>{sched_time.strftime('%H:%M')}</b>\n"
                f"📍 {escape(room_or_link) if room_or_link else 'Ko‘rsatilmagan'}\n\n"
                f"{status_line}"
            ),
            parse_mode=ParseMode.HTML,
        )
        notified_schedules.add(dedup_key)
        logger.info("Eslatma yuborildi (%sm): %s - %s", minutes_before, telegram_id, subject)
    except Exception as e:
        logger.error(f"Xatolik: {e}")


async def check_schedule_reminders(bot: Bot):
    """Darsdan 10 daqiqa, 5 daqiqa oldin va dars boshlanganda eslatma yuborish"""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                0x80000000 | 0x00000001 | 0x00000002 | 0x00000040
            )
        except Exception:
            pass
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    earliest_window = now - timedelta(minutes=1)
    latest_window = now + timedelta(minutes=12)

    async with async_session() as session:
        # 0. Barcha ro'yxatdan o'tgan foydalanuvchilarning kanal obunasini tekshirish
        all_users_res = await session.execute(select(User))
        for reg_user in all_users_res.scalars().all():
            await _check_user_channel_or_stop(bot, reg_user.telegram_id)

        # 1. Lokal bazadagi dars jadvallarini tekshirish
        result = await session.execute(
            select(Schedule, User)
            .join(User, Schedule.user_id == User.id)
            .where(
                Schedule.day_of_week.in_([
                    earliest_window.isoweekday(), latest_window.isoweekday()
                ]),
                User.notify_class.is_(True),
            )
        )
        rows = result.all()

        for schedule, user in rows:
            if not await _check_user_channel_or_stop(bot, user.telegram_id):
                continue
            sched_time = schedule.start_time
            schedule_date = (
                earliest_window.date()
                if schedule.day_of_week == earliest_window.isoweekday()
                else latest_window.date()
            )
            starts_at = datetime.combine(schedule_date, sched_time)
            date_str = schedule_date.strftime("%Y%m%d")

            for minutes_before, min_delta, max_delta in CLASS_REMINDER_WINDOWS:
                if now + min_delta <= starts_at <= now + max_delta:
                    key = f"{user.telegram_id}_{schedule.id}_{date_str}_{minutes_before}m"
                    await _send_class_reminder(
                        bot,
                        user.telegram_id,
                        schedule.subject,
                        sched_time,
                        schedule.room_or_link,
                        minutes_before,
                        key,
                    )

        # 2. HEMIS ulangan foydalanuvchilarning bugungi/ertangi darslarini tekshirish
        hemis_users_result = await session.execute(
            select(User).where(
                User.notify_class.is_(True),
                User.hemis_api_url.is_not(None),
                User.hemis_session_ciphertext.is_not(None),
            )
        )
        hemis_users = hemis_users_result.scalars().all()
        target_dates = sorted({earliest_window.date(), latest_window.date()})

        for user in hemis_users:
            if not await _check_user_channel_or_stop(bot, user.telegram_id):
                continue
            if time(6, 58) <= now.time() <= time(7, 2):
                morning_key = f"morning_motivation_{user.telegram_id}_{now.date():%Y%m%d}"
                if morning_key not in notified_schedules:
                    today_lessons = await _get_cached_hemis_day_lessons(session, user, now.date())
                    morning_text = format_morning_motivation(
                        now.date(), today_lessons, user.timezone
                    )
                    if morning_text:
                        try:
                            await bot.send_message(
                                chat_id=user.telegram_id,
                                text=morning_text,
                                parse_mode=ParseMode.HTML,
                            )
                            notified_schedules.add(morning_key)
                        except Exception as e:
                            logger.error(f"Ertalabki motivatsion xabar xatosi: {e}")
                    else:
                        notified_schedules.add(morning_key)

            for target_date in target_dates:
                lessons = await _get_cached_hemis_day_lessons(session, user, target_date)
                date_str = target_date.strftime("%Y%m%d")
                for index, lesson in enumerate(lessons):
                    lesson_date = _local_date(lesson.get("lesson_date"), TIMEZONE) or target_date
                    if lesson_date != target_date:
                        continue
                    pair = lesson.get("lessonPair")
                    subject_dict = lesson.get("subject")
                    if not isinstance(pair, dict) or not isinstance(subject_dict, dict):
                        continue
                    sched_time = _parse_lesson_time(pair.get("start_time"))
                    if sched_time is None:
                        continue

                    subject_name = str(subject_dict.get("name") or "Noma'lum fan")
                    details = []
                    room = lesson.get("auditorium")
                    teacher = lesson.get("employee")
                    if isinstance(room, dict) and room.get("name"):
                        details.append(str(room["name"]))
                    if isinstance(teacher, dict) and teacher.get("name"):
                        details.append(str(teacher["name"]))
                    room_or_link = " | ".join(details) if details else None

                    starts_at = datetime.combine(target_date, sched_time)
                    lesson_id = lesson.get("id") or f"{sched_time:%H%M}_{index}"

                    for minutes_before, min_delta, max_delta in CLASS_REMINDER_WINDOWS:
                        if now + min_delta <= starts_at <= now + max_delta:
                            key = f"hemis_{user.telegram_id}_{lesson_id}_{date_str}_{minutes_before}m"
                            await _send_class_reminder(
                                bot,
                                user.telegram_id,
                                subject_name,
                                sched_time,
                                room_or_link,
                                minutes_before,
                                key,
                            )


async def check_deadline_reminders(bot: Bot):
    """Deadline yaqinlashganda eslatma yuborish (3 kun, 1 kun va 3 soat oldin)"""
    now = datetime.now(TIMEZONE).replace(tzinfo=None)

    async with async_session() as session:
        # 1. Lokal bazadagi topshiriqlarni tekshirish
        result = await session.execute(
            select(Task, User)
            .join(User, Task.user_id == User.id)
            .where(
                Task.is_completed.is_(False),
                User.notify_deadline.is_(True),
                Task.deadline > now,
            )
        )
        rows = result.all()

        for task, user in rows:
            if not await _check_user_channel_or_stop(bot, user.telegram_id):
                continue
            diff = task.deadline - now
            diff_minutes = diff.total_seconds() / 60

            for prefix, min_minutes, max_minutes, time_text in DEADLINE_REMINDER_WINDOWS:
                if min_minutes <= diff_minutes <= max_minutes:
                    key = f"{prefix}_{user.telegram_id}_{task.id}"
                    if key not in notified_tasks:
                        if await send_deadline_msg(bot, user, task, time_text):
                            notified_tasks.add(key)
                    break

        # 2. HEMIS ulangan foydalanuvchilarning yuklanmagan topshiriqlarini tekshirish
        hemis_users_result = await session.execute(
            select(User).where(
                User.notify_deadline.is_(True),
                User.hemis_api_url.is_not(None),
                User.hemis_session_ciphertext.is_not(None),
            )
        )
        hemis_users = hemis_users_result.scalars().all()

        for user in hemis_users:
            if not await _check_user_channel_or_stop(bot, user.telegram_id):
                continue
            hemis_tasks = await _get_cached_hemis_tasks(session, user)
            for item in hemis_tasks:
                deadline_ts = _timestamp(item.get("deadline"))
                if deadline_ts is None:
                    continue
                deadline_dt = datetime.fromtimestamp(deadline_ts, TIMEZONE).replace(tzinfo=None)
                if deadline_dt <= now:
                    continue

                diff_minutes = (deadline_dt - now).total_seconds() / 60
                task_id = item.get("id") or f"{int(deadline_ts)}"
                subject_dict = item.get("subject")
                subject_name = (
                    str(subject_dict.get("name") or "Noma'lum fan")
                    if isinstance(subject_dict, dict)
                    else "Noma'lum fan"
                )
                task_title = str(item.get("name") or "Topshiriq").strip()
                comment = str(item.get("comment") or "").strip()
                description = (
                    f"{task_title} — {comment}"
                    if comment and comment.casefold() != task_title.casefold()
                    else task_title
                )

                for prefix, min_minutes, max_minutes, time_text in DEADLINE_REMINDER_WINDOWS:
                    if min_minutes <= diff_minutes <= max_minutes:
                        key = f"hemis_{prefix}_{user.telegram_id}_{task_id}"
                        if key not in notified_tasks:
                            pseudo_task = Task(
                                user_id=user.id,
                                subject=subject_name,
                                description=description,
                                deadline=deadline_dt,
                            )
                            if await send_deadline_msg(bot, user, pseudo_task, time_text):
                                notified_tasks.add(key)
                        break


async def send_deadline_msg(bot: Bot, user: User, task: Task, time_text: str) -> bool:
    """Deadline xabarini yuborish"""
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"🔔 <b>Deadline eslatmasi!</b>\n\n"
                f"📚 <b>{escape(task.subject)}</b>\n"
                f"📄 {escape(task.description)}\n"
                f"📅 Muddat: <b>{task.deadline.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
                f"{time_text}"
            ),
            parse_mode=ParseMode.HTML,
        )
        logger.info(f"Deadline eslatmasi: {user.telegram_id} - {task.subject}")
        return True
    except Exception as e:
        logger.error(f"Xatolik: {e}")
        return False


def start_scheduler(bot: Bot):
    """Schedulerni ishga tushirish"""
    # Har daqiqada tekshirish
    scheduler.add_job(
        check_schedule_reminders,
        trigger="interval",
        minutes=1,
        args=[bot],
        id="schedule_check",
        replace_existing=True,
    )
    scheduler.add_job(
        check_deadline_reminders,
        trigger="interval",
        minutes=1,
        args=[bot],
        id="deadline_check",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler ishga tushdi.")
