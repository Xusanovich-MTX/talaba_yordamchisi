from datetime import date
from typing import Any

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.hemis_schedule import WEEKDAY_NAMES


def hemis_weekdays_kb(week_dates: list[date]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for day in week_dates:
        builder.button(
            text=f"{WEEKDAY_NAMES[day.weekday()]} · {day:%d.%m}",
            callback_data=f"hemis_day:{day.isoformat()}",
        )
    builder.adjust(2)
    return builder.as_markup()


def resource_subjects_kb(
    semester_code: str, subjects: list[dict[str, Any]]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in subjects:
        subj = item.get("subject")
        if not isinstance(subj, dict) or subj.get("id") is None:
            continue
        subj_id = subj["id"]
        name = str(subj.get("name") or "Fan")
        res_count = int(item.get("resources_count") or 0)
        label = f"📚 {name[:42]} ({res_count})"
        builder.button(
            text=label,
            callback_data=f"hemis_res:{semester_code}:{subj_id}:0",
        )
    builder.adjust(1)
    return builder.as_markup()


def resource_pages_kb(
    semester_code: str, subject_id: int | str, page: int, total_pages: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"hemis_res:{semester_code}:{subject_id}:{page - 1}",
            )
        )
    if page + 1 < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"hemis_res:{semester_code}:{subject_id}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    rows.append([
        InlineKeyboardButton(
            text="🔙 Fanlar ro'yxatiga qaytish",
            callback_data="hemis_res_back",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gpa_semesters_kb(
    semester_summaries: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sem in semester_summaries:
        code = sem["code"]
        name = sem["name"]
        gpa_val = sem["gpa"]
        label = f"📊 {name} ({gpa_val:.2f})" if gpa_val > 0 else f"📊 {name}"
        builder.button(
            text=label,
            callback_data=f"hemis_gpa_sem:{code}",
        )
    builder.adjust(2)
    return builder.as_markup()


def task_actions_kb(task_id: int, is_completed: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if not is_completed:
        builder.button(text="✅ Bajarildi", callback_data=f"task_done:{task_id}")
    builder.button(text="🗑 O'chirish", callback_data=f"task_del:{task_id}")
    builder.adjust(2)
    return builder.as_markup()

def schedule_actions_kb(schedule_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 O'chirish", callback_data=f"sched_del:{schedule_id}")
    return builder.as_markup()
