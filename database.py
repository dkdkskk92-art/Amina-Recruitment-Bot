from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from config import settings

def normalize_dsn(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

engine = create_async_engine(normalize_dsn(settings.database_url), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=0, index=True)
    registered_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    avatar_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_type: Mapped[str | None] = mapped_column(String(10), nullable=True)

class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Warning(Base):
    __tablename__ = "warnings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    public_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Mute(Base):
    __tablename__ = "mutes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Ban(Base):
    __tablename__ = "bans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

class PunishmentLog(Base):
    __tablename__ = "punishment_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    moderator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    entities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    public_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportRoute(Base):
    __tablename__ = "report_routes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    destination_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    destination_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    destination_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    handled_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LinkWhitelist(Base):
    __tablename__ = "link_whitelist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link: Mapped[str] = mapped_column(String(512), unique=True)
    normalized_link: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommandAccess(Base):
    __tablename__ = "command_access"
    command_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
