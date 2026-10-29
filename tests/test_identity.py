import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, User
from handlers import schedule, start


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def message(self, username="MTX_2477"):
        return SimpleNamespace(
            from_user=SimpleNamespace(
                id=900001, username=username, full_name="Student <test>"
            ),
            via_bot=None,
            answer=AsyncMock(),
        )

    async def test_start_stores_username_separately_from_numeric_identity(self):
        message = self.message()
        async with self.sessions() as session:
            await start.cmd_start(message, session, AsyncMock())
            user = await session.scalar(select(User))
            self.assertEqual(user.telegram_id, 900001)
            self.assertEqual(user.username, "MTX_2477")
            self.assertNotEqual(user.id, user.telegram_id)
            self.assertIsNone(user.phone)
        self.assertIn("Asosiy menyu", message.answer.await_args.args[0])
        self.assertIn("&lt;test&gt;", message.answer.await_args.args[0])

    async def test_start_refreshes_username_without_changing_registered_profile(self):
        async with self.sessions() as session:
            user = User(
                telegram_id=900001, username="old_name", full_name="Ali Valiyev",
                first_name="Ali", last_name="Valiyev", phone="+998901234567",
                notify_class=False, notify_deadline=False,
            )
            session.add(user)
            await session.commit()
            original_id = user.id
            for username in ("MTX_2477", None):
                await start.cmd_start(self.message(username), session, AsyncMock())
                await session.refresh(user)
                self.assertEqual(user.username, username)
                self.assertEqual(user.id, original_id)
                self.assertEqual(user.full_name, "Ali Valiyev")
                self.assertFalse(user.notify_class)
                self.assertFalse(user.notify_deadline)

    async def test_registration_persists_names_and_own_phone(self):
        async with self.sessions() as session:
            user = User(telegram_id=900001, username="student", full_name="Student")
            session.add(user)
            await session.commit()
            message = self.message()
            message.contact = SimpleNamespace(
                user_id=900001, phone_number="+998901234567"
            )
            message.text = None
            state = AsyncMock()
            state.get_data.return_value = {"first_name": "Ali", "last_name": "Valiyev"}

            await start.registration_phone(message, session, state)

            await session.refresh(user)
            self.assertEqual(user.first_name, "Ali")
            self.assertEqual(user.last_name, "Valiyev")
            self.assertEqual(user.phone, "+998901234567")
            self.assertEqual(user.full_name, "Ali Valiyev")

    async def test_hemis_search_uses_catalog_and_saves_matches_in_fsm(self):
        from unittest.mock import patch

        institutions = [
            {
                "code": "341",
                "name": "Buxoro davlat universiteti",
                "api_url": "https://student.example.uz/rest/v1/",
                "student_url": "https://student.example.uz/",
            },
            {
                "code": "342",
                "name": "Samarqand davlat universiteti",
                "api_url": "https://student.sam.example.uz/rest/v1/",
                "student_url": "https://student.sam.example.uz/",
            },
        ]
        message = self.message()
        message.text = "S"
        state = AsyncMock()

        with patch(
            "handlers.start.get_hemis_institutions",
            new=AsyncMock(return_value=institutions),
        ):
            await start.search_hemis_institutions(message, state)

        saved = state.update_data.await_args.kwargs["hemis_institutions"]
        self.assertEqual([item["code"] for item in saved], ["342"])
        self.assertIn("1 ta OTM", message.answer.await_args.args[0])

    async def test_inline_inserted_institution_name_is_not_searched_again(self):
        message = self.message()
        message.text = "🎓 Sharof Rashidov nomidagi Samarqand davlat universiteti"
        message.via_bot = SimpleNamespace(username="assistant_n1_bot")
        state = AsyncMock()

        await start.search_hemis_institutions(message, state)

        message.answer.assert_not_awaited()

    async def test_hemis_choice_persists_selected_institution_for_user(self):
        institution = {
            "code": "341",
            "name": "Buxoro davlat universiteti",
            "api_url": "https://student.bux.example.uz/rest/v1/",
            "student_url": "https://student.bux.example.uz/",
        }
        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                first_name="Ali",
                last_name="Valiyev",
                phone="+998901234567",
            )
            session.add(user)
            await session.commit()
            state = AsyncMock()
            state.get_data.return_value = {"hemis_institutions": [institution]}
            callback = SimpleNamespace(
                data="hemis:pick:341",
                from_user=SimpleNamespace(id=900001),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
                answer=AsyncMock(),
            )

            await start.choose_hemis_institution(callback, session, state)

            await session.refresh(user)
            self.assertEqual(user.hemis_institution_code, "341")
            self.assertEqual(user.hemis_institution_name, institution["name"])
            self.assertEqual(user.hemis_api_url, institution["api_url"])
            self.assertEqual(user.hemis_student_url, institution["student_url"])

    async def test_inline_hemis_search_returns_results_while_user_types(self):
        from unittest.mock import patch

        institutions = [
            {
                "code": "341",
                "name": "Buxoro davlat universiteti",
                "api_url": "https://student.bux.example.uz/rest/v1/",
                "student_url": "https://student.bux.example.uz/",
            },
            {
                "code": "342",
                "name": "Samarqand davlat universiteti",
                "api_url": "https://student.sam.example.uz/rest/v1/",
                "student_url": "https://student.sam.example.uz/",
            },
        ]
        inline_query = SimpleNamespace(query="S", answer=AsyncMock())
        with patch(
            "handlers.start.get_hemis_institutions",
            new=AsyncMock(return_value=institutions),
        ):
            await start.search_hemis_inline(inline_query)

        results = inline_query.answer.await_args.args[0]
        self.assertEqual([result.title for result in results], ["Samarqand davlat universiteti"])
        self.assertEqual(
            results[0].reply_markup.inline_keyboard[0][0].callback_data,
            "hemis:inline:342",
        )

    async def test_hemis_connect_offers_current_chat_live_search(self):
        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                first_name="Ali",
                last_name="Valiyev",
                phone="+998901234567",
            )
            session.add(user)
            await session.commit()
            message = self.message()
            state = AsyncMock()

            await start.start_hemis_connect(message, session, state)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            button = keyboard.inline_keyboard[0][0]
            self.assertEqual(button.switch_inline_query_current_chat, "")
            self.assertIn("Jonli qidirish", button.text)

    async def test_inline_hemis_selection_persists_for_registered_user(self):
        from unittest.mock import patch

        institution = {
            "code": "342",
            "name": "Samarqand davlat universiteti",
            "api_url": "https://student.sam.example.uz/rest/v1/",
            "student_url": "https://student.sam.example.uz/",
        }
        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                first_name="Ali",
                last_name="Valiyev",
                phone="+998901234567",
            )
            session.add(user)
            await session.commit()
            callback = SimpleNamespace(
                data="hemis:inline:342",
                from_user=SimpleNamespace(id=900001),
                answer=AsyncMock(),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            state = AsyncMock()
            with patch(
                "handlers.start.get_hemis_institutions",
                new=AsyncMock(return_value=[institution]),
            ):
                await start.choose_hemis_inline(callback, session, state)

            await session.refresh(user)
            self.assertEqual(user.hemis_institution_code, "342")
            self.assertEqual(user.hemis_institution_name, institution["name"])
            self.assertEqual(user.hemis_api_url, institution["api_url"])
            self.assertEqual(user.hemis_student_url, institution["student_url"])

    async def test_hemis_login_prompts_for_password_without_persisting_login(self):
        from unittest.mock import patch

        message = self.message()
        message.text = "student-login"
        state = AsyncMock()
        with patch("handlers.start.HEMIS_SESSION_KEY", "configured-key"):
            await start.receive_hemis_login(message, state)

        state.update_data.assert_awaited_once_with(hemis_login="student-login")
        self.assertIn("parolingizni", message.answer.await_args.args[0])

    async def test_hemis_password_is_deleted_and_only_encrypted_session_is_saved(self):
        from unittest.mock import patch

        institution = {
            "code": "342",
            "name": "Samarqand davlat universiteti",
            "api_url": "https://student.sam.example.uz/rest/v1/",
            "student_url": "https://student.sam.example.uz/",
        }
        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                first_name="Ali",
                last_name="Valiyev",
                phone="+998901234567",
                **{
                    "hemis_institution_code": institution["code"],
                    "hemis_institution_name": institution["name"],
                    "hemis_api_url": institution["api_url"],
                    "hemis_student_url": institution["student_url"],
                },
            )
            session.add(user)
            await session.commit()
            message = self.message()
            message.text = "secret-user-password"
            message.delete = AsyncMock()
            state = AsyncMock()
            state.get_data.return_value = {"hemis_login": "student-login"}

            with (
                patch("handlers.start.HEMIS_SESSION_KEY", "configured-key"),
                patch(
                    "handlers.start.authenticate_hemis",
                    new=AsyncMock(return_value={
                        "access_token": "hemis-access",
                        "refresh_token": "hemis-refresh",
                    }),
                ) as authenticate,
                patch(
                    "handlers.start.encrypt_hemis_session",
                    return_value="encrypted-session",
                ) as encrypt_session,
            ):
                await start.receive_hemis_password(message, session, state)

            authenticate.assert_awaited_once_with(
                institution["api_url"], "student-login", "secret-user-password"
            )
            encrypt_session.assert_called_once_with({
                "access_token": "hemis-access",
                "refresh_token": "hemis-refresh",
            })
            message.delete.assert_awaited_once()
            await session.refresh(user)
            self.assertEqual(user.hemis_session_ciphertext, "encrypted-session")
            self.assertFalse(hasattr(user, "hemis_password"))

    async def test_help_contains_configured_contact(self):
        message = self.message()
        await start.help_handler(message)
        self.assertIn("@" + start.SUPPORT_USERNAME, message.answer.await_args.args[0])

    async def test_id_command_reports_authenticated_senders_numeric_id(self):
        message = self.message()
        await start.show_telegram_id(message)
        self.assertIn("<code>900001</code>", message.answer.await_args.args[0])

    async def test_hemis_schedule_handler_shows_days_before_contacting_hemis(self):
        from datetime import date, timedelta
        from unittest.mock import patch

        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                first_name="Ali",
                last_name="Valiyev",
                phone="+998901234567",
                hemis_institution_name="Samarqand davlat universiteti",
                hemis_api_url="https://student.sam.example.uz/rest/v1/",
                hemis_session_ciphertext="encrypted-token",
            )
            session.add(user)
            await session.commit()
            message = self.message()
            state = AsyncMock()
            days = [date(2026, 10, 26) + timedelta(days=offset) for offset in range(6)]
            with (
                patch(
                    "handlers.schedule.decrypt_hemis_session",
                    side_effect=AssertionError("The weekday menu must not open the session"),
                ) as decrypt,
                patch(
                    "handlers.schedule.fetch_day_schedule", new=AsyncMock(),
                ) as fetch,
                patch(
                    "handlers.schedule.schedule_week_dates", return_value=days,
                ),
            ):
                await schedule.show_hemis_schedule(message, session, state)

            decrypt.assert_not_called()
            fetch.assert_not_awaited()
            state.clear.assert_awaited_once()
            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [button for row in keyboard.inline_keyboard for button in row]
            self.assertEqual(len(buttons), 8)
            self.assertEqual(
                [button.callback_data for button in buttons[:6]],
                [f"hemis_day:{day.isoformat()}" for day in days],
            )
            for button, name in zip(buttons[:6], (
                "Dushanba", "Seshanba", "Chorshanba", "Payshanba",
                "Juma", "Shanba",
            )):
                self.assertIn(name, button.text)
            self.assertEqual(buttons[6].text, "⬅️ Oldingi hafta")
            self.assertEqual(buttons[6].callback_data, "hemis_week:2026-10-19")
            self.assertEqual(buttons[7].text, "Keyingi hafta ➡️")
            self.assertEqual(buttons[7].callback_data, "hemis_week:2026-11-02")

    async def test_channel_subscription_required_on_start_and_notifies_on_leave(self):
        from unittest.mock import patch
        from services import channel_sub

        channel_sub.unsubscribed_notified_users.clear()
        message = self.message()
        message.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left"))
        )
        with patch.object(channel_sub.config, "REQUIRED_CHANNEL", "@talaba_test_channel"):
            async with self.sessions() as session:
                await start.cmd_start(message, session, AsyncMock())
                self.assertIn("kanalimizga a'zo bo'ling", message.answer.await_args.args[0])

                # A'zo bo'lmasdan turib Tasdiqlashni bossa rad etilishi shart
                unsub_callback = SimpleNamespace(
                    data="check_channel_sub",
                    from_user=SimpleNamespace(
                        id=900001, username="MTX_2477", full_name="Student <test>"
                    ),
                    bot=SimpleNamespace(
                        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left"))
                    ),
                    message=SimpleNamespace(delete=AsyncMock(), answer=AsyncMock()),
                    answer=AsyncMock(),
                )
                await start.verify_channel_subscription(unsub_callback, session)
                unsub_callback.answer.assert_awaited_once()
                self.assertTrue(unsub_callback.answer.await_args.kwargs.get("show_alert"))
                unsub_callback.message.answer.assert_not_awaited()

                # Endi foydalanuvchi kanalga a'zo bo'lib Tasdiqlashni bosadi
                callback = SimpleNamespace(
                    data="check_channel_sub",
                    from_user=SimpleNamespace(
                        id=900001, username="MTX_2477", full_name="Student <test>"
                    ),
                    bot=SimpleNamespace(
                        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
                    ),
                    message=SimpleNamespace(delete=AsyncMock(), answer=AsyncMock()),
                    answer=AsyncMock(),
                )
                await start.verify_channel_subscription(callback, session)
                self.assertIn("obunasi tasdiqlandi", callback.message.answer.await_args.args[0])

                # Foydalanuvchi kanaldan chiqib ketganda habarnoma yuboriladi
                leave_event = SimpleNamespace(
                    chat=SimpleNamespace(id=-1001, username="talaba_test_channel"),
                    new_chat_member=SimpleNamespace(
                        status="left",
                        user=SimpleNamespace(id=900001, is_bot=False),
                    ),
                    bot=SimpleNamespace(send_message=AsyncMock()),
                )
                await start.on_channel_member_update(leave_event, session)
                leave_event.bot.send_message.assert_awaited_once()
                self.assertIn(
                    "Siz kanalga ulanmagansiz",
                    leave_event.bot.send_message.await_args.kwargs["text"],
                )
        channel_sub.unsubscribed_notified_users.clear()

    async def test_menu_places_hemis_connect_first_then_replaces_with_logout_when_connected(self):
        from keyboards.reply import main_menu_kb

        disconnected_kb = main_menu_kb(None)
        self.assertEqual(disconnected_kb.keyboard[0][0].text, "🎓 HEMIS ulash")
        all_disconnected_texts = [btn.text for row in disconnected_kb.keyboard for btn in row]
        self.assertNotIn("🚪 Tizimdan chiqish", all_disconnected_texts)

        async with self.sessions() as session:
            user = User(
                telegram_id=900001,
                username="student",
                full_name="Ali Valiyev",
                hemis_institution_name="SamDU",
                hemis_api_url="https://student.sam.example.uz/rest/v1/",
                hemis_session_ciphertext="encrypted-token",
            )
            session.add(user)
            await session.commit()

            connected_kb = main_menu_kb(user)
            all_connected_texts = [btn.text for row in connected_kb.keyboard for btn in row]
            self.assertNotIn("🎓 HEMIS ulash", all_connected_texts)
            self.assertIn("🚪 Tizimdan chiqish", all_connected_texts)

            # Tizimdan chiqish bosilganda sessiya tozalanib, HEMIS ulash 1-qatorga qaytadi
            msg = self.message()
            state = AsyncMock()
            await start.logout_hemis(msg, session, state)
            await session.refresh(user)
            self.assertIsNone(user.hemis_session_ciphertext)
            self.assertIsNone(user.hemis_api_url)
            reply_kb = msg.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(reply_kb.keyboard[0][0].text, "🎓 HEMIS ulash")

        # Bo'sh inline qidiruvda hech qanday havola chiqmasligi kerak
        empty_inline = SimpleNamespace(query="", answer=AsyncMock())
        await start.search_hemis_inline(empty_inline)
        self.assertEqual(empty_inline.answer.await_args.args[0], [])

    async def test_smart_institution_search_finds_samdaqu_and_all_variants(self):
        from services.hemis_catalog import SUPPLEMENTAL_INSTITUTIONS, match_hemis_institutions

        catalog = list(SUPPLEMENTAL_INSTITUTIONS) + [
            {
                "code": "313",
                "name": "Samarqand davlat texnika universiteti",
                "api_url": "https://student.samdaqu.edu.uz/rest/v1/",
                "student_url": "https://student.samdaqu.edu.uz/",
            },
            {
                "code": "342",
                "name": "Farg\u2018ona davlat universiteti",
                "api_url": "https://student.fdu.uz/rest/v1/",
                "student_url": "https://student.fdu.uz/",
            },
            {
                "code": "380",
                "name": "Muhammad al-Xorazmiy nomidagi Toshkent axborot texnologiyalari universiteti",
                "api_url": "https://xmn.tuit.uz/rest/v1/",
                "student_url": "https://xmn.tuit.uz/",
            },
        ]

        for q in (
            "samarqand davlat arxitektura qurilish",
            "Samarqand arxitektura",
            "SamDAQU",
            "Самарқанд давлат архитектура қурилиш",
        ):
            matches = match_hemis_institutions(catalog, q)
            self.assertTrue(
                any("313" in item["code"] for item in matches),
                f"Failed to match SamDAQU for query: {q}",
            )

        self.assertEqual(match_hemis_institutions(catalog, "Farg'ona")[0]["code"], "342")
        self.assertEqual(match_hemis_institutions(catalog, "Fargona")[0]["code"], "342")
        self.assertEqual(match_hemis_institutions(catalog, "TATU")[0]["code"], "380")


if __name__ == "__main__":
    unittest.main()
