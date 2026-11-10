
import logging
from datetime import datetime

from maxapi import Router, F
from maxapi.types import MessageCreated, MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import GROUP_CHAT_ID

from database.database import (
    get_user,
    get_latest_outage,
    save_panel,
    switch_power,
)

router = Router()


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"

    dt = datetime.fromisoformat(value)
    return dt.strftime("%d.%m.%Y в %H:%M:%S")

def format_time(value: str | None) -> str:
    if not value:
        return "—"

    return datetime.fromisoformat(value).strftime("%H:%M")

def calculate_duration(start: str, end: str) -> str:
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    total_seconds = max(
        0,
        int((end_dt - start_dt).total_seconds())
    )

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts = []

    if days:
        parts.append(f"{days} д.")

    if hours:
        parts.append(f"{hours} ч.")

    parts.append(f"{minutes} мин.")

    return " ".join(parts)


def create_keyboard(is_off: bool):
    builder = InlineKeyboardBuilder()

    if is_off:
        builder.row(
            CallbackButton(
                text="🟢 Свет есть",
                payload="power_on"
            )
        )
    else:
        builder.row(
            CallbackButton(
                text="🔴 Свет отключился",
                payload="power_off"
            )
        )

    return builder.as_markup()


def get_panel_text(user, outage) -> str:
    if outage is None:
        return (
            "⚡ АВАРИЙНОЕ ОПОВЕЩЕНИЕ\n\n"
            f"Магазин: {user['code']}\n"
            f"Адрес: {user['address']}\n\n"
            "🟢 Электроснабжение в норме.\n\n"
            "Нажмите кнопку, если электричество отключится."
        )

    started_at = format_datetime(outage["started_at"])

    # Отключение ещё продолжается
    if outage["ended_at"] is None:
        return (
            "⚠️ АВАРИЙНОЕ ОПОВЕЩЕНИЕ\n\n"
            f"Магазин: {user['code']}\n"
            f"Адрес: {user['address']}\n\n"
            "🔴 ЭЛЕКТРОСНАБЖЕНИЕ ОТКЛЮЧЕНО\n\n"
            f"Время отключения: {started_at}\n\n"
            "Нажмите кнопку, когда электричество появится."
        )

    ended_at = format_datetime(outage["ended_at"])
    duration = calculate_duration(
        outage["started_at"],
        outage["ended_at"]
    )

    return (
        "⚡ АВАРИЙНОЕ ОПОВЕЩЕНИЕ\n\n"
        f"Магазин: {user['code']}\n"
        f"Адрес: {user['address']}\n\n"
        "🟢 ЭЛЕКТРОСНАБЖЕНИЕ В НОРМЕ\n\n"
        f"Время отключения: {started_at}\n"
        f"Время включения: {ended_at}\n"
        f"Продолжительность: {duration}\n\n"
        "Если электричество снова отключится, нажмите кнопку."
    )


async def show_panel(event: MessageCreated):
    user = await event.fetch_from_user()

    if user is None:
        return

    db_user = await get_user(user.user_id)

    if db_user is None:
        return

    # Проверяем, есть ли уже сохранённая панель
    if db_user["panel_message_id"]:
        return

    outage = await get_latest_outage(db_user["shop_id"])
    is_off = outage is not None and outage["ended_at"] is None

    sent = await event.message.answer(
        text=get_panel_text(db_user, outage),
        attachments=[create_keyboard(is_off)]
    )

    if sent and sent.message and sent.message.body:
        await save_panel(
            shop_id=db_user["shop_id"],
            chat_id=sent.message.recipient.chat_id,
            message_id=sent.message.body.mid
        )



@router.message_callback(
    F.callback.payload.in_(["power_off", "power_on"])
)


async def power_callback(event: MessageCallback):
    try:
        user = await event.fetch_from_user()

        if user is None:
            await event.answer(notification="Не удалось определить пользователя.")
            return

        db_user = await get_user(user.user_id)

        if db_user is None:
            await event.answer(notification="У вас нет доступа к управлению магазином.")
            return

        if event.message is None or event.message.body is None:
            await event.answer(notification="Не удалось найти панель.")
            return

        action = event.callback.payload

        result = await switch_power(
            shop_id=db_user["shop_id"],
            action=action
        )

        outage = await get_latest_outage(db_user["shop_id"])
        is_off = outage is not None and outage["ended_at"] is None

        keyboard = create_keyboard(is_off)

        # print("\n========== PANEL DEBUG ==========")
        # print("ACTION:", action)
        # print("RESULT:", result)
        # print("OUTAGE:", dict(outage) if outage else None)
        # print("IS_OFF:", is_off)
        # print("MESSAGE ID:", event.message.body.mid)
        # print("KEYBOARD TYPE:", type(keyboard))
        # print("KEYBOARD:", repr(keyboard))

        edited = await event.bot.edit_message(
            message_id=event.message.body.mid,
            text=get_panel_text(db_user, outage),
            attachments=[keyboard],
            notify=False
        )

        print("EDIT RESULT:", repr(edited))
        # Отправляем оповещение в рабочую группу
        try:
            await send_group_notification(
                bot=event.bot,
                action=action,
                user=db_user,
                outage=outage,
                result=result
            )
            print("GROUP NOTIFICATION: отправлено")
        except Exception:
            logging.exception("Не удалось отправить сообщение в группу")

        if result["status"] == "off":
            notification = "Отключение зарегистрировано."
        elif result["status"] == "on":
            notification = (
                "Электричество включено. "
                f"Продолжительность: {result['duration']}."
            )
        elif result["status"] == "already_off":
            notification = "Электричество уже отключено."
        elif result["status"] == "already_on":
            notification = "Электричество уже включено."
        else:
            notification = "Состояние обновлено."

        # await event.answer(notification=notification)

    except Exception:
        logging.exception("Ошибка при обработке нажатия кнопки")

        try:
            await event.answer(
                notification="Не удалось обновить панель. Попробуйте ещё раз."
            )
        except Exception:
            pass



async def send_group_notification(bot, action, user, outage, result):
    if action == "power_off" and result["status"] == "off":
        emoji = "🔴"
        time_text = (
            f"Отключение ЭЭ с {format_time(outage['started_at'])}"
        )

    elif action == "power_on" and result["status"] == "on":
        emoji = "🟢"
        time_text = (
            f"Отключение ЭЭ с {format_time(outage['started_at'])} "
            f"до {format_time(outage['ended_at'])}"
        )

    else:
        return

    text = (
        f"{emoji}№{user['code']}\n"
        f"{user['address'].strip()}\n"
        f"{time_text}"
    )

    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=text
    )

async def refresh_shop_panel(bot, db_user):
    """Обновляет существующую главную панель магазина."""

    if not db_user["panel_message_id"]:
        logging.warning(
            "У магазина %s нет сохранённой панели",
            db_user["code"]
        )
        return None

    outage = await get_latest_outage(db_user["shop_id"])

    is_off = outage is not None and outage["ended_at"] is None

    await bot.edit_message(
        message_id=db_user["panel_message_id"],
        text=get_panel_text(db_user, outage),
        attachments=[create_keyboard(is_off)],
        notify=False
    )

    return outage