import asyncio
import html
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MessageEntity, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, InputRichMessage, FSInputFile
from aiogram.utils.text_decorations import html_decoration
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import Base, engine, Session, User, Chat, Warning, Mute, Ban, PunishmentLog, Note, ReportRoute, Report, LinkWhitelist, CommandAccess, CurrencyAccount, WelcomeSettings

RANKS = {0:"Участник", 1:"Модератор I", 2:"Модератор II", 3:"Старший модератор", 4:"Главный модератор", 5:"Владелец"}

RP_ACTIONS = {
    "передать": "передал",
    "посмотреть": "посмотрел на",
    "коснуться": "коснулся",
    "окликнуть": "окликнул",
    "обнять": "обнял",
    "указать": "указал на",
    "проигнорировать": "проигнорировал",
    "подойти": "подошёл к",
    "догнать": "догнал",
    "заметить": "заметил",
    "оскорбить": "оскорбил",
    "похвалить": "похвалил",
    "толкнуть": "толкнул",
    "обернуться": "обернулся к",
    "найти": "нашёл",
    "преградить путь": "преградил путь",
    "подозрительно изучить": "подозрительно изучил",
    "кивнуть": "кивнул",
    "притянуть к себе": "притянул к себе",
    "разозлиться": "разозлился на",
}

WELCOME_RULES_URL = "https://telegra.ph/Pravila-setki--T%E1%A7%90ch%CE%BA%E1%A5%B2-%CA%99%E1%A5%B4%E1%B4%9B%CF%81%E1%A5%B1ch%E1%A5%99--i-grupp-v-nej-10-22"
welcome_batches: dict[int, dict[int, dict]] = {}
welcome_tasks: dict[int, asyncio.Task] = {}

# Постоянные номера команд. Значения по умолчанию сохраняют прежние ограничения.
COMMANDS = {
    1: ("Профиль", "профиль", 0),
    2: ("Удаление сообщения", "-смс", 1),
    3: ("Бан", "бан", 3),
    4: ("Варн", "варн", 1),
    5: ("Мут", "молчи", 2),
    6: ("Снятие мута", "говори", 2),
    7: ("Разбан", "разбан", 3),
    8: ("Снятие варна", "снять варн", 1),
    9: ("Повышение ранга", "повысить", 4),
    10: ("Понижение ранга", "понизить", 4),
    11: ("Список администрации", "админы", 0),
    12: ("Создание заметки", "+заметка", 4),
    13: ("Удаление заметки", "-заметка", 4),
    14: ("Просмотр заметки", "заметка", 4),
    15: ("Список заметок", "заметки", 4),
    16: ("Добавление ссылки", "+ссылка", 4),
    17: ("Удаление ссылки", "-ссылка", 4),
    18: ("Список ссылок", "ссылки", 4),
    19: ("Репорт", "репорт", 0),
    20: ("Настройка репортов", "+репорты из чата", 4),
    21: ("ID чата", "ид чата", 0),
    101: ("RP-команды", "RP", 0),
    102: ("Фарм аур", "фармить", 0),
    103: ("Баланс аур", "баланс", 0),
    104: ("Настройка приветствия", "+приветствие", 5),
}
COMMAND_ALIASES = (
    [(trigger, 101) for trigger in RP_ACTIONS]
    + [("-приветствие", 104), ("!приветствие", 104), ("тест приветствия", 104)]
)
COMMAND_TRIGGERS = sorted(
    [(meta[1], command_id) for command_id, meta in COMMANDS.items() if command_id != 101]
    + COMMAND_ALIASES,
    key=lambda item: len(item[0]), reverse=True
)

def command_id_from_text(value: str) -> int | None:
    first_line = (value or "").splitlines()[0].strip().lower() if value else ""
    if first_line.startswith("/"):
        first_line = first_line[1:]
    for trigger, command_id in COMMAND_TRIGGERS:
        if first_line == trigger or first_line.startswith(trigger + " "):
            return command_id
    return None

def rank_access_allowed(rank: int, min_rank: int) -> bool:
    return min_rank != 6 and rank >= min_rank
DURATION_RE = re.compile(r"(?i)(навсегда|\d+\s*(?:секунд(?:у|ы)?|сек|минут(?:у|ы)?|мин|час(?:а|ов)?|дн(?:я|ей)?|день))")
URL_RE = re.compile(r"(?i)(?<![@\w])(?:(?:https?://|www\.)[^\s<>]+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?)")
TG_PUBLIC_RE = re.compile(r"(?i)^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,})(?:[/?#].*)?$")
TG_INVITE_RE = re.compile(r"(?i)^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)")


def normalize_link(value: str) -> str:
    value = value.strip().rstrip(".,);]}>").lower()
    if value.startswith("www."):
        value = "https://" + value
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value):
        value = "https://" + value
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    return (parsed.hostname or "").removeprefix("www.") + parsed.path.rstrip("/")


def message_links(message: Message) -> list[str]:
    body = message.text or message.caption or ""
    links = set(URL_RE.findall(body))
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == "text_link" and entity.url:
            links.add(entity.url)
    return list(links)


async def is_person_profile_link(bot: Bot, url: str) -> bool:
    url = url.strip().rstrip(".,);]}>")
    if TG_INVITE_RE.match(url):
        return False
    match = TG_PUBLIC_RE.match(url)
    if not match:
        return False
    username = match.group(1)
    # Bot usernames are required by Telegram to end in "bot".
    if username.lower().endswith("bot"):
        return False
    try:
        chat = await bot.get_chat("@" + username)
        return chat.type not in ("group", "supergroup", "channel")
    except Exception:
        # Safe default: an unresolved t.me username is treated as a person's profile.
        return True


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

class CommandAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.text or not event.from_user or event.from_user.is_bot:
            return await handler(event, data)
        command_id = command_id_from_text(event.text)
        if command_id is None:
            return await handler(event, data)
        async with Session() as session:
            user = await get_user(session, event.from_user.id)
            rank = user.rank if user else 0
            setting = await session.get(CommandAccess, command_id)
            min_rank = setting.min_rank if setting else COMMANDS[command_id][2]
        if not rank_access_allowed(rank, min_rank):
            await event.reply("⛔ У тебя нет доступа к этой команде.")
            return
        return await handler(event, data)

async def get_user(session, telegram_id):
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def allocate_public_id(session, model, owner_field, owner_id: int, active_only: bool = False):
    # Блокируем владельца ID, чтобы параллельные выдачи не получили одинаковый номер.
    await session.scalar(select(User.id).where(User.id == owner_id).with_for_update())
    query = select(model.public_id).where(owner_field == owner_id, model.public_id.is_not(None))
    if active_only:
        query = query.where(model.active.is_(True))
    used = set()
    for value in (await session.scalars(query)).all():
        try:
            prefix, suffix = value.rsplit("_", 1)
            if int(prefix) == owner_id:
                used.add(int(suffix))
        except (ValueError, AttributeError):
            continue
    number = 1
    while number in used:
        number += 1
    return f"{owner_id}_{number}"


def parse_public_id(value: str):
    return int(value.strip()) if re.fullmatch(r"\d+", value.strip()) else None


async def allocate_warning_number(session, user_id: int):
    await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
    values = (await session.scalars(select(Warning.public_id).where(
        Warning.user_id == user_id, Warning.active.is_(True), Warning.public_id.is_not(None)
    ))).all()
    used = set()
    for value in values:
        try:
            used.add(int(value.rsplit("_", 1)[-1]))
        except (ValueError, AttributeError):
            continue
    number = 1
    while number in used:
        number += 1
    return str(number)


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

def telegram_profile_url(user: User) -> str:
    return f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.telegram_id}"

def user_display_name(user: User) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Без имени"

async def resolve_rp_target(message: Message, session, raw: str):
    replied = message.reply_to_message.from_user if message.reply_to_message else None
    if replied and not replied.is_bot:
        user = await get_user(session, replied.id)
        if user is None:
            user = User(telegram_id=replied.id, username=replied.username,
                        first_name=replied.first_name or "", last_name=replied.last_name, rank=0)
            session.add(user)
            await session.flush()
        return user
    token = raw.strip().split()[0] if raw.strip() else ""
    if token.startswith("@"):
        return await session.scalar(select(User).where(func.lower(User.username) == token[1:].lower()))
    if token.isdigit():
        user = await session.get(User, int(token))
        return user or await get_user(session, int(token))
    return None

async def send_rp_action(message: Message, trigger: str, verb: str):
    if not message.from_user or message.from_user.is_bot:
        return
    lines = (message.text or "").split("\n", 1)
    header = lines[0].strip()
    target_raw = header[len(trigger):].strip()
    extra_raw = lines[1] if len(lines) > 1 else ""
    extra = extra_raw.strip()

    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        target = await resolve_rp_target(message, session, target_raw)
        if not actor:
            await message.reply("Не удалось определить твой профиль.")
            return
        if not target:
            await message.reply("Не удалось найти второго участника. Ответь на его сообщение или укажи @username/кастомный ID.")
            return
        actor_name = user_display_name(actor)
        target_name = user_display_name(target)
        actor_url = telegram_profile_url(actor)
        target_url = telegram_profile_url(target)
        await session.commit()

    main_text = f"{actor_name} {verb} {target_name}"
    output = main_text + (f"\n{extra}" if extra else "")
    entities = [
        MessageEntity(type="text_link", offset=utf16_length(""),
                      length=utf16_length(actor_name), url=actor_url),
        MessageEntity(type="text_link", offset=utf16_length(f"{actor_name} {verb} "),
                      length=utf16_length(target_name), url=target_url),
    ]
    if extra:
        leading = len(extra_raw) - len(extra_raw.lstrip())
        source_extra_offset = utf16_length(header + "\n" + extra_raw[:leading])
        output_extra_offset = utf16_length(main_text + "\n")
        for entity in message.entities or []:
            if entity.type == "custom_emoji" and entity.custom_emoji_id:
                relative = entity.offset - source_extra_offset
                if 0 <= relative and relative + entity.length <= utf16_length(extra):
                    entities.append(MessageEntity(
                        type="custom_emoji", offset=output_extra_offset + relative,
                        length=entity.length, custom_emoji_id=entity.custom_emoji_id
                    ))
    try:
        await message.answer(output, entities=entities, disable_web_page_preview=True)
    except TelegramBadRequest:
        fallback_entities = [entity for entity in entities if entity.type != "custom_emoji"]
        await message.answer(output, entities=fallback_entities, disable_web_page_preview=True)


def render_welcome(template: str, people: list[dict], group_title: str, group_url: str | None) -> str:
    rendered = html.escape(template)
    plural = len(people) >= 2
    rendered = re.sub(
        r"\{([^{}]*)/([^{}]*)\}",
        lambda match: match.group(2 if plural else 1),
        rendered,
    )
    names = []
    for person in people:
        name = html.escape(person["name"])
        url = person.get("url")
        names.append(f'<a href="{html.escape(url, quote=True)}">{name}</a>' if url else name)
    names_html = ", ".join(names)
    rendered = rendered.replace("{имя в тг + ссылка}", names_html)
    rendered = rendered.replace("{имя в тг}", names_html)
    safe_title = html.escape(group_title or "нашу группу")
    if group_url:
        group_value = f'<a href="{html.escape(group_url, quote=True)}">{safe_title}</a>'
    else:
        group_value = safe_title
    rendered = rendered.replace("{название группы + ссылка на неё}", group_value)
    rendered = rendered.replace("{название группы}", group_value)
    rules = f'<a href="{WELCOME_RULES_URL}">правила</a>'
    rendered = rendered.replace("{правила}", rules)
    return rendered


async def get_group_link(bot: Bot, chat_id: int, username: str | None = None) -> str | None:
    if username:
        return f"https://t.me/{username}"
    try:
        return await bot.export_chat_invite_link(chat_id)
    except Exception:
        logging.warning("Не удалось получить ссылку-приглашение для чата %s", chat_id)
        return None


async def flush_welcome_batch(bot: Bot, chat_id: int):
    await asyncio.sleep(5)
    people = list(welcome_batches.pop(chat_id, {}).values())
    welcome_tasks.pop(chat_id, None)
    if not people:
        return

    async with Session() as session:
        config = await session.get(WelcomeSettings, 1)
        if not config or not config.enabled or not config.text:
            return
        template = config.text

    try:
        chat = await bot.get_chat(chat_id)
        group_title = chat.title or "нашу группу"
        group_url = await get_group_link(bot, chat_id, chat.username)
    except Exception:
        logging.exception("Не удалось получить данные группы %s для приветствия", chat_id)
        return

    text_value = render_welcome(template, people, group_title, group_url)
    try:
        await bot.send_message(chat_id, text_value, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logging.exception("Не удалось отправить приветствие в чате %s", chat_id)

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

        user_id = user.id
        warns = (await session.scalars(
            select(Warning).where(Warning.user_id == user_id, Warning.active.is_(True))
            .order_by(Warning.created_at.desc())
        )).all()
        mutes = (await session.scalars(
            select(Mute).where(Mute.user_id == user_id, Mute.active.is_(True))
            .order_by(Mute.created_at.desc())
        )).all()
        bans = (await session.scalars(
            select(Ban).where(Ban.user_id == user_id, Ban.active.is_(True))
            .order_by(Ban.created_at.desc())
        )).all()

        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "Без имени"
        username = f"<br>Username: @{html_decoration.quote(user.username)}" if user.username else ""
        registered = user.registered_at.strftime("%d.%m.%Y") if user.registered_at else "неизвестно"
        profile_html = (
            f"👤 <b>Профиль</b><br>"
            f"Имя: {html_decoration.quote(name)}{username}<br>"
            f"Кастомный ID: <code>{user.id}</code><br>"
            f"Ранг: {html_decoration.quote(RANKS.get(user.rank, 'Участник'))} ({user.rank})<br>"
            f"Регистрация: {registered}"
        )

    def format_record(record):
        created = record.created_at.strftime("%d.%m.%Y %H:%M") if record.created_at else "неизвестно"
        expires = record.expires_at.strftime("%d.%m.%Y %H:%M") if record.expires_at else "навсегда"
        return f"• {created} · чат <code>{record.chat_id}</code> · до: {expires}"

    def details_block(title, records, warning=False):
        if not records:
            body = "Нет активных наказаний."
        else:
            entries = []
            for record in records:
                number = f"№{html_decoration.quote(str(record.public_id))} · " if warning and record.public_id else ""
                entries.append(number + format_record(record))
            body = "<br>".join(entries)
        return f"<details><summary>{title} · {len(records)}</summary><p>{body}</p></details>"

    punishment_html = (
        "<h3>📋 Активные наказания</h3>"
        + details_block("⚠️ Варны", warns, warning=True)
        + details_block("🔇 Муты", mutes)
        + details_block("⛔ Баны", bans)
    )
    await message.bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=InputRichMessage(html=profile_html + punishment_html)
    )

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
        note = Note(title=title, body=body,
                    entities_json=json.dumps(entities, ensure_ascii=False) if entities else None,
                    created_by=actor.id)
        session.add(note)
        await session.flush()
        note.public_id = str(note.id)
        public_id = note.public_id
        await session.commit()
    await message.reply(f"📝 Заметка «{html_decoration.quote(title)}» сохранена. ID: <code>{public_id}</code>", parse_mode="HTML")

async def delete_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parse_public_id(parts[1].strip()):
        await message.reply("Укажи ID заметки: -заметка 1")
        return
    note_public_id = parts[1].strip()
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        note = await session.scalar(select(Note).where(Note.public_id == note_public_id))
        if not note:
            await message.reply("Заметка с таким ID не найдена.")
            return
        title = note.title
        await session.delete(note)
        await session.commit()
    await message.reply(f"🗑 Заметка <code>{note_public_id}</code> «{html_decoration.quote(title)}» удалена.", parse_mode="HTML")

async def show_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parse_public_id(parts[1].strip()):
        return
    note_public_id = parts[1].strip()
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        note = await session.scalar(select(Note).where(Note.public_id == note_public_id))
        if not note:
            await message.reply("Заметка с таким ID не найдена.")
            return
        note_id, title, body = str(note.id), note.title, note.body
        raw_entities = json.loads(note.entities_json) if note.entities_json else []
    entities = [MessageEntity.model_validate(item) for item in raw_entities]
    await message.answer(f"📝 <b>Заметка №{note_id}: {title}</b>", parse_mode="HTML")
    await message.answer(body, entities=entities or None)

async def list_notes(message: Message):
    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        notes = (await session.scalars(select(Note).order_by(Note.id.asc()))).all()
        note_data = [
            (str(note.id), note.title, note.body,
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
        if not target:
            await message.reply("Пользователь не найден. Ответь на его сообщение или укажи кастомный ID/@username.")
            return
        if target.id == actor.id or actor.rank <= target.rank:
            await message.reply("⛔ Нельзя применять действие к себе или к равному/старшему рангу.")
            return
        if action == "варн":
            warning_public_id = await allocate_warning_number(session, target.id)
            session.add(Warning(user_id=target.id, chat_id=message.chat.id, moderator_id=actor.id,
                                expires_at=expiry, public_id=warning_public_id))
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
            await message.reply(f"⚠️ Варн выдан. ID: <code>{warning_public_id}</code>. Активных варнов: {count}." + (" Автоматический мут на 1 час." if auto_mute else ""), parse_mode="HTML")
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
    # Убираем полное имя команды, включая оба слова в «снять варн».
    raw = (message.text or "")[len(action):].strip()
    warning_number = None
    target_raw = raw
    reply_target = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id != message.bot.id
    )
    use_reply_target = bool(reply_target and (
        action != "снять варн" or len(raw.split()) == 1
    ))

    if action == "снять варн":
        parts = raw.split()
        if len(parts) == 2:
            target_raw, number_raw = parts
        elif len(parts) == 1 and use_reply_target:
            target_raw = ""
            number_raw = parts[0]
        else:
            await message.reply("Формат: снять варн @username 2, снять варн 123 2 или ответом на сообщение пользователя: снять варн 2")
            return
        if not number_raw.isdigit() or int(number_raw) < 1:
            await message.reply("Номер варна должен быть положительным числом.")
            return
        warning_number = str(int(number_raw))

    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        if use_reply_target:
            target = await get_user(session, message.reply_to_message.from_user.id)
        else:
            token = target_raw.strip().split()[-1] if target_raw.strip() else ""
            if token.startswith("@"):
                target = await session.scalar(
                    select(User).where(func.lower(User.username) == token[1:].lower())
                )
            elif token.isdigit():
                target = await session.get(User, int(token))
            else:
                target = None

        if not target or actor.rank <= target.rank:
            await message.reply("Пользователь не найден или его ранг равен/выше вашего.")
            return

        model = {"говори": Mute, "разбан": Ban, "снять варн": Warning}[action]
        if action == "снять варн":
            item = await session.scalar(select(Warning).where(
                Warning.public_id == warning_number, Warning.user_id == target.id,
                Warning.chat_id == message.chat.id, Warning.active.is_(True)))
        else:
            item = await session.scalar(select(model).where(
                model.user_id == target.id, model.chat_id == message.chat.id,
                model.active.is_(True)).order_by(model.created_at.desc()))

        if not item:
            await message.reply("Активное наказание с таким номером в этом чате не найдено.")
            return

        item.active = False
        session.add(PunishmentLog(
            user_id=target.id, moderator_id=actor.id, chat_id=message.chat.id,
            action=f"снятие: {action}",
            details=warning_number if action == "снять варн" else None
        ))
        await session.commit()

        try:
            if action == "говори":
                await clear_mute(message.bot, message.chat.id, target.telegram_id)
            elif action == "разбан":
                await message.bot.unban_chat_member(
                    message.chat.id, target.telegram_id, only_if_banned=True
                )
            await message.reply(
                f"Готово. Наказание снято{f' (варн №{warning_number})' if warning_number else ''}."
            )
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
    warning_public_id = None
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
                warning_public_id = await allocate_warning_number(session, target_custom_id)
                session.add(Warning(user_id=target_custom_id, chat_id=source_chat_id,
                                    moderator_id=actor.id, expires_at=expiry, public_id=warning_public_id))
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
        answer_text = "Наказание применено в исходной группе."
        if action == "warn" and warning_public_id:
            answer_text += f" ID варна: {warning_public_id}"
        await query.answer(answer_text)
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
    if action == "warn":
        choices = [("Выдать варн на 7 дней", "d7")]
    else:
        choices = [("10 минут", "m10"), ("1 час", "h1"), ("1 день", "d1"), ("7 дней", "d7")]
    if action == "ban":
        choices.append(("Навсегда", "forever"))
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"rdo:{report_id}:{action}:{code}")]
        for label, code in choices
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"rback:{report_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        await conn.execute(text("ALTER TABLE warnings ADD COLUMN IF NOT EXISTS public_id VARCHAR(40)"))
        await conn.execute(text("ALTER TABLE notes ADD COLUMN IF NOT EXISTS public_id VARCHAR(40)"))
        await conn.execute(text("""
            UPDATE warnings
            SET public_id = split_part(public_id, '_', 2)
            WHERE public_id LIKE '%\\_%' ESCAPE '\\'
        """))
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id, user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS n
                FROM warnings WHERE active = TRUE AND public_id IS NULL
            )
            UPDATE warnings w SET public_id = ranked.n::text
            FROM ranked WHERE w.id = ranked.id
        """))
        await conn.execute(text("""
            UPDATE notes SET public_id = id::text
            WHERE public_id IS DISTINCT FROM id::text
        """))
    async with Session() as session:
        welcome_config = await session.get(WelcomeSettings, 1)
        if welcome_config is None:
            session.add(WelcomeSettings(id=1, enabled=False, text=None))
        defaults = [
            {"command_id": command_id, "min_rank": meta[2]}
            for command_id, meta in COMMANDS.items()
        ]
        await session.execute(
            pg_insert(CommandAccess).values(defaults).on_conflict_do_nothing(index_elements=["command_id"])
        )
        await session.commit()
    bot = Bot(settings.bot_token)
    message_bot = bot
    dp = Dispatcher()
    dp.message.outer_middleware(RegistrationMiddleware())
    dp.message.outer_middleware(CommandAccessMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer("Привет! Я бот модерации. Напиши «профиль», чтобы посмотреть свой профиль.")

    @dp.message(F.text.regexp(r"(?i)^дк(?:\s|$)"))
    async def command_access_handler(message: Message):
        parts = (message.text or "").split()
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor or actor.rank != 5:
                await message.reply("⛔ Настраивать доступы команд может только владелец (5 ранг).")
                return
            if len(parts) == 1:
                rows = (await session.scalars(select(CommandAccess))).all()
                access_map = {row.command_id: row.min_rank for row in rows}
                lines = [
                    "🔐 <b>Настройки доступов команд</b>",
                    "0 — всем; 1–5 — с указанного ранга и выше; 6 — запрещено всем.",
                ]
                for command_id, (title, trigger, default) in COMMANDS.items():
                    value = access_map.get(command_id, default)
                    lines.append(f"<code>{command_id}</code> — {html_decoration.quote(title)} (<code>{value}</code>)")
                await message.reply("\n".join(lines), parse_mode="HTML")
                return
            if not parts[1].isdigit() or int(parts[1]) not in COMMANDS:
                await message.reply("Укажи существующий номер команды. Посмотреть список: «дк».")
                return
            command_id = int(parts[1])
            setting = await session.get(CommandAccess, command_id)
            current = setting.min_rank if setting else COMMANDS[command_id][2]
            if len(parts) == 2:
                await message.reply(f"Команда №{command_id} — {COMMANDS[command_id][0]}. Текущий доступ: {current}.")
                return
            if len(parts) != 3 or not parts[2].isdigit() or not 0 <= int(parts[2]) <= 6:
                await message.reply("Формат: дк {номер команды} {доступ от 0 до 6}.")
                return
            new_access = int(parts[2])
            if setting is None:
                session.add(CommandAccess(command_id=command_id, min_rank=new_access))
            else:
                setting.min_rank = new_access
            await session.commit()
        await message.reply(f"✅ Доступ команды №{command_id} «{COMMANDS[command_id][0]}» установлен: {new_access}.")

    @dp.message(F.text.regexp(r"(?i)^мдк\s*$"))
    async def my_command_access_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            rank = actor.rank if actor else 0
            rows = (await session.scalars(select(CommandAccess))).all()
            access_map = {row.command_id: row.min_rank for row in rows}
        available = [
            (command_id, meta[0]) for command_id, meta in COMMANDS.items()
            if rank_access_allowed(rank, access_map.get(command_id, meta[2]))
        ]
        if not available:
            await message.reply("У тебя пока нет доступных команд.")
            return
        lines = [f"🔐 <b>Твои доступные команды</b> (ранг {rank})"]
        lines.extend(f"<code>{command_id}</code> — {html_decoration.quote(title)}" for command_id, title in available)
        if rank == 5:
            lines.append("<code>дк</code> — Настройка доступов команд")
        await message.reply("\n".join(lines), parse_mode="HTML")

    @dp.message(F.text.regexp(r"(?is)^\+приветствие(?:\s|$)"))
    async def set_or_show_welcome(message: Message):
        raw = (message.text or "")[len("+приветствие"):].strip()
        async with Session() as session:
            config = await session.get(WelcomeSettings, 1)
            if not raw:
                if not config or not config.text:
                    await message.reply("Приветствие ещё не настроено. Используй: +приветствие Текст")
                    return
                status = "включено" if config.enabled else "отключено"
                await message.reply(f"Текущее приветствие ({status}):\n{config.text}")
                return
            if config is None:
                config = WelcomeSettings(id=1, enabled=True, text=raw)
                session.add(config)
            else:
                config.text = raw
                config.enabled = True
            await session.commit()
        await message.reply("✅ Приветствие сохранено и включено для всех групп.")

    @dp.message(F.text.regexp(r"(?i)^-приветствие\s*$"))
    async def disable_welcome(message: Message):
        async with Session() as session:
            config = await session.get(WelcomeSettings, 1)
            if not config or not config.text:
                await message.reply("Сохранённого приветствия нет.")
                return
            config.enabled = False
            await session.commit()
        await message.reply("Приветствия отключены. Текст сохранён.")

    @dp.message(F.text.regexp(r"(?i)^!приветствие\s*$"))
    async def delete_welcome(message: Message):
        async with Session() as session:
            config = await session.get(WelcomeSettings, 1)
            if not config or not config.text:
                await message.reply("Сохранённого приветствия нет.")
                return
            config.enabled = False
            config.text = None
            await session.commit()
        await message.reply("🗑 Текст приветствия удалён.")

    @dp.message(F.text.regexp(r"(?i)^тест приветствия\s*$"))
    async def test_welcome(message: Message):
        async with Session() as session:
            config = await session.get(WelcomeSettings, 1)
            if not config or not config.text:
                await message.reply("Сначала настрой приветствие командой +приветствие Текст.")
                return
            template = config.text
            actor = await get_user(session, message.from_user.id)
            actor_name = user_display_name(actor) if actor else (message.from_user.full_name or "Участник")
            actor_url = telegram_profile_url(actor) if actor else f"tg://user?id={message.from_user.id}"

        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            group_title = message.chat.title or "нашу группу"
            group_url = await get_group_link(message.bot, message.chat.id, message.chat.username)
        else:
            group_title, group_url = "Название группы", None

        single = render_welcome(template, [{"name": actor_name, "url": actor_url}], group_title, group_url)
        multiple = render_welcome(
            template,
            [{"name": actor_name, "url": actor_url}, {"name": "Участник 2", "url": None}],
            group_title,
            group_url,
        )
        preview = (
            "<h1>🧪 Тест приветствия</h1>"
            "<details><summary>Единственное число · 1 участник</summary><p>" + single + "</p></details>"
            "<details><summary>Множественное число · 2+ участника</summary><p>" + multiple + "</p></details>"
        )
        await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(html=preview)
        )

    @dp.message(F.text.regexp(r"(?i)^баланс\s*$"))
    async def balance_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor:
                await message.reply("Не удалось определить твой профиль.")
                return
            account = await session.get(CurrencyAccount, actor.id)
            balance = account.balance if account else 0
        await message.reply(f"💰 Твой баланс: <b>{balance}</b> аур.", parse_mode="HTML")

    @dp.message(F.text.regexp(r"(?i)^фармить\s*$"))
    async def farm_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor:
                await message.reply("Не удалось определить твой профиль.")
                return
            await session.scalar(select(User.id).where(User.id == actor.id).with_for_update())
            account = await session.get(CurrencyAccount, actor.id)
            now = datetime.now(timezone.utc)
            if account is None:
                account = CurrencyAccount(user_id=actor.id, balance=0)
                session.add(account)
                await session.flush()
            if account.last_farm_at:
                ready_at = account.last_farm_at + timedelta(hours=5)
                if ready_at > now:
                    remaining_seconds = (ready_at - now).total_seconds()
                    remaining_minutes = max(1, int((remaining_seconds + 59) // 60))
                    hours, minutes = divmod(remaining_minutes, 60)
                    await message.reply(f"Ты уже фармил, попробуй через {hours}:{minutes:02d}.")
                    return
            reward = random.randint(100, 300)
            account.balance += reward
            account.last_farm_at = now
            await session.commit()
        await message.reply(
            f"Ты нафармил {reward} аур!\\nСледующий раз доступен через 5 часов."
        )

    @dp.chat_member()
    async def welcome_member_handler(event):
        chat = event.chat
        joined = event.new_chat_member
        user = joined.user
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or user.is_bot:
            return
        new_status = joined.status
        old_status = event.old_chat_member.status
        is_member = new_status in ("member", "administrator", "creator") or (
            new_status == "restricted" and bool(getattr(joined, "is_member", False))
        )
        was_member = old_status in ("member", "administrator", "creator") or (
            old_status == "restricted" and bool(getattr(event.old_chat_member, "is_member", False))
        )
        if not is_member or was_member:
            return

        async with Session() as session:
            actor = await get_user(session, user.id)
            if actor is None:
                actor = User(
                    telegram_id=user.id, username=user.username,
                    first_name=user.first_name or "", last_name=user.last_name, rank=0
                )
                session.add(actor)
            else:
                actor.username = user.username
                actor.first_name = user.first_name or ""
                actor.last_name = user.last_name
            chat_record = await session.scalar(select(Chat).where(Chat.telegram_chat_id == chat.id))
            if chat_record is None:
                session.add(Chat(telegram_chat_id=chat.id, title=chat.title or ""))
            else:
                chat_record.title = chat.title or ""
            await session.commit()

        batch = welcome_batches.setdefault(chat.id, {})
        batch[user.id] = {
            "name": " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Без имени",
            "url": f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}",
        }
        task = welcome_tasks.get(chat.id)
        if task is None or task.done():
            welcome_tasks[chat.id] = asyncio.create_task(flush_welcome_batch(message_bot, chat.id))

    @dp.message(Command("help"))
    async def help_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            rank = actor.rank if actor else 0
            rows = (await session.scalars(select(CommandAccess))).all()
            access_map = {row.command_id: row.min_rank for row in rows}

        help_items = {
            1: ("👤 Профиль", ["<code>профиль</code> — открыть свой профиль.", "<code>профиль 123</code> — открыть профиль по кастомному ID.", "Ответь командой на сообщение, чтобы открыть профиль его автора."]),
            2: ("🛡️ Модерация", ["<code>-смс</code> — удалить сообщение (ответом на него)."]),
            3: ("🛡️ Модерация", ["<code>бан 2 часа</code> или <code>бан навсегда</code> — заблокировать участника."]),
            4: ("🛡️ Модерация", ["<code>варн 1 день</code> — выдать предупреждение."]),
            5: ("🛡️ Модерация", ["<code>молчи 10 минут</code> — выдать мут."]),
            6: ("🛡️ Модерация", ["<code>говори</code> — снять мут (ответом на сообщение или указав пользователя)."]),
            7: ("🛡️ Модерация", ["<code>разбан</code> — снять бан (ответом на сообщение или указав пользователя)."]),
            8: ("🛡️ Модерация", ["<code>снять варн 2</code> — снять варн №2 ответом на сообщение.", "<code>снять варн @username 2</code> или <code>снять варн 123 2</code> — снять варн по username или кастомному ID."]),
            9: ("🎖️ Ранги", ["<code>повысить</code> — повысить участника на один ранг (ответом на сообщение или указав ID/@username)."]),
            10: ("🎖️ Ранги", ["<code>понизить</code> — понизить участника на один ранг."]),
            11: ("👥 Администрация", ["<code>админы</code> — показать список администрации по рангам."]),
            12: ("📝 Заметки", ["<code>+заметка Название</code>, затем текст со следующей строки — создать заметку. Название до 20 символов."]),
            13: ("📝 Заметки", ["<code>-заметка 1</code> — удалить заметку по ID."]),
            14: ("📝 Заметки", ["<code>заметка 1</code> — открыть заметку по ID."]),
            15: ("📝 Заметки", ["<code>заметки</code> — показать список заметок."]),
            16: ("🔗 Белый список ссылок", ["<code>+ссылка https://example.com</code> — добавить разрешённую ссылку."]),
            17: ("🔗 Белый список ссылок", ["<code>-ссылка https://example.com</code> — удалить ссылку из белого списка."]),
            18: ("🔗 Белый список ссылок", ["<code>ссылки</code> — показать общий белый список."]),
            19: ("📨 Репорты", ["<code>репорт</code>, затем текст со следующей строки — отправить жалобу. Для жалобы на участника ответь на его сообщение."]),
            20: ("📨 Репорты", ["<code>+репорты из чата -100...</code> — подключить группу-источник к чату администрации."]),
            21: ("📨 Репорты", ["<code>ид чата</code> — показать Telegram ID текущей группы."]),
            101: ("🎭 RP-команды", [
                "<code>передать @username</code> — «Имя1 передал Имя2».",
                "<code>посмотреть @username</code>, <code>коснуться @username</code>, <code>окликнуть @username</code>, <code>обнять @username</code>.",
                "<code>указать</code>, <code>проигнорировать</code>, <code>подойти</code>, <code>догнать</code>, <code>заметить</code>, <code>оскорбить</code>, <code>похвалить</code>, <code>толкнуть</code>, <code>обернуться</code>, <code>найти</code>, <code>преградить путь</code>, <code>подозрительно изучить</code>, <code>кивнуть</code>, <code>притянуть к себе</code>, <code>разозлиться</code> — остальные RP-действия.",
                "Укажи @username/кастомный ID или ответь на сообщение участника. Дополнительный текст можно написать со следующей строки. Все RP-действия используют один общий номер ДК."
            ]),
            102: ("💰 Ауры", ["<code>фармить</code> — получить случайно от 100 до 300 аур. Повторный фарм доступен через 5 часов."]),
            103: ("💰 Ауры", ["<code>баланс</code> — посмотреть свой баланс аур."]),
            104: ("👋 Приветствия", [
                "<code>+приветствие Текст</code> — установить или заменить приветствие. Можно использовать шаблонные переменные.",
                "<code>+приветствие</code> — посмотреть сохранённое приветствие.",
                "<code>-приветствие</code> — временно отключить приветствия.",
                "<code>!приветствие</code> — удалить сохранённый текст.",
                "<code>тест приветствия</code> — показать единственное и множественное число в одном сообщении."
            ]),
        }

        groups = {}
        for command_id, (section, descriptions) in help_items.items():
            meta = COMMANDS.get(command_id)
            if not meta:
                continue
            min_rank = access_map.get(command_id, meta[2])
            if not rank_access_allowed(rank, min_rank):
                continue
            groups.setdefault(section, []).extend(descriptions)

        help_html = (
            f"<h1>🦋 Айзен | помощник</h1>"
            f"<blockquote>Твой ранг: {rank} — {html_decoration.quote(RANKS.get(rank, 'Участник'))}. Ниже только команды, доступные тебе с учётом текущих настроек ДК.</blockquote>"
        )
        for section, descriptions in groups.items():
            help_html += f"<details><summary>{section}</summary><p>" + "<br>".join(descriptions) + "</p></details>"

        help_html += (
            "<details><summary>🔐 Общие команды доступа</summary>"
            "<p><code>/help</code> — эта справка с учётом твоего ранга и ДК.<br>"
            "<code>мдк</code> — показать только доступные тебе команды.</p></details>"
        )
        if rank == 5:
            help_html += (
                "<details><summary>🔐 Настройка ДК · владелец</summary>"
                "<p><code>дк</code> — список постоянных номеров и текущих настроек.<br>"
                "<code>дк 3</code> — посмотреть доступ команды №3.<br>"
                "<code>дк 3 4</code> — установить доступ с 4 ранга.<br>"
                "0 — всем; 1–5 — с указанного ранга и выше; 6 — запрещено всем.</p></details>"
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
        await query.message.edit_reply_markup(reply_markup=report_duration_markup(report_id, action))
        await query.answer("Подтверди действие или вернись назад.")

    @dp.callback_query(F.data.startswith("rback:"))
    async def report_action_back(query: CallbackQuery):
        try:
            report_id = int(query.data.split(":")[1])
        except (ValueError, AttributeError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        async with Session() as session:
            actor = await get_user(session, query.from_user.id)
            report = await session.get(Report, report_id)
            if not actor or actor.rank < 1:
                await query.answer("Недостаточно прав.", show_alert=True)
                return
            if not report or report.status != "pending":
                await query.answer("Этот репорт уже обрабатывается или обработан.", show_alert=True)
                return
        await query.message.edit_reply_markup(reply_markup=report_action_markup(report_id))
        await query.answer("Вернулись к выбору действия.")

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



    @dp.message(F.text.regexp(r"(?i)^админы\s*$"))
    async def admins_handler(message: Message):
        rank_names = {5: "Владелец", 4: "Старший администратор", 3: "Старший модератор",
                      2: "Модератор II", 1: "Модератор I"}
        async with Session() as session:
            admins = (await session.scalars(
                select(User).where(User.rank >= 1).order_by(User.rank.desc(), User.first_name.asc())
            )).all()
            rows = [(u.rank, u.first_name, u.last_name, u.username, u.telegram_id) for u in admins]
        if not rows:
            await message.reply("Список администрации пока пуст.")
            return
        lines = ["🛡 <b>Администрация</b>"]
        for rank in range(5, 0, -1):
            members = [u for u in rows if u[0] == rank]
            if not members:
                continue
            lines.append(f"\n<b>{rank} ранг — {rank_names[rank]}</b>")
            for _, first, last, username, telegram_id in members:
                name = html_decoration.quote(" ".join(x for x in (first, last) if x) or "Без имени")
                link = f"https://t.me/{username}" if username else f"tg://user?id={telegram_id}"
                lines.append(f'<a href="{link}">{name}</a>')
        lines.append('\nПо всем вопросам писать <a href="https://t.me/kaoruko_off">t.me/kaoruko_off</a>')
        admins_image = Path(__file__).resolve().parent / "admins.png"
        admins_caption = "\n".join(lines)
        if admins_image.is_file() and utf16_length(admins_caption) <= 1024:
            await message.answer_photo(
                FSInputFile(str(admins_image)),
                caption=admins_caption,
                parse_mode="HTML",
            )
        elif admins_image.is_file():
            # Telegram limits photo captions to 1024 UTF-16 units.
            await message.answer_photo(
                FSInputFile(str(admins_image)),
                caption=lines[0],
                parse_mode="HTML",
            )
            await message.answer("\n".join(lines[1:]), parse_mode="HTML", disable_web_page_preview=True)
        else:
            await message.reply(admins_caption, parse_mode="HTML", disable_web_page_preview=True)

    @dp.message(F.text.regexp(r"(?i)^профиль(?:\s+\d+)?\s*$"))
    async def profile_handler(message: Message):
        await profile(message)

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
    async def unban_handler(message: Message): await unpunish(message, "разбан", 3)

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
            if not actor or actor.rank < 3 or not target or actor.rank <= target.rank or not ban:
                await query.answer("⛔ Разбан доступен с 3 ранга. Снимать бан можно только пользователям с рангом ниже вашего.", show_alert=True)
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

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?i)^\+ссылка\s+"))
    async def add_allowed_link(message: Message):
        raw = message.text.split(maxsplit=1)[1].strip()
        normalized = normalize_link(raw)
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            existing = await session.scalar(select(LinkWhitelist).where(LinkWhitelist.normalized_link == normalized))
            if existing:
                await message.reply("Эта ссылка уже находится в белом списке.")
                return
            session.add(LinkWhitelist(link=raw, normalized_link=normalized, created_by=actor.id))
            await session.commit()
        await message.reply("✅ Ссылка добавлена в общий белый список.")

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?i)^-ссылка\s+"))
    async def remove_allowed_link(message: Message):
        raw = message.text.split(maxsplit=1)[1].strip()
        normalized = normalize_link(raw)
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            existing = await session.scalar(select(LinkWhitelist).where(LinkWhitelist.normalized_link == normalized))
            if not existing:
                await message.reply("Такой ссылки нет в белом списке.")
                return
            await session.delete(existing)
            await session.commit()
        await message.reply("🗑 Ссылка удалена из белого списка.")

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(r"(?i)^ссылки\s*$"))
    async def list_allowed_links(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            entries = (await session.scalars(select(LinkWhitelist).order_by(LinkWhitelist.id.asc()))).all()
            values = [entry.link for entry in entries]
        if not values:
            await message.reply("Белый список ссылок пуст.")
            return
        safe = [html_decoration.quote(value) for value in values]
        await message.reply("🔗 <b>Общий белый список:</b>\n" + "\n".join("• " + value for value in safe), parse_mode="HTML")

    def make_rp_handler(trigger: str, verb: str):
        async def handler(message: Message):
            await send_rp_action(message, trigger, verb)
        return handler

    for trigger, verb in RP_ACTIONS.items():
        dp.message.register(
            make_rp_handler(trigger, verb),
            F.text.regexp(rf"(?is)^{re.escape(trigger)}(?:\s|$)")
        )

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    async def link_guard(message: Message):
        if not message.from_user:
            return
        links = message_links(message)
        if not links:
            return
        async with Session() as session:
            allowed = {row.normalized_link for row in (await session.scalars(select(LinkWhitelist))).all()}
        blocked = []
        for link in links:
            if normalize_link(link) in allowed:
                continue
            if await is_person_profile_link(message.bot, link):
                continue
            blocked.append(link)
        if not blocked:
            return
        try:
            await message.delete()
        except Exception:
            logging.exception("Не удалось удалить сообщение со ссылкой в чате %s", message.chat.id)
            return
        if message.from_user.is_bot:
            return
        async with Session() as session:
            user = await get_user(session, message.from_user.id)
            if not user:
                return
            session.add(Ban(user_id=user.id, chat_id=message.chat.id, moderator_id=None, expires_at=None))
            session.add(PunishmentLog(user_id=user.id, moderator_id=None, chat_id=message.chat.id,
                                      action="автобан за запрещённую ссылку", details=", ".join(blocked)[:1000]))
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
