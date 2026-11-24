from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import load_config
from keyboards.main_menu import main_menu

router = Router()
LABELS = {'applications':'📝 Анкеты','interviews':'🎙 Собеседования','trial':'⏳ Испытательный срок','voting':'🗳 Голосования','reputation':'⭐ Репутация','settings':'⚙️ Настройки'}

@router.callback_query(F.data == 'menu:voting')
async def voting_menu(callback: CallbackQuery):
    await callback.answer()
    if not callback.message: return
    cfg = load_config()
    if callback.from_user.id != cfg.owner_id:
        await callback.message.answer('🗳 Голосования\nСоздание опросов доступно владельцу бота.')
        return
    kb = InlineKeyboardBuilder()
    kb.button(text='➕ Создать голосование', callback_data='poll:create')
    kb.button(text='📚 История', callback_data='poll:history')
    kb.button(text='↩️ Главное меню', callback_data='poll:menu')
    kb.adjust(1)
    await callback.message.answer('🗳 <b>Голосования</b>\n\nСоздай нативный Telegram-опрос. Он закроется автоматически через 24 часа, после чего бот опубликует подсчёт.', reply_markup=kb.as_markup())

@router.callback_query(F.data == 'poll:menu')
async def poll_back(callback: CallbackQuery):
    await callback.answer()
    if callback.message: await callback.message.answer('Главное меню:', reply_markup=main_menu())

@router.callback_query(F.data.startswith('menu:'))
async def menu_stub(callback: CallbackQuery):
    section = (callback.data or '').partition(':')[2]
    title = LABELS.get(section, 'Раздел')
    await callback.answer()
    if callback.message: await callback.message.answer(f'{title}\n\nРаздел пока в разработке — наполним его следующим этапом.')
