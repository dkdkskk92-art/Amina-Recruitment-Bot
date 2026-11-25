import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MessageEntity, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, InputRichMessage
from aiogram.utils.text_decorations import html_decoration
from sqlalchemy import select, func, text

from config import settings
from database import Base, engine, Session, User, Chat, Warning, Mute, Ban, PunishmentLog, Note, ReportRoute, Report

RANKS = {0:"Участник", 1:"Модератор I", 2:"Модератор II", 3:"Старший модератор", 4:"Главный модератор", 5:"Владелец"}
DURATION_RE = re.compile(r"(?i)(навсегда|\d+\s*(?:секунд(?:у|ы)?|сек|минут(?:у|ы)?|мин|час(?:а|ов)?|дн(?:я|ей)?|день))")
BLOCKED_LINK = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:t\.me/(?:\+|joinchat/|[A-Za-z0-9_]{5,})|telegram\.me/(?:\+|joinchat/|[A-Za-z0-9_]{5,})|chat\.whatsapp\.com/[A-Za-z0-9_-]+|wa\.me/[A-Za-z0-9]+)"
)

def parse_expiry(value: str):
    if value.lower() == "навсегда":
        return None
    m = DURATION_RE.search(value)
    if not m:
        raise ValueError("Укажи срок: например «10 минут», «2 часа», «1 день» или «навсегда».")
    amount_match = re.match(r"\d+", m.group())
    amount = int(amount_match.group())
    unit = m.group().lower()[len(amount_match.group()):].strip()
    if amount < 1:
        raise ValueError("Срок должен быть больше нуля.")
    if unit.startswith("сек"):
        delta = timedelta(seconds=amount)
    elif unit.startswith("мин"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("час"):
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(days=amount)
    return datetime.now(timezone.utc) + delta

def parse_target_parts(raw: str):
    m = DURATION_RE.search(raw)
    duration = m.group() if m else None
    rest = (raw[:m.start()] + raw[m.end():]).strip() if m else raw.strip()
    return duration, rest

class RegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if actor and not actor.is_bot:
            async with Session() as session:
                user = await session.scalar(select(User).where(User.telegram_id == actor.id))
                if user is None:
                    user = User(telegram_id=actor.id, username=actor.username,
                                first_name=actor.first_name or "", last_name=actor.last_name,
                                rank=5 if settings.owner_telegram_id and actor.id == settings.owner_telegram_id else 0)
                    session.add(user)
                else:
                    user.username, user.first_name, user.last_name = actor.username, actor.first_name or "", actor.last_name
                    if settings.owner_telegram_id and actor.id == settings.owner_telegram_id:
                        user.rank = 5
                if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                    record = await session.scalar(select(Chat).where(Chat.telegram_chat_id == chat.id))
                    if record is None:
                        session.add(Chat(telegram_chat_id=chat.id, title=chat.title or ""))
                    else:
                        record.title = chat.title or ""
                await session.commit()
        return await handler(event, data)

async def get_user(session, telegram_id):
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))

async def resolve_target(message: Message, session, raw: str):
    if message.reply_to_message and message.reply_to_message.from_user:
        return await get_user(session, message.reply_to_message.from_user.id)
    token = raw.strip().split()[-1] if raw.strip() else ""
    if token.startswith("@"):
        return await session.scalar(select(User).where(func.lower(User.username) == token[1:].lower()))
    if token.isdigit():
        return await session.get(User, int(token))
    return None

def unban_markup(custom_id: int, chat_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Разбанить", callback_data=f"unban:{custom_id}:{chat_id}")
    ]])

async def apply_mute(bot, chat_id, telegram_id, until):
    await bot.restrict_chat_member(
        chat_id, telegram_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=int(until.timestamp()) if until else None
    )

async def clear_mute(bot, chat_id, telegram_id):
    await bot.restrict_chat_member(chat_id, telegram_id, permissions=ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True
    ))

async def rank_for(session, telegram_id):
    user = await get_user(session, telegram_id)
    return user.rank if user else 0

async def profile(message: Message):
    parts = (message.text or "").split()
    async with Session() as session:
        # Приоритет: ответ на сообщение пользователя, затем кастомный ID, иначе свой профиль.
        if message.reply_to_message and message.reply_to_message.from_user:
            user = await get_user(session, message.reply_to_message.from_user.id)
        elif len(parts) > 1 and parts[1].isdigit():
            user = await session.get(User, int(parts[1]))
        else:
            user = await get_user(session, message.from_user.id)
        if not user:
            await message.reply("Пользователь не найден.")
            return
        warns = await session.scalar(select(func.count(Warning.id)).where(Warning.user_id == user.id, Warning.active.is_(True))) or 0
        mutes = await session.scalar(select(func.count(Mute.id)).where(Mute.user_id == user.id, Mute.active.is_(True))) or 0
        bans = await session.scalar(select(func.count(Ban.id)).where(Ban.user_id == user.id, Ban.active.is_(True))) or 0
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "Без имени"
        username = f"\nUsername: @{user.username}" if user.username else ""
        registered = user.registered_at.strftime("%d.%m.%Y") if user.registered_at else "неизвестно"
        avatar_file_id, avatar_type = user.avatar_file_id, user.avatar_type
        caption = (
            f"👤 <b>Профиль</b>\nИмя: {name}{username}\nКастомный ID: <code>{user.id}</code>"
            f"\nРанг: {RANKS.get(user.rank, 'Участник')} ({user.rank})"
            f"\nАктивные варны: {warns}\nАктивные муты: {mutes}\nАктивные баны: {bans}"
            f"\nРегистрация: {registered}"
        )
    if avatar_file_id and avatar_type == "photo":
        await message.answer_photo(avatar_file_id, caption=caption, parse_mode="HTML")
    elif avatar_file_id and avatar_type == "video":
        await message.answer_video(avatar_file_id, caption=caption, parse_mode="HTML")
    else:
        await message.answer(caption, parse_mode="HTML")

def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2

async def save_note(message: Message):
    if not message.text or "\n" not in message.text:
        await message.reply("Формат: +заметка Название (до 20 символов), затем текст заметки с новой строки.")
        return
    header, body = message.text.split("\n", 1)
    title = header[len("+заметка"):].strip()
    if not title or len(title) > 20:
        await message.reply("Название должно содержать от 1 до 20 символов.")
        return
    if not body.strip():
        await message.reply("Добавь текст заметки на следующей строке.")
        return
    body_offset = utf16_length(header + "\n")
    total_length = utf16_length(message.text)
    entities = []
    for entity in message.entities or []:
        if entity.offset >= body_offset and entity.offset + entity.length <= total_length:
            entities.append(entity.model_dump(mode="json", exclude_none=True) | {
                "offset": entity.offset - body_offset
            })
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        if not actor or actor.rank < 4:
            await message.reply("⛔ Заметки доступны только с 4 ранга.")
            return
        note = Note(title=title, body=body,
                    entities_json=json.dumps(entities, ensure_ascii=False) if entities else None,
                    created_by=actor.id)
        session.add(note)
        await session.commit()
        await session.refresh(note)
        note_id = note.id
    await message.reply(f"📝 Заметка «{title}» сохранена. Номер: <code>{note_id}</code>", parse_mode="HTML")

async def delete_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.reply("Укажи номер: -заметка 12")
        return
    note_id = int(parts[1].strip())
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        if not actor or actor.rank < 4:
            await message.reply("⛔ Заметки доступны только с 4 ранга.")
            return
        note = await session.get(Note, note_id)
        if not note:
            await message.reply("Заметка с таким номером не найдена.")
            return
        title = note.title
        await session.delete(note)
        await session.commit()
    await message.reply(f"🗑 Заметка №{note_id} «{title}» удалена.")

async def show_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        return
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        if not actor or actor.rank < 4:
            await message.reply("⛔ Заметки доступны только с 4 ранга.")
            return
        note = await session.get(Note, int(parts[1].strip()))
        if not note:
            await message.reply("Заметка с таким номером не найдена.")
            return
        note_id, title, body = note.id, note.title, note.body
        raw_entities = json.loads(note.entities_json) if note.entities_json else []
    entities = [MessageEntity.model_validate(item) for item in raw_entities]
    await message.answer(f"📝 <b>Заметка №{note_id}: {title}</b>", parse_mode="HTML")
    await message.answer(body, entities=entities or None)

async def list_notes(message: Message):
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        if not actor or actor.rank < 4:
            await message.reply("⛔ Заметки доступны только с 4 ранга.")
            return
        notes = (await session.scalars(select(Note).order_by(Note.id.asc()))).all()
        note_data = [
            (note.id, note.title, note.body,
             [MessageEntity.model_validate(item) for item in json.loads(note.entities_json)]
             if note.entities_json else [])
            for note in notes
        ]

    if not note_data:
        await message.reply("🗒 Список заметок пока пуст.")
        return

    # Собираем несколько раскрывающихся заметок в одно компактное rich-сообщение.
    chunks = []
    current = []
    current_size = 0
    for note_id, title, body, entities in note_data:
        formatted_body = html_decoration.unparse(body, entities).replace("\n", "<br>")
        block = (
            f"<details><summary>📝 №{note_id} — {html_decoration.quote(title)}</summary>"
            f"<p>{formatted_body}</p></details>"
        )
        block_size = len(block)
        if current and current_size + block_size > 3500:
            chunks.append("".join(current))
            current, current_size = [], 0
        current.append(block)
        current_size += block_size
    if current:
        chunks.append("".join(current))

    total = len(note_data)
    for index, chunk in enumerate(chunks, start=1):
        heading = f"<h3>🗒 Заметки · {total}</h3>" if index == 1 else f"<p>🗒 Продолжение · {index}/{len(chunks)}</p>"
        await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(html=heading + chunk)
        )


async def moderation_action(message: Message, action: str, required_rank: int):
    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    duration_text, target_text = parse_target_parts(raw)
    if action in ("варн", "молчи", "бан") and not duration_text:
        await message.reply("Укажи срок: «10 минут», «2 часа», «1 день» или «навсегда».")
        return
    try:
        expiry = parse_expiry(duration_text) if duration_text else None
    except ValueError as exc:
        await message.reply(str(exc))
        return
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        target = await resolve_target(message, session, target_text)
        if not actor or actor.rank < required_rank:
            await message.reply("⛔ Недостаточно прав.")
            return
        if not target:
            await message.reply("Пользователь не найден. Ответь на его сообщение или укажи кастомный ID/@username.")
            return
        if target.id == actor.id or actor.rank <= target.rank:
            await message.reply("⛔ Нельзя применять действие к себе или к равному/старшему рангу.")
            return
        if action == "варн":
            session.add(Warning(user_id=target.id, chat_id=message.chat.id, moderator_id=actor.id, expires_at=expiry))
            await session.flush()
            count = await session.scalar(select(func.count(Warning.id)).where(Warning.user_id == target.id, Warning.active.is_(True))) or 0
            session.add(PunishmentLog(user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id, action="варн"))
            auto_mute = count == 3
            if auto_mute:
                auto_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
                session.add(Mute(user_id=target.id, chat_id=message.chat.id, moderator_id=None, expires_at=auto_expiry))
                session.add(PunishmentLog(user_id=target.id, moderator_id=None, chat_id=message.chat.id, action="автомут", details="3 активных варна"))
            await session.commit()
            if auto_mute:
                try:
                    await apply_mute(message.bot, message.chat.id, target.telegram_id, auto_expiry)
                except Exception:
                    await message.reply("Варн записан, но Telegram не применил автоматический мут. Проверь права бота.")
                    return
            await message.reply(f"⚠️ Варн выдан. Активных варнов: {count}." + (" Автоматический мут на 1 час." if auto_mute else ""))
        elif action == "молчи":
            session.add(Mute(user_id=target.id, chat_id=message.chat.id, moderator_id=actor.id, expires_at=expiry))
            session.add(PunishmentLog(user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id, action="мут"))
            await session.commit()
            try:
                await apply_mute(message.bot, message.chat.id, target.telegram_id, expiry)
                await message.reply("🔇 Мут выдан.")
            except Exception:
                await message.reply("Запись создана, но Telegram не применил мут. Проверь права бота.")
        elif action == "бан":
            session.add(Ban(user_id=target.id, chat_id=message.chat.id, moderator_id=actor.id, expires_at=expiry))
            session.add(PunishmentLog(user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id, action="бан"))
            await session.commit()
            try:
                await message.bot.ban_chat_member(message.chat.id, target.telegram_id, until_date=int(expiry.timestamp()) if expiry else None)
                await message.reply(f"🚫 Пользователь <code>{target.id}</code> заблокирован.",
                                    parse_mode="HTML", reply_markup=unban_markup(target.id, message.chat.id))
            except Exception:
                await message.reply("Запись создана, но Telegram не применил бан. Проверь права бота.")

async def change_rank(message: Message, step: int):
    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        target = await resolve_target(message, session, raw)
        if not actor or actor.rank < 4:
            await message.reply("⛔ Изменять ранги можно только с 4 ранга.")
            return
        if not target or actor.rank <= target.rank:
            await message.reply("Пользователь не найден или его ранг равен/выше вашего.")
            return
        new_rank = target.rank + step
        max_rank = 3 if actor.rank == 4 else 4
        if new_rank < 0 or new_rank > max_rank:
            await message.reply("⛔ Нельзя назначить этот ранг: вы можете менять только ранги ниже своего.")
            return
        old = target.rank
        target.rank = new_rank
        session.add(PunishmentLog(user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id,
                                  action="изменение ранга", details=f"{old} -> {new_rank}"))
        await session.commit()
        await message.reply(f"Ранг пользователя <code>{target.id}</code>: {old} → {new_rank}.", parse_mode="HTML")

async def unpunish(message: Message, action: str, required: int):
    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        target = await resolve_target(message, session, raw)
        if not actor or actor.rank < required:
            await message.reply("⛔ Недостаточно прав.")
            return
        if not target or actor.rank <= target.rank:
            await message.reply("Пользователь не найден или его ранг равен/выше вашего.")
            return
        model = {"говори": Mute, "разбан": Ban, "снять варн": Warning}[action]
        item = await session.scalar(select(model).where(model.user_id == target.id,
            model.chat_id == message.chat.id, model.active.is_(True)).order_by(model.created_at.desc()))
        if not item:
            await message.reply("Активное наказание в этом чате не найдено.")
            return
        item.active = False
        session.add(PunishmentLog(user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id, action=f"снятие: {action}"))
        await session.commit()
        try:
            if action == "говори":
                await clear_mute(message.bot, message.chat.id, target.telegram_id)
            elif action == "разбан":
                await message.bot.unban_chat_member(message.chat.id, target.telegram_id, only_if_banned=True)
            await message.reply("Готово. Наказание снято.")
        except Exception:
            await message.reply("Статус обновлён в базе, но Telegram не выполнил действие. Проверь права бота.")

async def expiry_worker(bot: Bot):
    while True:
        now = datetime.now(timezone.utc)
        async with Session() as session:
            warnings = (await session.scalars(select(Warning).where(Warning.active.is_(True), Warning.expires_at.is_not(None), Warning.expires_at <= now))).all()
            mutes = (await session.scalars(select(Mute).where(Mute.active.is_(True), Mute.expires_at.is_not(None), Mute.expires_at <= now))).all()
            bans = (await session.scalars(select(Ban).where(Ban.active.is_(True), Ban.expires_at.is_not(None), Ban.expires_at <= now))).all()
            mute_targets = [(x.user_id, x.chat_id) for x in mutes]
            ban_targets = [(x.user_id, x.chat_id) for x in bans]
            for row in warnings + mutes + bans:
                row.active = False
            await session.commit()
        for user_id, chat_id in mute_targets:
            async with Session() as session:
                user = await session.get(User, user_id)
            if user:
                try:
                    await clear_mute(bot, chat_id, user.telegram_id)
                except Exception:
                    logging.exception("Не удалось снять истёкший мут")
        for user_id, chat_id in ban_targets:
            async with Session() as session:
                user = await session.get(User, user_id)
            if user:
                try:
                    await bot.unban_chat_member(chat_id, user.telegram_id, only_if_banned=True)
                except Exception:
                    logging.exception("Не удалось снять истёкший бан")
        await asyncio.sleep(20)



async def apply_report_punishment(query: CallbackQuery, report_id: int, action: str, duration_code: str):
    duration_map = {
        "m10": timedelta(minutes=10), "h1": timedelta(hours=1),
        "d1": timedelta(days=1), "d7": timedelta(days=7)
    }
    if action not in {"mute", "ban", "warn"} or (duration_code != "forever" and duration_code not in duration_map):
        await query.answer("Некорректный срок.", show_alert=True)
        return
    required = {"mute": 2, "ban": 3, "warn": 1}[action]
    async with Session() as session:
        actor = await get_user(session, query.from_user.id)
        report = await session.get(Report, report_id)
        if not report or report.status != "pending":
            await query.answer("Этот репорт уже обрабатывается или обработан.", show_alert=True)
            return
        target = await session.get(User, report.target_user_id) if report.target_user_id else None
        if not actor or actor.rank < required or not target or actor.id == target.id or actor.rank <= target.rank:
            await query.answer("Недостаточно прав или цель имеет равный/старший ранг.", show_alert=True)
            return
        report.status = "processing"
        report.handled_by = actor.id
        await session.commit()
        source_chat_id = report.source_chat_id
        target_tg_id, target_custom_id = target.telegram_id, target.id
    expiry = None if duration_code == "forever" else datetime.now(timezone.utc) + duration_map[duration_code]
    try:
        if action == "mute":
            await query.bot.restrict_chat_member(source_chat_id, target_tg_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(expiry.timestamp()) if expiry else None)
        elif action == "ban":
            await query.bot.ban_chat_member(source_chat_id, target_tg_id,
                until_date=int(expiry.timestamp()) if expiry else None)
        else:
            async with Session() as session:
                session.add(Warning(user_id=target_custom_id, chat_id=source_chat_id,
                                    moderator_id=actor.id, expires_at=expiry))
                session.add(PunishmentLog(user_id=target_custom_id, moderator_id=actor.id,
                    chat_id=source_chat_id, action="варн по репорту", details="7 дней"))
                await session.commit()
        if action in {"mute", "ban"}:
            async with Session() as session:
                if action == "mute":
                    session.add(Mute(user_id=target_custom_id, chat_id=source_chat_id,
                                     moderator_id=actor.id, expires_at=expiry))
                else:
                    session.add(Ban(user_id=target_custom_id, chat_id=source_chat_id,
                                    moderator_id=actor.id, expires_at=expiry))
                session.add(PunishmentLog(user_id=target_custom_id, moderator_id=actor.id,
                    chat_id=source_chat_id, action=action + " по репорту",
                    details=duration_code))
                await session.commit()
        async with Session() as session:
            report = await session.get(Report, report_id)
            report.status, report.handled_at = "handled", datetime.now(timezone.utc)
            await session.commit()
        await render_report(query.bot, report_id)
        await query.answer("Наказание применено в исходной группе.")
    except Exception:
        logging.exception("Ошибка применения наказания по репорту %s", report_id)
        async with Session() as session:
            report = await session.get(Report, report_id)
            if report and report.status == "processing":
                report.status, report.handled_by = "pending", None
                await session.commit()
        await query.answer("Не удалось применить наказание. Проверь права бота в исходной группе.", show_alert=True)


def report_action_markup(report_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔇 Мут", callback_data=f"ract:{report_id}:mute"),
         InlineKeyboardButton(text="🚫 Бан", callback_data=f"ract:{report_id}:ban")],
        [InlineKeyboardButton(text="⚠️ Варн 7 дней", callback_data=f"ract:{report_id}:warn"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"rreject:{report_id}")]
    ])


def report_duration_markup(report_id: int, action: str):
    choices = [("10 минут", "m10"), ("1 час", "h1"), ("1 день", "d1"), ("7 дней", "d7")]
    if action == "ban":
        choices.append(("Навсегда", "forever"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"rdo:{report_id}:{action}:{code}")]
        for label, code in choices
    ])


async def render_report(bot: Bot, report_id: int):
    async with Session() as session:
        report = await session.get(Report, report_id)
        if not report:
            return
        author = await session.get(User, report.author_id)
        target = await session.get(User, report.target_user_id) if report.target_user_id else None
        source = await session.scalar(select(Chat).where(Chat.telegram_chat_id == report.source_chat_id))
        values = (report.destination_chat_id, report.destination_message_id, report.status,
                  author.first_name if author else "Неизвестно", author.id if author else 0,
                  target.first_name if target else "не указан", target.id if target else None,
                  report.text, source.title if source else str(report.source_chat_id))
    dest_id, msg_id, status, author_name, author_custom_id, target_name, target_custom_id, body, source_title = values
    status_text = {"pending": "🕓 Ожидает рассмотрения", "processing": "⏳ Выполняется", "handled": "✅ Обработана", "rejected": "❌ Отклонена"}.get(status, status)
    safe = html_decoration.quote
    target_line = f"\n<b>На кого жалоба:</b> {safe(target_name)} · ID <code>{target_custom_id}</code>" if target_custom_id else "\n<b>На кого жалоба:</b> не указан (репорт отправлен без ответа на сообщение)"
    text_body = (f"🚨 <b>Новая жалоба №{report_id}</b>\n"
                 f"<b>Чат:</b> {safe(source_title)}\n"
                 f"<b>Автор:</b> {safe(author_name)} · кастомный ID <code>{author_custom_id}</code>"
                 f"{target_line}\n<b>Текст:</b>\n{safe(body)}\n\n<b>Статус:</b> {status_text}")
    markup = report_action_markup(report_id) if status == "pending" and target_custom_id else None
    try:
        await bot.edit_message_text(text_body, chat_id=dest_id, message_id=msg_id,
                                    parse_mode="HTML", reply_markup=markup)
    except Exception:
        logging.exception("Не удалось обновить карточку репорта %s", report_id)

async def main():
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_file_id VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_type VARCHAR(10)"))
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.message.outer_middleware(RegistrationMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer("Привет! Я бот модерации. Напиши «профиль», чтобы посмотреть свой профиль.")

    @dp.message(Command("help"))
    async def help_handler(message: Message):
        help_html = (
            "<h2>🤖 Айзен | помощник</h2>"
            "<p>Управление чатом, профилями и заметками. Нажми на раздел, чтобы раскрыть подробности.</p>"
            "<details><summary>👤 Профили</summary>"
            "<p><code>профиль</code> — свой профиль.<br>"
            "<code>профиль 123</code> — профиль по кастомному ID.<br>"
            "Чтобы установить аватар, отправь фото или видео с подписью <code>аватар</code>.</p></details>"
            "<details><summary>🛡 Модерация</summary>"
            "<p><code>варн 1 день</code> — предупреждение.<br>"
            "<code>молчи 10 минут</code> — мут.<br>"
            "<code>бан навсегда</code> — бан.<br>"
            "<code>говори</code> — снять мут.<br>"
            "<code>разбан</code> — снять бан.<br>"
            "<code>снять варн</code> — снять последнее активное предупреждение.<br>"
            "<code>-смс</code> — удалить сообщение, на которое ответил.</p></details>"
            "<details><summary>🎖 Ранги</summary>"
            "<p><code>повысить</code> / <code>понизить</code> — изменить ранг на один уровень.<br>"
            "0 — Участник; 1 — Модератор I; 2 — Модератор II; "
            "3 — Старший модератор; 4 — Главный модератор; 5 — Владелец.</p></details>"
            "<details><summary>📝 Заметки · ранги 4–5</summary>"
            "<p><code>+заметка Название</code>, затем текст со следующей строки — создать заметку "
            "(название до 20 символов, форматирование сохраняется).<br>"
            "<code>заметки</code> — компактный список раскрывающихся заметок.<br>"
            "<code>заметка 12</code> — открыть заметку №12.<br>"
            "<code>-заметка 12</code> — удалить заметку №12.</p></details>"
            "<details><summary>⏱ Сроки наказаний</summary>"
            "<p>Поддерживаются секунды, минуты, часы и дни, а также <code>навсегда</code>.<br>"
            "Примеры: <code>30 секунд</code>, <code>10 минут</code>, <code>2 часа</code>, <code>1 день</code>.<br>"
            "Третий активный варн автоматически выдаёт мут на 1 час. Сроки отслеживаются автоматически.</p></details>"
            "<details><summary>📌 Как указать пользователя</summary>"
            "<p>Ответь командой на сообщение пользователя или добавь в конец команды кастомный ID / "
            "<code>@username</code>.<br>Пример: <code>молчи 10 минут @username</code>.<br>"
            "Нельзя применять наказания к себе или пользователям с равным/более высоким рангом.</p></details>"
            "<details><summary>🔐 Права</summary>"
            "<p>Удаление сообщений, варны и снятие варнов — с 1 ранга.<br>"
            "Муты и снятие мутов — со 2 ранга.<br>Баны — с 3 ранга.<br>"
            "Разбан, управление рангами и заметками — с 4 ранга.<br>"
            "Повышать и понижать можно только пользователей ниже себя.</p></details>"
            "<details><summary>📨 Репорты</summary>"
            "<p><code>ид чата</code> — показать ID группы.<br>"
            "В чате администрации: <code>+репорты из чата -100...</code> — подключить источник.<br>"
            "Любой участник может отправить <code>репорт</code> с текстом, ответив на сообщение нарушителя. " 
            "Один активный репорт на пользователя до решения администрации.</p></details>"
            "<details><summary>⚠️ Защита чата</summary>"
            "<p>Айзен удаляет распознаваемые ссылки Telegram и WhatsApp и пытается заблокировать отправителя. "
            "Для модерации и защиты чата боту нужны соответствующие права администратора.</p></details>"
        )
        await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(html=help_html)
        )


    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?i)^ид чата\s*$"))
    async def chat_id_handler(message: Message):
        await message.reply(f"ID этого чата равен <code>{message.chat.id}</code>", parse_mode="HTML")

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?i)^\+репорты из чата\s+-100\d+\s*$"))
    async def set_report_route_handler(message: Message):
        parts = message.text.split()
        source_id = int(parts[-1])
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor or actor.rank < 4:
                await message.reply("⛔ Настраивать маршруты репортов могут только ранги 4–5.")
                return
            if source_id == message.chat.id:
                await message.reply("Нельзя направить репорты чата в него же.")
                return
            source = await session.scalar(select(Chat).where(Chat.telegram_chat_id == source_id))
            if not source:
                await message.reply("Этот чат ещё не зарегистрирован. Добавьте бота в исходную группу и выполните там «ид чата».")
                return
            route = await session.scalar(select(ReportRoute).where(ReportRoute.source_chat_id == source_id))
            if route:
                route.destination_chat_id = message.chat.id
                route.created_by = actor.id
            else:
                session.add(ReportRoute(source_chat_id=source_id, destination_chat_id=message.chat.id, created_by=actor.id))
            await session.commit()
            source_title = source.title or str(source_id)
        await message.reply(f"✅ Репорты из чата «{html_decoration.quote(source_title)}» будут приходить сюда.", parse_mode="HTML")

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?is)^репорт(?:\s|$)"))
    async def report_handler(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        body = message.text.split("\n", 1)[1].strip() if "\n" in message.text else ""
        if not body:
            await message.reply("Напиши репорт в формате:\nрепорт\nТекст жалобы")
            return
        async with Session() as session:
            author = await get_user(session, message.from_user.id)
            route = await session.scalar(select(ReportRoute).where(ReportRoute.source_chat_id == message.chat.id))
            if not route:
                await message.reply("Для этого чата ещё не настроен приём репортов.")
                return
            existing = await session.scalar(select(Report).where(
                Report.source_chat_id == message.chat.id,
                Report.author_id == author.id,
                Report.status.in_([ "pending", "processing" ])
            ))
            if existing:
                await message.reply("У тебя уже есть репорт, который ожидает рассмотрения. Повторный можно отправить после решения администрации.")
                return
            target = await get_user(session, message.reply_to_message.from_user.id) if message.reply_to_message and message.reply_to_message.from_user else None
            report = Report(source_chat_id=message.chat.id, destination_chat_id=route.destination_chat_id,
                            author_id=author.id, target_user_id=target.id if target else None, text=body)
            session.add(report)
            await session.commit()
            await session.refresh(report)
            report_id, destination_id = report.id, route.destination_chat_id
            author_name, author_custom_id = author.first_name or "Без имени", author.id
            target_name, target_custom_id = (target.first_name or "Без имени", target.id) if target else (None, None)
            source_title = message.chat.title or str(message.chat.id)
        safe = html_decoration.quote
        target_line = f"\n<b>На кого жалоба:</b> {safe(target_name)} · кастомный ID <code>{target_custom_id}</code>" if target_custom_id else "\n<b>На кого жалоба:</b> не указан — для кнопок наказания нужно отправить репорт ответом на сообщение пользователя"
        card = (f"🚨 <b>Новая жалоба №{report_id}</b>\n<b>Чат:</b> {safe(source_title)}\n"
                f"<b>Автор:</b> {safe(author_name)} · кастомный ID <code>{author_custom_id}</code>"
                f"{target_line}\n<b>Текст:</b>\n{safe(body)}\n\n<b>Статус:</b> 🕓 Ожидает рассмотрения")
        try:
            sent = await message.bot.send_message(destination_id, card, parse_mode="HTML",
                                                  reply_markup=report_action_markup(report_id) if target else None)
            async with Session() as session:
                saved = await session.get(Report, report_id)
                saved.destination_message_id = sent.message_id
                await session.commit()
            await message.reply("Ваша жалоба отправлена администрации! Спасибо.")
        except Exception:
            logging.exception("Не удалось доставить репорт %s", report_id)
            async with Session() as session:
                saved = await session.get(Report, report_id)
                if saved and saved.status == "pending":
                    await session.delete(saved)
                    await session.commit()
            await message.reply("Не удалось отправить жалобу. Сообщи администрации чата.")

    @dp.callback_query(F.data.startswith("ract:"))
    async def report_action_choose(query: CallbackQuery):
        try:
            _, raw_id, action = query.data.split(":")
            report_id = int(raw_id)
        except (ValueError, AttributeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        required = {"mute": 2, "ban": 3, "warn": 1}.get(action)
        if required is None:
            await query.answer("Неизвестное действие.", show_alert=True)
            return
        async with Session() as session:
            actor = await get_user(session, query.from_user.id)
            report = await session.get(Report, report_id)
            if not report or report.status != "pending":
                await query.answer("Этот репорт уже обработан.", show_alert=True)
                return
            target = await session.get(User, report.target_user_id) if report.target_user_id else None
            if not actor or actor.rank < required or not target or actor.id == target.id or actor.rank <= target.rank:
                await query.answer("Недостаточно прав или цель имеет равный/старший ранг.", show_alert=True)
                return
        if action == "warn":
            await apply_report_punishment(query, report_id, action, "d7")
        else:
            await query.message.edit_reply_markup(reply_markup=report_duration_markup(report_id, action))
            await query.answer("Выбери срок наказания.")

    @dp.callback_query(F.data.startswith("rdo:"))
    async def report_action_execute(query: CallbackQuery):
        try:
            _, raw_id, action, duration = query.data.split(":")
            report_id = int(raw_id)
        except (ValueError, AttributeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        await apply_report_punishment(query, report_id, action, duration)

    @dp.callback_query(F.data.startswith("rreject:"))
    async def report_reject(query: CallbackQuery):
        try:
            report_id = int(query.data.split(":")[1])
        except (ValueError, AttributeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        async with Session() as session:
            actor = await get_user(session, query.from_user.id)
            report = await session.get(Report, report_id)
            if not actor or actor.rank < 1:
                await query.answer("Отклонять репорты могут модераторы от 1 ранга.", show_alert=True)
                return
            if not report or report.status != "pending":
                await query.answer("Этот репорт уже обработан.", show_alert=True)
                return
            report.status, report.handled_by, report.handled_at = "rejected", actor.id, datetime.now(timezone.utc)
            await session.commit()
        await render_report(query.bot, report_id)
        await query.answer("Репорт отклонён.")



    @dp.message(F.text.regexp(r"(?i)^профиль(?:\s+\d+)?\s*$"))
    async def profile_handler(message: Message):
        await profile(message)

    @dp.message((F.photo | F.video) & F.caption.regexp(r"(?i)^аватар$"))
    async def avatar_handler(message: Message):
        async with Session() as session:
            user = await get_user(session, message.from_user.id)
            if not user:
                await message.reply("Сначала напиши «профиль».")
                return
            if message.photo:
                user.avatar_file_id = message.photo[-1].file_id
                user.avatar_type = "photo"
            else:
                user.avatar_file_id = message.video.file_id
                user.avatar_type = "video"
            await session.commit()
        await message.reply("✅ Аватарка сохранена. Теперь она будет отображаться в твоём профиле.")

    @dp.message(F.text.regexp(r"(?is)^\+заметка(?:\s|$)"))
    async def add_note_handler(message: Message):
        await save_note(message)

    @dp.message(F.text.regexp(r"(?i)^-заметка(?:\s|$)"))
    async def remove_note_handler(message: Message):
        await delete_note(message)

    @dp.message(F.text.regexp(r"(?i)^заметка\s+\d+\s*$"))
    async def show_note_handler(message: Message):
        await show_note(message)

    @dp.message(F.text.regexp(r"(?i)^заметки\s*$"))
    async def list_notes_handler(message: Message):
        await list_notes(message)

    @dp.message(F.text.regexp(r"(?i)^-смс\s*$"))
    async def delete_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor or actor.rank < 1:
                await message.reply("⛔ Команда доступна с 1 ранга.")
                return
        if not message.reply_to_message:
            await message.reply("Ответь на сообщение командой -смс.")
            return
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except Exception:
            await message.reply("Не удалось удалить сообщение. Проверь права бота.")

    @dp.message(F.text.regexp(r"(?i)^варн\s+"))
    async def warn_handler(message: Message): await moderation_action(message, "варн", 1)

    @dp.message(F.text.regexp(r"(?i)^молчи\s+"))
    async def mute_handler(message: Message): await moderation_action(message, "молчи", 2)

    @dp.message(F.text.regexp(r"(?i)^бан\s+"))
    async def ban_handler(message: Message): await moderation_action(message, "бан", 3)

    @dp.message(F.text.regexp(r"(?i)^повысить(?:\s|$)"))
    async def promote_handler(message: Message): await change_rank(message, 1)

    @dp.message(F.text.regexp(r"(?i)^понизить(?:\s|$)"))
    async def demote_handler(message: Message): await change_rank(message, -1)

    @dp.message(F.text.regexp(r"(?i)^говори(?:\s|$)"))
    async def unmute_handler(message: Message): await unpunish(message, "говори", 2)

    @dp.message(F.text.regexp(r"(?i)^разбан(?:\s|$)"))
    async def unban_handler(message: Message): await unpunish(message, "разбан", 4)

    @dp.message(F.text.regexp(r"(?i)^снять\s+варн(?:\s|$)"))
    async def unwarn_handler(message: Message): await unpunish(message, "снять варн", 1)

    @dp.callback_query(F.data.startswith("unban:"))
    async def unban_button(query: CallbackQuery):
        try:
            _, uid, cid = query.data.split(":")
            custom_id, chat_id = int(uid), int(cid)
        except (ValueError, AttributeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        async with Session() as session:
            actor = await get_user(session, query.from_user.id)
            target = await session.get(User, custom_id)
            ban = await session.scalar(select(Ban).where(Ban.user_id == custom_id, Ban.chat_id == chat_id, Ban.active.is_(True)).order_by(Ban.created_at.desc()))
            if not actor or actor.rank < 4 or not target or actor.rank <= target.rank or not ban:
                await query.answer("Доступно рангам 4–5 для разблокировки младших рангов.", show_alert=True)
                return
            ban.active = False
            session.add(PunishmentLog(user_id=custom_id, moderator_id=actor.id, chat_id=chat_id, action="разбан кнопкой"))
            await session.commit()
        try:
            await query.bot.unban_chat_member(chat_id, target.telegram_id, only_if_banned=True)
            await query.message.edit_text(f"✅ Пользователь <code>{custom_id}</code> разблокирован.", parse_mode="HTML", reply_markup=None)
            await query.answer("Бан снят.")
        except Exception:
            await query.answer("Не удалось снять бан. Проверь права бота.", show_alert=True)

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    async def link_guard(message: Message):
        body = message.text or message.caption or ""
        if not body or not message.from_user or message.from_user.is_bot or not BLOCKED_LINK.search(body):
            return
        try:
            await message.delete()
        except Exception:
            pass
        async with Session() as session:
            user = await get_user(session, message.from_user.id)
            if not user:
                return
            session.add(Ban(user_id=user.id, chat_id=message.chat.id, moderator_id=None, expires_at=None))
            session.add(PunishmentLog(user_id=user.id, moderator_id=None, chat_id=message.chat.id, action="автобан за ссылку"))
            await session.commit()
            custom_id, telegram_id = user.id, user.telegram_id
        try:
            await message.bot.ban_chat_member(message.chat.id, telegram_id)
            await message.answer(f"🚫 Пользователь <code>{custom_id}</code> заблокирован за запрещённую ссылку.",
                                 parse_mode="HTML", reply_markup=unban_markup(custom_id, message.chat.id))
        except Exception:
            await message.answer("🚫 Найдена запрещённая ссылка, но бан не выполнен. Проверь права бота.")

    asyncio.create_task(expiry_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
