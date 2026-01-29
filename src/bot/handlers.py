import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InputFile
from telegram.ext import ContextTypes

from src.bot.config import BotConfig
from src.bot.service import download_with_callbacks
from src.utils.ytsage_logger import logger
from src.core.ytsage_ffmpeg import get_ffmpeg_path


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class ProgressReporter:
    def __init__(self, application, chat_id: int, message_id: int, loop: asyncio.AbstractEventLoop) -> None:
        self._application = application
        self._chat_id = chat_id
        self._message_id = message_id
        self._loop = loop
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._status: Optional[str] = None
        self._progress: Optional[float] = None
        self._details: Optional[str] = None

    def status(self, text: str) -> None:
        with self._lock:
            self._status = text
        self._schedule_update()

    def progress(self, value: float) -> None:
        with self._lock:
            self._progress = value
        self._schedule_update()

    def details(self, text: str) -> None:
        with self._lock:
            self._details = text
        self._schedule_update()

    def _format_text(self) -> str:
        with self._lock:
            status = self._status or "Downloading"
            progress = self._progress
            details = self._details
        parts = [status]
        if progress is not None:
            parts.append(f"Progress: {progress:.1f}%")
        if details:
            parts.append(details)
        return "\n".join(parts)

    def _schedule_update(self) -> None:
        now = time.monotonic()
        if now - self._last_send < 2.0:
            return
        self._last_send = now
        text = self._format_text()
        asyncio.run_coroutine_threadsafe(
            self._application.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            ),
            self._loop,
        )

    def force_update(self, text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            self._application.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            ),
            self._loop,
        )


def _get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.application.bot_data["config"]


def _is_allowed(update: Update, config: BotConfig) -> bool:
    if not config.allowed_chat_ids:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return (user_id in config.allowed_chat_ids) or (chat_id in config.allowed_chat_ids)


async def _ensure_allowed(update: Update, config: BotConfig) -> bool:
    if _is_allowed(update, config):
        return True
    message = update.effective_message
    if message:
        await message.reply_text(
            "Доступ к бета-версии выдает автор: @gruzd1sok. Напишите ему, чтобы получить доступ."
        )
    return False


def _extract_url(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _format_usage(command: str) -> str:
    return f"Usage: {command} <YouTube URL>"


def _file_too_large(path: Path, config: BotConfig) -> bool:
    max_bytes = config.max_upload_mb * 1024 * 1024
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return True


def _get_ffprobe_path() -> Optional[str]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    ffmpeg_path = get_ffmpeg_path()
    if isinstance(ffmpeg_path, Path):
        candidate = ffmpeg_path.parent / "ffprobe"
        if candidate.exists():
            return str(candidate)
    return None


def _probe_video_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    ffprobe = _get_ffprobe_path()
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return None
        width = streams[0].get("width")
        height = streams[0].get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
        return None
    except Exception:
        logger.exception("Failed to probe video dimensions")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, config):
        return
    message = (
        "Send /download <url> to fetch a video or /audio <url> for audio-only.\n"
        "You can also paste a YouTube URL directly."
    )
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, config):
        return
    url = _extract_url(" ".join(context.args)) or _extract_url(update.message.text)
    if not url:
        await update.message.reply_text(_format_usage("/download"))
        return

    await _handle_download(update, context, url, is_audio_only=False)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, config):
        return
    url = _extract_url(" ".join(context.args)) or _extract_url(update.message.text)
    if not url:
        await update.message.reply_text(_format_usage("/audio"))
        return

    await _handle_download(update, context, url, is_audio_only=True)


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, config):
        return
    url = _extract_url(update.message.text)
    if not url:
        return
    await _handle_download(update, context, url, is_audio_only=False)


async def _handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_audio_only: bool) -> None:
    chat_id = update.effective_chat.id
    status_message = await update.message.reply_text("Preparing download...")

    loop = asyncio.get_running_loop()
    reporter = ProgressReporter(context.application, chat_id, status_message.message_id, loop)

    def on_status(text: str) -> None:
        reporter.status(text)

    def on_progress(value: float) -> None:
        reporter.progress(value)

    def on_details(text: str) -> None:
        reporter.details(text)

    result = await asyncio.to_thread(
        download_with_callbacks,
        url,
        _get_config(context),
        is_audio_only,
        on_status,
        on_progress,
        on_details,
    )

    if not result.ok:
        error_text = result.error or "Download failed"
        reporter.force_update(f"Error: {error_text}")
        return

    file_path = result.file_path
    if not file_path:
        reporter.force_update("Download finished but file was not found")
        return

    if _file_too_large(file_path, _get_config(context)):
        reporter.force_update(
            f"File is too large for Telegram upload ({file_path.stat().st_size / (1024 * 1024):.1f} MB)."
        )
        return

    reporter.force_update("Uploading file to Telegram...")
    try:
        with open(file_path, "rb") as f:
            input_file = InputFile(f, filename=file_path.name)
            if is_audio_only:
                await context.bot.send_audio(chat_id=chat_id, audio=input_file)
            else:
                ext = file_path.suffix.lower()
                video_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}
                if ext in video_exts:
                    dims = _probe_video_dimensions(file_path)
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=input_file,
                        supports_streaming=True,
                        width=dims[0] if dims else None,
                        height=dims[1] if dims else None,
                    )
                else:
                    await context.bot.send_document(chat_id=chat_id, document=input_file)
    except Exception as e:
        logger.exception(f"Failed to send file: {e}")
        reporter.force_update(f"Failed to send file: {e}")
        return

    reporter.force_update("Done")

    config = _get_config(context)
    if config.cleanup_after_send:
        try:
            file_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup file {file_path}: {e}")
