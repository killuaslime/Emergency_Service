import asyncio
import logging

from maxapi import Bot, Dispatcher

from config import BOT_TOKEN
from database.database import (
    init_db,
    sync_supervisors_from_json,
    sync_shops_from_json,
)



from handlers.start import router as start_router
from handlers.registration import router as registration_router
from handlers.panel import (
    router as panel_router,
    group_notification_scheduler,
)
from handlers.supervisor import router as supervisor_router
from handlers.daily_check import (
    router as daily_check_router,
    daily_check_scheduler,
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


async def _run_background_task(name: str, coroutine):
    """Не даёт фоновой задаче незаметно завершиться с исключением."""
    try:
        await coroutine
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Фоновая задача %s аварийно завершилась", name)
        raise


async def main():
    await init_db()
    await sync_supervisors_from_json()
    await sync_shops_from_json()

    logging.info("Бот запускается...")

    tasks = [
        asyncio.create_task(
            _run_background_task("daily_check_scheduler", daily_check_scheduler(bot)),
            name="daily_check_scheduler",
        ),
        asyncio.create_task(
            _run_background_task("group_notification_scheduler", group_notification_scheduler(bot)),
            name="group_notification_scheduler",
        ),
    ]

    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
