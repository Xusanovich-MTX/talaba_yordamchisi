import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.schedule import _split_telegram_text
from services.hemis_schedule import (
    HemisScheduleError,
    fetch_current_week_schedule,
    fetch_day_schedule,
    format_day_schedule,
    format_week_schedule,
    schedule_week_dates,
)


API_URL = "https://student.example.uz/rest/v1/"
SESSION = {"access_token": "access-old", "refresh_token": "refresh-old"}
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()
SEMESTERS = [{
    "code": 10,
    "name": "2026-2027 kuzgi semestr",
    "current": True,
    "weeks": [
        {"id": 4, "start_date": "2026-10-19", "end_date": "2026-10-25"},
        {"id": 5, "start_date": "2026-10-26", "end_date": "2026-11-01"},
    ],
}]

DATED_SEMESTERS = [
    {
        "code": 9,
        "current": True,
        "weeks": [{"id": 3, "start_date": "2025-10-27", "end_date": "2025-11-02"}],
    },
    {
        "code": 10,
        "name": "2026-2027 kuzgi semestr",
        "current": True,
        # Deliberately return the future week first: selection must use both
        # start and end dates rather than relying on response order.
        "weeks": [
            {"id": 6, "start_date": "2026-11-02", "end_date": "2026-11-08"},
            {"id": 5, "start_date": "2026-10-26", "end_date": "2026-11-01"},
        ],
    },
]


def lesson_for(lesson_date, *, title="Fizika", start_time="09:00"):
    return {
        "lesson_date": lesson_date,
        "subject": {"name": title},
        "lessonPair": {"start_time": start_time, "end_time": "10:20"},
        "employee": {"name": "Ali <Valiyev>"},
        "auditorium": {"name": "305 & 306"},
    }


class HemisScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_week_dates_follow_user_timezone_at_monday_boundary(self):
        dates = schedule_week_dates(
            "Asia/Tashkent", now=datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
        )

        self.assertEqual(dates[0], date(2026, 10, 26))
        self.assertEqual(dates[-1], date(2026, 10, 31))
        self.assertEqual(len(set(dates)), 6)
        self.assertEqual([day.weekday() for day in dates], list(range(6)))

    async def test_day_selects_date_range_among_multiple_current_semesters(self):
        selected_date = date(2026, 10, 28)
        included = lesson_for("2026-10-27T19:00:00Z")
        request = AsyncMock(side_effect=[
            (200, {"success": True, "data": DATED_SEMESTERS}),
            (200, {"success": True, "data": [
                lesson_for("2026-10-27T18:59:59Z", title="Before"),
                included,
                lesson_for("2026-10-28T19:00:00Z", title="After"),
                lesson_for("invalid", title="Invalid"),
            ]}),
        ])

        with patch("services.hemis_schedule._request", new=request):
            semester, lessons, updated_session = await fetch_day_schedule(
                API_URL, SESSION, selected_date
            )

        self.assertEqual(semester["code"], 10)
        self.assertEqual(lessons, [included])
        self.assertEqual(updated_session, SESSION)
        self.assertEqual(request.await_args_list[1].kwargs["params"], {"week": "5"})

    async def test_day_includes_both_calendar_boundaries_of_teaching_week(self):
        for selected_date in (date(2026, 10, 26), date(2026, 11, 1)):
            with self.subTest(selected_date=selected_date):
                request = AsyncMock(side_effect=[
                    (200, {"success": True, "data": DATED_SEMESTERS}),
                    (200, {"success": True, "data": []}),
                ])
                with patch("services.hemis_schedule._request", new=request):
                    await fetch_day_schedule(API_URL, SESSION, selected_date)
                self.assertEqual(
                    request.await_args_list[1].kwargs["params"], {"week": "5"}
                )

    async def test_day_outside_teaching_weeks_returns_empty_without_fetching_future_week(self):
        request = AsyncMock(return_value=(200, {"success": True, "data": DATED_SEMESTERS}))

        with patch("services.hemis_schedule._request", new=request):
            semester, lessons, updated_session = await fetch_day_schedule(
                API_URL, SESSION, date(2026, 10, 25)
            )

        self.assertEqual(lessons, [])
        self.assertEqual(semester, {})
        self.assertEqual(updated_session, SESSION)
        self.assertEqual(request.await_count, 1)

    async def test_day_with_no_semesters_returns_empty(self):
        request = AsyncMock(return_value=(200, {"success": True, "data": []}))
        with patch("services.hemis_schedule._request", new=request):
            semester, lessons, _ = await fetch_day_schedule(
                API_URL, SESSION, date(2026, 10, 28)
            )
        self.assertEqual((semester, lessons), ({}, []))
        self.assertEqual(request.await_count, 1)

    async def test_day_does_not_treat_api_failure_as_empty_schedule(self):
        for status, payload in (
            (503, {"success": False, "data": []}),
            (200, {"success": False, "data": []}),
            (200, {"success": True, "data": [{"code": 10}]}),
        ):
            with self.subTest(status=status, payload=payload):
                request = AsyncMock(return_value=(status, payload))
                with patch("services.hemis_schedule._request", new=request):
                    with self.assertRaises(HemisScheduleError):
                        await fetch_day_schedule(API_URL, SESSION, date(2026, 10, 28))

    async def test_day_refreshes_token_during_schedule_fetch(self):
        selected_lesson = lesson_for("2026-10-28")
        request = AsyncMock(side_effect=[
            (200, {"success": True, "data": DATED_SEMESTERS}),
            (401, {"success": False, "data": {}}),
            (200, {"success": True, "data": {
                "token": "access-new", "refresh_token": "refresh-new"
            }}),
            (200, {"success": True, "data": [selected_lesson]}),
        ])

        with patch("services.hemis_schedule._request", new=request):
            _, lessons, updated_session = await fetch_day_schedule(
                API_URL, SESSION, date(2026, 10, 28)
            )

        self.assertEqual(lessons, [selected_lesson])
        self.assertEqual(updated_session, {
            "access_token": "access-new", "refresh_token": "refresh-new"
        })
        self.assertEqual(SESSION["access_token"], "access-old")
        self.assertEqual(request.await_args_list[2].args[1], "POST")
        self.assertEqual(
            request.await_args_list[3].kwargs["headers"]["Authorization"], "Bearer access-new"
        )
        self.assertEqual(request.await_args_list[3].kwargs["params"], {"week": "5"})

    def test_formats_only_selected_day_in_time_order_and_escapes_api_text(self):
        text = format_day_schedule([
            lesson_for("2026-10-28", title="Later <fan>", start_time="11:00"),
            lesson_for("2026-10-27T19:00:00Z", title="Earlier & fan", start_time="09:00"),
            lesson_for("2026-10-29", title="Other day"),
        ], date(2026, 10, 28), semester_name="Kuz <2026>")

        self.assertIn("Chorshanba, 28.10.2026", text)
        self.assertIn("Kuz &lt;2026&gt;", text)
        self.assertIn("09:00–10:20", text)
        self.assertIn("Ali &lt;Valiyev&gt;", text)
        self.assertIn("305 &amp; 306", text)
        self.assertLess(text.index("Earlier &amp; fan"), text.index("Later &lt;fan&gt;"))
        self.assertNotIn("Other day", text)

    def test_empty_daily_schedule_names_the_selected_date(self):
        text = format_day_schedule([], date(2026, 11, 1))

        self.assertIn("Yakshanba, 01.11.2026", text)
        self.assertIn("Bu kun uchun HEMIS dars jadvali topilmadi", text)

    async def test_fetches_schedule_for_current_week_with_bearer_token(self):
        request = AsyncMock(side_effect=[
            (200, {"success": True, "data": SEMESTERS}),
            (200, {"success": True, "data": [{"id": 1}]}),
        ])

        with patch("services.hemis_schedule._request", new=request):
            semester, lessons, updated_session = await fetch_current_week_schedule(
                API_URL, SESSION, now=NOW
            )

        self.assertEqual(semester["code"], 10)
        self.assertEqual(lessons, [{"id": 1}])
        self.assertEqual(updated_session, SESSION)
        calls = request.await_args_list
        self.assertEqual(calls[0].args[2], API_URL.rstrip("/") + "/education/semesters")
        self.assertEqual(calls[0].kwargs["headers"]["Authorization"], "Bearer access-old")
        self.assertEqual(calls[1].kwargs["params"], {"week": "5"})

    async def test_refreshes_expired_access_token_and_returns_new_tokens(self):
        request = AsyncMock(side_effect=[
            (401, {"success": False, "data": {}}),
            (200, {"success": True, "data": {
                "token": "access-new", "refresh_token": "refresh-new"
            }}),
            (200, {"success": True, "data": SEMESTERS}),
            (200, {"success": True, "data": []}),
        ])

        with patch("services.hemis_schedule._request", new=request):
            _, lessons, updated_session = await fetch_current_week_schedule(
                API_URL, SESSION, now=NOW
            )

        self.assertEqual(lessons, [])
        self.assertEqual(updated_session["access_token"], "access-new")
        self.assertEqual(updated_session["refresh_token"], "refresh-new")
        self.assertEqual(
            request.await_args_list[1].kwargs["headers"]["X-Refresh-Token"],
            "refresh-old",
        )

    async def test_weekly_api_also_selects_date_among_multiple_current_semesters(self):
        all_week_lessons = [lesson_for("2026-10-26"), lesson_for("2026-10-29")]
        request = AsyncMock(side_effect=[
            (200, {"success": True, "data": DATED_SEMESTERS}),
            (200, {"success": True, "data": all_week_lessons}),
        ])
        with patch("services.hemis_schedule._request", new=request):
            semester, lessons, _ = await fetch_current_week_schedule(
                API_URL, SESSION, now=NOW
            )
        self.assertEqual(semester["code"], 10)
        self.assertEqual(lessons, all_week_lessons)
        self.assertEqual(request.await_args_list[1].kwargs["params"], {"week": "5"})

    def test_formats_lesson_day_time_subject_teacher_and_room(self):
        text = format_week_schedule([{
            "lesson_date": "2026-10-28T08:00:00+05:00",
            "subject": {"name": "Fizika <1>"},
            "lessonPair": {"start_time": "09:00", "end_time": "10:20"},
            "employee": {"name": "Ali Valiyev"},
            "auditorium": {"name": "305-xona"},
        }])

        self.assertIn("Chorshanba, 28.10.2026", text)
        self.assertIn("09:00–10:20", text)
        self.assertIn("Fizika &lt;1&gt;", text)
        self.assertIn("Ali Valiyev", text)
        self.assertIn("305-xona", text)

    def test_splits_long_timetable_at_line_boundaries(self):
        text = "\n".join(f"lesson {index} " + "x" * 90 for index in range(100))
        chunks = _split_telegram_text(text, limit=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
