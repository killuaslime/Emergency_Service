
import asyncio
import logging

from maxapi import Bot, Dispatcher

from config import BOT_TOKEN
from database.database import init_db, sync_shops_from_json

from handlers.start import router as start_router
from handlers.registration import router as registration_router
from handlers.panel import router as panel_router
from handlers.supervisor import router as supervisor_router
from handlers.daily_check import (
    router as daily_check_router,
    daily_check_scheduler,send_daily_checks,
)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.include_routers(
    start_router,
    registration_router,
    panel_router,
    supervisor_router,
    daily_check_router,
)


async def main():
    await init_db()
    await sync_shops_from_json()    
    logging.info("Бот запускается...")

    # ТЕСТ УТРЕННЕГО ОПРОСА
    # await send_daily_checks(bot)

    scheduler_task = asyncio.create_task(daily_check_scheduler(bot))
    
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
