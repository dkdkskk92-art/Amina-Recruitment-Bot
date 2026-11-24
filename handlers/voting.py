import asyncio, json, logging, time
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer
from config import load_config
from database.db import save_poll, save_vote, get_expired_polls, finalize_poll, get_poll_history

router = Router()
log = logging.getLogger(__name__)
# Short-lived creation steps; persisted polls/votes survive bot restarts.
drafts: dict[int, dict] = {}

def owner(user_id: int) -> bool:
    return user_id == load_config().owner_id

@router.callback_query(F.data == 'poll:create')
async def create_poll_begin(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    drafts[callback.from_user.id] = {'step':'question'}
    await callback.answer()
    if callback.message: await callback.message.answer('✏️ Напиши вопрос для голосования:')

@router.message(F.text)
async def poll_creation_step(message: Message):
    user = message.from_user
    if not user or not owner(user.id) or user.id not in drafts: return
    draft = drafts[user.id]
    text = (message.text or '').strip()
    if draft['step'] == 'question':
        if len(text) > 300: await message.answer('Вопрос слишком длинный (максимум 300 символов).'); return
        draft['question'] = text; draft['step'] = 'options'
        await message.answer('Теперь отправь варианты ответа, каждый с новой строки (от 2 до 12):')
    elif draft['step'] == 'options':
        options = [line.strip() for line in text.splitlines() if line.strip()]
        if not 2 <= len(options) <= 12 or len(set(options)) != len(options):
            await message.answer('Нужно 2–12 уникальных вариантов, каждый на отдельной строке. Попробуй ещё раз.'); return
        if any(len(o)>100 for o in options):
            await message.answer('Каждый вариант должен быть до 100 символов. Попробуй снова.'); return
        draft['options'] = options; draft['step'] = 'chat'
        await message.answer('Укажи ID чата или группы, куда отправить опрос (например, -1001234567890). Бот должен быть добавлен в этот чат.')
    elif draft['step'] == 'chat':
        try: chat_id = int(text)
        except ValueError: await message.answer('ID чата должен быть числом.'); return
        options = draft['options']
        close_at = int(time.time()) + 86400
        try:
            sent = await message.bot.send_poll(chat_id=chat_id, question=draft['question'], options=options,
                is_anonymous=False, allows_multiple_answers=False, allows_revoting=True,
                close_date=close_at, protect_content=False)
            await save_poll(sent.poll.id, sent.chat.id, sent.message_id, draft['question'], options,
                            user.id, int(time.time()), close_at)
            await message.answer('✅ Опрос опубликован!\n\n⏳ Закроется через 24 часа. После закрытия бот пришлёт итоги в чат голосования.')
        except Exception as exc:
            log.exception('Poll creation failed')
            await message.answer(f'Не получилось отправить опрос: {exc}')
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
            for poll_id, chat_id, msg_id, question, options_json in await get_expired_polls(int(time.time())):
                try:
                    await bot.stop_poll(chat_id=chat_id, message_id=msg_id)
                except Exception as exc:
                    log.info('stop_poll possibly already closed: %s', exc)
                result = await finalize_poll(poll_id)
                total = sum(n for _,n in result)
                lines = [f'🗳 <b>Голосование завершено!</b>', f'<b>{question}</b>', '']
                for option, count in result:
                    lines.append(f'{option}: <b>{count}</b>')
                lines.extend(['', f'Всего голосов: <b>{total}</b>', 'Итоги сохранены в истории.'])
                try: await bot.send_message(chat_id, '\n'.join(lines))
                except Exception: log.exception('Could not send poll results')
        except Exception: log.exception('Poll closer loop failed')
        await asyncio.sleep(20)

@router.callback_query(F.data == 'poll:history')
async def show_poll_history(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    await callback.answer()
    if not callback.message: return
    records = await get_poll_history()
    if not records:
        await callback.message.answer('📚 История голосований пока пуста.')
        return
    chunks = ['📚 <b>Последние голосования</b>']
    for question, options, counts, created, status in records:
        if counts is None:
            lines = [f'• <b>{question}</b> — в процессе']
        else:
            total = sum(counts)
            lines = [f'• <b>{question}</b>', *(f'{opt}: {cnt}' for opt,cnt in zip(options,counts)), f'Всего: {total}']
        chunks.append('\\n'.join(lines))
    await callback.message.answer('\\n\\n'.join(chunks))
