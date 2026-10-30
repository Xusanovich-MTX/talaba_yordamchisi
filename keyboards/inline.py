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
        label = f"📚 {name[:36]} ({res_count})"
        builder.button(
            text=label,
            callback_data=f"hemis_res_card:{semester_code}:{subj_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def subject_sections_kb(
    semester_code: str,
    subject_id: int | str,
    res_count: int,
    submits_cnt: int = 0,
    tasks_count: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"📁 Resurslar soni ({res_count})",
        callback_data=f"hemis_res_files:{semester_code}:{subject_id}:0",
    )
    builder.button(
        text=f"📝 Topshiriqlar soni ({submits_cnt}/{tasks_count})",
        callback_data=f"hemis_res_tasks:{semester_code}:{subject_id}",
    )
    builder.button(
        text="🔙 Fanlar ro'yxatiga qaytish",
        callback_data="hemis_res_back",
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
                callback_data=f"hemis_res_files:{semester_code}:{subject_id}:{page - 1}",
            )
        )
    if page + 1 < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"hemis_res_files:{semester_code}:{subject_id}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    rows.append([
        InlineKeyboardButton(
            text="🔙 Fanga qaytish",
            callback_data=f"hemis_res_card:{semester_code}:{subject_id}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_task_submittable(task: dict[str, Any]) -> bool:
    activity = task.get("studentTaskActivity")
    if isinstance(activity, dict) and activity.get("mark") is not None:
        return False

    status_obj = task.get("taskStatus") or {}
    status_code = str(status_obj.get("code") or "")
    if status_code == "13":
        return False

    attempt_limit = task.get("attempt_limit")
    attempt_count = task.get("attempt_count")
    if (
        isinstance(attempt_limit, int)
        and isinstance(attempt_count, int)
        and attempt_count >= attempt_limit
    ):
        return False

    return True


def subject_task_detail_back_kb(
    semester_code: str,
    subject_id: int | str,
    tasks: list[dict[str, Any]] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if tasks:
        submittables = [
            (idx, t) for idx, t in enumerate(tasks, 1)
            if is_task_submittable(t) and t.get("id") is not None
        ]
        for idx, task in submittables:
            tid = task["id"]
            if len(submittables) == 1:
                label = "📤 Topshiriqni yuklash"
            else:
                name = str(task.get("name") or f"{idx}-topshiriq")[:20].strip()
                label = f"📤 {idx}. {name} — topshirish"
            builder.button(
                text=label,
                callback_data=f"hemis_task_up:{tid}",
            )
        builder.adjust(1)

    builder.row(
        InlineKeyboardButton(
            text="🔙 Fanga qaytish",
            callback_data=f"hemis_res_card:{semester_code}:{subject_id}",
        )
    )
    return builder.as_markup()


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


def task_subjects_kb(
    subjects: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    """Faqat topshiriqlari mavjud bo'lgan fanlar ro'yxatini chiqaradi (ortiqcha bo'sh fanlarsiz)."""
    builder = InlineKeyboardBuilder()

    task_map: dict[Any, list[dict[str, Any]]] = {}
    for t in tasks:
        sid = (t.get("subject") or {}).get("id")
        if sid is not None:
            task_map.setdefault(sid, []).append(t)

    added_sids = set()
    # 1. subjects ro'yxatidan topshirig'i bor fanlarni tartib bilan olish
    for item in subjects:
        s_obj = item.get("subject") or {}
        sid = s_obj.get("id")
        if sid is None or sid not in task_map or not task_map[sid]:
            continue
        added_sids.add(sid)
        tlist = task_map[sid]
        sname = str(s_obj.get("name") or "Fan")
        unsubmitted_cnt = sum(
            1 for t in tlist if str((t.get("taskStatus") or {}).get("code") or "") == "11"
        )
        if unsubmitted_cnt > 0:
            badge = f"🔴 {unsubmitted_cnt} ta topshiriq"
        else:
            badge = f"🟢 {len(tlist)} ta bajarilgan"

        label = f"📚 {sname[:34]} ({badge})"
        builder.button(
            text=label,
            callback_data=f"hemis_task_subj:{sid}",
        )

    # 2. Agar qaysidir topshiriqning fani subjects ro'yxatida uchramasa, uni ham qo'shish
    for sid, tlist in task_map.items():
        if sid in added_sids or not tlist:
            continue
        sname = str((tlist[0].get("subject") or {}).get("name") or "Fan")
        unsubmitted_cnt = sum(
            1 for t in tlist if str((t.get("taskStatus") or {}).get("code") or "") == "11"
        )
        if unsubmitted_cnt > 0:
            badge = f"🔴 {unsubmitted_cnt} ta topshiriq"
        else:
            badge = f"🟢 {len(tlist)} ta bajarilgan"

        label = f"📚 {sname[:34]} ({badge})"
        builder.button(
            text=label,
            callback_data=f"hemis_task_subj:{sid}",
        )

    builder.adjust(1)
    return builder.as_markup()


def task_subject_detail_kb(
    tasks: list[dict[str, Any]] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if tasks:
        submittables = [
            (idx, t) for idx, t in enumerate(tasks, 1)
            if is_task_submittable(t) and t.get("id") is not None
        ]
        for idx, task in submittables:
            tid = task["id"]
            if len(submittables) == 1:
                label = "📤 Topshiriqni yuklash"
            else:
                name = str(task.get("name") or f"{idx}-topshiriq")[:20].strip()
                label = f"📤 {idx}. {name} — topshirish"
            builder.button(
                text=label,
                callback_data=f"hemis_task_up:{tid}",
            )
        builder.adjust(1)

    builder.row(
        InlineKeyboardButton(
            text="🔙 Barcha fanlar topshiriqlariga qaytish",
            callback_data="hemis_tasks_back",
        )
    )
    return builder.as_markup()


def task_upload_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="hemis_upload_cancel",
                )
            ]
        ]
    )
