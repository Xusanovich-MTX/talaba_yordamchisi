"""Unit tests for HEMIS upcoming tasks service and handler."""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, User
from handlers import tasks as tasks_handler
from services.hemis_schedule import HemisScheduleError, HemisSessionExpired
from services.hemis_tasks import (
    fetch_upcoming_tasks,
    format_upcoming_tasks,
    select_upcoming_tasks,
)


API_URL = "https://student.example.uz/rest/v1/"
SESSION = {"access_token": "access-old", "refresh_token": "refresh-old"}
NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc).timestamp()

SEMESTERS = [
    {
        "id": 5359,
        "code": "16",
        "name": "6-semestr",
        "current": True,
        "education_year": {"code": "2025", "name": "2025-2026", "current": False},
        "weeks": [{"id": 1, "start_date": "2026-01-19", "end_date": "2026-05-09"}],
    },
    {
        "id": 5360,
        "code": "17",
        "name": "7-semestr",
        "current": True,
        "education_year": {"code": "2026", "name": "2026-2027", "current": True},
        "weeks": [{"id": 5, "start_date": "2026-10-26", "end_date": "2026-10-31"}],
    },
]


def make_task(
    task_id: int,
    *,
    name: str,
    deadline: int | float,
    status_code: str = "11",
    status_name: str = "Berildi",
    subject_name: str = "Informatika",
    comment: str = "",
    attempt_count: int | None = None,
    activity: dict | None = None,
):
    return {
        "id": task_id,
        "name": name,
        "comment": comment or name,
        "deadline": deadline,
        "max_ball": 5,
        "attempt_limit": 2,
        "attempt_count": attempt_count,
        "subject": {"name": subject_name},
        "employee": {"name": "Ali Valiyev"},
        "taskStatus": {"code": status_code, "name": status_name},
        "studentTaskActivity": activity,
        "files": [{"name": "topshiriq.pdf", "url": "https://example.uz/file.pdf"}],
    }


class HemisTasksTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_three_nearest_unsubmitted_tasks_in_deadline_order(self):
        raw_tasks = [
            make_task(1, name="Uzoq muddat 4", deadline=NOW + 86400 * 20),
            make_task(2, name="Topshirilgan", deadline=NOW + 86400 * 1, status_code="12", status_name="Topshirdi", attempt_count=1),
            make_task(3, name="Baholangan", deadline=NOW + 86400 * 2, status_code="13", status_name="Baholandi", activity={"mark": 5}),
            make_task(4, name="Yaqin 2", deadline=NOW + 86400 * 5),
            make_task(5, name="Eng yaqin 1", deadline=NOW + 86400 * 2),
            make_task(6, name="Yaqin 3", deadline=NOW + 86400 * 10),
        ]

        selected = select_upcoming_tasks(raw_tasks, now=NOW, limit=3)

        self.assertEqual([t["id"] for t in selected], [5, 4, 6])

    async def test_fetches_semester_17_tasks_and_returns_top_three(self):
        raw_tasks = [
            make_task(10, name="4-ish", deadline=NOW + 86400 * 30),
            make_task(11, name="3-ish", deadline=NOW + 86400 * 15),
            make_task(12, name="2-ish", deadline=NOW + 86400 * 10),
            make_task(13, name="1-ish", deadline=NOW + 86400 * 3),
            make_task(14, name="Topshirilgan ish", deadline=NOW + 86400 * 1, status_code="12", status_name="Topshirdi"),
        ]
        request = AsyncMock(side_effect=[
            (200, {"success": True, "data": SEMESTERS}),
            (200, {"success": True, "data": raw_tasks}),
        ])

        with patch("services.hemis_tasks._request", new=request):
            semester, upcoming, updated_session = await fetch_upcoming_tasks(
                API_URL, SESSION, now=NOW, limit=3
            )

        self.assertEqual(semester["code"], "17")
        self.assertEqual([t["id"] for t in upcoming], [13, 12, 11])
        self.assertEqual(updated_session, SESSION)
        self.assertEqual(
            request.await_args_list[1].kwargs["params"], {"semester": "17"}
        )

    def test_formats_tasks_with_escaped_fields_and_countdown(self):
        task = make_task(
            1,
            name="1-mustaqil <ish>",
            comment="Alohida izoh & talab",
            deadline=NOW + 3600 * 26,
            subject_name="Sun'iy intellekt & <ML>",
        )
        text = format_upcoming_tasks(
            [task], "Asia/Tashkent", semester_name="7-semestr", now=NOW
        )

        self.assertIn("7-semestr", text)
        self.assertIn("Sun&#x27;iy intellekt &amp; &lt;ML&gt;", text)
        self.assertIn("1-mustaqil &lt;ish&gt;", text)
        self.assertIn("Alohida izoh &amp; talab", text)
        self.assertIn("1 kun 2 soat qoldi", text)
        self.assertIn('href="https://example.uz/file.pdf"', text)

    async def test_handler_fetches_hemis_tasks_for_connected_user(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        storage = MemoryStorage()

        try:
            async with sessions() as session:
                session.add(
                    User(
                        id=1,
                        telegram_id=900001,
                        full_name="Student",
                        hemis_institution_name="SamDU",
                        hemis_api_url=API_URL,
                        hemis_session_ciphertext="encrypted-session",
                        timezone="Asia/Tashkent",
                    )
                )
                await session.commit()

            message = SimpleNamespace(
                text="📝 Topshiriqlar",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
            )
            state = FSMContext(
                storage=storage,
                key=StorageKey(bot_id=1, chat_id=900001, user_id=900001),
            )
            sample_tasks = [
                make_task(1, name="1-ish", deadline=NOW + 86400 * 2),
                make_task(2, name="2-ish", deadline=NOW + 86400 * 5),
                make_task(3, name="3-ish", deadline=NOW + 86400 * 9),
            ]

            with (
                patch.object(
                    tasks_handler, "decrypt_hemis_session", return_value=dict(SESSION)
                ),
                patch.object(
                    tasks_handler,
                    "fetch_upcoming_tasks",
                    new=AsyncMock(
                        return_value=({"name": "7-semestr"}, sample_tasks, dict(SESSION))
                    ),
                ) as fetch_mock,
            ):
                async with sessions() as session:
                    await tasks_handler.tasks_menu(message, session, state)

            fetch_mock.assert_awaited_once_with(
                API_URL, SESSION, "Asia/Tashkent", limit=3
            )
            reply = "\n".join(
                call.args[0] for call in message.answer.await_args_list
            )
            self.assertIn("1-ish", reply)
            self.assertIn("2-ish", reply)
            self.assertIn("3-ish", reply)
        finally:
            await storage.close()
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
