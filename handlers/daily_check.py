
import asyncio
import logging

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from maxapi import Router, F
from maxapi.types import MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import GROUP_CHAT_ID

from handlers.panel import refresh_shop_panel

from database.database import (
    get_user,
    get_daily_check_shops,
    create_daily_check,
    save_daily_check_message,
    get_daily_check,
    answer_daily_check,
    get_daily_check_results,
    mark_daily_checks_summary_sent,
    has_pending_daily_summary,
    get_latest_outage,
    switch_power,
    get_closing_summary_shops,
    has_daily_summary_been_sent,
    mark_daily_summary_as_sent,
)

router = Router()

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def daily_keyboard(check_id: int):
    builder = InlineKeyboardBuilder()

    builder.row(
        CallbackButton(
            text="🟢 Есть свет",
            payload=f"daily_check:on:{check_id}"
        )
    )

    builder.row(
        CallbackButton(
            text="🔴 Нет света",
            payload=f"daily_check:off:{check_id}"
        )
    )

    return builder.as_markup()


async def send_daily_checks(bot):
    now = datetime.now(MOSCOW_TZ)
    check_date = now.date().isoformat()

    shops = await get_daily_check_shops()

    logging.info(
        "Начинаем утренний опрос. Магазинов: %s",
        len(shops)
    )

    for shop in shops:
        check_id = await create_daily_check(
            shop["id"],
            check_date
        )

        if check_id is None:
            continue

        try:
            sent = await bot.send_message(
                chat_id=shop["panel_chat_id"],
                text=(
                    "☀️ ДОБРОЕ УТРО!\n\n"
                    f"Магазин: {shop['code']}\n"
                    f"Адрес: {shop['address']}\n\n"
                    "Пожалуйста, сообщите текущее состояние "
                    "электроснабжения:"
                ),
                attachments=[daily_keyboard(check_id)]
            )

            if (
                sent is None
                or sent.message is None
                or sent.message.body is None
            ):
                raise RuntimeError(
                    "MAX не вернул отправленное сообщение"
                )

            await save_daily_check_message(
                check_id=check_id,
                chat_id=shop["panel_chat_id"],
                message_id=sent.message.body.mid
            )

            logging.info(
                "Опрос отправлен магазину %s",
                shop["code"]
            )

        except Exception:
            logging.exception(
                "Не удалось отправить опрос магазину %s",
                shop["code"]
            )


async def send_daily_summary(bot, check_date: str):
    # Не отправляем сводку повторно
    if not await has_pending_daily_summary(check_date):
        logging.info(
            "Сводка за %s уже отправлена или опросов нет",
            check_date
        )
        return

    results = await get_daily_check_results(check_date)

    if not results:
        return

    messages = []

    for shop in results:
        code = shop["code"]
        address = shop["address"]
        status = shop["status"]

        if status == "off":
            emoji = "🔴"
            status_text = "8:15 открыт без ЭЭ"

        elif status == "on":
            emoji = "🟢"
            status_text = "8:15 открыт с ЭЭ"

        else:
            emoji = "⚪"
            status_text = "8:15 нет ответа"

        message = (
            f"{emoji} {code}\n"
            f"{address}\n"
            f"{status_text}"
        )

        messages.append(message)

    text = "\n\n".join(messages)

    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=text
    )

    await mark_daily_checks_summary_sent(check_date)

    logging.info(
        "Утренняя сводка за %s отправлена. Магазинов: %s",
        check_date,
        len(results)
    )
async def send_closing_summary(bot, summary_date: str):
    """
    Вечерняя сводка в 22:10.
    """

    if await has_daily_summary_been_sent(
        summary_date,
        "closing"
    ):
        logging.info(
            "Вечерняя сводка за %s уже отправлена",
            summary_date
        )
        return

    shops = await get_closing_summary_shops()

    if not shops:
        logging.warning(
            "Нет зарегистрированных магазинов для вечерней сводки"
        )
        await mark_daily_summary_as_sent(
            summary_date,
            "closing"
        )
        return

    messages = []

    for shop in shops:
        if shop["status"] == "off":
            emoji = "🔴"
            status_text = "Закрыт без ЭЭ"
        else:
            emoji = "🟢"
            status_text = "Закрыт с ЭЭ"

        messages.append(
            f"{emoji} №{shop['code']}\n"
            f"{shop['address']}\n"
            f"{status_text}"
        )

    text = "\n\n".join(messages)

    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=text
    )

    await mark_daily_summary_as_sent(
        summary_date,
        "closing"
    )

    logging.info(
        "Вечерняя сводка за %s отправлена. Магазинов: %s",
        summary_date,
        len(shops)
    )

async def daily_check_scheduler(bot):
    """
    08:15 — утренние опросы.
    08:30 — утренняя сводка.
    22:10 — вечерняя сводка.
    """

    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            check_date = now.date().isoformat()

            poll_time = now.replace(
                hour=8, minute=15, second=0, microsecond=0
            )
            morning_summary_time = now.replace(
                hour=8, minute=30, second=0, microsecond=0
            )
            closing_summary_time = now.replace(
                hour=22, minute=10, second=0, microsecond=0
            )

            # Если бот запустился до 08:15 — ждём
            if now < poll_time:
                await asyncio.sleep(
                    (poll_time - now).total_seconds()
                )
                await send_daily_checks(bot)

            # Если бот запустился между 08:15 и 08:30
            elif now < morning_summary_time:
                await send_daily_checks(bot)

            # Ждём 08:30, если время ещё не наступило
            now = datetime.now(MOSCOW_TZ)

            if now < morning_summary_time:
                await asyncio.sleep(
                    (morning_summary_time - now).total_seconds()
                )

            # Утренняя сводка
            await send_daily_summary(bot, check_date)

            # Ждём 22:10
            now = datetime.now(MOSCOW_TZ)

            if now < closing_summary_time:
                await asyncio.sleep(
                    (closing_summary_time - now).total_seconds()
                )

            # Вечерняя сводка
            await send_closing_summary(bot, check_date)

            # Ждём следующего дня
            now = datetime.now(MOSCOW_TZ)

            next_poll = now.replace(
                hour=8,
                minute=15,
                second=0,
                microsecond=0
            ) + timedelta(days=1)

            logging.info(
                "Следующий утренний опрос: %s",
                next_poll.strftime("%d.%m.%Y %H:%M:%S")
            )

            await asyncio.sleep(
                (next_poll - now).total_seconds()
            )

        except Exception:
            logging.exception(
                "Ошибка планировщика ежедневных проверок"
            )
            await asyncio.sleep(60)

@router.message_callback(
    F.callback.payload.startswith("daily_check:")
)

async def daily_check_callback(event: MessageCallback):
    try:
        user = await event.fetch_from_user()

        if user is None:
            return

        db_user = await get_user(user.user_id)

        if db_user is None:
            return

        if event.message is None or event.message.body is None:
            return

        parts = event.callback.payload.split(":")

        if len(parts) != 3:
            return

        _, status, check_id_raw = parts

        if status not in ("on", "off"):
            return

        check_id = int(check_id_raw)

        check = await get_daily_check(check_id)

        if check is None:
            return

        # Проверяем, что сотрудник отвечает за этот магазин
        if check["shop_id"] != db_user["shop_id"]:
            return

        # После 08:30 ответы уже не принимаются
        deadline = datetime.fromisoformat(
            f"{check['check_date']}T08:30:00+03:00"
        )

        if datetime.now(MOSCOW_TZ) >= deadline:
            return

        saved = await answer_daily_check(
            check_id=check_id,
            shop_id=db_user["shop_id"],
            status=status
        )

        if not saved:
            return

        morning_time = datetime.fromisoformat(
            f"{check['check_date']}T08:15:00+03:00"
        ).isoformat(timespec="seconds")

        try:
            if status == "off":
                # Если отключение уже существует,
                # switch_power оставит его открытым.
                result = await switch_power(
                    shop_id=db_user["shop_id"],
                    action="power_off",
                    started_at_override=morning_time
                )

            else:
                # Если есть старое незакрытое отключение,
                # закрываем его временем 08:15.
                result = await switch_power(
                    shop_id=db_user["shop_id"],
                    action="power_on",
                    ended_at_override=morning_time
                )

            # Обновляем панель магазина
            await refresh_shop_panel(
                event.bot,
                db_user
            )

            logging.info(
                "Утренний ответ: магазин=%s статус=%s результат=%s",
                db_user["code"],
                status,
                result["status"]
            )

        except Exception:
            logging.exception(
                "Не удалось обновить состояние электричества магазина %s",
                db_user["code"]
            )

        # Удаляем сообщение с кнопками после сохранения ответа
        try:
            await event.bot.delete_message(
                message_id=event.message.body.mid
            )
        except Exception:
            logging.exception(
                "Ответ сохранён, но сообщение удалить не удалось"
            )

    except Exception:
        logging.exception(
            "Ошибка обработки утреннего опроса"
        )
