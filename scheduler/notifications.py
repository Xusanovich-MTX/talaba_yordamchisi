import ctypes
import json
from pathlib import Path
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from services.hemis_extra import (
    _fmt_hours,
    build_attendance_subjects,
    fetch_attendance,
    format_morning_motivation,
)
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

# Davomat (NB) eslatmalarini takror yubormaslik uchun faylda saqlaymiz
NOTIFIED_ATTENDANCE_PATH = (
    Path(__file__).resolve().parent.parent / "runtime" / "notified_attendance.json"
)


def _load_notified_attendance() -> set[str]:
    if NOTIFIED_ATTENDANCE_PATH.exists():
        try:
            data = json.loads(NOTIFIED_ATTENDANCE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
        except Exception as exc:
            logger.warning("Could not load %s: %s", NOTIFIED_ATTENDANCE_PATH, exc)
    return set()


def _save_notified_attendance(keys: set[str]) -> None:
    try:
        NOTIFIED_ATTENDANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        NOTIFIED_ATTENDANCE_PATH.write_text(
            json.dumps(sorted(keys), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not save %s: %s", NOTIFIED_ATTENDANCE_PATH, exc)


notified_attendance_records: set[str] = _load_notified_attendance()


def _attendance_record_key(user_telegram_id: int, rec: dict[str, Any]) -> str:
    rec_id = rec.get("id")
    if rec_id is not None and str(rec_id).strip():
        return f"nb_{user_telegram_id}_{rec_id}"
    lesson_date = str(rec.get("lesson_date") or "")
    subj_dict = rec.get("subject") or {}
    subj_name = str(subj_dict.get("name") or "")
    hours = int(rec.get("absent_on") or 0) + int(rec.get("absent_off") or 0)
    return f"nb_{user_telegram_id}_{lesson_date}_{subj_name}_{hours}"

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

# Dars tugashidan 10 daqiqa, 5 daqiqa oldin va dars tugaganda eslatma oynalari
CLASS_END_REMINDER_WINDOWS: tuple[tuple[int, timedelta, timedelta], ...] = (
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


async def _send_class_end_reminder(
    bot: Bot,
    telegram_id: int,
    subject: str,
    end_time: time,
    room_or_link: str | None,
    minutes_before: int,
    dedup_key: str,
    extra_text: str | None = None,
) -> None:
    if dedup_key in notified_schedules:
        return
    status_line = (
        "🏁 <b>Dars tugadi! Tanaffus vaqti ☕️</b>"
        if minutes_before == 0
        else f"⏳ <b>Dars tugashiga {minutes_before} daqiqa qoldi!</b>"
    )
    if minutes_before == 0 and extra_text:
        status_line = f"{status_line}\n\n{extra_text}"
    time_label = "Dars yakunlandi" if minutes_before == 0 else "Tugash vaqti"
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🔔 <b>Dars eslatmasi!</b>\n\n"
                f"📚 <b>{escape(subject)}</b>\n"
                f"🕐 {time_label}: <b>{end_time.strftime('%H:%M')}</b>\n"
                f"📍 {escape(room_or_link) if room_or_link else 'Ko‘rsatilmagan'}\n\n"
                f"{status_line}"
            ),
            parse_mode=ParseMode.HTML,
        )
        notified_schedules.add(dedup_key)
        logger.info("Dars tugash eslatmasi yuborildi (%sm): %s - %s", minutes_before, telegram_id, subject)
    except Exception as e:
        logger.error(f"Dars tugash eslatmasi xatolik: {e}")


async def _send_gap_reminder(
    bot: Bot,
    telegram_id: int,
    gap_start_str: str,
    gap_end_str: str,
    next_subject: str,
    next_start: time,
    next_room: str | None,
    dedup_key: str,
) -> None:
    if dedup_key in notified_schedules:
        return
    try:
        room_line = f"📍 {escape(next_room)}\n" if next_room else ""
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🎉 <b>Dars yo‘q soati (Bo‘sh vaqt)!</b>\n\n"
                f"☕️ Hozir sizda dars belgilanmagan (<b>{gap_start_str} – {gap_end_str}</b>).\n"
                f"Dam olishingiz yoki topshiriqlarni tayyorlashingiz mumkin.\n\n"
                f"🕐 <b>Keyingi dars:</b> <b>{next_start.strftime('%H:%M')}</b> da\n"
                f"📚 <b>{escape(next_subject)}</b>\n"
                f"{room_line}"
            ),
            parse_mode=ParseMode.HTML,
        )
        notified_schedules.add(dedup_key)
        logger.info("Dars yo'q soati eslatmasi yuborildi: %s (Keyingi dars: %s)", telegram_id, next_subject)
    except Exception as e:
        logger.error(f"Dars yo'q soati eslatmasi xatolik: {e}")


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

        # 1. Lokal bazadagi dars jadvallarini tekshirish (faqat HEMIS ulanmagan foydalanuvchilar uchun)
        result = await session.execute(
            select(Schedule, User)
            .join(User, Schedule.user_id == User.id)
            .where(
                Schedule.day_of_week.in_([
                    earliest_window.isoweekday(), latest_window.isoweekday()
                ]),
                User.notify_class.is_(True),
                User.hemis_session_ciphertext.is_(None),
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
            ends_at = starts_at + timedelta(minutes=80)
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

            for minutes_before, min_delta, max_delta in CLASS_END_REMINDER_WINDOWS:
                if now + min_delta <= ends_at <= now + max_delta:
                    key = f"{user.telegram_id}_{schedule.id}_{date_str}_end_{minutes_before}m"
                    await _send_class_end_reminder(
                        bot,
                        user.telegram_id,
                        schedule.subject,
                        ends_at.time(),
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
                parsed_lessons = []
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
                    end_time_val = _parse_lesson_time(pair.get("end_time"))
                    if end_time_val is not None:
                        ends_at = datetime.combine(target_date, end_time_val)
                        if ends_at < starts_at:
                            ends_at += timedelta(days=1)
                    else:
                        ends_at = starts_at + timedelta(minutes=80)
                    lesson_id = lesson.get("id") or f"{sched_time:%H%M}_{index}"

                    parsed_lessons.append({
                        "id": lesson_id,
                        "subject": subject_name,
                        "sched_time": sched_time,
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "room_or_link": room_or_link,
                    })

                parsed_lessons.sort(key=lambda x: x["starts_at"])

                for i, item in enumerate(parsed_lessons):
                    lesson_id = item["id"]
                    subject_name = item["subject"]
                    sched_time = item["sched_time"]
                    starts_at = item["starts_at"]
                    ends_at = item["ends_at"]
                    room_or_link = item["room_or_link"]
                    next_item = parsed_lessons[i + 1] if i + 1 < len(parsed_lessons) else None

                    # Dars boshlanishi bo'yicha eslatmalar
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

                    # Dars tugashi bo'yicha eslatmalar
                    extra_text = None
                    if next_item is None:
                        extra_text = "🎉 <b>Bugungi barcha darslar yakunlandi! Maroqli hordiq chiqaring!</b>"
                    else:
                        gap_minutes = (next_item["starts_at"] - ends_at).total_seconds() / 60
                        if gap_minutes > 40:
                            extra_text = (
                                f"☕️ <b>Keyingi soatda sizda dars yo‘q (bo‘sh vaqt / okno 🎉).</b>\n"
                                f"🕐 Keyingi dars: <b>{next_item['starts_at'].strftime('%H:%M')} da</b> ({escape(next_item['subject'])})"
                            )

                    for minutes_before, min_delta, max_delta in CLASS_END_REMINDER_WINDOWS:
                        if now + min_delta <= ends_at <= now + max_delta:
                            key = f"hemis_{user.telegram_id}_{lesson_id}_{date_str}_end_{minutes_before}m"
                            await _send_class_end_reminder(
                                bot,
                                user.telegram_id,
                                subject_name,
                                ends_at.time(),
                                room_or_link,
                                minutes_before,
                                key,
                                extra_text=extra_text if minutes_before == 0 else None,
                            )

                    # Dars yo'q soati (Bo'sh soat / Okno eslatmasi)
                    if next_item is not None:
                        gap_minutes = (next_item["starts_at"] - ends_at).total_seconds() / 60
                        if gap_minutes > 40:
                            gap_start = ends_at + timedelta(minutes=10)
                            gap_key = f"hemis_{user.telegram_id}_{date_str}_gap_{lesson_id}_{next_item['id']}"
                            if gap_start <= now <= next_item["starts_at"] - timedelta(minutes=15):
                                await _send_gap_reminder(
                                    bot,
                                    user.telegram_id,
                                    ends_at.strftime('%H:%M'),
                                    next_item["starts_at"].strftime('%H:%M'),
                                    next_item["subject"],
                                    next_item["starts_at"].time(),
                                    next_item["room_or_link"],
                                    gap_key,
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


async def _send_new_nb_notification(
    bot: Bot,
    user: User,
    rec: dict[str, Any],
    subject_stats: list[dict[str, Any]],
) -> bool:
    """Yangi qo'yilgan NB haqida talabaga foizi va qizil chiziq chegarasi bilan xabar berish."""
    try:
        absent_on = int(rec.get("absent_on") or 0)
        absent_off = int(rec.get("absent_off") or 0)
        explicable = bool(rec.get("explicable"))
        hours = absent_on + absent_off
        if hours == 0:
            hours = 2
        is_excused = absent_on > 0 or explicable
        badge = "🟢 Sababli" if is_excused else "🔴 Sababsiz"

        subject_dict = rec.get("subject") or {}
        subject_name = str(subject_dict.get("name") or "Noma'lum fan").strip()

        training_dict = rec.get("trainingType") or {}
        training_name = str(training_dict.get("name") or "").strip()

        employee_dict = rec.get("employee") or {}
        employee_name = str(employee_dict.get("name") or "").strip()

        pair = rec.get("lessonPair") or {}
        start_t = str(pair.get("start_time") or "")
        end_t = str(pair.get("end_time") or "")
        time_str = f"{start_t}–{end_t}" if start_t and end_t else start_t

        local_tz = ZoneInfo(user.timezone) if user.timezone else TIMEZONE
        date_val = _local_date(rec.get("lesson_date"), local_tz)
        date_str = date_val.strftime("%d.%m.%Y") if date_val else "Noma'lum sana"

        matching_stat = next(
            (
                s
                for s in subject_stats
                if str(s.get("name") or "").strip().casefold()
                == subject_name.casefold()
            ),
            None,
        )

        if matching_stat:
            total_h = int(matching_stat.get("total") or hours)
            pct = float(matching_stat.get("pct") or 0.0)
            remaining_h = float(matching_stat.get("remaining_hours") or 0.0)
            rem_pairs = _fmt_hours(
                matching_stat.get("remaining_pairs") or (remaining_h // 2)
            )
        else:
            total_h = hours
            pct = round((total_h / 60) * 100, 1)
            remaining_h = max(0.0, 15.0 - total_h)
            rem_pairs = _fmt_hours(remaining_h // 2)

        total_pairs = _fmt_hours(total_h / 2)

        if pct >= 25.0:
            status_line = (
                f"🚨 <b>DIQQAT! 25% qizil chiziqqa yetdingiz yoki oshdingiz!</b>\n"
                f"<i>(Oraliq/yakuniy nazoratga kiritilmaslik xavfi mavjud, zudlik bilan dekanatga murojaat qiling!)</i>"
            )
        else:
            status_line = (
                f"🔴 <b>25% qizil chiziqqacha:</b> yana <b>{_fmt_hours(remaining_h)} soat</b> "
                f"({rem_pairs} para) qoldi."
            )

        lines = [
            "⚠️ <b>Davomat eslatmasi: Yangi NB qo'yildi!</b>\n",
            f"📚 <b>Fan:</b> <b>{escape(subject_name, quote=False)}</b>"
            + (f" ({escape(training_name, quote=False)})" if training_name else ""),
            f"📅 <b>Sana va vaqt:</b> {date_str}"
            + (f" | {time_str}" if time_str else ""),
            f"⏰ <b>Qoldirilgan:</b> <b>{hours} soat</b> ({badge})",
        ]
        if employee_name:
            lines.append(f"👨‍🏫 <b>O'qituvchi:</b> {escape(employee_name, quote=False)}")

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━",
            "📊 <b>Ushbu fandan jami davomat:</b>",
            f"• Qoldirilgan: <b>{total_h} soat</b> ({total_pairs} para)",
            f"• NB foizi: <b>{pct}%</b> (Qizil chiziq chegarasi: <b>25%</b>)",
            status_line,
            "━━━━━━━━━━━━━━━━━━━",
            "💡 <i>Darslarni sababsiz qoldirmaslikka harakat qiling!</i>",
        ])
        text = "\n".join(lines)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📊 Batafsil davomat",
                        callback_data="hemis_attendance",
                    )
                ]
            ]
        )

        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        logger.info(
            f"NB eslatmasi yuborildi: user={user.telegram_id}, fan={subject_name}, {hours} soat"
        )
        return True
    except Exception as exc:
        logger.error(f"NB xabarini yuborishda xatolik ({user.telegram_id}): {exc}")
        return False


async def check_attendance_notifications(bot: Bot) -> None:
    """HEMIS tizimidagi yangi NB (dars qoldirish) qaydlarini aniqlash va talabaga xabar berish."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                User.notify_class.is_(True),
                User.hemis_api_url.is_not(None),
                User.hemis_session_ciphertext.is_not(None),
            )
        )
        users = result.scalars().all()

        for user in users:
            try:
                if not await _check_user_channel_or_stop(bot, user.telegram_id):
                    continue

                session_data = decrypt_hemis_session(user.hemis_session_ciphertext)
                if not session_data or not session_data.get("access_token"):
                    continue

                try:
                    semester, records, updated_session = await fetch_attendance(
                        user.hemis_api_url,
                        session_data,
                        timezone_name=user.timezone or "Asia/Tashkent",
                    )
                except (HemisScheduleError, HemisSessionExpired) as exc:
                    logger.debug(f"HEMIS davomat olinmadi (user {user.telegram_id}): {exc}")
                    continue
                except Exception as exc:
                    logger.warning(f"HEMIS davomat tekshirishda xatolik ({user.telegram_id}): {exc}")
                    continue

                if updated_session != session_data:
                    user.hemis_session_ciphertext = encrypt_hemis_session(updated_session)
                    await session.commit()

                init_key = f"att_init_{user.telegram_id}"
                if init_key not in notified_attendance_records:
                    # Birinchi ishga tushganda eski oylardagi NBlardan spam yubormaslik uchun
                    # barcha mavjud NBlarni bazaga belgilab olamiz
                    for rec in records:
                        rec_k = _attendance_record_key(user.telegram_id, rec)
                        notified_attendance_records.add(rec_k)
                    notified_attendance_records.add(init_key)
                    _save_notified_attendance(notified_attendance_records)
                    continue

                # Foydalanuvchi initsializatsiya qilingan bo'lsa, yangi paydo bo'lgan NBlarni tekshiramiz
                subjects = semester.get("_subjects") if isinstance(semester, dict) else []
                subject_stats = build_attendance_subjects(
                    records, subjects, timezone_name=user.timezone or "Asia/Tashkent"
                )

                new_records_found = False
                for rec in records:
                    rec_k = _attendance_record_key(user.telegram_id, rec)
                    if rec_k not in notified_attendance_records:
                        sent = await _send_new_nb_notification(
                            bot, user, rec, subject_stats
                        )
                        if sent:
                            notified_attendance_records.add(rec_k)
                            new_records_found = True

                if new_records_found:
                    _save_notified_attendance(notified_attendance_records)

            except Exception as e:
                logger.error(f"Foydalanuvchi {user.telegram_id} davomatini tekshirishda xatolik: {e}")


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
    scheduler.add_job(
        check_attendance_notifications,
        trigger="interval",
        minutes=2,
        args=[bot],
        id="attendance_check",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler ishga tushdi.")
