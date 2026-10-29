import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers import start


def session_for(user):
    result = Mock()
    result.scalar_one_or_none.return_value = user
    session = AsyncMock()
    session.execute.return_value = result
    return session


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_and_callbacks_are_registered_before_help(self):
        self.assertIn(
            start.settings_handler,
            [handler.callback for handler in start.router.message.handlers],
        )
        callbacks = [
            handler.callback for handler in start.router.callback_query.handlers
        ]
        self.assertIn(start.toggle_class, callbacks)
        self.assertIn(start.toggle_deadline, callbacks)

        handler_counts = (
            len(start.router.message.handlers),
            len(start.router.callback_query.handlers),
        )
        message = SimpleNamespace(answer=AsyncMock())
        await start.help_handler(message)
        help_text = message.answer.await_args.args[0]
        self.assertIn("HEMIS tizimiga qanday ulaniladi?", help_text)
        self.assertIn("HEMIS tizimiga kira olmasangiz nima qilish kerak?", help_text)
        await start.help_handler(message)
        self.assertEqual(
            handler_counts,
            (
                len(start.router.message.handlers),
                len(start.router.callback_query.handlers),
            ),
        )

    async def test_settings_show_stored_preferences(self):
        user = SimpleNamespace(notify_class=False, notify_deadline=True)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42), answer=AsyncMock()
        )
        session = session_for(user)

        await start.settings_handler(message, session)

        keyboard = message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "❌ Dars eslatmalari")
        self.assertEqual(
            keyboard.inline_keyboard[1][0].text, "✅ Deadline eslatmalari"
        )

    async def test_toggles_update_clicked_users_existing_keyboard(self):
        for handler, field, row in (
            (start.toggle_class, "notify_class", 0),
            (start.toggle_deadline, "notify_deadline", 1),
        ):
            with self.subTest(field=field):
                user = SimpleNamespace(notify_class=True, notify_deadline=True)
                session = session_for(user)
                message = SimpleNamespace(
                    from_user=SimpleNamespace(id=999),
                    answer=AsyncMock(),
                    edit_reply_markup=AsyncMock(),
                )
                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=42),
                    message=message,
                    answer=AsyncMock(),
                )

                await handler(callback, session)

                self.assertFalse(getattr(user, field))
                session.commit.assert_awaited_once()
                session.execute.assert_awaited_once()
                query = session.execute.await_args.args[0]
                self.assertEqual(list(query.compile().params.values()), [42])
                callback.answer.assert_awaited_once_with("O'zgartirildi!")
                message.answer.assert_not_awaited()
                message.edit_reply_markup.assert_awaited_once()
                keyboard = message.edit_reply_markup.await_args.kwargs["reply_markup"]
                self.assertTrue(keyboard.inline_keyboard[row][0].text.startswith("❌"))
                self.assertTrue(
                    keyboard.inline_keyboard[1 - row][0].text.startswith("✅")
                )

    async def test_unknown_user_gets_start_instruction(self):
        session = session_for(None)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            answer=AsyncMock(),
        )

        await start.toggle_class(callback, session)

        callback.answer.assert_awaited_once_with(
            "Avval /start buyrug'ini yuboring.", show_alert=True
        )
        session.commit.assert_not_awaited()
        callback.message.edit_reply_markup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
