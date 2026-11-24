from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from database.db import upsert_user
from keyboards.main_menu import main_menu

router = Router()

@router.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    if user:
        await upsert_user(user.id, user.username, user.full_name)
    await message.answer(
        '🖤 <b>Амина</b>\n\nПомощник для учёта анкет, собеседований, испытательного срока, голосований и репутации.\n\nВыбери раздел:',
        reply_markup=main_menu(),
    )
