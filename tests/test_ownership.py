"""Handler ownership regressions using SQLite foreign keys and mocked Telegram IO."""

from datetime import datetime, time, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, Schedule, Task, User
from handlers import schedule, tasks


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            self.assertEqual(await connection.scalar(text("PRAGMA foreign_keys")), 1)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        # A Telegram ID can coincide with someone else's internal ID. A query
        # using the wrong identifier must not expose or reassign their records.
        self.owner = User(id=17, telegram_id=900017, full_name="Owner")
        self.other = User(id=900017, telegram_id=900018, full_name="Other student")
        await self.save(self.owner, self.other)
        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        await self.storage.close()
        await self.engine.dispose()

    async def save(self, *objects):
        async with self.sessions() as session:
            session.add_all(objects)
            await session.commit()

    def state(self, telegram_id=None):
        telegram_id = telegram_id or self.owner.telegram_id
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
        )

    def message(self, value, telegram_id=None):
        return SimpleNamespace(
            text=value,
            from_user=SimpleNamespace(id=telegram_id or self.owner.telegram_id),
            answer=AsyncMock(),
        )

    def callback(self, data, telegram_id=None):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=telegram_id or self.owner.telegram_id),
            answer=AsyncMock(),
            message=SimpleNamespace(
                from_user=SimpleNamespace(id=self.other.telegram_id),
                edit_text=AsyncMock(),
            ),
        )

    def lesson(self, owner=None, **changes):
        values = dict(
            user_id=(owner or self.owner).id,
            subject="My lesson",
            day_of_week=1,
            start_time=time(9, 30),
        )
        values.update(changes)
        return Schedule(**values)

    def task(self, owner=None, **changes):
        values = dict(
            user_id=(owner or self.owner).id,
            subject="My task",
            description="Read chapter 1",
            deadline=datetime.now() + timedelta(days=1),
        )
        values.update(changes)
        return Task(**values)

    async def test_task_list_shows_only_current_users_incomplete_tasks(self):
        await self.save(
            self.task(),
            self.task(self.other, subject="Private task"),
            self.task(subject="Completed task", is_completed=True),
        )
        message = self.message("📝 Topshiriqlar")

        async with self.sessions() as session:
            await tasks.tasks_menu(message, session, self.state())

        reply = "\n".join(call.args[0] for call in message.answer.await_args_list)
        self.assertIn("My task", reply)
        self.assertNotIn("Private task", reply)
        self.assertNotIn("Completed task", reply)

    async def test_task_insert_uses_internal_user_id(self):
        state = self.state()
        await state.set_state(tasks.AddTask.deadline)
        await state.set_data({"subject": "New task", "description": "Exercise 5"})

        async with self.sessions() as session:
            await tasks.add_task_deadline(
                self.message("25.12.2030 23:59"), state, session
            )
        async with self.sessions() as session:
            saved = (await session.scalars(select(Task))).one()
            self.assertEqual(saved.user_id, self.owner.id)
            self.assertEqual(saved.description, "Exercise 5")
        self.assertIsNone(await state.get_state())

    async def test_unregistered_writes_preserve_state_and_do_not_commit(self):
        for handler, fsm_state, value, data, model in (
            (
                tasks.add_task_deadline, tasks.AddTask.deadline, "25.12.2030 23:59",
                {"subject": "Unsaved task", "description": "Keep this text"}, Task,
            ),
        ):
            with self.subTest(handler=handler.__name__):
                state = self.state(77777)
                await state.set_state(fsm_state)
                await state.set_data(data)
                message = self.message(value, 77777)
                async with self.sessions() as session:
                    with patch.object(session, "commit", new=AsyncMock()) as commit:
                        await handler(message, state, session)
                        commit.assert_not_awaited()
                        self.assertEqual(
                            await session.scalar(select(func.count()).select_from(model)), 0
                        )
                        self.assertFalse(session.new)
                message.answer.assert_awaited_once_with("Avval /start buyrug'ini yuboring.")
                self.assertEqual(await state.get_state(), fsm_state.state)
                self.assertEqual(await state.get_data(), data)

    async def test_unregistered_task_list_requests_start_and_preserves_task_state(self):
        state = self.state(77777)
        await state.set_state(tasks.AddTask.description)
        await state.set_data({"subject": "Keep my draft"})
        task_message = self.message("📝 Topshiriqlar", 77777)
        async with self.sessions() as session:
            await tasks.tasks_menu(task_message, session, state)
        task_message.answer.assert_awaited_once_with("Avval /start buyrug'ini yuboring.")
        self.assertEqual(await state.get_state(), tasks.AddTask.description.state)
        self.assertEqual(await state.get_data(), {"subject": "Keep my draft"})

    async def test_other_users_callbacks_cannot_delete_or_complete_records(self):
        lesson = self.lesson(self.other)
        task = self.task(self.other)
        await self.save(lesson, task)
        for handler, prefix, record in (
            (schedule.delete_schedule, "sched_del", lesson),
            (tasks.delete_task, "task_del", task),
            (tasks.mark_task_done, "task_done", task),
        ):
            with self.subTest(handler=handler.__name__):
                callback = self.callback(f"{prefix}:{record.id}")
                async with self.sessions() as session:
                    with patch.object(session, "commit", new=AsyncMock()) as commit:
                        await handler(callback, session)
                        commit.assert_not_awaited()
                callback.answer.assert_awaited_once_with("❌ Topilmadi!", show_alert=True)
                callback.message.edit_text.assert_not_awaited()
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(Schedule, lesson.id))
            self.assertFalse((await session.get(Task, task.id)).is_completed)

    async def test_owner_can_complete_and_delete_own_records(self):
        lesson = self.lesson()
        task = self.task()
        other_task = self.task(self.other)
        await self.save(lesson, task, other_task)
        completed = self.callback(f"task_done:{task.id}")
        async with self.sessions() as session:
            await tasks.mark_task_done(completed, session)
        completed.message.edit_text.assert_awaited_once()
        async with self.sessions() as session:
            self.assertTrue((await session.get(Task, task.id)).is_completed)
            self.assertFalse((await session.get(Task, other_task.id)).is_completed)

        for handler, prefix, record, model in (
            (schedule.delete_schedule, "sched_del", lesson, Schedule),
            (tasks.delete_task, "task_del", task, Task),
        ):
            callback = self.callback(f"{prefix}:{record.id}")
            async with self.sessions() as session:
                await handler(callback, session)
            callback.message.edit_text.assert_awaited_once()
            async with self.sessions() as session:
                self.assertIsNone(await session.get(model, record.id))

    async def test_invalid_callback_data_is_rejected_without_writes(self):
        for handler, prefix in (
            (schedule.delete_schedule, "sched_del"),
            (tasks.delete_task, "task_del"),
            (tasks.mark_task_done, "task_done"),
        ):
            invalid_data = [None, prefix, "wrong:1"] + [
                f"{prefix}:{suffix}" for suffix in (
                    "", "abc", "0", "-1", "1:2", " 1", "١", "9223372036854775808", "9" * 10000,
                )
            ]
            for data in invalid_data:
                with self.subTest(handler=handler.__name__, data=(data or "")[:50]):
                    callback = self.callback(data)
                    async with self.sessions() as session:
                        with patch.object(session, "commit", new=AsyncMock()) as commit:
                            await handler(callback, session)
                            commit.assert_not_awaited()
                    callback.answer.assert_awaited_once_with("❌ Topilmadi!", show_alert=True)
                    callback.message.edit_text.assert_not_awaited()

    async def test_unregistered_callbacks_request_start_without_writes(self):
        lesson = self.lesson()
        task = self.task()
        await self.save(lesson, task)
        for handler, prefix, record in (
            (schedule.delete_schedule, "sched_del", lesson),
            (tasks.delete_task, "task_del", task),
            (tasks.mark_task_done, "task_done", task),
        ):
            with self.subTest(handler=handler.__name__):
                callback = self.callback(f"{prefix}:{record.id}", 77777)
                async with self.sessions() as session:
                    with patch.object(session, "commit", new=AsyncMock()) as commit:
                        await handler(callback, session)
                        commit.assert_not_awaited()
                callback.answer.assert_awaited_once_with(
                    "Avval /start buyrug'ini yuboring.", show_alert=True
                )
                callback.message.edit_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
