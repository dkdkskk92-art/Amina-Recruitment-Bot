import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from config import load_config
from database.db import init_db
from handlers.start import router as start_router
from handlers.menu import router as menu_router
from handlers.voting import router as voting_router, poll_closer

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')

async def main() -> None:
    cfg = load_config()
    bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(start_router)
    dp.include_router(menu_router)
    dp.include_router(voting_router)
    await init_db()
    closer_task = asyncio.create_task(poll_closer(bot))
    logging.info('Амина запускается; polling started')
    try:
        await dp.start_polling(bot)
    finally:
        closer_task.cancel()
        try:
            await closer_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
