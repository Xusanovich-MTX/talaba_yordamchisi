"""Unit tests for Attendance, Grades, Exams, Study Resources, and Today Summary."""

from datetime import date, datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, User
from handlers import academic
from services.hemis_extra import (
    build_attendance_subjects,
    calculate_semester_gpas,
    format_attendance,
    format_exams,
    format_gpa_report,
    format_grades_overview,
    format_morning_motivation,
    format_semester_gpa_details,
    format_subject_attendance_detail,
    format_subject_card,
    format_subject_resources,
    format_today_summary,
)


NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc).timestamp()


class AcademicFeaturesTests(unittest.IsolatedAsyncioTestCase):
    def test_format_attendance_calculates_excused_and_unexcused_hours(self):
        records = [
            {
                "subject": {"name": "Loyihalarni boshqarish"},
                "trainingType": {"name": "Ma'ruza"},
                "lessonPair": {"start_time": "13:50", "end_time": "15:10"},
                "employee": {"name": "PRIMOVA X. A."},
                "absent_on": 0,
                "absent_off": 2,
                "explicable": False,
                "lesson_date": 1791936000,
            },
            {
                "subject": {"name": "Bulutli texnologiyalar"},
                "trainingType": {"name": "Amaliy"},
                "lessonPair": {"start_time": "09:00", "end_time": "10:20"},
                "employee": {"name": "ISMAILOV I. T."},
                "absent_on": 2,
                "absent_off": 0,
                "explicable": True,
                "lesson_date": 1792022400,
            },
        ]
        text = format_attendance(records, semester_name="7-semestr")
        self.assertIn("Jami qoldirilgan:</b> 4 soat", text)
        self.assertIn("Sababsiz: <b>2 soat</b>", text)
        self.assertIn("Sababli: <b>2 soat</b>", text)
        self.assertIn("3.3%", text)
        subject_stats = build_attendance_subjects(records)
        self.assertEqual(len(subject_stats), 2)
        subject_names = {s["name"] for s in subject_stats}
        self.assertEqual(subject_names, {"Loyihalarni boshqarish", "Bulutli texnologiyalar"})

    def test_format_attendance_warns_when_subject_reaches_25_percent_red_zone(self):
        records = [
            {
                "subject": {"name": "Algoritmlar"},
                "trainingType": {"name": "Ma'ruza"},
                "absent_on": 0,
                "absent_off": 16,
                "explicable": False,
                "lesson_date": 1791936000,
            }
        ]
        subjects = [
            {
                "subject": {"name": "Algoritmlar"},
                "total_acload": 120,  # 60 auditoriya soati -> 16/60 = 26.7% (>= 25%)
            }
        ]
        text = format_attendance(records, semester_name="7-semestr", subjects=subjects)
        self.assertIn("DIQQAT OGOHLANTIRISH", text)
        self.assertIn("Algoritmlar", text)

    def test_format_subject_attendance_detail_shows_missed_pct_and_remaining_to_red_zone(self):
        records = [
            {
                "subject": {"name": "Dasturlash"},
                "trainingType": {"name": "Amaliy"},
                "lessonPair": {"start_time": "10:30", "end_time": "11:50"},
                "employee": {"name": "KARIMOV A."},
                "absent_on": 2,
                "absent_off": 4,
                "explicable": False,
                "lesson_date": 1791936000,
            }
        ]
        subjects = [{"subject": {"name": "Dasturlash"}, "auditorium_hours": 60}]
        subject_stats = build_attendance_subjects(records, subjects)
        self.assertEqual(len(subject_stats), 1)
        detail_text = format_subject_attendance_detail(subject_stats[0], semester_name="7-semestr")
        self.assertIn("Dasturlash", detail_text)
        self.assertIn("6 soat", detail_text)
        self.assertIn("10.0%", detail_text)
        # 25% of 60h = 15h; 15h - 6h = 9 soat (4.5 para) remaining to red zone
        self.assertIn("9 soat", detail_text)
        self.assertIn("KARIMOV A.", detail_text)

    def test_format_grades_overview_includes_subject_credits_and_marked_tasks(self):
        subjects = [
            {
                "subject": {"name": "Sun'iy intellekt texnologiyalari"},
                "credit": 6,
                "total_acload": 180,
                "tasks_count": 4,
                "submits_count": 4,
                "marked_count": 1,
                "student_ball": 8,
            }
        ]
        tasks = [
            {
                "subject": {"name": "Sun'iy intellekt texnologiyalari"},
                "name": "1-Amaliy ish",
                "max_ball": 5,
                "studentTaskActivity": {"mark": 4},
            }
        ]
        text = format_grades_overview(subjects, tasks, semester_name="7-semestr")
        self.assertIn("Sun&#x27;iy intellekt texnologiyalari", text)
        self.assertIn("Kredit: <b>6</b>", text)
        self.assertIn("1-Amaliy ish: <b>4/5 ball</b>", text)

    def test_format_exams_orders_by_date_and_shows_details(self):
        exams = [
            {
                "subject": {"name": "Umumiy psixologiya"},
                "examType": {"name": "Oraliq nazorat"},
                "finalExamType": {"name": "1-qaydnoma"},
                "employee": {"name": "NAZAROV F. M."},
                "auditorium": {"name": "SI va RT_220"},
                "lessonPair": {"start_time": "15:20", "end_time": "16:40"},
                "examDate": NOW + 86400 * 5,
            }
        ]
        text = format_exams(exams, semester_name="7-semestr", now=NOW)
        self.assertIn("Umumiy psixologiya", text)
        self.assertIn("Oraliq nazorat (1-qaydnoma)", text)
        self.assertIn("SI va RT_220", text)
        self.assertIn("5 kun qoldi", text)

    def test_format_subject_resources_renders_download_links_and_pagination(self):
        resources = [
            {
                "title": f"Mavzu {i}",
                "trainingType": {"name": "Ma'ruza"},
                "employee": {"name": "DJUMANOV O."},
                "subjectFileResourceItems": [
                    {
                        "files": [
                            {
                                "name": f"maruza_{i}.pdf",
                                "url": f"https://hemis.samdu.uz/file_{i}.pdf",
                            }
                        ]
                    }
                ],
            }
            for i in range(1, 11)
        ]
        text, total_pages = format_subject_resources(
            resources, subject_name="Kompyuter tarmoqlari", page=0, page_size=8
        )
        self.assertEqual(total_pages, 2)
        self.assertIn("Kompyuter tarmoqlari", text)
        self.assertIn('href="https://hemis.samdu.uz/file_1.pdf"', text)
        self.assertNotIn("file_9.pdf", text)

    def test_gpa_calculation_and_formatting_for_semesters_and_years(self):
        semesters = [
            {
                "code": "15",
                "name": "5-semestr",
                "education_year": {"name": "2025-2026"},
            },
            {
                "code": "16",
                "name": "6-semestr",
                "education_year": {"name": "2025-2026"},
            },
        ]
        subject_list = [
            {
                "_semester": "15",
                "curriculumSubject": {
                    "credit": 6,
                    "subject": {"name": "Mobil ilovalar"},
                },
                "overallScore": {"grade": 5, "percent": 92},
            },
            {
                "_semester": "15",
                "curriculumSubject": {
                    "credit": 4,
                    "subject": {"name": "Tarmoqlar"},
                },
                "overallScore": {"grade": 4, "percent": 80},
            },
        ]
        gpa_list = [
            {
                "level": {"code": "13", "name": "3-kurs"},
                "educationYear": {"code": "2025", "name": "2025-2026"},
                "gpa": "4.75",
                "credit_sum": "60.0",
                "subjects": 14,
                "debt_subjects": 0,
            }
        ]
        summaries = calculate_semester_gpas(semesters, subject_list)
        self.assertEqual(len(summaries), 1)
        self.assertAlmostEqual(summaries[0]["gpa"], 4.60)

        report_text = format_gpa_report(gpa_list, summaries)
        self.assertIn("3-kurs", report_text)
        self.assertIn("4.75", report_text)
        self.assertIn("5-semestr", report_text)
        self.assertIn("4.60", report_text)

        details_text = format_semester_gpa_details(summaries[0])
        self.assertIn("Mobil ilovalar", details_text)
        self.assertIn("5/5", details_text)

    def test_format_morning_motivation_only_returns_text_on_days_with_classes(self):
        today = date(2026, 10, 28)
        self.assertIsNone(format_morning_motivation(today, []))

        lessons = [
            {
                "lesson_date": "2026-10-28",
                "subject": {"name": "Raqamli tasvirlarga ishlov berish"},
                "lessonPair": {"start_time": "16:50", "end_time": "18:10"},
                "auditorium": {"name": "205-xona"},
                "employee": {"name": "ABDIYEVA X. S."},
            }
        ]
        text = format_morning_motivation(today, lessons)
        self.assertIsNotNone(text)
        self.assertIn("Xayrli tong!", text)
        self.assertIn("16:50–18:10", text)
        self.assertIn("Raqamli tasvirlarga ishlov berish", text)

    def test_format_today_summary_combines_lessons_and_tasks(self):
        today = date(2026, 10, 28)
        lessons = [
            {
                "lesson_date": "2026-10-28",
                "subject": {"name": "Raqamli tasvirlarga ishlov berish"},
                "lessonPair": {"start_time": "16:50", "end_time": "18:10"},
                "auditorium": {"name": "205-xona"},
                "employee": {"name": "ABDIYEVA X. S."},
            }
        ]
        tasks = [
            {
                "name": "2-mustaqil ish",
                "deadline": NOW + 86400 * 3,
                "subject": {"name": "Refaktoring"},
            }
        ]
        text = format_today_summary(
            today, lessons, tasks, semester_name="7-semestr", now=NOW
        )
        self.assertIn("16:50–18:10", text)
        self.assertIn("Raqamli tasvirlarga ishlov berish", text)
        self.assertIn("2-mustaqil ish", text)

    async def test_academic_handlers_respond_for_connected_user(self):
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
                        hemis_api_url="https://student.example.uz/rest/v1/",
                        hemis_session_ciphertext="encrypted",
                        timezone="Asia/Tashkent",
                    )
                )
                await session.commit()

            state = FSMContext(
                storage=storage,
                key=StorageKey(bot_id=1, chat_id=900001, user_id=900001),
            )
            msg = SimpleNamespace(
                text="📊 Davomat",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
            )
            session_dict = {"access_token": "tok", "refresh_token": "ref"}

            with (
                patch.object(academic, "decrypt_hemis_session", return_value=session_dict),
                patch.object(
                    academic,
                    "fetch_attendance",
                    new=AsyncMock(return_value=({"name": "7-semestr"}, [], session_dict)),
                ),
            ):
                async with sessions() as session:
                    await academic.show_attendance(msg, session, state)

            msg.answer.assert_awaited_once()
            self.assertIn("Davomat", msg.answer.await_args.args[0])

            # Fan ustiga bosganda o'sha fan bo'yicha alohida davomat tahlili chiqishi kerak
            sample_rec = [
                {
                    "subject": {"name": "Dasturlash"},
                    "absent_on": 0,
                    "absent_off": 4,
                    "explicable": False,
                    "lesson_date": 1791936000,
                }
            ]
            cb = SimpleNamespace(
                data="hemis_att:0",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            with (
                patch.object(academic, "decrypt_hemis_session", return_value=session_dict),
                patch.object(
                    academic,
                    "fetch_attendance",
                    new=AsyncMock(return_value=({"name": "7-semestr"}, sample_rec, session_dict)),
                ),
            ):
                async with sessions() as session:
                    await academic.show_subject_attendance_callback(cb, session)

            cb.message.edit_text.assert_awaited_once()
            edited_text = cb.message.edit_text.await_args.args[0]
            self.assertIn("Dasturlash", edited_text)
            self.assertIn("6.7%", edited_text)
            self.assertIn("11 soat", edited_text)

            # Test show_subject_card_callback
            sample_subjs = [
                {
                    "subject": {"id": 101, "name": "Loyihalarni boshqarish"},
                    "subjectType": {"name": "Majburiy"},
                    "total_acload": 120,
                    "credit": 4.0,
                    "resources_count": 40,
                    "tasks_count": 1,
                    "submits_count": 0,
                }
            ]
            cb_card = SimpleNamespace(
                data="hemis_res_card:17:101",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            with (
                patch.object(academic, "decrypt_hemis_session", return_value=session_dict),
                patch.object(
                    academic,
                    "fetch_semester_subjects",
                    new=AsyncMock(return_value=({"name": "7-semestr"}, sample_subjs, session_dict)),
                ),
            ):
                async with sessions() as session:
                    await academic.show_subject_card_callback(cb_card, session)

            cb_card.message.edit_text.assert_awaited_once()
            card_text = cb_card.message.edit_text.await_args.args[0]
            self.assertIn("Loyihalarni boshqarish", card_text)
            self.assertIn("Majburiy | 120 soat | 4.0 kredit", card_text)
            self.assertIn("Resurslar soni:</b> 40", card_text)
            self.assertIn("Topshiriqlar soni:</b> 0/1", card_text)
        finally:
            await storage.close()
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
