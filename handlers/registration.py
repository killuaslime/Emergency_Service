
from maxapi import Router, F
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.types import MessageCreated

from services.shops import get_shop_by_code
from services.supervisors import get_supervisor_id

from database.database import register_or_create_request
from handlers.panel import show_panel


router = Router()


class Registration(StatesGroup):
    waiting_for_shop_code = State()


@router.message_created(
    F.message.body.text,
    Registration.waiting_for_shop_code
)
async def process_shop_code(
    event: MessageCreated,
    context: MemoryContext
):
    user = await event.fetch_from_user()

    if user is None:
        await event.message.answer(
            "Не удалось определить ваш аккаунт. Попробуйте ещё раз."
        )
        return

    max_user_id = user.user_id
    shop_code = event.message.body.text.strip()

    if not shop_code.isdigit():
        await event.message.answer(
            "Пожалуйста, введите номер магазина цифрами."
        )
        return

    shop = get_shop_by_code(shop_code)

    if shop is None:
        await event.message.answer(
            "Магазин с таким номером не найден.\n"
            "Проверьте номер и попробуйте ещё раз."
        )
        return

    supervisor_id = get_supervisor_id(shop["supervisor"])

    result = await register_or_create_request(
        max_user_id=max_user_id,
        shop_code=shop["code"],
        address=shop["address"],
        supervisor_id=supervisor_id
    )

    if result == "registered":
        await context.clear()
        await show_panel(event)

    elif result == "request_created":
        await context.clear()
        await event.message.answer(
            "🕐 Ваш магазин уже зарегистрирован.\n\n"
            "Заявка на повторную регистрацию отправлена "
            "разработчику и супервайзеру.\n"
            "Ожидайте подтверждения."
        )

    elif result == "request_exists":
        await context.clear()
        await event.message.answer(
            "⏳ По этому магазину уже существует заявка на регистрацию.\n"
            "Ожидайте её рассмотрения."
        )

    elif result == "already_registered":
        await context.clear()
        await show_panel(event)
