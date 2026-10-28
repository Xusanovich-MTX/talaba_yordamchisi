"""Fetch and format attendance, grades/performance, exams, study resources, morning motivation, and GPA from HEMIS."""

import asyncio
from datetime import date, datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from services.hemis_schedule import (
    WEEKDAY_NAMES,
    HemisScheduleError,
    HemisSessionExpired,
    _data_list,
    _local_date,
    _local_timezone,
    _request,
    _timestamp,
    fetch_day_schedule,
)
from services.hemis_tasks import (
    _format_time_remaining,
    _select_current_semester,
    select_upcoming_tasks,
)


async def _hemis_authorized_session(
    api_url: str,
    session_data: dict[str, str],
    callback,
):
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HemisScheduleError("The selected HEMIS API URL is invalid.")

    current_session = dict(session_data)
    access_token = current_session.get("access_token")
    if not access_token:
        raise HemisSessionExpired("The HEMIS access token is missing.")

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async def authorized_get(path: str, params: dict[str, str] | None = None):
            nonlocal access_token
            url = f"{api_url.rstrip('/')}{path}"
            status, payload = await _request(
                client,
                "GET",
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if status == 401:
                refresh_token = current_session.get("refresh_token")
                if not refresh_token:
                    raise HemisSessionExpired("The HEMIS session expired.")
                refresh_status, refresh_payload = await _request(
                    client,
                    "POST",
                    f"{api_url.rstrip('/')}/auth/refresh-token",
                    headers={"X-Refresh-Token": refresh_token},
                )
                refresh_data = (
                    refresh_payload.get("data")
                    if isinstance(refresh_payload, dict)
                    else None
                )
                if (
                    refresh_status >= 400
                    or not isinstance(refresh_data, dict)
                    or not refresh_data.get("token")
                ):
                    raise HemisSessionExpired("The HEMIS session expired.")
                access_token = str(refresh_data["token"])
                current_session["access_token"] = access_token
                current_session["refresh_token"] = str(
                    refresh_data.get("refresh_token") or refresh_token
                )
                status, payload = await _request(
                    client,
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
            if status == 401:
                raise HemisSessionExpired("The HEMIS session expired.")
            if status >= 400:
                raise HemisScheduleError(f"HEMIS returned HTTP {status}.")
            return _data_list(payload)

        try:
            result = await callback(authorized_get)
            return (*result, current_session)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise HemisScheduleError("Could not reach HEMIS.") from error


async def _resolve_semester(
    authorized_get, timezone_name: str, now: float | None = None
) -> dict[str, Any] | None:
    semesters = await authorized_get("/education/semesters")
    if not semesters:
        return None
    local_timezone = _local_timezone(timezone_name)
    current_dt = (
        datetime.fromtimestamp(now, local_timezone)
        if now is not None
        else datetime.now(local_timezone)
    )
    return _select_current_semester(semesters, current_dt.date(), local_timezone)


# --- 1. DAVOMAT (ATTENDANCE) ---

async def fetch_attendance(
    api_url: str,
    session_data: dict[str, str],
    timezone_name: str = "Asia/Tashkent",
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        semester = await _resolve_semester(authorized_get, timezone_name, now)
        if not semester:
            return {}, []
        sem_code = semester.get("code") or semester.get("id")
        params = {"semester": str(sem_code)} if sem_code is not None else None
        records = await authorized_get("/education/attendance", params)
        try:
            subjects = await authorized_get("/education/subjects", params)
        except Exception:
            subjects = []
        semester_copy = dict(semester)
        semester_copy["_subjects"] = subjects
        return semester_copy, records

    return await _hemis_authorized_session(api_url, session_data, _run)


def _resolve_subject_auditorium_hours(
    subject_name: str,
    subject_hours_map: dict[str, int],
    rec: dict[str, Any] | None = None,
) -> int:
    key = subject_name.strip().casefold()
    if key in subject_hours_map and subject_hours_map[key] > 0:
        return subject_hours_map[key]
    if rec:
        explicit_aud = int(rec.get("auditorium_hours") or 0)
        if explicit_aud > 0:
            return explicit_aud
        acload = int(rec.get("total_acload") or 0)
        if acload > 0:
            return acload // 2 if acload >= 60 else acload
    return 60


def _fmt_hours(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def format_attendance(
    records: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
    subjects: list[dict[str, Any]] | None = None,
) -> str:
    local_timezone = _local_timezone(timezone_name)
    lines = ["📊 <b>HEMIS: Davomat (NB va 25% qizil chiziq nazorati)</b>"]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    if not records:
        lines.append("✅ Joriy semestrda qoldirilgan darslar (NB) yo'q! Barakalla! (0%)")
        return "\n".join(lines)

    subject_hours_map: dict[str, int] = {}
    for item in subjects or []:
        if not isinstance(item, dict):
            continue
        subj_info = item.get("subject") or {}
        sname = str(subj_info.get("name") or "").strip().casefold()
        if not sname:
            continue
        aud = int(item.get("auditorium_hours") or 0)
        if aud <= 0:
            acload = int(item.get("total_acload") or 0)
            aud = (acload // 2) if acload >= 60 else (acload if acload > 0 else 60)
        subject_hours_map[sname] = aud

    total_excused = 0
    total_unexcused = 0
    per_subject: dict[str, dict[str, Any]] = {}
    sorted_records = sorted(
        records,
        key=lambda r: _timestamp(r.get("lesson_date")) or 0.0,
        reverse=True,
    )

    item_lines = []
    for idx, rec in enumerate(sorted_records, 1):
        absent_on = int(rec.get("absent_on") or 0)
        absent_off = int(rec.get("absent_off") or 0)
        explicable = bool(rec.get("explicable"))
        hours = absent_on + absent_off
        if hours == 0:
            hours = 2
        is_excused = absent_on > 0 or explicable
        if is_excused:
            total_excused += hours
            badge = "🟢 Sababli"
        else:
            total_unexcused += hours
            badge = "🔴 Sababsiz"

        subject = str((rec.get("subject") or {}).get("name") or "Noma'lum fan")
        entry = per_subject.setdefault(
            subject,
            {
                "excused": 0,
                "unexcused": 0,
                "total": 0,
                "aud_hours": _resolve_subject_auditorium_hours(
                    subject, subject_hours_map, rec
                ),
            },
        )
        entry["total"] += hours
        if is_excused:
            entry["excused"] += hours
        else:
            entry["unexcused"] += hours

        training = (rec.get("trainingType") or {}).get("name") or ""
        employee = (rec.get("employee") or {}).get("name") or ""
        pair = rec.get("lessonPair") or {}
        start_t = str(pair.get("start_time") or "")
        end_t = str(pair.get("end_time") or "")
        time_str = f"{start_t}–{end_t}" if start_t and end_t else start_t

        date_val = _local_date(rec.get("lesson_date"), local_timezone)
        date_str = date_val.strftime("%d.%m.%Y") if date_val else "Noma'lum sana"

        meta = [f"<b>{hours} soat</b> ({badge})"]
        if training:
            meta.append(escape(str(training)))
        if time_str:
            meta.append(escape(time_str))

        row_text = (
            f"<b>{idx}. 📚 {escape(subject)}</b>\n"
            f"📅 {date_str} | {' | '.join(meta)}"
        )
        if employee:
            row_text += f"\n👨‍🏫 {escape(str(employee))}"
        item_lines.append(row_text)

    total_all = total_excused + total_unexcused
    lines.append(
        f"📌 <b>Jami qoldirilgan:</b> {total_all} soat "
        f"(🔴 Sababsiz: <b>{total_unexcused} soat</b> | 🟢 Sababli: <b>{total_excused} soat</b>)"
    )
    lines.append(
        "⚠️ <i>Qoida: Fan bo'yicha davomat <b>25%</b> ga yetsa, talaba qizil chiziqqa tushadi (yakuniy nazoratdan chetlatiladi).</i>"
    )
    lines.append("")
    lines.append("📊 <b>Fanlar kesimida davomat foizi:</b>")

    red_subjects: list[str] = []
    for s_idx, (subj_name, stats) in enumerate(per_subject.items(), 1):
        aud_h = max(int(stats["aud_hours"]), 1)
        tot_h = int(stats["total"])
        unex_h = int(stats["unexcused"])
        exc_h = int(stats["excused"])
        pct = round((tot_h / aud_h) * 100, 1)
        limit_h = aud_h * 0.25
        remaining_h = max(0.0, limit_h - tot_h)

        if pct >= 25.0:
            red_subjects.append(subj_name)
            status_line = (
                f"🚨 <b>QIZIL CHIZIQ ({pct}% >= 25%)!</b> "
                "25% lik chegaradan oshdi — fandan qizilga tushish (chetlatilish) xavfi!"
            )
            pct_badge = f"🔴 <b>{pct}%</b>"
        elif pct >= 20.0:
            status_line = (
                f"⚠️ <b>Xavfli chegara ({pct}%)!</b> "
                f"25% qizil chiziqqa tushishga atigi <b>{_fmt_hours(remaining_h)} soat</b> qoldi!"
            )
            pct_badge = f"🟡 <b>{pct}%</b>"
        else:
            status_line = (
                f"🟢 Me'yorda (25% qizil chiziqqacha yana <b>{_fmt_hours(remaining_h)} soat</b> bor)"
            )
            pct_badge = f"🟢 <b>{pct}%</b>"

        lines.append(
            f"<b>{s_idx}. 📚 {escape(subj_name)}</b> — {pct_badge}\n"
            f"   • Qoldirildi: <b>{tot_h} / {aud_h} soat</b> "
            f"(🔴 Sababsiz: {unex_h} s | 🟢 Sababli: {exc_h} s)\n"
            f"   • Holat: {status_line}"
        )

    if red_subjects:
        lines.append("")
        lines.append(
            "🚨 <b>DIQQAT OGOHLANTIRISH:</b> Sizda "
            f"<b>{', '.join(escape(s) for s in red_subjects)}</b> "
            "fan(lar)idan davomat <b>25%</b> dan oshib qizilga tushgan!"
        )

    lines.append("")
    lines.append("📋 <b>Batafsil NB lar tarixi:</b>")
    lines.append("\n\n".join(item_lines))
    return "\n".join(lines)


# --- 2. BAHOLAR VA O'ZLASHTIRISH (GRADES & SUBJECTS) ---

async def fetch_grades_overview(
    api_url: str,
    session_data: dict[str, str],
    timezone_name: str = "Asia/Tashkent",
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        semester = await _resolve_semester(authorized_get, timezone_name, now)
        if not semester:
            return {}, [], []
        sem_code = semester.get("code") or semester.get("id")
        if sem_code is None:
            return semester, [], []
        params = {"semester": str(sem_code)}
        subjects = await authorized_get("/education/subjects", params)
        tasks = await authorized_get("/education/task-list", params)
        return semester, subjects, tasks

    return await _hemis_authorized_session(api_url, session_data, _run)


def format_grades_overview(
    subjects: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    semester_name: str = "",
) -> str:
    lines = ["📈 <b>HEMIS: Fanlar va O'zlashtirish ko'rsatkichlari</b>"]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    if not subjects:
        lines.append("📭 Joriy semestr uchun fanlar topilmadi.")
        return "\n".join(lines)

    marked_by_subject: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        act = task.get("studentTaskActivity")
        if not isinstance(act, dict) or act.get("mark") is None:
            continue
        subj_name = str((task.get("subject") or {}).get("name") or "").strip()
        task_name = str(task.get("name") or "Topshiriq").strip()
        mark = act.get("mark")
        max_b = task.get("max_ball")
        score_str = f"{mark}/{max_b}" if max_b is not None else str(mark)
        marked_by_subject.setdefault(subj_name, []).append(
            f"   ✅ {escape(task_name)}: <b>{escape(score_str)} ball</b>"
        )

    blocks = []
    for idx, item in enumerate(subjects, 1):
        subj = item.get("subject") or {}
        name = str(subj.get("name") or "Noma'lum fan")
        credit = item.get("credit") or 0
        acload = item.get("total_acload") or 0
        tasks_cnt = item.get("tasks_count") or 0
        submits_cnt = item.get("submits_count") or 0
        marked_cnt = item.get("marked_count") or 0
        student_ball = item.get("student_ball") or 0

        block = [
            f"<b>{idx}. 📚 {escape(name)}</b>",
            f"🎓 Kredit: <b>{escape(str(credit))}</b> ({escape(str(acload))} soat) | "
            f"⭐ To'plangan ball: <b>{escape(str(student_ball))}</b>",
        ]
        if tasks_cnt > 0:
            block.append(
                f"📝 Topshiriqlar: <b>{tasks_cnt} ta</b> "
                f"(Yuklandi: <b>{submits_cnt}</b> | Baholandi: <b>{marked_cnt}</b>)"
            )
        if name in marked_by_subject:
            block.extend(marked_by_subject[name])
        blocks.append("\n".join(block))

    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


# --- 3. IMTIHONLAR JADVALI (EXAMS) ---

async def fetch_exams(
    api_url: str,
    session_data: dict[str, str],
    timezone_name: str = "Asia/Tashkent",
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        semester = await _resolve_semester(authorized_get, timezone_name, now)
        if not semester:
            return {}, []
        sem_code = semester.get("code") or semester.get("id")
        if sem_code is None:
            return semester, []
        exams = await authorized_get("/education/exam-table", {"semester": str(sem_code)})
        return semester, exams

    return await _hemis_authorized_session(api_url, session_data, _run)


def format_exams(
    exams: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
    now: float | None = None,
) -> str:
    local_timezone = _local_timezone(timezone_name)
    current_ts = now if now is not None else datetime.now(local_timezone).timestamp()

    lines = ["🎓 <b>HEMIS: Imtihonlar jadvali</b>"]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    if not exams:
        lines.append("📭 Joriy semestr uchun imtihonlar jadvali hali kiritilmagan.")
        return "\n".join(lines)

    sorted_exams = sorted(
        exams,
        key=lambda e: _timestamp(e.get("examDate")) or float("inf"),
    )

    blocks = []
    for idx, exam in enumerate(sorted_exams, 1):
        subj = (exam.get("subject") or {}).get("name") or "Noma'lum fan"
        exam_type = (exam.get("examType") or {}).get("name") or "Imtihon"
        final_type = (exam.get("finalExamType") or {}).get("name") or ""
        employee = (exam.get("employee") or {}).get("name") or ""
        room = (exam.get("auditorium") or {}).get("name") or ""
        pair = exam.get("lessonPair") or {}
        start_t = str(pair.get("start_time") or "")
        end_t = str(pair.get("end_time") or "")
        time_label = f"{start_t}–{end_t}" if start_t and end_t else start_t

        exam_ts = _timestamp(exam.get("examDate"))
        if exam_ts is not None:
            exam_dt = datetime.fromtimestamp(exam_ts, local_timezone)
            date_str = f"{WEEKDAY_NAMES[exam_dt.weekday()]}, {exam_dt:%d.%m.%Y}"
            remaining = _format_time_remaining(exam_ts - current_ts)
        else:
            date_str = "Sana belgilanmagan"
            remaining = ""

        type_str = f"{exam_type} ({final_type})" if final_type else exam_type
        block = [
            f"<b>{idx}. 📚 {escape(str(subj))}</b>",
            f"🏷 Turi: <b>{escape(type_str)}</b>",
            f"📅 Sana: <b>{escape(date_str)}</b>" + (f" | 🕐 {escape(time_label)}" if time_label else ""),
        ]
        if room or employee:
            details = [escape(str(x)) for x in (room, employee) if x]
            block.append("📍 " + " | ".join(details))
        if remaining:
            block.append(remaining)
        blocks.append("\n".join(block))

    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


# --- 4. FAN MATERIALLARI VA RESURSLAR (RESOURCES) ---

async def fetch_semester_subjects(
    api_url: str,
    session_data: dict[str, str],
    timezone_name: str = "Asia/Tashkent",
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        semester = await _resolve_semester(authorized_get, timezone_name, now)
        if not semester:
            return {}, []
        sem_code = semester.get("code") or semester.get("id")
        if sem_code is None:
            return semester, []
        subjects = await authorized_get("/education/subjects", {"semester": str(sem_code)})
        return semester, subjects

    return await _hemis_authorized_session(api_url, session_data, _run)


async def fetch_subject_resources(
    api_url: str,
    session_data: dict[str, str],
    semester_code: str,
    subject_id: int | str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        resources = await authorized_get(
            "/education/resources",
            {"semester": str(semester_code), "subject": str(subject_id)},
        )
        return (resources,)

    resources, updated_session = await _hemis_authorized_session(
        api_url, session_data, _run
    )
    return resources, updated_session


def format_subject_resources(
    resources: list[dict[str, Any]],
    *,
    subject_name: str = "",
    page: int = 0,
    page_size: int = 8,
) -> tuple[str, int]:
    total = len(resources)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    lines = ["📚 <b>HEMIS: Fan materiallari va ma'ruzalar</b>"]
    if subject_name:
        lines.append(f"<b>{escape(subject_name)}</b> (Jami: {total} ta, sahifa {page + 1}/{total_pages})")
    lines.append("")

    if not resources:
        lines.append("📭 Bu fan bo'yicha elektron resurslar topilmadi.")
        return "\n".join(lines), total_pages

    start = page * page_size
    subset = resources[start : start + page_size]

    blocks = []
    for idx, res in enumerate(subset, start + 1):
        title = str(res.get("title") or "Material").strip().lstrip(".")
        training = (res.get("trainingType") or {}).get("name") or ""
        employee = (res.get("employee") or {}).get("name") or ""

        file_links = []
        items = res.get("subjectFileResourceItems")
        if isinstance(items, list):
            seen_urls = set()
            for r_item in items:
                if not isinstance(r_item, dict):
                    continue
                files = r_item.get("files")
                if isinstance(files, list):
                    for f_obj in files:
                        if not isinstance(f_obj, dict):
                            continue
                        fname = str(f_obj.get("name") or "Yuklab olish").strip()
                        furl = str(f_obj.get("url") or "").strip()
                        if furl and furl not in seen_urls and furl.startswith(("https://", "http://")):
                            seen_urls.add(furl)
                            file_links.append(f'<a href="{escape(furl, quote=True)}">📥 {escape(fname)}</a>')
                ext_url = str(r_item.get("url") or "").strip()
                if ext_url and ext_url not in seen_urls and ext_url.startswith(("https://", "http://")):
                    seen_urls.add(ext_url)
                    file_links.append(f'<a href="{escape(ext_url, quote=True)}">🔗 Havola</a>')

        meta = [escape(str(x)) for x in (training, employee) if x]
        row = [f"<b>{idx}. {escape(title)}</b>"]
        if meta:
            row.append("   <i>" + " | ".join(meta) + "</i>")
        if file_links:
            row.append("   " + " | ".join(file_links))
        blocks.append("\n".join(row))

    lines.append("\n\n".join(blocks))
    return "\n".join(lines), total_pages


# --- 5. ERTALABKI MOTIVATSION XABAR VA KUNLIK XULOSA ---

MORNING_MOTIVATIONS = (
    "🌟 Har bir yangi tong — yangi bilim va katta marralar sari yana bir qadam! Bugungi darslaringizda faol bo'ling!",
    "🚀 Bugungi mehnatingiz — ertangi muvaffaqiyatingiz poydevori! O'zingizga ishoning va olg'a intiling!",
    "💡 Bilim — hech kim tortib ololmaydigan eng katta boylik. Bugungi kunni unumli va yuqori kayfiyatda o'tkazing!",
    "🔥 Katta natijalar kichik, ammo har kungi qat'iy harakatlardan boshlanadi. Bugungi darslarda omad yor bo'lsin!",
    "🎯 Maqsadingiz sari dadil qadam tashlang! Bugun o'rgangan har bir yangi bilimingiz kelajakda sizga xizmat qiladi!",
    "✨ Ilm olishdan charchamang — bugungi tirishqoqligingiz sizni eng kuchli mutaxassisga aylantiradi!",
    "🏆 O'z ustida ishlagan inson hech qachon yutqazmaydi. Kuningiz barakali va omadli o'tsin!",
)


def format_morning_motivation(
    today_date: date,
    lessons: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
) -> str | None:
    local_timezone = _local_timezone(timezone_name)
    lesson_rows = []
    for lesson in lessons:
        if _local_date(lesson.get("lesson_date"), local_timezone) != today_date:
            continue
        subj = (lesson.get("subject") or {}).get("name") or "Noma'lum fan"
        pair = lesson.get("lessonPair") or {}
        start_t = str(pair.get("start_time") or "--:--")
        end_t = str(pair.get("end_time") or "")
        time_label = f"{start_t}–{end_t}" if end_t else start_t
        room = (lesson.get("auditorium") or {}).get("name") or ""
        emp = (lesson.get("employee") or {}).get("name") or ""
        extra = " | ".join(x for x in (room, emp) if x)
        lesson_rows.append(
            (
                start_t,
                f"• <b>{escape(time_label)}</b> — {escape(str(subj))}"
                + (f" ({escape(extra)})" if extra else ""),
            )
        )

    if not lesson_rows:
        return None

    date_label = f"{WEEKDAY_NAMES[today_date.weekday()]}, {today_date:%d.%m.%Y}"
    quote = MORNING_MOTIVATIONS[today_date.toordinal() % len(MORNING_MOTIVATIONS)]

    lines = [
        f"🌅 <b>Xayrli tong! ({date_label})</b>",
        "",
        f"<i>{escape(quote)}</i>",
        "",
        f"📅 <b>Bugun sizda {len(lesson_rows)} para dars bor:</b>",
    ]
    for _, row_text in sorted(lesson_rows):
        lines.append(row_text)
    lines.append("")
    lines.append("💪 <b>Kuningiz unumli va omadli o'tsin!</b>")
    return "\n".join(lines)


def format_today_summary(
    today_date: date,
    lessons: list[dict[str, Any]],
    upcoming_tasks: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
    now: float | None = None,
) -> str:
    local_timezone = _local_timezone(timezone_name)
    current_ts = now if now is not None else datetime.now(local_timezone).timestamp()
    date_label = f"{WEEKDAY_NAMES[today_date.weekday()]}, {today_date:%d.%m.%Y}"

    lines = [
        f"☀️ <b>Bugungi kun rejasi — {date_label}</b>",
    ]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    lines.append("📅 <b>Bugungi darslar:</b>")
    lesson_rows = []
    for lesson in lessons:
        if _local_date(lesson.get("lesson_date"), local_timezone) != today_date:
            continue
        subj = (lesson.get("subject") or {}).get("name") or "Noma'lum fan"
        pair = lesson.get("lessonPair") or {}
        start_t = str(pair.get("start_time") or "--:--")
        end_t = str(pair.get("end_time") or "")
        time_label = f"{start_t}–{end_t}" if end_t else start_t
        room = (lesson.get("auditorium") or {}).get("name") or ""
        emp = (lesson.get("employee") or {}).get("name") or ""
        extra = " | ".join(x for x in (room, emp) if x)
        lesson_rows.append(
            (start_t, f"• <b>{escape(time_label)}</b> — {escape(str(subj))}" + (f" ({escape(extra)})" if extra else ""))
        )

    if not lesson_rows:
        lines.append("🎉 Bugun HEMIS jadvalida darslar yo'q.")
    else:
        for _, row_text in sorted(lesson_rows):
            lines.append(row_text)

    lines.append("")
    lines.append("📝 <b>Yuklashga eng yaqin topshiriqlar:</b>")
    if not upcoming_tasks:
        lines.append("✅ Yuklanmagan topshiriqlar yo'q!")
    else:
        for idx, task in enumerate(upcoming_tasks, 1):
            subj = (task.get("subject") or {}).get("name") or "Noma'lum fan"
            title = str(task.get("name") or "Topshiriq").strip()
            dl_ts = _timestamp(task.get("deadline"))
            if dl_ts is not None:
                dl_dt = datetime.fromtimestamp(dl_ts, local_timezone)
                rem = _format_time_remaining(dl_ts - current_ts)
                dl_str = f"{dl_dt:%d.%m.%Y %H:%M} ({rem})"
            else:
                dl_str = "Muddat belgilanmagan"
            lines.append(
                f"{idx}. <b>{escape(str(subj))}</b> — {escape(title)}\n"
                f"   📅 {dl_str}"
            )

    return "\n".join(lines)


# --- 6. SEMESTR VA YILLIK GPA BALLARI (GPA REPORT) ---

async def fetch_gpa_report(
    api_url: str,
    session_data: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    async def _run(authorized_get):
        semesters = await authorized_get("/education/semesters")
        gpa_list = await authorized_get("/education/gpa-list")
        subject_list = await authorized_get("/education/subject-list")
        return semesters, gpa_list, subject_list

    return await _hemis_authorized_session(api_url, session_data, _run)


def calculate_semester_gpas(
    semesters: list[dict[str, Any]],
    subject_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sem_names: dict[str, str] = {}
    sem_edu_years: dict[str, str] = {}
    for s in semesters:
        if not isinstance(s, dict):
            continue
        code = str(s.get("code") or s.get("id") or "").strip()
        if not code:
            continue
        sem_names[code] = str(s.get("name") or f"{code}-semestr")
        edu = s.get("education_year")
        if isinstance(edu, dict) and edu.get("name"):
            sem_edu_years[code] = str(edu["name"])

    by_sem: dict[str, list[dict[str, Any]]] = {}
    for item in subject_list:
        if not isinstance(item, dict):
            continue
        sem_code = str(item.get("_semester") or "").strip()
        if not sem_code:
            cs = item.get("curriculumSubject")
            if isinstance(cs, dict):
                sem_code = str(cs.get("_semester") or "").strip()
        if not sem_code:
            continue
        by_sem.setdefault(sem_code, []).append(item)

    summaries: list[dict[str, Any]] = []
    for sem_code in sorted(by_sem.keys(), key=lambda x: (int(x) if x.isdigit() else 999, x)):
        items = by_sem[sem_code]
        if sem_code in sem_names:
            name = sem_names[sem_code]
        elif sem_code.isdigit() and int(sem_code) > 10:
            name = f"{int(sem_code) - 10}-semestr"
        else:
            name = f"{sem_code}-semestr"

        weighted_sum = 0.0
        graded_credits = 0.0
        total_credits = 0.0
        graded_count = 0
        subject_rows = []

        for s_item in items:
            cs = s_item.get("curriculumSubject") or {}
            subj = cs.get("subject") or {}
            subj_name = str(subj.get("name") or "Noma'lum fan")
            try:
                credit = float(cs.get("credit") or 0)
            except (TypeError, ValueError):
                credit = 0.0
            overall = s_item.get("overallScore") or {}
            try:
                grade = float(overall.get("grade") or 0)
            except (TypeError, ValueError):
                grade = 0.0
            try:
                percent = float(overall.get("percent") or 0)
            except (TypeError, ValueError):
                percent = 0.0

            total_credits += credit
            if grade > 0 and credit > 0:
                weighted_sum += grade * credit
                graded_credits += credit
                graded_count += 1

            subject_rows.append({
                "name": subj_name,
                "credit": credit,
                "grade": grade,
                "percent": percent,
            })

        gpa_val = round(weighted_sum / graded_credits, 2) if graded_credits > 0 else 0.0
        summaries.append({
            "code": sem_code,
            "name": name,
            "education_year": sem_edu_years.get(sem_code, ""),
            "gpa": gpa_val,
            "graded_credits": graded_credits,
            "total_credits": total_credits,
            "graded_count": graded_count,
            "total_count": len(items),
            "subjects": subject_rows,
        })

    return summaries


def format_gpa_report(
    gpa_list: list[dict[str, Any]],
    semester_summaries: list[dict[str, Any]],
) -> str:
    lines = ["🎯 <b>HEMIS: Semestr va Yillik GPA ko'rsatkichlari</b>", ""]

    # 1. Yillik GPA (/education/gpa-list)
    lines.append("🏛 <b>Yillik GPA (Kurslar bo'yicha):</b>")
    if not gpa_list:
        lines.append("📭 Yillik GPA ma'lumotlari topilmadi.")
    else:
        sorted_yearly = sorted(
            gpa_list,
            key=lambda g: str((g.get("level") or {}).get("code") or (g.get("educationYear") or {}).get("code") or ""),
        )
        yearly_vals = []
        for item in sorted_yearly:
            level_name = str((item.get("level") or {}).get("name") or "Kurs")
            edu_year = str((item.get("educationYear") or {}).get("name") or "")
            gpa_str = str(item.get("gpa") or "0.00")
            credits = str(item.get("credit_sum") or "0")
            subjects_cnt = item.get("subjects") or 0
            debts = int(item.get("debt_subjects") or 0)
            try:
                yearly_vals.append(float(gpa_str))
            except ValueError:
                pass
            year_label = f" ({escape(edu_year)})" if edu_year else ""
            debt_label = f" | ⚠️ Qarzdorlik: {debts}" if debts > 0 else ""
            lines.append(
                f"• <b>{escape(level_name)}</b>{year_label}: "
                f"GPA <b>{escape(gpa_str)}</b> "
                f"(🎓 {escape(credits)} kredit, {subjects_cnt} ta fan{debt_label})"
            )
        if yearly_vals:
            overall_avg = sum(yearly_vals) / len(yearly_vals)
            lines.append(f"⭐ <b>Umumiy o'rtacha GPA:</b> <code>{overall_avg:.2f}</code>")

    lines.append("")
    # 2. Semestrlar bo'yicha GPA
    lines.append("📊 <b>Semestrlar bo'yicha GPA:</b>")
    if not semester_summaries:
        lines.append("📭 Semestrlar bo'yicha ma'lumot topilmadi.")
    else:
        for sem in semester_summaries:
            name = escape(sem["name"])
            edu_y = f" ({escape(sem['education_year'])})" if sem["education_year"] else ""
            if sem["gpa"] > 0:
                lines.append(
                    f"• <b>{name}</b>{edu_y}: GPA <b>{sem['gpa']:.2f}</b> "
                    f"(🎓 {sem['graded_credits']:g} kredit, {sem['graded_count']}/{sem['total_count']} fan)"
                )
            else:
                lines.append(
                    f"• <b>{name}</b>{edu_y}: <i>Joriy o'quv jarayonida</i> "
                    f"(🎓 {sem['total_credits']:g} kredit, {sem['total_count']} fan)"
                )

    lines.append("")
    lines.append("👇 <i>Semestr fanlari va baholarini batafsil ko'rish uchun quyidagi tugmani tanlang:</i>")
    return "\n".join(lines)


def format_semester_gpa_details(sem_summary: dict[str, Any]) -> str:
    name = escape(sem_summary["name"])
    edu_y = f" ({escape(sem_summary['education_year'])})" if sem_summary.get("education_year") else ""
    gpa_val = sem_summary["gpa"]

    lines = [
        f"📊 <b>{name}{edu_y} — Fanlar va GPA</b>",
        f"🎯 Semestr GPA: <b>{gpa_val:.2f}</b> | "
        f"🎓 Kreditlar: <b>{sem_summary['graded_credits']:g}/{sem_summary['total_credits']:g}</b>",
        "",
    ]

    for idx, subj in enumerate(sem_summary.get("subjects", []), 1):
        s_name = escape(subj["name"])
        credit = subj["credit"]
        grade = subj["grade"]
        percent = subj["percent"]
        if grade > 0:
            grade_str = f"Baho: <b>{grade:g}/5</b> ({percent:g}%)"
        else:
            grade_str = "<i>Hali yakuniy baho qo'yilmagan</i>"
        lines.append(
            f"<b>{idx}. 📚 {s_name}</b>\n"
            f"   🎓 Kredit: <b>{credit:g}</b> | {grade_str}"
        )

    return "\n".join(lines)
