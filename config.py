from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int

def load_config() -> Config:
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('BOT_TOKEN is missing. Copy .env.example to .env and set it.')
    try:
        owner_id = int(os.getenv('OWNER_ID', '0'))
    except ValueError as exc:
        raise RuntimeError('OWNER_ID must be an integer.') from exc
    return Config(bot_token=token, owner_id=owner_id)
