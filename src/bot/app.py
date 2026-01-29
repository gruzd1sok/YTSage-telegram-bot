from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from src.bot.config import load_config
from src.bot.handlers import audio_command, download_command, help_command, start, url_message
from src.utils.ytsage_logger import logger


def run() -> None:
    config = load_config()
    if not config.token:
        raise RuntimeError("YTSAGE_BOT_TOKEN is not set")

    application = ApplicationBuilder().token(config.token).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("audio", audio_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))

    logger.info("Starting Telegram bot polling")
    application.run_polling(drop_pending_updates=True)
