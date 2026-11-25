import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from sqlalchemy import select, func

from config import settings
from database import Base, engine, Session, User, Chat, Warning, Mute, Ban, PunishmentLog

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
    parts = message.text.split()
    async with Session() as session:
        user = await session.get(User, int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else await get_user(session, message.from_user.id)
        if not user:
            await message.reply("Пользователь не найден.")
            return
        warns = await session.scalar(select(func.count(Warning.id)).where(Warning.user_id == user.id, Warning.active.is_(True))) or 0
        mutes = await session.scalar(select(func.count(Mute.id)).where(Mute.user_id == user.id, Mute.active.is_(True))) or 0
        bans = await session.scalar(select(func.count(Ban.id)).where(Ban.user_id == user.id, Ban.active.is_(True))) or 0
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "Без имени"
        username = f"\nUsername: @{user.username}" if user.username else ""
        registered = user.registered_at.strftime("%d.%m.%Y") if user.registered_at else "неизвестно"
        await message.reply(
            f"👤 <b>Профиль</b>\nИмя: {name}{username}\nКастомный ID: <code>{user.id}</code>"
            f"\nРанг: {RANKS.get(user.rank, 'Участник')} ({user.rank})"
            f"\nАктивные варны: {warns}\nАктивные муты: {mutes}\nАктивные баны: {bans}"
            f"\nРегистрация: {registered}", parse_mode="HTML"
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

async def main():
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.message.outer_middleware(RegistrationMiddleware())

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer("Привет! Я бот модерации. Напиши «профиль», чтобы посмотреть свой профиль.")

    @dp.message(F.text.regexp(r"(?i)^профиль(?:\s+\d+)?\s*$"))
    async def profile_handler(message: Message):
        await profile(message)

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
