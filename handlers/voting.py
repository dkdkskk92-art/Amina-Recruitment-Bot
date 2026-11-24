import asyncio
import logging
import time
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer
from config import load_config
from database.db import save_poll, save_vote, get_expired_polls, finalize_poll, get_poll_history

router = Router()
log = logging.getLogger(__name__)
# Drafts are temporary; published polls and votes are stored in SQLite.
drafts: dict[int, dict] = {}

def owner(user_id: int) -> bool:
    return user_id == load_config().owner_id

@router.callback_query(F.data == 'poll:create')
async def create_poll_begin(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    drafts[callback.from_user.id] = {'step': 'question'}
    await callback.answer()
    if callback.message:
        await callback.message.answer('✏️ Напиши вопрос для голосования:')

@router.message(F.text)
async def poll_creation_step(message: Message):
    user = message.from_user
    if not user or not owner(user.id) or user.id not in drafts:
        return
    draft = drafts[user.id]
    text = (message.text or '').strip()
    if draft['step'] == 'question':
        if not text or len(text) > 300:
            await message.answer('Вопрос должен быть не пустым и не длиннее 300 символов.')
            return
        draft['question'] = text
        draft['step'] = 'options'
        await message.answer('Отправь варианты ответа, каждый с новой строки (от 2 до 12):')
    elif draft['step'] == 'options':
        options = [line.strip() for line in text.splitlines() if line.strip()]
        if not 2 <= len(options) <= 12 or len(set(options)) != len(options):
            await message.answer('Нужно 2–12 уникальных вариантов, каждый на отдельной строке. Попробуй ещё раз.')
            return
        if any(len(option) > 100 for option in options):
            await message.answer('Каждый вариант должен быть не длиннее 100 символов.')
            return
        draft['options'] = options
        draft['step'] = 'chat'
        await message.answer('Укажи ID группы/чата, куда отправить опрос (например, -1001234567890). Бот должен быть добавлен туда.')
    elif draft['step'] == 'chat':
        try:
            chat_id = int(text)
        except ValueError:
            await message.answer('ID чата должен быть числом.')
            return
        options = draft['options']
        created_at = int(time.time())
        close_at = created_at + 86400
        try:
            sent = await message.bot.send_poll(
                chat_id=chat_id,
                question=draft['question'],
                options=options,
                is_anonymous=False,
                allows_multiple_answers=False,
                allows_revoting=True,
                close_date=close_at,
            )
            await save_poll(sent.poll.id, sent.chat.id, sent.message_id, draft['question'],
                            options, user.id, created_at, close_at)
            await message.answer('✅ Опрос опубликован! Он закроется через 24 часа, и бот отправит итоговый подсчёт в чат голосования.')
        except Exception:
            log.exception('Poll creation failed')
            await message.answer('Не удалось отправить опрос. Проверь ID чата и права бота.')
        finally:
            drafts.pop(user.id, None)

@router.poll_answer()
async def on_poll_answer(answer: PollAnswer):
    if answer.user:
        selected = answer.option_ids[0] if len(answer.option_ids) == 1 else None
        await save_vote(answer.poll_id, answer.user.id, selected)

async def poll_closer(bot):
    while True:
        try:
            expired = await get_expired_polls(int(time.time()))
            for poll_id, chat_id, message_id, question, _options_json in expired:
                try:
                    await bot.stop_poll(chat_id=chat_id, message_id=message_id)
                except Exception as exc:
                    log.info('Poll may already be closed: %s', exc)
                results = await finalize_poll(poll_id)
                total = sum(count for _, count in results)
                lines = ['🗳 <b>Голосование завершено!</b>', f'<b>{question}</b>', '']
                lines.extend([f'{option}: <b>{count}</b>' for option, count in results])
                lines.extend(['', f'Всего голосов: <b>{total}</b>', 'Итоги сохранены в истории.'])
                try:
                    await bot.send_message(chat_id, '\\n'.join(lines))
                except Exception:
                    log.exception('Could not send poll results')
        except Exception:
            log.exception('Poll closer loop error')
        await asyncio.sleep(20)

@router.callback_query(F.data == 'poll:history')
async def show_poll_history(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    await callback.answer()
    if not callback.message:
        return
    records = await get_poll_history()
    if not records:
        await callback.message.answer('📚 История голосований пока пуста.')
        return
    parts = ['📚 <b>Последние голосования</b>']
    for question, options, counts, _created, status in records:
        if counts is None:
            parts.append(f'• <b>{question}</b> — ещё идёт')
        else:
            lines = [f'• <b>{question}</b>']
            lines.extend([f'{option}: {count}' for option, count in zip(options, counts)])
            lines.append(f'Всего: {sum(counts)}')
            parts.append('\\n'.join(lines))
    await callback.message.answer('\\n\\n'.join(parts))
