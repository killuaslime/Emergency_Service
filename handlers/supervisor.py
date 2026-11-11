
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from maxapi import Router, F
from maxapi.types import MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database.database import (
    get_supervisor_shops,
    get_supervisor_history,
    get_supervisor_daily_check_results,
    save_supervisor_daily_report,
    get_supervisor_daily_report,
    clear_supervisor_daily_report,
)
from services.supervisors import get_supervisor_by_id

router = Router()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

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
async def get_daily_stats_text(supervisor_id: int, check_date: str):
    results = await get_supervisor_daily_check_results(
        supervisor_id,
        check_date
    )

    if not results:
        return (
            "🌅 УТРЕННЯЯ СТАТИСТИКА\n\n"
            "Опросы на сегодня ещё не созданы."
        )

    online = sum(1 for shop in results if shop["status"] == "on")
    offline = sum(1 for shop in results if shop["status"] == "off")
    unanswered = sum(1 for shop in results if shop["status"] is None)

    lines = [
        "🌅 УТРЕННЯЯ СТАТИСТИКА",
        "━━━━━━━━━━━━━━━━━━",
        f"🟢 Открытые магазины со светом: {online}",
        f"🔴 Открытые магазины без света: {offline}",
        f"⚪ Не ответили на опрос: {unanswered}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for shop in results:
        if shop["status"] == "off":
            lines.append(
                f"❌ №{shop['code']}\n"
                f"{shop['address']}\n"
                f"Открылся без ЭЭ в 08:15"
            )

        elif shop["status"] is None:
            lines.append(
                f"⚪ №{shop['code']}\n"
                f"{shop['address']}\n"
                f"Ожидается ответ"
            )

    return "\n\n".join(lines)

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
            CallbackButton(
                text="🌅 Утренняя статистика",
                payload="sv:daily_stats"
            )
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

    if view == "daily_stats":
        check_date = datetime.now(MOSCOW_TZ).date().isoformat()
        return await get_daily_stats_text(
            supervisor_id,
            check_date
        )


    if view == "shops":
        if not shops:
            return "🏪 У вас пока нет закреплённых магазинов."

        total = len(shops)

        registered_shops = [
            shop for shop in shops
            if shop["is_registered"]
        ]

        offline = sum(
            1 for shop in registered_shops
            if shop["started_at"] is not None
        )

        online = len(registered_shops) - offline

        pending = sum(
            1 for shop in shops
            if not shop["is_registered"]
            and shop["has_pending_request"]
        )

        not_registered = total - len(registered_shops) - pending

        lines = ["🏪 МОИ МАГАЗИНЫ\n"]

        for shop in shops:
            if shop["is_registered"]:
                if shop["started_at"] is not None:
                    started_time = datetime.fromisoformat(
                        shop["started_at"]
                    ).strftime("%H:%M")

                    lines.append(
                        f"🔴 {shop['code']}\n"
                        f"{shop['address']}\n"
                        f"ЭЭ отсутствует с {started_time}"
                    )
                else:
                    lines.append(
                        f"🟢 {shop['code']}\n"
                        f"{shop['address']}"
                    )

            elif shop["has_pending_request"]:
                lines.append(
                    f"🟡 {shop['code']}\n"
                    f"{shop['address']}\n"
                    f"Регистрация: 🟡 Ожидает подтверждения"
                )

            else:
                lines.append(
                    f"⚪ {shop['code']}\n"
                    f"{shop['address']}\n"
                    f"Регистрация: 🔴 Не зарегистрирован"
                )

        lines.append(
            "------------------------------------\n"
            "📊 ТЕКУЩАЯ СИТУАЦИЯ\n\n"
            f"🟢 Со светом: {online}\n"
            f"🔴 Без света: {offline}"
        )

        return "\n\n".join(lines)



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
        "sv:daily_stats",
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

        check_date = datetime.now(MOSCOW_TZ).date().isoformat()

        edited = await event.bot.edit_message(
            message_id=event.message.body.mid,
            text=await get_view_text(user.user_id, view),
            attachments=[create_keyboard(view)],
            notify=False
        )

        if edited.success:
            if view == "daily_stats":
                await save_supervisor_daily_report(
                    supervisor_id=user.user_id,
                    check_date=check_date,
                    message_id=event.message.body.mid
                )
            else:
                await clear_supervisor_daily_report(
                    supervisor_id=user.user_id,
                    check_date=check_date
                )

        logging.info(
            "Панель супервайзера обновлена: view=%s success=%s",
            view,
            edited.success
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
async def refresh_supervisor_daily_report(
    bot,
    supervisor_id: int,
    check_date: str
):
    message_id = await get_supervisor_daily_report(
        supervisor_id,
        check_date
    )

    if message_id is None:
        return

    try:
        edited = await bot.edit_message(
            message_id=message_id,
            text=await get_daily_stats_text(
                supervisor_id,
                check_date
            ),
            attachments=[create_keyboard("daily_stats")],
            notify=False
        )

        logging.info(
            "Утренняя статистика супервайзера %s обновлена: %s",
            supervisor_id,
            edited.success
        )

    except Exception:
        logging.exception(
            "Не удалось обновить статистику супервайзера %s",
            supervisor_id
        )