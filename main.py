import argparse
import asyncio
import ctypes
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InputProfilePhotoStatic

from config import BOT_TOKEN
from database.engine import init_db
from middlewares.db_middleware import ChannelSubscriptionMiddleware, DbSessionMiddleware
from scheduler.notifications import start_scheduler

# Handlerlarni import qilish
from handlers import start, schedule, tasks, academic

# Logging sozlash
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)
PROFILE_PHOTO = Path(__file__).resolve().parent / "assets" / "profile-photo.jpg"


async def set_profile_photo():
    """Botning profil rasmini o'rnatish; chatga xabar yuborilmaydi."""
    async with Bot(token=BOT_TOKEN) as bot:
        await bot.set_my_profile_photo(
            photo=InputProfilePhotoStatic(photo=FSInputFile(PROFILE_PHOTO))
        )
    logger.info("Botning profil rasmi o'rnatildi.")


async def _start_health_server() -> web.AppRunner | None:
    """Render / Koyeb bepul Web Service uchun HTTP health-check server."""
    port_str = os.getenv("PORT", "").strip()
    if not port_str.isdigit():
        return None

    async def handle_health(_request: web.Request) -> web.Response:
        return web.Response(text="OK - Talaba Yordamchisi Bot is running 24/7")

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(port_str))
    await site.start()
    logger.info("Health-check HTTP server port %s da ishga tushdi.", port_str)

    ping_url = (
        os.getenv("SELF_PING_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    ).strip()
    if ping_url:
        async def _self_ping_loop() -> None:
            url = f"{ping_url.rstrip('/')}/health"
            while True:
                await asyncio.sleep(600)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=10):
                            pass
                except Exception:
                    pass

        asyncio.create_task(_self_ping_loop())
    return runner


def keep_system_awake() -> None:
    """Windows uyqu rejimiga va Wi-Fi uzilishiga tushmasligi uchun."""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                0x80000000 | 0x00000001 | 0x00000002 | 0x00000040
            )
        except Exception:
            pass


async def main():
    keep_system_awake()
    # Bazani ishga tushirish
    await init_db()
    logger.info("Ma'lumotlar bazasi tayyor.")
    await _start_health_server()

    # Bot va Dispatcher (qisqa timeout Wi-Fi qotib qolishini oldini oladi)
    session = AiohttpSession(timeout=12)
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot_info = await bot.get_me()
    if not bot_info.supports_inline_queries:
        logger.warning(
            "Telegram Inline Mode o'chirilgan. @BotFather'da /setinline orqali yoqing."
        )
    try:
        await bot.set_my_short_description(
            short_description=(
                "🎓 OTM talabalari uchun HEMIS yordamchisi: dars jadvali, topshiriq eslatmalari, davomat, baholar, GPA va resurslar."
            )
        )
        await bot.set_my_description(
            description=(
                "🎓 Talaba Yordamchisi — OTM talabalari uchun HEMIS bot!\n\n"
                "📅 Dars jadvali + darsga 10, 5 daqiqa qolganda va boshlanganda eslatma\n"
                "📝 Topshiriqlar + muddatidan 3 kun, 1 kun va 3 soat oldin ogohlantirish\n"
                "📊 Davomat (NB) — sababli va sababsiz darslar nazorati\n"
                "📈 Baholar va 🎯 GPA — fan ballari, semestr va yillik GPA\n"
                "🎓 Imtihonlar va 📚 Resurslar — imtihon sanalari hamda elektron fayllar\n\n"
                "👇 Boshlash uchun «START» tugmasini bosing!"
            )
        )
    except Exception as exc:
        logger.warning("Bot tavsifini o'rnatib bo'lmadi: %s", exc)
    dp = Dispatcher()

    # Middleware ni ulash
    dp.message.middleware(DbSessionMiddleware())
    dp.message.middleware(ChannelSubscriptionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(ChannelSubscriptionMiddleware())
    dp.chat_member.middleware(DbSessionMiddleware())

    # Routerlarni ulash
    dp.include_router(start.router)
    dp.include_router(schedule.router)
    dp.include_router(tasks.router)
    dp.include_router(academic.router)

    # Schedulerni ishga tushirish (eslatmalar uchun)
    start_scheduler(bot)

    logger.info("Bot ishga tushmoqda...")

    # Qayta ishga tushirish paytida navbatdagi xabarlarni saqlash.
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(
        bot,
        polling_timeout=5,
        allowed_updates=["message", "callback_query", "inline_query", "chat_member"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Talaba Yordamchisi bot")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--set-profile-photo",
        action="store_true",
        help="Botning profil rasmini o'rnatish va chiqish",
    )
    actions.add_argument(
        "--migrate-db",
        action="store_true",
        help="Bazani zaxiralab yangilash va botni ishga tushirmasdan chiqish",
    )
    args = parser.parse_args()
    try:
        if args.set_profile_photo:
            asyncio.run(set_profile_photo())
        elif args.migrate_db:
            asyncio.run(init_db())
        else:
            asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
