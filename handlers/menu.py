from aiogram import Router, F
from aiogram.types import CallbackQuery

router = Router()
LABELS = {
    'applications': '📝 Анкеты',
    'interviews': '🎙 Собеседования',
    'trial': '⏳ Испытательный срок',
    'voting': '🗳 Голосования',
    'reputation': '⭐ Репутация',
    'settings': '⚙️ Настройки',
}

@router.callback_query(F.data.startswith('menu:'))
async def menu_stub(callback: CallbackQuery) -> None:
    section = (callback.data or '').partition(':')[2]
    title = LABELS.get(section, 'Раздел')
    await callback.answer()
    if callback.message:
        await callback.message.answer(f'{title}\n\nРаздел пока в разработке — наполним его следующим этапом.')
