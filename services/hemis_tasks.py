"""Fetch and format upcoming unsubmitted student tasks from HEMIS."""

import asyncio
from datetime import date, datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from services.hemis_schedule import (
    HemisScheduleError,
    HemisSessionExpired,
    _data_list,
    _local_date,
    _local_timezone,
    _request,
    _timestamp,
    _week_for_date,
)


SUBMITTED_STATUS_CODES = {"12", "13"}
SUBMITTED_STATUS_NAMES = {"topshirdi", "baholandi"}


def _select_current_semester(
    semesters: list[dict[str, Any]], target_date: date, local_timezone: ZoneInfo
) -> dict[str, Any] | None:
    if not semesters:
        return None

    try:
        semester, week = _week_for_date(semesters, target_date, local_timezone)
        if week is not None and semester:
            return semester
    except HemisScheduleError:
        pass

    # Check if target_date falls between the first and last week of any semester (e.g. Sunday)
    for semester in semesters:
        weeks = semester.get("weeks")
        if not isinstance(weeks, list) or not weeks:
            continue
        starts = [
            d for w in weeks if isinstance(w, dict)
            and (d := _local_date(w.get("start_date"), local_timezone)) is not None
        ]
        ends = [
            d for w in weeks if isinstance(w, dict)
            and (d := _local_date(w.get("end_date"), local_timezone)) is not None
        ]
        if starts and ends and min(starts) <= target_date <= max(ends):
            return semester

    # Prefer semester where both semester.current and education_year.current are True
    current_edu_semesters = [
        s for s in semesters
        if bool(s.get("current"))
        and isinstance(s.get("education_year"), dict)
        and bool(s["education_year"].get("current"))
    ]
    if current_edu_semesters:
        return current_edu_semesters[-1]

    current_semesters = [s for s in semesters if bool(s.get("current"))]
    if current_semesters:
        return current_semesters[-1]

    return semesters[-1]


def _is_unsubmitted_task(task: dict[str, Any]) -> bool:
    status = task.get("taskStatus")
    if isinstance(status, dict):
        code = str(status.get("code") or "").strip()
        name = str(status.get("name") or "").strip().casefold()
        if code in SUBMITTED_STATUS_CODES or name in SUBMITTED_STATUS_NAMES:
            return False

    activity = task.get("studentTaskActivity")
    if isinstance(activity, dict) and (
        activity.get("send_date") is not None or activity.get("mark") is not None
    ):
        return False

    attempt_count = task.get("attempt_count")
    if isinstance(attempt_count, (int, float)) and attempt_count > 0:
        return False

    return True


def select_upcoming_tasks(
    tasks: list[dict[str, Any]],
    *,
    now: float | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    current_ts = now if now is not None else datetime.now().timestamp()
    unsubmitted = [t for t in tasks if isinstance(t, dict) and _is_unsubmitted_task(t)]

    upcoming: list[tuple[float, int, dict[str, Any]]] = []
    overdue: list[tuple[float, int, dict[str, Any]]] = []
    no_deadline: list[tuple[int, dict[str, Any]]] = []

    for index, task in enumerate(unsubmitted):
        deadline_ts = _timestamp(task.get("deadline"))
        if deadline_ts is None:
            no_deadline.append((index, task))
        elif deadline_ts >= current_ts:
            upcoming.append((deadline_ts, index, task))
        else:
            overdue.append((deadline_ts, index, task))

    # Closest upcoming deadlines first
    upcoming.sort(key=lambda item: (item[0], item[1]))
    # Most recently expired first if needed to fill remaining slots
    overdue.sort(key=lambda item: (-item[0], item[1]))

    ordered = (
        [item[2] for item in upcoming]
        + [item[2] for item in overdue]
        + [item[1] for item in no_deadline]
    )
    return ordered[: max(0, limit)]


async def fetch_upcoming_tasks(
    api_url: str,
    session_data: dict[str, str],
    timezone_name: str = "Asia/Tashkent",
    *,
    now: float | None = None,
    limit: int = 3,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
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
            semesters = await authorized_get("/education/semesters")
            if not semesters:
                return {}, [], current_session

            local_timezone = _local_timezone(timezone_name)
            current_dt = (
                datetime.fromtimestamp(now, local_timezone)
                if now is not None
                else datetime.now(local_timezone)
            )
            semester = _select_current_semester(
                semesters, current_dt.date(), local_timezone
            )
            if not semester:
                return {}, [], current_session

            semester_code = semester.get("code") or semester.get("id")
            if semester_code is None:
                raise HemisScheduleError("The selected semester has no code.")

            raw_tasks = await authorized_get(
                "/education/task-list", {"semester": str(semester_code)}
            )
            selected = select_upcoming_tasks(
                raw_tasks, now=current_dt.timestamp(), limit=limit
            )
            return semester, selected, current_session
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise HemisScheduleError("Could not reach HEMIS.") from error


def _format_time_remaining(diff_seconds: float) -> str:
    if diff_seconds < 0:
        return "⏰ Muddati o'tgan!"
    total_minutes = int(diff_seconds // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    if days > 0:
        if hours > 0:
            return f"⏳ {days} kun {hours} soat qoldi"
        return f"⏳ {days} kun qoldi"
    if hours > 0:
        if minutes > 0:
            return f"⏳ {hours} soat {minutes} daqiqa qoldi"
        return f"⏳ {hours} soat qoldi"
    return f"⏳ {max(1, minutes)} daqiqa qoldi"


def format_single_task_block(
    task: dict[str, Any],
    idx: int,
    local_timezone,
    current_ts: float,
) -> str:
    subject = task.get("subject")
    subject_name = (
        str(subject.get("name") or "Noma'lum fan")
        if isinstance(subject, dict)
        else "Noma'lum fan"
    )
    title = str(task.get("name") or "Topshiriq").strip()
    comment = str(task.get("comment") or "").strip()

    status_obj = task.get("taskStatus") or {}
    status_code = str(status_obj.get("code") or "")
    status_name = str(status_obj.get("name") or "")

    activity = task.get("studentTaskActivity") or {}
    mark = activity.get("mark")

    if status_code == "13" or mark is not None:
        status_badge = f"🟢 Baholandi: {mark} ball" if mark is not None else "🟢 Baholandi"
    elif status_code == "12":
        status_badge = "🔵 Topshirildi (tekshirilmoqda)"
    else:
        status_badge = "🔴 Bajarilmagan (yuklash kerak)"

    lines = [f"<b>{idx}. 📌 {escape(title)}</b>"]
    lines.append(f"   • Holati: <b>{status_badge}</b>")

    if comment and comment.casefold() != title.casefold():
        lines.append(f"   • Izoh: <i>{escape(comment)}</i>")

    employee = task.get("employee")
    if isinstance(employee, dict) and employee.get("name"):
        lines.append(f"   • O'qituvchi: {escape(str(employee['name']))}")

    max_ball = task.get("max_ball")
    attempt_limit = task.get("attempt_limit")
    meta_parts = []
    if max_ball is not None:
        meta_parts.append(f"Maks. ball: <b>{escape(str(max_ball))}</b>")
    if attempt_limit is not None:
        used = task.get("attempt_count") or 0
        meta_parts.append(f"Urinish: <b>{escape(str(used))}/{escape(str(attempt_limit))}</b>")
    if meta_parts:
        lines.append("   • " + " | ".join(meta_parts))

    deadline_ts = _timestamp(task.get("deadline"))
    if deadline_ts is not None:
        deadline_dt = datetime.fromtimestamp(deadline_ts, local_timezone)
        time_left = _format_time_remaining(deadline_ts - current_ts)
        lines.append(f"   • 📅 Muddat: <b>{deadline_dt:%d.%m.%Y %H:%M}</b> ({time_left})")

    files = task.get("files")
    if isinstance(files, list) and files:
        file_links = []
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            fname = str(file_item.get("name") or "Fayl").strip()
            furl = str(file_item.get("url") or "").strip()
            if furl.startswith(("https://", "http://")):
                file_links.append(f'<a href="{escape(furl, quote=True)}">📎 {escape(fname)}</a>')
            else:
                file_links.append(f"📎 {escape(fname)}")
        if file_links:
            lines.append("   • " + ", ".join(file_links))

    return "\n".join(lines)


def format_subject_tasks_detail(
    subject_name: str,
    tasks: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
    now: float | None = None,
) -> str:
    local_timezone = _local_timezone(timezone_name)
    current_ts = now if now is not None else datetime.now(local_timezone).timestamp()

    lines = [f"📚 <b>{escape(subject_name)} — Topshiriqlar</b>"]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    if not tasks:
        lines.append("✅ Ushbu fandan berilgan topshiriqlar mavjud emas!")
        return "\n".join(lines)

    unsubmitted = [t for t in tasks if str((t.get("taskStatus") or {}).get("code") or "") == "11"]
    lines.append(
        f"📊 <b>Jami topshiriqlar:</b> {len(tasks)} ta\n"
        f"   • 🔴 Bajarilmagan: <b>{len(unsubmitted)} ta</b> | 🟢 Bajarilgan/Baholangan: <b>{len(tasks) - len(unsubmitted)} ta</b>\n"
    )

    blocks: list[str] = []
    for idx, task in enumerate(tasks, 1):
        blocks.append(format_single_task_block(task, idx, local_timezone, current_ts))

    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


def format_upcoming_tasks(
    tasks: list[dict[str, Any]],
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
    now: float | None = None,
) -> str:
    local_timezone = _local_timezone(timezone_name)
    current_ts = now if now is not None else datetime.now(local_timezone).timestamp()

    header = ["📝 <b>HEMIS: yuklashga yaqin topshiriqlar</b>"]
    if semester_name:
        header.append(escape(str(semester_name)))
    header.append("")

    if not tasks:
        header.append("🎉 Hozircha yuklanmagan topshiriqlar topilmadi!")
        return "\n".join(header)

    blocks: list[str] = []
    for idx, task in enumerate(tasks, 1):
        subject = task.get("subject")
        subject_name = (
            str(subject.get("name") or "Noma'lum fan")
            if isinstance(subject, dict)
            else "Noma'lum fan"
        )
        title = str(task.get("name") or "Topshiriq").strip()
        comment = str(task.get("comment") or "").strip()

        lines = [f"<b>{idx}. 📚 {escape(subject_name)}</b>"]
        lines.append(f"📌 <b>Topshiriq:</b> {escape(title)}")
        if comment and comment.casefold() != title.casefold():
            lines.append(f"💬 <b>Izoh:</b> {escape(comment)}")

        employee = task.get("employee")
        if isinstance(employee, dict) and employee.get("name"):
            lines.append(f"👨‍🏫 <b>O'qituvchi:</b> {escape(str(employee['name']))}")

        max_ball = task.get("max_ball")
        attempt_limit = task.get("attempt_limit")
        meta_parts = []
        if max_ball is not None:
            meta_parts.append(f"🎯 Maks. ball: <b>{escape(str(max_ball))}</b>")
        if attempt_limit is not None:
            used = task.get("attempt_count") or 0
            meta_parts.append(f"🔄 Urinish: <b>{escape(str(used))}/{escape(str(attempt_limit))}</b>")
        if meta_parts:
            lines.append(" | ".join(meta_parts))

        deadline_ts = _timestamp(task.get("deadline"))
        if deadline_ts is not None:
            deadline_dt = datetime.fromtimestamp(deadline_ts, local_timezone)
            time_left = _format_time_remaining(deadline_ts - current_ts)
            lines.append(f"📅 <b>Muddat:</b> {deadline_dt:%d.%m.%Y %H:%M} ({time_left})")

        files = task.get("files")
        if isinstance(files, list) and files:
            file_links = []
            for file_item in files:
                if not isinstance(file_item, dict):
                    continue
                fname = str(file_item.get("name") or "Fayl").strip()
                furl = str(file_item.get("url") or "").strip()
                if furl.startswith(("https://", "http://")):
                    file_links.append(f'<a href="{escape(furl, quote=True)}">{escape(fname)}</a>')
                else:
                    file_links.append(escape(fname))
            if file_links:
                lines.append("📎 " + ", ".join(file_links))

        blocks.append("\n".join(lines))

    return "\n".join(header) + "\n" + "\n\n".join(blocks)


async def fetch_task_detail(
    api_url: str,
    session_data: dict[str, str],
    task_id: int | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HemisScheduleError("The selected HEMIS API URL is invalid.")

    current_session = dict(session_data)
    access_token = current_session.get("access_token")
    if not access_token:
        raise HemisSessionExpired("The HEMIS access token is missing.")

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        url = f"{api_url.rstrip('/')}/education/task-detail"
        params = {"task": str(task_id)}
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
            err = (
                payload.get("error")
                if isinstance(payload, dict)
                else f"HEMIS returned HTTP {status}."
            )
            raise HemisScheduleError(str(err or f"HEMIS returned HTTP {status}."))

        data = payload.get("data") if isinstance(payload, dict) else {}
        return data if isinstance(data, dict) else {}, current_session


async def upload_hemis_task(
    api_url: str,
    session_data: dict[str, str],
    task_id: int | str,
    file_bytes: bytes,
    filename: str,
    comment: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HemisScheduleError("The selected HEMIS API URL is invalid.")

    current_session = dict(session_data)
    access_token = current_session.get("access_token")
    if not access_token:
        raise HemisSessionExpired("The HEMIS access token is missing.")

    upload_url = f"{api_url.rstrip('/')}/education/task-upload"
    params = {"task": str(task_id)}

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async def post_upload(token: str) -> tuple[int, Any]:
            form = aiohttp.FormData()
            form.add_field("filename", file_bytes, filename=filename)
            if comment:
                form.add_field("comment", comment)
            async with client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                data=form,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                return response.status, payload

        try:
            status, payload = await post_upload(access_token)
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
                status, payload = await post_upload(access_token)

            if status == 401:
                raise HemisSessionExpired("The HEMIS session expired.")

            if isinstance(payload, dict):
                if not payload.get("success", False) or status >= 400:
                    err_msg = payload.get("error") or f"HEMIS returned HTTP {status}."
                    raise HemisScheduleError(str(err_msg))
                data = payload.get("data")
                return data if isinstance(data, dict) else payload, current_session

            if status >= 400:
                raise HemisScheduleError(f"HEMIS returned HTTP {status}.")

            return {}, current_session
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise HemisScheduleError("Could not reach HEMIS.") from error
