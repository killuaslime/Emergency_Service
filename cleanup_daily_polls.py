
import asyncio
import aiosqlite

from datetime import datetime
from maxapi import Bot

from config import BOT_TOKEN
from database.database import DB_PATH, MOSCOW_TZ


async def main():
    today = datetime.now(MOSCOW_TZ).date().isoformat()

    # Получаем только сегодняшние опросы без ответа
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                dc.id,
                dc.message_chat_id,
                dc.message_id,
                s.code
            FROM daily_checks dc
            JOIN shops s ON s.id = dc.shop_id
            WHERE dc.check_date = ?
              AND dc.status IS NULL
              AND dc.message_chat_id IS NOT NULL
              AND dc.message_id IS NOT NULL
            ORDER BY s.code
        """, (today,))

        polls = await cursor.fetchall()

    if not polls:
        print("Просроченных опросов для удаления нет.")
        return

    print(f"Найдено опросов: {len(polls)}")

    for poll in polls:
        print(
            f"Магазин №{poll['code']} | "
            f"ID опроса: {poll['id']}"
        )

    confirmation = input(
        "\nДля удаления введи DELETE: "
    )

    if confirmation != "DELETE":
        print("Удаление отменено.")
        return

    bot = Bot(token=BOT_TOKEN)

    deleted = 0
    failed = 0

    for poll in polls:
        try:
            await bot.delete_message(
                message_id=poll["message_id"]
            )

            deleted += 1

            print(
                f"Удалено: магазин №{poll['code']}"
            )

        except Exception as error:
            failed += 1

            print(
                f"Ошибка удаления №{poll['code']}: {error}"
            )

    print("\n========== РЕЗУЛЬТАТ ==========")
    print(f"Удалено: {deleted}")
    print(f"Ошибок: {failed}")
    print("База данных не изменялась.")


if __name__ == "__main__":
    asyncio.run(main())
