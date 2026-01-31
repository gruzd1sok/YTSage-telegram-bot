from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from src.bot.config import load_config
from src.bot.handlers import (
    audio_command,
    beta_request_callback,
    download_command,
    format_selection_callback,
    help_command,
    start,
    url_message,
)
from src.utils.ytsage_logger import logger
from src.utils.ytsage_localization import LocalizationManager


def run() -> None:
    LocalizationManager.set_language("ru")
    config = load_config()
    if not config.token:
        raise RuntimeError("YTSAGE_BOT_TOKEN is not set")

    request = HTTPXRequest(media_write_timeout=config.telegram_media_write_timeout)
    application = ApplicationBuilder().token(config.token).request(request).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("audio", audio_command))
    application.add_handler(CallbackQueryHandler(beta_request_callback, pattern=r"^beta:"))
    application.add_handler(CallbackQueryHandler(format_selection_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))

    logger.info("Starting Telegram bot polling")
    application.run_polling(drop_pending_updates=True)
