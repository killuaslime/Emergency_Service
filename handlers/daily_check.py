import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import aiosqlite
import json
from collections import defaultdict

from maxapi import Router, F
from maxapi.types import MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from handlers.panel import refresh_shop_panel
from handlers.supervisor import refresh_supervisor_daily_report

from database.database import (
    get_user,
    get_daily_check_shops,
    create_daily_check,
    claim_daily_check_delivery,
    mark_daily_check_delivery_failed,
    save_daily_check_message,
    get_daily_check,
    answer_daily_check,
    get_daily_check_results,
    mark_daily_checks_summary_sent,
    has_pending_daily_summary,
    switch_power,
    get_closing_summary_shops,
    has_daily_summary_been_sent,
    get_daily_summary_job,
    save_daily_summary_job,
    claim_daily_summary_job,
    mark_daily_summary_job_sent,
    update_daily_summary_job_payload,
)

router = Router()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def daily_keyboard(check_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(
        text="🟢 Есть свет",
        payload=f"daily_check:on:{check_id}"
    ))
    builder.row(CallbackButton(
        text="🔴 Нет света",
        payload=f"daily_check:off:{check_id}"
    ))
    return builder.as_markup()


async def send_daily_checks(bot, check_date: str | None = None):
    """Создаёт/доставляет опросы за дату; незавершённые отправки повторяются."""
    check_date = check_date or datetime.now(MOSCOW_TZ).date().isoformat()
    shops = await get_daily_check_shops()
    logging.info(
        "Проверяем утренние опросы за %s. Магазинов: %s",
        check_date, len(shops)
    )

    for shop in shops:
        check_id = await create_daily_check(shop["id"], check_date)
        if check_id is None:
            continue  # Опрос уже доставлен.

        claimed = await claim_daily_check_delivery(check_id, retry_after_seconds=60)
        if not claimed:
            continue  # Другая попытка ещё выполняется или не истёк интервал.

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
            if sent is None or sent.message is None or sent.message.body is None:
                raise RuntimeError("MAX не вернул отправленное сообщение")

            await save_daily_check_message(
                check_id=check_id,
                chat_id=shop["panel_chat_id"],
                message_id=sent.message.body.mid
            )
            logging.info("Опрос доставлен магазину %s", shop["code"])
        except Exception as exc:
            await mark_daily_check_delivery_failed(check_id, repr(exc))
            logging.exception("Не удалось отправить опрос магазину %s", shop["code"])


async def send_daily_summary(bot, check_date: str):
    """Отправляет утреннюю сводку отдельно в группу каждого супервайзера."""
    if not await has_pending_daily_summary(check_date):
        logging.info("Утренняя сводка за %s уже отправлена или опросов нет", check_date)
        return

    results = await get_daily_check_results(check_date)
    if not results:
        logging.info("Нет результатов утреннего опроса за %s", check_date)
        return

    grouped = defaultdict(list)
    for shop in results:
        grouped[shop["group_chat_id"]].append(shop)

    for group_id, group_shops in grouped.items():
        messages = []
        for shop in group_shops:
            status = shop["status"]
            if status == "off":
                emoji, status_text = "🔴", "8:15 открыт без ЭЭ"
            elif status == "on":
                emoji, status_text = "🟢", "8:15 открыт с ЭЭ"
            else:
                emoji, status_text = "⚪", "8:15 нет ответа"
            messages.append(f"{emoji} {shop['code']}\n{shop['address']}\n{status_text}")

        await bot.send_message(chat_id=group_id, text="\n\n".join(messages))

    await mark_daily_checks_summary_sent(check_date)
    logging.info("Утренние сводки за %s отправлены. Магазинов: %s", check_date, len(results))



def build_closing_summary(shops) -> str:
    """Формирует согласованный вечерний отчёт; unknown никогда не маскируется под on."""
    if not shops:
        return "Вечерний отчёт\n\nНет зарегистрированных магазинов для отчёта"

    if all(shop["status"] == "on" for shop in shops):
        return "Вечерний отчёт\n\n🟢 Все магазины закрылись со светом"

    messages = []
    for shop in shops:
        if shop["status"] == "off":
            messages.append(
                f"❌ №{shop['code']}\n{shop['address']}\nЗакрыт без ЭЭ"
            )

    return "Вечерний отчёт\n\n" + "\n\n".join(messages)

async def send_closing_summary(bot, summary_date):
    summary_type = "closing"

    if await has_daily_summary_been_sent(summary_date, summary_type):
        return

    shops = await get_closing_summary_shops(summary_date)

    if not shops:
        logging.warning("Нет магазинов для вечернего отчёта")
        return

    grouped = defaultdict(list)

    for shop in shops:
        grouped[shop["group_chat_id"]].append(shop)

    job = await get_daily_summary_job(summary_date, summary_type)

    if job is None:
        reports = {}

        for group_id, group_shops in grouped.items():
            messages = []

            for shop in group_shops:
                if shop["status"] == "off":
                    messages.append(
                        f"❌ №{shop['code']}\n"
                        f"{shop['address']}\n"
                        "Закрыт без ЭЭ"
                    )

                elif shop["status"] == "unknown":
                    messages.append(
                        f"⚠️ №{shop['code']}\n"
                        f"{shop['address']}\n"
                        "Нет подтверждения статуса"
                    )

            if messages:
                report = (
                    "Вечерний отчёт\n\n"
                    + "\n\n".join(messages)
                )
            else:
                report = (
                    "Вечерний отчёт\n\n"
                    "🟢 Все магазины закрылись со светом"
                )

            reports[str(group_id)] = report

        payload = {
            "reports": reports,
            "sent_groups": []
        }

        await save_daily_summary_job(
            summary_date,
            summary_type,
            json.dumps(payload, ensure_ascii=False)
        )

    else:
        payload = json.loads(job["payload"])

        # Совместимость с ранее сохранённым форматом.
        if "reports" not in payload:
            sent_groups = payload.pop("_sent_groups", [])

            payload = {
                "reports": payload,
                "sent_groups": sent_groups
            }

            await update_daily_summary_job_payload(
                summary_date,
                summary_type,
                json.dumps(payload, ensure_ascii=False)
            )

    claimed = await claim_daily_summary_job(
        summary_date,
        summary_type,
        retry_after_seconds=600
    )

    if not claimed:
        return

    job = await get_daily_summary_job(summary_date, summary_type)
    payload = json.loads(job["payload"])

    reports = payload["reports"]
    sent_groups = set(payload["sent_groups"])

    for group_id, report in reports.items():
        if group_id in sent_groups:
            continue

        chat_id = (
            int(group_id)
            if group_id.lstrip("-").isdigit()
            else group_id
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=report
            )

            sent_groups.add(group_id)

            payload["sent_groups"] = list(sent_groups)

            await update_daily_summary_job_payload(
                summary_date,
                summary_type,
                json.dumps(payload, ensure_ascii=False)
            )

            logging.info(
                "Вечерний отчёт отправлен в группу %s за %s",
                group_id,
                summary_date
            )

        except Exception:
            logging.exception(
                "Не удалось отправить вечерний отчёт в группу %s",
                group_id
            )

    if sent_groups >= set(reports.keys()):
        await mark_daily_summary_job_sent(
            summary_date,
            summary_type
        )
        logging.info(
            "Вечерний отчёт за %s полностью доставлен",
            summary_date
        )
    else:
        logging.warning(
            "Вечерний отчёт за %s доставлен не всем группам. "
            "Успешно: %s из %s",
            summary_date,
            len(sent_groups),
            len(reports)
        )


async def daily_check_scheduler(bot):
    """
    08:15 — утренние опросы.
    22:10 — вечерний отчёт.

    Без постоянного опроса каждые 30 секунд.
    Поддерживает восстановление после перезапуска.
    """

    logging.info("Планировщик запущен (Europe/Moscow)")

    last_morning_date = None
    last_closing_date = None

    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            today = now.date().isoformat()

            poll_time = now.replace(
                hour=8, minute=15, second=0, microsecond=0
            )

            closing_time = now.replace(
                hour=22, minute=10, second=0, microsecond=0
            )

            # Утренний опрос
            if now >= poll_time and last_morning_date != today:
                logging.info("Запуск утренних опросов за %s", today)

                await send_daily_checks(bot, today)
                last_morning_date = today

            # Вечерний отчёт
            if now >= closing_time and last_closing_date != today:
                logging.info("Запуск вечернего отчёта за %s", today)

                await send_closing_summary(bot, today)

                if await has_daily_summary_been_sent(today, "closing"):
                    last_closing_date = today
                    logging.info("Вечерний отчёт успешно доставлен")
                else:
                    logging.warning(
                        "Отчёт доставлен не полностью. "
                        "Следующая попытка через 10 минут"
                    )

            # Определяем следующее событие
            now = datetime.now(MOSCOW_TZ)
            today = now.date().isoformat()

            poll_time = now.replace(
                hour=8, minute=15, second=0, microsecond=0
            )

            closing_time = now.replace(
                hour=22, minute=10, second=0, microsecond=0
            )

            if last_morning_date != today and now < poll_time:
                next_run = poll_time

            elif last_closing_date != today and now < closing_time:
                next_run = closing_time

            elif last_closing_date != today and now >= closing_time:
                # Повторная доставка неуспешного вечернего отчёта
                next_run = now + timedelta(minutes=10)

            else:
                # Следующий день, 08:15
                next_run = (now + timedelta(days=1)).replace(
                    hour=8, minute=15, second=0, microsecond=0
                )

            sleep_seconds = max(
                1, (next_run - now).total_seconds()
            )

            logging.info(
                "Следующая задача: %s",
                next_run.strftime("%d.%m.%Y %H:%M:%S")
            )

            await asyncio.sleep(sleep_seconds)

        except asyncio.CancelledError:
            logging.info("Планировщик остановлен")
            raise

        except Exception:
            logging.exception("Ошибка планировщика")
            await asyncio.sleep(5)


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
        # Автоматически обновляем статистику супервайзера
        if db_user["supervisor_id"] is not None:
            await refresh_supervisor_daily_report(
                bot=event.bot,
                supervisor_id=db_user["supervisor_id"],
                check_date=check["check_date"]
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
