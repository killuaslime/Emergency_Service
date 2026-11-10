
import logging
from datetime import datetime

from maxapi import Router, F
from maxapi.types import MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database.database import (
    get_supervisor_shops,
    get_supervisor_history,
)
from services.supervisors import get_supervisor_by_id

router = Router()


def format_datetime(value):
    if not value:
        return "—"

    return datetime.fromisoformat(value).strftime(
        "%d.%m.%Y в %H:%M:%S"
    )


def calculate_duration(start, end=None):
    start_dt = datetime.fromisoformat(start)
    end_dt = (
        datetime.fromisoformat(end)
        if end else datetime.now().astimezone()
    )

    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts = []

    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")

    parts.append(f"{minutes} мин.")
    return " ".join(parts)


def create_keyboard(view="menu"):
    builder = InlineKeyboardBuilder()

    if view == "menu":
        builder.row(
            CallbackButton(text="🏪 Мои магазины", payload="sv:shops")
        )
        builder.row(
            CallbackButton(text="📊 Текущая ситуация", payload="sv:status")
        )
        builder.row(
            CallbackButton(text="🔴/🟢 Детали отключения", payload="sv:details")
        )
        builder.row(
            CallbackButton(text="📚 История", payload="sv:history")
        )
    else:
        builder.row(
            CallbackButton(text="⬅️ Назад", payload="sv:menu")
        )

    return builder.as_markup()


async def get_view_text(supervisor_id, view):
    shops = await get_supervisor_shops(supervisor_id)
    if view == "menu":
        return (
            "👨‍💼 ПАНЕЛЬ СУПЕРВАЙЗЕРА\n\n"
            "Выберите нужный раздел:"
        )



    if view == "shops":
        if not shops:
            return "🏪 У вас пока нет закреплённых магазинов."

        total = len(shops)

        registered = sum(
            1 for shop in shops
            if shop["is_registered"]
        )

        pending = sum(
            1 for shop in shops
            if not shop["is_registered"]
            and shop["has_pending_request"]
        )

        not_registered = total - registered - pending

        lines = ["🏪 ВАШИ МАГАЗИНЫ\n"]

        for shop in shops:
            if shop["is_registered"]:
                registration_status = "🟢 Зарегистрирован"

                power_status = (
                    "🔴 Нет электричества"
                    if shop["started_at"] is not None
                    else "🟢 Работает"
                )

            elif shop["has_pending_request"]:
                registration_status = "🟡 Ожидает подтверждения"
                power_status = "⚪ Статус неизвестен"

            else:
                registration_status = "🔴 Не зарегистрирован"
                power_status = "⚪ Статус неизвестен"

            lines.append(
                f"Магазин: {shop['code']}\n"
                f"Адрес: {shop['address']}\n"
                f"Регистрация: {registration_status}\n"
                f"Электричество: {power_status}\n"
            )

        lines.append(
            "━━━━━━━━━━━━━━━━━━\n"
            "📊 СТАТИСТИКА РЕГИСТРАЦИИ\n\n"
            f"Всего магазинов: {total}\n"
            f"🟢 Зарегистрировано: {registered}\n"
            f"🟡 Ожидают подтверждения: {pending}\n"
            f"🔴 Не зарегистрировано: {not_registered}"
        )

        return "\n".join(lines)


    if view == "status":
        total = len(shops)
        offline = sum(
            1 for shop in shops if shop["started_at"] is not None
        )
        online = total - offline

        return (
            "📊 ТЕКУЩАЯ СИТУАЦИЯ\n\n"
            f"Всего магазинов: {total}\n"
            f"🟢 Электроснабжение в норме: {online}\n"
            f"🔴 Без электричества: {offline}"
        )

    if view == "details":
        outages = [
            shop for shop in shops
            if shop["started_at"] is not None
        ]

        if not outages:
            return (
                "🔴/🟢 ДЕТАЛИ ОТКЛЮЧЕНИЙ\n\n"
                "Сейчас все ваши магазины с электричеством."
            )

        lines = ["🔴 ТЕКУЩИЕ ОТКЛЮЧЕНИЯ\n"]

        for shop in outages:
            lines.append(
                f"Магазин: {shop['code']}\n"
                f"Адрес: {shop['address']}\n"
                f"Отключение: {format_datetime(shop['started_at'])}\n"
                f"Длительность: "
                f"{calculate_duration(shop['started_at'])}\n"
            )

        return "\n".join(lines)

    if view == "history":
        history = await get_supervisor_history(supervisor_id)

        if not history:
            return "📚 ИСТОРИЯ\n\nОтключений пока не было."

        lines = ["📚 ИСТОРИЯ ОТКЛЮЧЕНИЙ\n"]

        for item in history:
            ended = item["ended_at"]

            if ended:
                duration = calculate_duration(
                    item["started_at"], ended
                )
                status = f"Продолжительность: {duration}"
            else:
                status = "🔴 Отключение ещё продолжается"

            lines.append(
                f"Магазин: {item['code']}\n"
                f"Адрес: {item['address']}\n"
                f"Начало: {format_datetime(item['started_at'])}\n"
                f"Окончание: {format_datetime(ended)}\n"
                f"{status}\n"
            )

        return "\n".join(lines)

    return "Неизвестный раздел."


async def show_supervisor_panel(event, supervisor):
    await event.message.answer(
        text=await get_view_text(
            supervisor["max_user_id"], "menu"
        ),
        attachments=[create_keyboard("menu")]
    )


@router.message_callback(
    F.callback.payload.in_([
        "sv:menu",
        "sv:shops",
        "sv:status",
        "sv:details",
        "sv:history",
    ])
)
async def supervisor_callback(event: MessageCallback):
    try:
        user = await event.fetch_from_user()

        if user is None:
            return

        supervisor = get_supervisor_by_id(user.user_id)

        if supervisor is None:
            return

        if event.message is None or event.message.body is None:
            return

        view = event.callback.payload.split(":", 1)[1]

        edited = await event.bot.edit_message(
            message_id=event.message.body.mid,
            text=await get_view_text(user.user_id, view),
            attachments=[create_keyboard(view)],
            notify=False
        )

        logging.info(
            "Панель супервайзера обновлена: view=%s success=%s",
            view,
            edited.success
        )

    except Exception:
        logging.exception(
            "Ошибка панели супервайзера"
        )
