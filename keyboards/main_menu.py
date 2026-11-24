from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu() -> InlineKeyboardMarkup:
    items = [
        ('📝 Анкеты', 'menu:applications'),
        ('🎙 Собеседования', 'menu:interviews'),
        ('⏳ Испытательный срок', 'menu:trial'),
        ('🗳 Голосования', 'menu:voting'),
        ('⭐ Репутация', 'menu:reputation'),
        ('⚙️ Настройки', 'menu:settings'),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=callback)] for label, callback in items
    ])
