"""Regression tests with an isolated database and mocked Telegram delivery."""

import asyncio
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importing the application must not require a real token or database.
with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "123456:test-token",
        "DB_URL": "sqlite+aiosqlite:///:memory:",
    },
):
    from database.models import Base, Schedule, Task, User
    from scheduler import notifications


def clock_at(local_now):
    """Simulate a UTC host while local timestamps use Asia/Tashkent."""
    instant = local_now.replace(tzinfo=timezone(timedelta(hours=5)))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(timezone.utc).replace(tzinfo=None)
            return instant.astimezone(tz)

    return patch.object(notifications, "datetime", FrozenDateTime)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _connection_record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session_patch = patch.object(
            notifications, "async_session", self.sessions
        )
        self.session_patch.start()
        notifications.notified_schedules.clear()
        notifications.notified_tasks.clear()
        notifications._hemis_day_cache.clear()
        notifications._hemis_tasks_cache.clear()
        self.bot = AsyncMock()
        # Database ownership and Telegram delivery use distinct identifiers.
        self.user = User(id=1, telegram_id=900001, full_name="Test student")
        await self.save(self.user)

    async def asyncTearDown(self):
        self.session_patch.stop()
        notifications.notified_schedules.clear()
        notifications.notified_tasks.clear()
        notifications._hemis_day_cache.clear()
        notifications._hemis_tasks_cache.clear()
        await self.engine.dispose()

    async def save(self, *objects):
        async with self.sessions() as session:
            session.add_all(objects)
            await session.commit()

    def schedule(self, subject="Mathematics", **changes):
        values = {
            "user_id": self.user.id,
            "subject": subject,
            "day_of_week": 1,
            "start_time": time(9, 10),
        }
        values.update(changes)
        return Schedule(**values)

    def task(self, deadline, subject="Mathematics", **changes):
        values = {
            "user_id": self.user.id,
            "subject": subject,
            "description": "Read chapter 1",
            "deadline": deadline,
        }
        values.update(changes)
        return Task(**values)

    async def test_class_uses_tashkent_clock_on_utc_host_at_10m_and_5m(self):
        await self.save(self.schedule(start_time=time(9, 10)))

        with clock_at(datetime(2026, 10, 26, 9, 0)):
            await notifications.check_schedule_reminders(self.bot)

        self.bot.send_message.assert_awaited_once()
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["chat_id"],
            self.user.telegram_id,
        )
        self.assertIn(
            "10 daqiqadan keyin boshlanadi!",
            self.bot.send_message.await_args.kwargs["text"],
        )

        with clock_at(datetime(2026, 10, 26, 9, 5)):
            await notifications.check_schedule_reminders(self.bot)

        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn(
            "5 daqiqadan keyin boshlanadi!",
            self.bot.send_message.await_args.kwargs["text"],
        )

        with clock_at(datetime(2026, 10, 26, 9, 10)):
            await notifications.check_schedule_reminders(self.bot)

        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertIn(
            "Dars boshlandi!",
            self.bot.send_message.await_args.kwargs["text"],
        )

    async def test_two_users_receive_only_their_own_reminders(self):
        now = datetime(2026, 10, 26, 9, 0)
        other_user = User(
            id=self.user.telegram_id,
            telegram_id=self.user.id,
            full_name="Other student",
        )
        await self.save(other_user)
        await self.save(
            self.schedule(subject="First class", start_time=time(9, 10)),
            self.task(now + timedelta(hours=3), subject="First task"),
            self.schedule(
                subject="Second class",
                start_time=time(9, 10),
                user_id=other_user.id,
            ),
            self.task(
                now + timedelta(hours=3),
                subject="Second task",
                user_id=other_user.id,
            ),
        )

        with clock_at(now):
            await notifications.check_schedule_reminders(self.bot)
            await notifications.check_deadline_reminders(self.bot)

        expected_subjects = {
            self.user.telegram_id: {"First class", "First task"},
            other_user.telegram_id: {"Second class", "Second task"},
        }
        self.assertEqual(self.bot.send_message.await_count, 4)
        for telegram_id, subjects in expected_subjects.items():
            texts = [
                call.kwargs["text"]
                for call in self.bot.send_message.await_args_list
                if call.kwargs["chat_id"] == telegram_id
            ]
            self.assertEqual(len(texts), 2)
            for subject in subjects:
                self.assertTrue(any(f"<b>{subject}</b>" in text for text in texts))

        async with self.sessions() as session:
            result = await session.execute(
                select(User).options(
                    selectinload(User.schedules), selectinload(User.tasks)
                )
            )
            for user in result.scalars():
                self.assertEqual(
                    {item.subject for item in [*user.schedules, *user.tasks]},
                    expected_subjects[user.telegram_id],
                )

    async def test_sunday_midnight_window_checks_both_weekdays(self):
        await self.save(
            self.schedule(
                subject="Sunday class", day_of_week=7, start_time=time(23, 59)
            ),
            self.schedule(subject="Monday class", start_time=time(0, 4)),
            self.schedule(
                subject="Already passed", day_of_week=7, start_time=time(0, 1)
            ),
        )

        with clock_at(datetime(2026, 10, 25, 23, 54)):
            await notifications.check_schedule_reminders(self.bot)

        texts = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("Sunday class" in text for text in texts))
        self.assertTrue(any("Monday class" in text for text in texts))

    async def test_class_dedup_uses_class_date_and_recurs_next_week(self):
        schedule = self.schedule(start_time=time(0, 0))
        await self.save(schedule)

        with clock_at(datetime(2026, 10, 25, 23, 49)):
            await notifications.check_schedule_reminders(self.bot)
        with clock_at(datetime(2026, 10, 25, 23, 51)):
            await notifications.check_schedule_reminders(self.bot)

        self.bot.send_message.assert_awaited_once()
        self.assertIn(
            f"{self.user.telegram_id}_{schedule.id}_20261026_10m",
            notifications.notified_schedules,
        )

        with clock_at(datetime(2026, 11, 1, 23, 50)):
            await notifications.check_schedule_reminders(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_class_escapes_subject_and_room(self):
        await self.save(
            self.schedule(subject="C++ & <AI>", room_or_link="Room <2> & lab")
        )

        with clock_at(datetime(2026, 10, 26, 9, 0)):
            await notifications.check_schedule_reminders(self.bot)
            await notifications.check_schedule_reminders(self.bot)

        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("<b>C++ &amp; &lt;AI&gt;</b>", text)
        self.assertIn("Room &lt;2&gt; &amp; lab", text)

    async def test_disabled_class_preferences_and_outside_window_are_excluded(self):
        disabled_user = User(
            id=2,
            telegram_id=900002,
            full_name="Notifications disabled",
            notify_class=False,
        )
        await self.save(disabled_user)
        await self.save(
            self.schedule(user_id=disabled_user.id),
            self.schedule(subject="Too soon", start_time=time(9, 2)),
            self.schedule(subject="Too late", start_time=time(9, 13)),
            self.schedule(subject="Other day", day_of_week=2),
        )

        with clock_at(datetime(2026, 10, 26, 9, 0)):
            await notifications.check_schedule_reminders(self.bot)

        self.bot.send_message.assert_not_awaited()

    async def test_deadline_failure_retries_and_success_is_deduplicated(self):
        now = datetime(2026, 10, 26, 9, 0)
        await self.save(self.task(now + timedelta(hours=3)))
        self.bot.send_message.side_effect = [
            RuntimeError("Temporary delivery failure"),
            None,
        ]

        with clock_at(now):
            with self.assertLogs(notifications.logger, level="ERROR"):
                await notifications.check_deadline_reminders(self.bot)
            self.assertFalse(notifications.notified_tasks)
            await notifications.check_deadline_reminders(self.bot)
            await notifications.check_deadline_reminders(self.bot)

        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(len(notifications.notified_tasks), 1)

    async def test_deadline_escapes_subject_and_description(self):
        now = datetime(2026, 10, 26, 9, 0)
        await self.save(
            self.task(
                now + timedelta(hours=3),
                subject="C++ & <AI>",
                description="Compare x < 3 & y > 1",
            )
        )

        with clock_at(now):
            await notifications.check_deadline_reminders(self.bot)

        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("<b>C++ &amp; &lt;AI&gt;</b>", text)
        self.assertIn("Compare x &lt; 3 &amp; y &gt; 1", text)

    async def test_deadline_windows_include_3d_1d_3h_boundaries_and_exclude_other_tasks(self):
        now = datetime(2026, 10, 26, 9, 0)
        disabled_user = User(
            id=2,
            telegram_id=900002,
            full_name="Notifications disabled",
            notify_deadline=False,
        )
        await self.save(disabled_user)
        due_minutes = (175, 185, 1435, 1445, 4315, 4325)
        excluded_minutes = (-1, 0, 60, 174, 186, 1434, 1446, 4314, 4326)
        await self.save(
            *[
                self.task(now + timedelta(minutes=minutes), subject=f"Due {minutes}")
                for minutes in due_minutes
            ],
            *[
                self.task(
                    now + timedelta(minutes=minutes), subject=f"Excluded {minutes}"
                )
                for minutes in excluded_minutes
            ],
            self.task(
                now + timedelta(hours=3), subject="Completed", is_completed=True
            ),
            self.task(
                now + timedelta(hours=3),
                subject="Disabled",
                user_id=disabled_user.id,
            ),
        )

        with clock_at(now):
            await notifications.check_deadline_reminders(self.bot)
            await notifications.check_deadline_reminders(self.bot)

        texts = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual(len(texts), len(due_minutes))
        for minutes in due_minutes:
            self.assertTrue(any(f"<b>Due {minutes}</b>" in text for text in texts))
        self.assertEqual(sum("3 kun qoldi" in text for text in texts), 2)
        self.assertEqual(sum("1 kun qoldi" in text for text in texts), 2)
        self.assertEqual(sum("3 soat qoldi" in text for text in texts), 2)

    async def test_same_task_gets_3d_1d_and_3h_reminders(self):
        now = datetime(2026, 10, 26, 9, 0)
        await self.save(self.task(now + timedelta(days=3)))

        with clock_at(now):
            await notifications.check_deadline_reminders(self.bot)
        with clock_at(now + timedelta(days=2)):
            await notifications.check_deadline_reminders(self.bot)
        with clock_at(now + timedelta(days=2, hours=21)):
            await notifications.check_deadline_reminders(self.bot)
            await notifications.check_deadline_reminders(self.bot)

        self.assertEqual(self.bot.send_message.await_count, 3)
        texts = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertIn("3 kun qoldi", texts[0])
        self.assertIn("1 kun qoldi", texts[1])
        self.assertIn("3 soat qoldi", texts[2])

    async def test_hemis_class_and_task_reminders_are_sent(self):
        now = datetime(2026, 10, 26, 9, 0)
        async with self.sessions() as session:
            user = await session.get(User, self.user.id)
            user.hemis_api_url = "https://student.example.uz/rest/v1/"
            user.hemis_session_ciphertext = "encrypted-session"
            await session.commit()

        hemis_lesson = {
            "id": 777,
            "lesson_date": "2026-10-26",
            "subject": {"name": "Sun'iy intellekt"},
            "lessonPair": {"start_time": "09:10", "end_time": "10:30"},
            "auditorium": {"name": "204-xona"},
            "employee": {"name": "Karimov A."},
        }
        deadline_3d_ts = (
            now + timedelta(days=3)
        ).replace(tzinfo=timezone(timedelta(hours=5))).timestamp()
        hemis_task = {
            "id": 888,
            "name": "2-mustaqil ish",
            "comment": "Prezentatsiya",
            "deadline": deadline_3d_ts,
            "subject": {"name": "Refaktoring"},
        }

        with (
            patch.object(
                notifications,
                "decrypt_hemis_session",
                return_value={"access_token": "tok", "refresh_token": "ref"},
            ),
            patch.object(
                notifications,
                "fetch_day_schedule",
                new=AsyncMock(return_value=({}, [hemis_lesson], {"access_token": "tok", "refresh_token": "ref"})),
            ),
            patch.object(
                notifications,
                "fetch_upcoming_tasks",
                new=AsyncMock(return_value=({}, [hemis_task], {"access_token": "tok", "refresh_token": "ref"})),
            ),
        ):
            with clock_at(now):
                await notifications.check_schedule_reminders(self.bot)
                await notifications.check_deadline_reminders(self.bot)
            with clock_at(now + timedelta(minutes=5)):
                await notifications.check_schedule_reminders(self.bot)
            with clock_at(now + timedelta(minutes=10)):
                await notifications.check_schedule_reminders(self.bot)

        texts = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual(len(texts), 4)
        self.assertIn("10 daqiqadan keyin boshlanadi!", texts[0])
        self.assertIn("3 kun qoldi!", texts[1])
        self.assertIn("5 daqiqadan keyin boshlanadi!", texts[2])
        self.assertIn("Dars boshlandi!", texts[3])

    async def test_start_scheduler_twice_keeps_one_job_per_check(self):
        scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
        try:
            with patch.object(notifications, "scheduler", scheduler):
                notifications.start_scheduler(self.bot)
                notifications.start_scheduler(self.bot)
            self.assertTrue(scheduler.running)
            self.assertEqual(
                {job.id for job in scheduler.get_jobs()},
                {"schedule_check", "deadline_check"},
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)
        self.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
