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

from pathlib import Path
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
    start_morning_outage,
    get_shop_group,
        # Опросы вывесок
    get_sign_check_shops,
    create_sign_check,
    claim_sign_check_delivery,
    mark_sign_check_delivery_failed,
    save_sign_check_message,
    get_sign_check,
    answer_sign_check,

    # Напоминания по утреннему опросу
    prepare_daily_check_reminders,
    get_due_daily_check_reminders,
    advance_daily_check_reminder,
    stop_daily_check_reminders,
    mark_daily_check_escalation_sent,
    
)


SUPERVISORS_FILE = Path(__file__).resolve().parent.parent / "supervisors.json"
router = Router()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

def get_supervisors_without_sign_checks() -> set[int]:
    """MAX user_id супервайзеров, у которых sign_checks = false в supervisors.json."""
    try:
        with open(SUPERVISORS_FILE, encoding="utf-8") as f:
            supervisors = json.load(f)
    except Exception:
        logging.exception("Не удалось прочитать supervisors.json")
        return set()

    return {
        s["max_user_id"]
        for s in supervisors
        if not s.get("sign_checks", True)
    }

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

def sign_keyboard(check_id: int, check_type: str):
    builder = InlineKeyboardBuilder()

    if check_type == "on":
        builder.row(CallbackButton(
            text="✅ Включили",
            payload=f"sign_check:on:{check_id}"
        ))
    else:
        builder.row(CallbackButton(
            text="❌ Выключили",
            payload=f"sign_check:off:{check_id}"
        ))

    return builder.as_markup()

async def send_sign_checks(bot, check_type: str, check_date: str | None = None):
    check_date = check_date or datetime.now(MOSCOW_TZ).date().isoformat()

    excluded = get_supervisors_without_sign_checks()
    all_shops = await get_sign_check_shops()
    shops = [
        shop for shop in all_shops
        if dict(shop).get("supervisor_id") not in excluded
    ]

    question = (
        "💡 ВКЛЮЧИТЕ ВЫВЕСКУ!"
        if check_type == "on"
        else "🌙 ВЫКЛЮЧИТЕ ВЫВЕСКУ!"
    )

    logging.info(
        "Отправляем опрос вывесок: type=%s, shops=%s (исключено: %s)",
        check_type,
        len(shops),
        len(all_shops) - len(shops),
    )

    for shop in shops:
        check_id = await create_sign_check(
            shop_id=shop["id"],
            check_date=check_date,
            check_type=check_type
        )

        if check_id is None:
            continue

        claimed = await claim_sign_check_delivery(check_id)

        if not claimed:
            continue

        try:
            sent = await bot.send_message(
                chat_id=shop["panel_chat_id"],
                text=(
                    f"{question}\n\n"
                    f"Магазин: {shop['code']}\n"
                    f"Адрес: {shop['address']}"
                ),
                attachments=[sign_keyboard(check_id, check_type)]
            )

            if sent is None or sent.message is None or sent.message.body is None:
                raise RuntimeError("MAX не вернул отправленное сообщение")

            await save_sign_check_message(
                check_id=check_id,
                chat_id=shop["panel_chat_id"],
                message_id=sent.message.body.mid
            )

            logging.info("Опрос вывески отправлен магазину %s", shop["code"])

        except Exception as exc:
            await mark_sign_check_delivery_failed(check_id, repr(exc))
            logging.exception(
                "Ошибка отправки опроса вывески магазину %s",
                shop["code"]
            )
            
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

# Сообщения-напоминания удаляются через 5 секунд.
_temporary_message_tasks = set()


async def delete_message_after_delay(bot, message_id, delay=5):
    await asyncio.sleep(delay)

    try:
        await bot.delete_message(message_id=message_id)
    except Exception:
        logging.exception(
            "Не удалось удалить временное сообщение напоминания"
        )


def schedule_message_deletion(bot, message_id):
    task = asyncio.create_task(
        delete_message_after_delay(bot, message_id)
    )

    _temporary_message_tasks.add(task)
    task.add_done_callback(_temporary_message_tasks.discard)



async def process_daily_check_reminders(bot):
    """
    Обрабатывает напоминания по утреннему опросу.

    Стадии:
    0 — 09:15, первое напоминание.
    1 — 09:30, второе напоминание.
    2 — 10:00, последнее напоминание.
    3 — пропуск уведомления супервайзеру.
    4+ — повторные напоминания магазину каждый час.
    """

    # После 20:00 напоминания не отправляем.
    if datetime.now(MOSCOW_TZ).hour >= 20:
        return

    today = datetime.now(MOSCOW_TZ).date().isoformat()

    # Создаём записи для всех доставленных опросов без ответа.
    await prepare_daily_check_reminders(today)

    reminders = await get_due_daily_check_reminders(today)

    for reminder in reminders:
        shop_id = reminder["shop_id"]
        check_date = reminder["check_date"]
        stage = reminder["reminder_stage"]

        # Стадия 3 — пропускаем уведомление супервайзеру.
        if stage == 3:
            await advance_daily_check_reminder(
                shop_id,
                check_date,
                stage
            )
            continue

        if stage == 0:
            message_text = (
                "⚠️ НАПОМИНАНИЕ!\n\n"
                "Вы ещё не ответили на утренний опрос "
                "о состоянии электроснабжения.\n"
                "Пожалуйста, пройдите опрос!"
            )

        elif stage == 1:
            message_text = (
                "⚠️ ВТОРОЕ НАПОМИНАНИЕ!\n\n"
                "Мы всё ещё не получили ваш ответ на утренний опрос.\n"
                "Пожалуйста, ответьте сейчас!"
            )

        elif stage == 2:
            message_text = (
                "🚨 ПОСЛЕДНЕЕ НАПОМИНАНИЕ!\n\n"
                "Вы не ответили на утренний опрос.\n"
                "Пожалуйста, ответьте сейчас!"
            )

        else:
            message_text = (
                "⚠️ ВЫ НЕ ПРОШЛИ УТРЕННИЙ ОПРОС!\n\n"
                "Пожалуйста, сообщите состояние электроснабжения "
                "нажатием кнопки в утреннем опросе."
            )

        try:
            sent = await bot.send_message(
                chat_id=reminder["panel_chat_id"],
                text=message_text
            )

            if (
                sent is not None
                and sent.message is not None
                and sent.message.body is not None
            ):
                schedule_message_deletion(
                    bot,
                    sent.message.body.mid
                )

            advanced = await advance_daily_check_reminder(
                shop_id,
                check_date,
                stage
            )

            if advanced:
                logging.info(
                    "Отправлено напоминание магазину %s, стадия %s",
                    reminder["code"],
                    stage
                )

        except Exception:
            logging.exception(
                "Не удалось отправить напоминание магазину %s",
                reminder["code"]
            )

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

    all_sent = True

    for group_id, group_shops in grouped.items():
        messages = []
        for shop in group_shops:
            if shop["status"] == "off":
                messages.append(
                    f"🔴 {shop['code']}\n"
                    f"{shop['address']}\n"
                    "8:15 открыт без ЭЭ"
                )

        text = (
            "\n\n".join(messages)
            if messages
            else "🟢 Все магазины открылись с ЭЭ"
        )

        try:
            await bot.send_message(chat_id=group_id, text=text)
        except Exception:
            all_sent = False
            logging.exception(
                "Не удалось отправить утреннюю сводку в группу %s",
                group_id
            )

    if all_sent:
        await mark_daily_checks_summary_sent(check_date)
        logging.info(
            "Утренние сводки за %s отправлены. Магазинов: %s",
            check_date, len(results)
        )

def build_closing_summary(shops) -> str:
    """Формирует вечерний отчёт, исключая магазины без подтверждённого статуса."""
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

        elif shop["status"] == "unknown":
            continue

    if not messages:
        return "Вечерний отчёт\n\nНет магазинов, закрывшихся без ЭЭ"

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
    Расписание (Europe/Moscow):
    08:15–12:00 — утренний опрос электричества.
    08:30–12:00 — опрос включения вывески.
    Напоминания проверяются каждые 30 секунд.
    22:00 — опрос выключения вывески.
    22:10 — вечерний отчёт.

    Каждый вид отправки запускается один раз за календарный день.
    Верхняя граница окна нужна, чтобы рестарт бота вечером
    не рассылал утренние опросы.
    """

    logging.info("Планировщик запущен (Europe/Moscow)")

    completed = {
        "daily": None,
        "sign_on": None,
        "sign_off": None,
        "summary": None,
    }

    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            today = now.date().isoformat()
            m = now.hour * 60 + now.minute

            # 08:15–12:00 — утренний опрос электричества.
            if 8 * 60 + 15 <= m < 12 * 60 and completed["daily"] != today:
                completed["daily"] = today
                await send_daily_checks(bot, today)

            # 08:30–12:00 — включение вывески.
            if 8 * 60 + 30 <= m < 12 * 60 and completed["sign_on"] != today:
                completed["sign_on"] = today
                await send_sign_checks(
                    bot,
                    check_type="on",
                    check_date=today
                )

            # Напоминания проверяются постоянно.
            await process_daily_check_reminders(bot)

            # 22:00–24:00 — выключение вывески.
            if m >= 22 * 60 and completed["sign_off"] != today:
                completed["sign_off"] = today
                await send_sign_checks(
                    bot,
                    check_type="off",
                    check_date=today
                )

            # 22:10–24:00 — вечерний отчёт.
            if m >= 22 * 60 + 10 and completed["summary"] != today:
                completed["summary"] = today
                await send_closing_summary(bot, today)

            await asyncio.sleep(30)

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

        await stop_daily_check_reminders(
            db_user["shop_id"],
            check["check_date"]
        )

        morning_time = datetime.fromisoformat(
            f"{check['check_date']}T08:15:00+03:00"
        ).isoformat(timespec="seconds")

        try:
            if status == "off":
                # Закрываем предыдущее отключение и создаём новое с 08:15.
                result = await start_morning_outage(
                    shop_id=db_user["shop_id"],
                    morning_time=morning_time
                )

                # В группу супервайзера пишем только про отсутствие света.
                group = await get_shop_group(db_user["shop_id"])

                if group is not None:
                    await event.bot.send_message(
                        chat_id=group["group_chat_id"],
                        text=(
                            f"🔴 №{db_user['code']}\n"
                            f"{db_user['address']}\n"
                            "Открылся без ЭЭ в 8:15"
                        )
                    )

            else:
                # Закрываем активное отключение, если оно есть.
                # В группу ничего не отправляем.
                result = await switch_power(
                    shop_id=db_user["shop_id"],
                    action="power_on",
                    ended_at_override=morning_time
                )

            # Обновляем панель магазина.
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
                "Не удалось обработать утренний ответ магазина %s",
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


@router.message_callback(
    F.callback.payload.startswith("sign_check:")
)

async def sign_check_callback(event: MessageCallback):
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

        check = await get_sign_check(check_id)

        if check is None:
            return

        if check["shop_id"] != db_user["shop_id"]:
            return
        
        if check["check_type"] != status:
            return
        
        saved = await answer_sign_check(
            check_id=check_id,
            shop_id=db_user["shop_id"]
        )

        if not saved:
            return

        logging.info(
            "Магазин %s ответил на опрос вывески: %s",
            db_user["code"],
            status
        )

        try:
            await event.bot.delete_message(
                message_id=event.message.body.mid
            )
        except Exception:
            logging.exception(
                "Не удалось удалить сообщение опроса вывески"
            )

    except Exception:
        logging.exception("Ошибка обработки опроса вывески")