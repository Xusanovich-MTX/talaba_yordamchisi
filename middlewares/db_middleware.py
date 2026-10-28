from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from database.engine import async_session
from services.channel_sub import (
    get_channel_config,
    is_user_subscribed,
    subscription_prompt_kb,
    subscription_prompt_text,
)


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        async with async_session() as session:
            data["session"] = session
            return await handler(event, data)


class ChannelSubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        cfg = get_channel_config()
        if cfg is None:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_channel_sub":
            return await handler(event, data)

        bot = data.get("bot") or getattr(event, "bot", None)
        from_user = getattr(event, "from_user", None)
        if bot is None or from_user is None:
            return await handler(event, data)

        if await is_user_subscribed(bot, from_user.id):
            return await handler(event, data)

        _, channel_url = cfg
        if isinstance(event, Message):
            if event.text and event.text.startswith("/start"):
                return await handler(event, data)
            await event.answer(
                subscription_prompt_text(left=True),
                reply_markup=subscription_prompt_kb(channel_url),
            )
            return None

        if isinstance(event, CallbackQuery):
            await event.answer(
                "⚠️ Botdan foydalanish uchun avval kanalga a'zo bo'ling!",
                show_alert=True,
            )
            if event.message:
                await event.message.answer(
                    subscription_prompt_text(left=True),
                    reply_markup=subscription_prompt_kb(channel_url),
                )
            return None

        return await handler(event, data)
