
import asyncio
import logging

from maxapi import Bot

from config import BOT_TOKEN
from database.database import (
    init_db,
    get_daily_check_shops,
    create_daily_check,
    save_daily_check_message,
)

from handlers.daily_check import daily_keyboard

logging.basicConfig(level=logging.INFO)


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)

    try:
        shops = await get_daily_check_shops()

        # Укажи код нужного магазина
        test_shop_code = "99999"

        shop = next(
            (s for s in shops if s["code"] == test_shop_code),
            None
        )

        if shop is None:
            print(f"Магазин {test_shop_code} не найден")
            return

        check_id = await create_daily_check(
            shop["id"],
            "test-" + __import__("datetime").datetime.now().strftime("%Y%m%d%H%M%S")
        )

        if check_id is None:
            print("Не удалось создать тестовый опрос")
            return

        sent = await bot.send_message(
            chat_id=shop["panel_chat_id"],
            text=(
                "🧪 ТЕСТОВЫЙ ОПРОС\n\n"
                f"Магазин: {shop['code']}\n"
                f"Адрес: {shop['address']}\n\n"
                "Проверьте работу кнопок. "
                "Это тестовое сообщение."
            ),
            attachments=[daily_keyboard(check_id)]
        )

        if (
            sent is None
            or sent.message is None
            or sent.message.body is None
        ):
            raise RuntimeError("MAX не вернул отправленное сообщение")

        await save_daily_check_message(
            check_id=check_id,
            chat_id=shop["panel_chat_id"],
            message_id=sent.message.body.mid
        )

        print(f"Тестовый опрос отправлен магазину {test_shop_code}")

    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
