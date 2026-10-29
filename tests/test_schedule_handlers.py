"""Regressions for the weekday picker and its authenticated daily requests."""

from datetime import date
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, User
from handlers import schedule
from services.hemis_schedule import HemisScheduleError, HemisSessionExpired


API_URL = "https://student.example.uz/rest/v1/"
SESSION_DATA = {"access_token": "access-old", "refresh_token": "refresh-old"}
SELECTED_DATE = date(2026, 10, 28)
LESSON = {
    "lesson_date": "2026-10-28T08:00:00+05:00",
    "subject": {"name": "Fizika <1>"},
    "lessonPair": {"start_time": "09:00", "end_time": "10:20"},
    "employee": {"name": "Ali Valiyev"},
    "auditorium": {"name": "305-xona"},
}


class ScheduleHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all([
                User(
                    id=17, telegram_id=900017, full_name="Student",
                    hemis_institution_name="Example university",
                    hemis_api_url=API_URL,
                    hemis_session_ciphertext="owner-encrypted-session",
                    timezone="Asia/Tashkent",
                ),
                User(
                    id=900017, telegram_id=900018, full_name="Other student",
                    hemis_institution_name="Other university",
                    hemis_api_url="https://other.example.uz/rest/v1/",
                    hemis_session_ciphertext="other-encrypted-session",
                    timezone="Asia/Tashkent",
                ),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def callback(self, data="hemis_day:2026-10-28", telegram_id=900017):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=telegram_id),
            answer=AsyncMock(),
            message=SimpleNamespace(
                # Telegram's message sender is the bot, not the clicked user.
                from_user=SimpleNamespace(id=900018), answer=AsyncMock(),
            ),
        )

    async def stored_ciphertext(self, telegram_id=900017):
        async with self.sessions() as session:
            return await session.scalar(
                select(User.hemis_session_ciphertext).where(User.telegram_id == telegram_id)
            )

    def reply_text(self, callback):
        return "\n".join(call.args[0] for call in callback.message.answer.await_args_list)

    async def test_selected_day_uses_clicked_users_session_and_acknowledges_before_fetch(self):
        callback = self.callback()

        async def fetch(api_url, data, selected_date, timezone_name):
            callback.answer.assert_awaited_once()
            self.assertEqual(api_url, API_URL)
            self.assertEqual(data, SESSION_DATA)
            self.assertEqual(selected_date, SELECTED_DATE)
            self.assertEqual(timezone_name, "Asia/Tashkent")
            return {"name": "Semestr <7>"}, [LESSON], dict(SESSION_DATA)

        with (
            patch.object(schedule, "decrypt_hemis_session", return_value=dict(SESSION_DATA)) as decrypt,
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock(side_effect=fetch)) as request,
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)

        decrypt.assert_called_once_with("owner-encrypted-session")
        request.assert_awaited_once()
        reply = self.reply_text(callback)
        for text in ("Chorshanba, 28.10.2026", "Fizika &lt;1&gt;", "09:00–10:20", "Ali Valiyev", "305-xona", "Semestr &lt;7&gt;"):
            self.assertIn(text, reply)
        keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(sum(len(row) for row in keyboard.inline_keyboard), 8)

    async def test_refreshed_tokens_are_encrypted_and_saved_only_for_clicked_user(self):
        updated = {"access_token": "access-new", "refresh_token": "refresh-new"}
        callback = self.callback()
        with (
            patch.object(schedule, "decrypt_hemis_session", return_value=dict(SESSION_DATA)),
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock(return_value=({}, [LESSON], updated))),
            patch.object(schedule, "encrypt_hemis_session", return_value="new-encrypted-session") as encrypt,
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        encrypt.assert_called_once_with(updated)
        self.assertEqual(await self.stored_ciphertext(), "new-encrypted-session")
        self.assertEqual(await self.stored_ciphertext(900018), "other-encrypted-session")

    async def test_day_without_lessons_reports_selected_date_and_keeps_picker(self):
        callback = self.callback()
        with (
            patch.object(schedule, "decrypt_hemis_session", return_value=dict(SESSION_DATA)),
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock(return_value=({}, [], dict(SESSION_DATA)))),
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        reply = self.reply_text(callback)
        self.assertIn("28.10.2026", reply)
        self.assertIn("topilmadi", reply)
        self.assertNotIn("Birozdan keyin", reply)
        self.assertTrue(callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard)

    async def test_expired_session_is_cleared_and_requires_reconnection(self):
        callback = self.callback()
        with (
            patch.object(schedule, "decrypt_hemis_session", return_value=dict(SESSION_DATA)),
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock(side_effect=HemisSessionExpired("expired"))),
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        self.assertIsNone(await self.stored_ciphertext())
        self.assertEqual(await self.stored_ciphertext(900018), "other-encrypted-session")
        self.assertIn("qayta kiring", self.reply_text(callback))

    async def test_temporary_hemis_error_preserves_session_and_day_picker_for_retry(self):
        callback = self.callback()
        with (
            patch.object(schedule, "decrypt_hemis_session", return_value=dict(SESSION_DATA)),
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock(side_effect=HemisScheduleError("Could not reach HEMIS."))),
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        self.assertEqual(await self.stored_ciphertext(), "owner-encrypted-session")
        self.assertIn("qayta", self.reply_text(callback).lower())
        self.assertTrue(callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard)

    async def test_invalid_day_callback_does_not_read_credentials_or_fetch(self):
        for data in (
            None, "hemis_day", "wrong:2026-10-28", "hemis_day:",
            "hemis_day:2026-02-30", "hemis_day:20261028", "hemis_day:2026-W44-3",
            "hemis_day:2026-10-28:1", "hemis_day: 2026-10-28",
        ):
            with self.subTest(data=data):
                callback = self.callback(data)
                with (
                    patch.object(schedule, "decrypt_hemis_session") as decrypt,
                    patch.object(schedule, "fetch_day_schedule", new=AsyncMock()) as fetch,
                ):
                    async with self.sessions() as session:
                        await schedule.show_hemis_day(callback, session)
                callback.answer.assert_awaited_once()
                decrypt.assert_not_called()
                fetch.assert_not_awaited()

    async def test_callback_without_accessible_message_does_not_fetch(self):
        callback = self.callback()
        callback.message = None
        with patch.object(schedule, "fetch_day_schedule", new=AsyncMock()) as fetch:
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        fetch.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_unregistered_or_disconnected_user_cannot_fetch(self):
        for telegram_id in (77777, 900017):
            with self.subTest(telegram_id=telegram_id):
                callback = self.callback(telegram_id=telegram_id)
                with (
                    patch.object(schedule, "decrypt_hemis_session") as decrypt,
                    patch.object(schedule, "fetch_day_schedule", new=AsyncMock()) as fetch,
                ):
                    async with self.sessions() as session:
                        if telegram_id == 900017:
                            user = await session.get(User, 17)
                            user.hemis_session_ciphertext = None
                            await session.commit()
                        await schedule.show_hemis_day(callback, session)
                callback.answer.assert_awaited_once()
                decrypt.assert_not_called()
                fetch.assert_not_awaited()

    async def test_unreadable_session_has_specific_reconnect_message_without_api_request(self):
        callback = self.callback()
        with (
            patch.object(schedule, "decrypt_hemis_session", side_effect=ValueError("cannot decrypt")),
            patch.object(schedule, "fetch_day_schedule", new=AsyncMock()) as fetch,
        ):
            async with self.sessions() as session:
                await schedule.show_hemis_day(callback, session)
        fetch.assert_not_awaited()
        self.assertIn("qayta kiring", self.reply_text(callback))
        self.assertNotIn("Birozdan keyin", self.reply_text(callback))



    async def test_switch_hemis_week_updates_message_with_new_week_dates(self):
        callback = self.callback(data="hemis_week:2026-11-02")
        callback.message.edit_text = AsyncMock()
        async with self.sessions() as session:
            await schedule.switch_hemis_week(callback, session)
        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("02.11.2026 — 07.11.2026", text)
        keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 8)
        self.assertEqual(buttons[0].callback_data, "hemis_day:2026-11-02")
        self.assertEqual(buttons[5].callback_data, "hemis_day:2026-11-07")
        self.assertEqual(buttons[6].callback_data, "hemis_week:2026-10-26")
        self.assertEqual(buttons[7].callback_data, "hemis_week:2026-11-09")


if __name__ == "__main__":
    unittest.main()
