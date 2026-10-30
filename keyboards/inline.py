from datetime import date, timedelta
from typing import Any

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.hemis_schedule import WEEKDAY_NAMES


def hemis_weekdays_kb(week_dates: list[date]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    active_days = [d for d in week_dates if d.weekday() != 6][:6]
    for day in active_days:
        builder.button(
            text=f"{WEEKDAY_NAMES[day.weekday()]} · {day:%d.%m}",
            callback_data=f"hemis_day:{day.isoformat()}",
        )
    builder.adjust(2)

    if active_days:
        ref_day = active_days[0]
        monday = ref_day - timedelta(days=ref_day.weekday())
        prev_monday = monday - timedelta(days=7)
        next_monday = monday + timedelta(days=7)
        builder.row(
            InlineKeyboardButton(
                text="⬅️ Oldingi hafta",
                callback_data=f"hemis_week:{prev_monday.isoformat()}",
            ),
            InlineKeyboardButton(
                text="Keyingi hafta ➡️",
                callback_data=f"hemis_week:{next_monday.isoformat()}",
            ),
        )

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
            callback_data=f"hemis_res_cat:{semester_code}:{subj_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def resource_categories_kb(
    semester_code: str,
    subject_id: int | str,
    counts: dict[str, int],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    cats = [
        ("lecture", "📖 Ma'ruza"),
        ("lab", "🧪 Laboratoriya"),
        ("practice", "💻 Amaliyot"),
        ("seminar", "👥 Seminar"),
        ("coursework", "📄 Kurs ishi"),
        ("independent", "📝 Mustaqil ta'lim"),
    ]

    for key, title in cats:
        cnt = counts.get(key, 0)
        label = f"{title} ({cnt})"
        builder.button(
            text=label,
            callback_data=f"hemis_res_files:{semester_code}:{subject_id}:{key}:0",
        )
    builder.adjust(2)

    total_cnt = counts.get("all", sum(counts.get(k, 0) for k, _ in cats))
    other_cnt = counts.get("other", 0)
    extra_row = []
    if other_cnt > 0:
        extra_row.append(
            InlineKeyboardButton(
                text=f"📁 Boshqa ({other_cnt})",
                callback_data=f"hemis_res_files:{semester_code}:{subject_id}:other:0",
            )
        )
    extra_row.append(
        InlineKeyboardButton(
            text=f"📁 Barcha fayllar ({total_cnt})",
            callback_data=f"hemis_res_files:{semester_code}:{subject_id}:all:0",
        )
    )
    builder.row(*extra_row)

    builder.row(
        InlineKeyboardButton(
            text="🔙 Fanlar ro'yxatiga qaytish",
            callback_data="hemis_res_back",
        )
    )
    return builder.as_markup()


def resource_pages_kb(
    semester_code: str,
    subject_id: int | str,
    page: int,
    total_pages: int,
    category_key: str = "all",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"hemis_res_files:{semester_code}:{subject_id}:{category_key}:{page - 1}",
            )
        )
    if page + 1 < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"hemis_res_files:{semester_code}:{subject_id}:{category_key}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Bo'limlarga qaytish",
            callback_data=f"hemis_res_cat:{semester_code}:{subject_id}",
        ),
        InlineKeyboardButton(
            text="🔙 Fanlar ro'yxatiga qaytish",
            callback_data="hemis_res_back",
        ),
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


def attendance_subjects_kb(
    subject_stats: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, stats in enumerate(subject_stats):
        name = str(stats.get("name") or "Fan")
        tot_h = int(stats.get("total") or 0)
        pct = float(stats.get("pct") or 0.0)
        if pct >= 25.0:
            icon = "🔴"
        elif pct >= 20.0:
            icon = "🟡"
        else:
            icon = "🟢"
        label = f"{icon} {name[:36]} — {tot_h} s ({pct}%)"
        builder.button(
            text=label,
            callback_data=f"hemis_att:{idx}",
        )
    builder.adjust(1)
    return builder.as_markup()


def attendance_detail_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Barcha fanlar davomatiga qaytish",
                    callback_data="hemis_att_back",
                )
            ]
        ]
    )


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
