
from maxapi import Router
from maxapi.context import MemoryContext
from maxapi.types import MessageCreated, Command

from database.database import get_user
from handlers.registration import Registration
from handlers.panel import show_panel
from handlers.supervisor import show_supervisor_panel
from services.supervisors import get_supervisor_by_id

router = Router()


@router.message_created(Command("start"))
async def start_handler(
    event: MessageCreated,
    context: MemoryContext
):
    user = await event.fetch_from_user()

    if user is None:
        return

    # Проверяем, является ли пользователь супервайзером
    supervisor = get_supervisor_by_id(user.user_id)

    if supervisor is not None:
        await context.clear()
        await show_supervisor_panel(event, supervisor)
        return

    db_user = await get_user(user.user_id)

    if db_user is not None:
        await context.clear()
        await show_panel(event)
        return

    await context.set_state(Registration.waiting_for_shop_code)

    await event.message.answer(
        "Здравствуйте!\n\n"
        "Для регистрации введите номер своего магазина:"
    )
