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
    fetch_task_detail,
    fetch_upcoming_tasks,
    format_upcoming_tasks,
    select_upcoming_tasks,
    sort_subject_tasks,
    upload_hemis_task,
)
from keyboards.inline import is_task_submittable, task_subject_detail_kb, subject_task_detail_back_kb


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
        "subject": {"id": 1, "name": subject_name},
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

            sample_subjects = [
                {"subject": {"id": 1, "name": "Algoritmlar"}},
            ]
            with (
                patch.object(
                    tasks_handler, "decrypt_hemis_session", return_value=dict(SESSION)
                ),
                patch.object(
                    tasks_handler,
                    "fetch_grades_overview",
                    new=AsyncMock(
                        return_value=({"name": "7-semestr"}, sample_subjects, sample_tasks, dict(SESSION))
                    ),
                ) as fetch_mock,
            ):
                async with sessions() as session:
                    await tasks_handler.tasks_menu(message, session, state)

            fetch_mock.assert_awaited_once_with(
                API_URL, SESSION, "Asia/Tashkent"
            )
            reply = "\n".join(
                call.args[0] for call in message.answer.await_args_list
            )
            self.assertIn("HEMIS: Topshiriqlar", reply)
            self.assertIn("Jami topshiriqlar:", reply)

            # Test clicking a subject button to see its tasks
            cb = SimpleNamespace(
                data="hemis_task_subj:1",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            with (
                patch.object(
                    tasks_handler, "decrypt_hemis_session", return_value=dict(SESSION)
                ),
                patch.object(
                    tasks_handler,
                    "fetch_grades_overview",
                    new=AsyncMock(
                        return_value=({"name": "7-semestr"}, sample_subjects, sample_tasks, dict(SESSION))
                    ),
                ),
            ):
                async with sessions() as session:
                    await tasks_handler.show_subject_tasks_callback(cb, session)

            cb.message.edit_text.assert_awaited_once()
            detail_reply = cb.message.edit_text.await_args.args[0]
            self.assertIn("Algoritmlar — Topshiriqlar", detail_reply)
            self.assertIn("1-ish", detail_reply)
        finally:
            await storage.close()
            await engine.dispose()



    def test_sort_subject_tasks_orders_tasks_sequentially(self):
        tasks = [
            make_task(40, name="4-mustaqil ish", deadline=NOW + 4000),
            make_task(30, name="3-mustaqil ish", deadline=NOW + 3000),
            make_task(20, name="2-mustaqil ish", deadline=NOW + 2000),
            make_task(10, name="1-mustaqil ish", deadline=NOW + 1000),
        ]
        sorted_tasks = sort_subject_tasks(tasks)
        self.assertEqual(
            [t["name"] for t in sorted_tasks],
            ["1-mustaqil ish", "2-mustaqil ish", "3-mustaqil ish", "4-mustaqil ish"],
        )

    def test_is_task_submittable(self):
        # 1. New task (no activity, code 11) -> True
        t1 = make_task(1, name="Task 1", deadline=NOW + 1000)
        self.assertTrue(is_task_submittable(t1))

        # 2. Graded task (mark=5) -> False
        t2 = make_task(2, name="Task 2", deadline=NOW + 1000, activity={"mark": 5})
        self.assertFalse(is_task_submittable(t2))

        # 3. Status 13 (Baholandi) -> False
        t3 = make_task(3, name="Task 3", deadline=NOW + 1000, status_code="13")
        self.assertFalse(is_task_submittable(t3))

        # 4. Attempt limit reached -> False
        t4 = make_task(4, name="Task 4", deadline=NOW + 1000, attempt_count=2)
        self.assertFalse(is_task_submittable(t4))

        # 5. Submitted once but attempt limit is 2 -> True (can resubmit)
        t5 = make_task(5, name="Task 5", deadline=NOW + 1000, status_code="12", attempt_count=1)
        self.assertTrue(is_task_submittable(t5))

    def test_task_subject_detail_kb_generates_upload_buttons(self):
        tasks = [
            make_task(101, name="1-Mustaqil ish", deadline=NOW + 1000),
            make_task(102, name="2-Mustaqil ish", deadline=NOW + 1000),
        ]
        kb = task_subject_detail_kb(tasks)
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [btn.callback_data for btn in buttons]
        self.assertIn("hemis_task_up:101", callbacks)
        self.assertIn("hemis_task_up:102", callbacks)
        self.assertIn("hemis_tasks_back", callbacks)

    async def test_fetch_task_detail_success(self):
        with patch("services.hemis_tasks._request") as req_mock:
            req_mock.return_value = (200, {"success": True, "data": {"id": 123, "name": "Test Task"}})
            data, session = await fetch_task_detail(API_URL, SESSION, 123)
            self.assertEqual(data["id"], 123)
            self.assertEqual(data["name"], "Test Task")

    async def test_upload_hemis_task_success(self):
        with patch("aiohttp.ClientSession.post") as post_mock:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={
                "success": True,
                "error": None,
                "data": {"id": 999, "comment": "uploaded"}
            })
            post_mock.return_value.__aenter__.return_value = mock_resp

            data, updated_session = await upload_hemis_task(
                API_URL,
                SESSION,
                123,
                b"dummy content",
                "mustaqil.docx",
                comment="Izoh",
            )
            self.assertEqual(data["id"], 999)

    async def test_upload_hemis_task_rejects_zip_format(self):
        with self.assertRaises(HemisScheduleError) as ctx:
            await upload_hemis_task(
                API_URL,
                SESSION,
                123,
                b"PK...",
                "mustaqil.zip",
            )
        self.assertIn("Faqat Word (.doc, .docx) yoki PDF (.pdf)", str(ctx.exception))

    async def test_upload_hemis_task_rejects_large_size(self):
        large_bytes = b"0" * (11 * 1024 * 1024)
        with self.assertRaises(HemisScheduleError) as ctx:
            await upload_hemis_task(
                API_URL,
                SESSION,
                123,
                large_bytes,
                "mustaqil.pdf",
            )
        self.assertIn("10 MB dan oshmasligi kerak", str(ctx.exception))

    async def test_upload_hemis_task_handles_error(self):
        with patch("aiohttp.ClientSession.post") as post_mock:
            mock_resp = AsyncMock()
            mock_resp.status = 400
            mock_resp.json = AsyncMock(return_value={
                "success": False,
                "error": "Topshiriqni bajarish muddati tugagan"
            })
            post_mock.return_value.__aenter__.return_value = mock_resp

            with self.assertRaises(HemisScheduleError) as ctx:
                await upload_hemis_task(
                    API_URL,
                    SESSION,
                    123,
                    b"dummy content",
                    "mustaqil.docx",
                )
            self.assertIn("Topshiriqni bajarish muddati tugagan", str(ctx.exception))

    async def test_task_upload_fsm_flow(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        storage = MemoryStorage()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with sessions() as session:
                user = User(
                    telegram_id=900002,
                    full_name="Student 900002",
                    hemis_api_url=API_URL,
                    hemis_session_ciphertext="encrypted-session",
                    timezone="Asia/Tashkent",
                )
                session.add(user)
                await session.commit()

            state = FSMContext(
                storage=storage,
                key=StorageKey(bot_id=1, chat_id=900002, user_id=900002),
            )

            cb = SimpleNamespace(
                data="hemis_task_up:555",
                from_user=SimpleNamespace(id=900002),
                answer=AsyncMock(),
                message=SimpleNamespace(answer=AsyncMock()),
            )

            # 1. Ask file upload
            with (
                patch.object(tasks_handler, "decrypt_hemis_session", return_value=dict(SESSION)),
                patch.object(
                    tasks_handler,
                    "fetch_task_detail",
                    new=AsyncMock(return_value=({"id": 555, "name": "Lab 1", "subject": {"name": "Fizika"}}, dict(SESSION))),
                ),
            ):
                async with sessions() as session:
                    await tasks_handler.ask_task_upload_file(cb, state, session)

            cb.message.answer.assert_awaited_once()
            self.assertIn("Topshiriqni HEMIS tizimiga yuklash", cb.message.answer.await_args.args[0])
            self.assertEqual(await state.get_state(), tasks_handler.UploadHemisTask.waiting_for_file.state)

            # 2. Reject zip upload
            zip_msg = SimpleNamespace(
                from_user=SimpleNamespace(id=900002),
                document=SimpleNamespace(file_size=1024, file_name="lab1.zip"),
                caption=None,
                bot=SimpleNamespace(download=AsyncMock()),
                answer=AsyncMock(),
            )
            async with sessions() as session:
                await tasks_handler.handle_task_upload_document(zip_msg, state, session)
            self.assertIn("Noto'g'ri fayl formati", zip_msg.answer.await_args.args[0])
            self.assertEqual(await state.get_state(), tasks_handler.UploadHemisTask.waiting_for_file.state)

            # 3. Reject oversized upload (> 10 MB)
            large_msg = SimpleNamespace(
                from_user=SimpleNamespace(id=900002),
                document=SimpleNamespace(file_size=12 * 1024 * 1024, file_name="lab1.pdf"),
                caption=None,
                bot=SimpleNamespace(download=AsyncMock()),
                answer=AsyncMock(),
            )
            async with sessions() as session:
                await tasks_handler.handle_task_upload_document(large_msg, state, session)
            self.assertIn("Fayl hajmi juda katta", large_msg.answer.await_args.args[0])
            self.assertEqual(await state.get_state(), tasks_handler.UploadHemisTask.waiting_for_file.state)

            # 4. Receive valid PDF file -> should transition to confirm_upload state
            doc_msg = SimpleNamespace(
                from_user=SimpleNamespace(id=900002),
                document=SimpleNamespace(file_size=1024, file_name="lab1.pdf"),
                caption=None,
                bot=SimpleNamespace(download=AsyncMock()),
                answer=AsyncMock(),
            )

            async with sessions() as session:
                await tasks_handler.handle_task_upload_document(doc_msg, state, session)

            self.assertEqual(await state.get_state(), tasks_handler.UploadHemisTask.confirm_upload.state)
            self.assertIn("tasdiqlaysizmi", doc_msg.answer.await_args.args[0])

            # 5. Confirm upload -> uploads to HEMIS and clears state
            confirm_cb = SimpleNamespace(
                data="hemis_upload_confirm",
                from_user=SimpleNamespace(id=900002),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )

            with (
                patch.object(tasks_handler, "decrypt_hemis_session", return_value=dict(SESSION)),
                patch.object(
                    tasks_handler,
                    "upload_hemis_task",
                    new=AsyncMock(return_value=({"id": 1}, dict(SESSION))),
                ),
            ):
                async with sessions() as session:
                    await tasks_handler.confirm_task_upload_callback(confirm_cb, state, session)

            self.assertIsNone(await state.get_state())
            confirm_cb.message.answer.assert_awaited_once()
        finally:
            await storage.close()
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
