"""Read HEMIS teaching weeks and display schedules for selected calendar days."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp


WEEKDAY_NAMES = (
    "Dushanba", "Seshanba", "Chorshanba", "Payshanba",
    "Juma", "Shanba", "Yakshanba",
)


class HemisScheduleError(RuntimeError):
    pass


class HemisSessionExpired(HemisScheduleError):
    pass


async def _request(
    client: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, Any]:
    async with client.request(method, url, headers=headers, params=params) as response:
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            payload = None
        return response.status, payload


def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return None
    return None


def _data_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise HemisScheduleError("HEMIS returned an unsuccessful response.")
    data = payload.get("data")
    if not isinstance(data, list):
        raise HemisScheduleError("HEMIS returned an unexpected response.")
    return [item for item in data if isinstance(item, dict)]


def _local_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo("Asia/Tashkent")


def _local_date(value: object, local_timezone: ZoneInfo) -> date | None:
    timestamp = _timestamp(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, local_timezone).date()
    except (OverflowError, OSError, ValueError):
        return None


def schedule_week_dates(
    timezone_name: str = "Asia/Tashkent", *, now: datetime | None = None
) -> list[date]:
    local_timezone = _local_timezone(timezone_name)
    current = now if now is not None else datetime.now(local_timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_timezone)
    today = current.astimezone(local_timezone).date()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=offset) for offset in range(7)]


def _week_for_date(
    semesters: list[dict[str, Any]], selected_date: date, local_timezone: ZoneInfo
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    ordered_semesters = sorted(semesters, key=lambda item: not bool(item.get("current")))
    if ordered_semesters and not any(
        isinstance(semester.get("weeks"), list) for semester in ordered_semesters
    ):
        raise HemisScheduleError("HEMIS returned no semester week lists.")

    for semester in ordered_semesters:
        weeks = semester.get("weeks")
        if not isinstance(weeks, list):
            continue
        for week in weeks:
            if not isinstance(week, dict):
                continue
            end = _local_date(week.get("end_date"), local_timezone)
            start = _local_date(week.get("start_date"), local_timezone)
            # Older HEMIS responses may omit start_date; a teaching week
            # still cannot include dates more than six days before its end.
            if start is None and end is not None:
                start = end - timedelta(days=6)
            if start is not None and end is not None and start <= selected_date <= end:
                if week.get("id") is None:
                    raise HemisScheduleError("The selected teaching week has no ID.")
                return semester, week
    return {}, None


async def fetch_current_week_schedule(
    api_url: str,
    session_data: dict[str, str],
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    return await _fetch_schedule(api_url, session_data, now=now)


async def fetch_day_schedule(
    api_url: str,
    session_data: dict[str, str],
    selected_date: date,
    timezone_name: str = "Asia/Tashkent",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    return await _fetch_schedule(
        api_url, session_data, selected_date=selected_date, timezone_name=timezone_name
    )


async def _fetch_schedule(
    api_url: str,
    session_data: dict[str, str],
    *,
    now: float | None = None,
    selected_date: date | None = None,
    timezone_name: str = "Asia/Tashkent",
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
            if not semesters and selected_date is None:
                raise HemisScheduleError("No HEMIS semesters were returned.")
            local_timezone = _local_timezone(timezone_name)
            target_date = selected_date or (
                datetime.fromtimestamp(now, local_timezone).date()
                if now is not None else datetime.now(local_timezone).date()
            )
            semester, week = _week_for_date(
                semesters, target_date, local_timezone
            )
            if week is None:
                if selected_date is not None:
                    return {}, [], current_session
                raise HemisScheduleError("No current teaching week was found.")
            lessons = await authorized_get(
                "/education/schedule", {"week": str(week["id"])}
            )
            if selected_date is not None:
                lessons = [
                    lesson for lesson in lessons
                    if _local_date(lesson.get("lesson_date"), local_timezone) == selected_date
                ]
            return semester, lessons, current_session
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise HemisScheduleError("Could not reach HEMIS.") from error


def format_week_schedule(
    lessons: list[dict[str, Any]], timezone_name: str = "Asia/Tashkent"
) -> str:
    local_timezone = _local_timezone(timezone_name)
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for lesson in lessons:
        timestamp = _timestamp(lesson.get("lesson_date"))
        subject = lesson.get("subject")
        pair = lesson.get("lessonPair")
        if timestamp is None or not isinstance(subject, dict) or not isinstance(pair, dict):
            continue
        lesson_time = str(pair.get("start_time") or "--:--")
        end_time = str(pair.get("end_time") or "")
        title = str(subject.get("name") or "Noma'lum fan")
        teacher = lesson.get("employee")
        room = lesson.get("auditorium")
        details = []
        if isinstance(teacher, dict) and teacher.get("name"):
            details.append(str(teacher["name"]))
        if isinstance(room, dict) and room.get("name"):
            details.append(str(room["name"]))
        date = datetime.fromtimestamp(timestamp, local_timezone)
        day = WEEKDAY_NAMES[date.weekday()]
        date_label = date.strftime("%d.%m.%Y")
        grouped.setdefault(f"{day}, {date_label}", []).append(
            (lesson_time, end_time, title + (" | " + " | ".join(details) if details else ""))
        )

    if not grouped:
        return "📭 Joriy hafta uchun HEMIS dars jadvali topilmadi."

    lines = ["📅 <b>HEMIS: joriy hafta dars jadvali</b>", ""]
    for day, day_lessons in grouped.items():
        lines.append(f"<b>{day}</b>")
        for start_time, end_time, title in sorted(day_lessons):
            time_label = f"{start_time}–{end_time}" if end_time else start_time
            lines.append(f"{escape(time_label)}  {escape(title)}")
        lines.append("")
    return "\n".join(lines).strip()


def format_day_schedule(
    lessons: list[dict[str, Any]],
    selected_date: date,
    timezone_name: str = "Asia/Tashkent",
    *,
    semester_name: str = "",
) -> str:
    local_timezone = _local_timezone(timezone_name)
    date_label = f"{WEEKDAY_NAMES[selected_date.weekday()]}, {selected_date:%d.%m.%Y}"
    lines = ["📅 <b>HEMIS dars jadvali</b>", f"<b>{date_label}</b>"]
    if semester_name:
        lines.append(escape(str(semester_name)))
    lines.append("")

    rows = []
    for lesson in lessons:
        if _local_date(lesson.get("lesson_date"), local_timezone) != selected_date:
            continue
        subject = lesson.get("subject")
        pair = lesson.get("lessonPair")
        if not isinstance(subject, dict) or not isinstance(pair, dict):
            continue
        start_time = str(pair.get("start_time") or "--:--")
        end_time = str(pair.get("end_time") or "")
        details = [str(subject.get("name") or "Noma'lum fan")]
        for key in ("employee", "auditorium"):
            detail = lesson.get(key)
            if isinstance(detail, dict) and detail.get("name"):
                details.append(str(detail["name"]))
        rows.append((start_time, end_time, " | ".join(details)))

    if not rows:
        lines.append("📭 Bu kun uchun HEMIS dars jadvali topilmadi.")
    else:
        for start_time, end_time, title in sorted(rows):
            time_label = f"{start_time}–{end_time}" if end_time else start_time
            lines.append(f"{escape(time_label)}  {escape(title)}")
    return "\n".join(lines)
