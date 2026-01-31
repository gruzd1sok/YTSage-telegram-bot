import asyncio
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import ContextTypes

from src.bot.config import BotConfig
from src.bot.service import FormatOption, list_formats, download_with_callbacks
from src.utils.ytsage_logger import logger
from src.core.ytsage_ffmpeg import get_ffmpeg_path


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SELECTION_PREFIX = "fmt"
SELECTION_TTL_SECONDS = 60 * 60
BOT_SIGNATURE = "@gruzd_downloader_bot"
TELEGRAM_VIDEO_EXTS = {"mp4", "m4v", "mov"}
BETA_REQUEST_PREFIX = "beta"
WELCOME_MESSAGE = (
    "Привет! Я помогу скачать видео с YouTube.\n\n"
    "Как пользоваться:\n"
    "1) Отправьте ссылку на YouTube.\n"
    "2) Выберите качество в кнопках под сообщением.\n"
    "3) Дождитесь загрузки — я пришлю файл.\n\n"
    "Поддерживаются только отдельные видео (не плейлисты)."
)
BETA_PENDING_MESSAGE = (
    "Сейчас идет бета-тест. Все заявки обрабатываются вручную.\n"
    "Ожидайте решения."
)


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
            status = self._status or "Скачиваю…"
            progress = self._progress
            details = self._details
        parts = [status]
        if progress is not None:
            parts.append(f"Прогресс: {progress:.1f}%")
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


def _attempts_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, object]:
    state = context.application.bot_data.setdefault("attempts_state", {"seen": set(), "loaded": False})
    if not isinstance(state, dict):
        state = {"seen": set(), "loaded": False}
        context.application.bot_data["attempts_state"] = state
    if "seen" not in state or not isinstance(state.get("seen"), set):
        state["seen"] = set()
    if "loaded" not in state:
        state["loaded"] = False
    return state


def _load_attempts_once(state: Dict[str, object], path: Path) -> None:
    if state.get("loaded"):
        return
    state["loaded"] = True
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                seen_ids = state.get("seen")
                if isinstance(seen_ids, set):
                    seen_ids.add(int(line))
            except ValueError:
                logger.warning(f"Invalid attempt id in {path}: {line}")
    except OSError as exc:
        logger.warning(f"Failed to read attempts file {path}: {exc}")


def _record_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    config = _get_config(context)
    attempts_path = config.attempts_log_path
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    attempt_id = user_id if user_id is not None else chat_id
    if attempt_id is None:
        return False

    state = _attempts_state(context)
    _load_attempts_once(state, attempts_path)
    seen_ids = state.get("seen")
    if not isinstance(seen_ids, set):
        seen_ids = set()
        state["seen"] = seen_ids
    if attempt_id in seen_ids:
        return False

    try:
        attempts_path.parent.mkdir(parents=True, exist_ok=True)
        with attempts_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{attempt_id}\n")
    except OSError as exc:
        logger.warning(f"Failed to write attempts file {attempts_path}: {exc}")
        return False

    seen_ids.add(attempt_id)
    return True


def _runtime_allowed_ids(context: ContextTypes.DEFAULT_TYPE) -> set:
    store = context.application.bot_data.setdefault("runtime_allowed_ids", set())
    if not isinstance(store, set):
        store = set()
        context.application.bot_data["runtime_allowed_ids"] = store
    return store


def _combined_allowed_ids(context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> set:
    combined = set()
    if config.allowed_chat_ids:
        combined |= config.allowed_chat_ids
    combined |= _runtime_allowed_ids(context)
    return combined


def _is_admin(update: Update, config: BotConfig) -> bool:
    if not config.admin_chat_id:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return config.admin_chat_id in {user_id, chat_id}


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> bool:
    if not config.beta_enabled:
        return True
    if _is_admin(update, config):
        return True
    allowed_ids = _combined_allowed_ids(context, config)
    if not allowed_ids:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return (user_id in allowed_ids) or (chat_id in allowed_ids)


def _append_to_whitelist(path: Path, user_id: int) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.add(int(line))
                except ValueError:
                    continue
        if user_id in existing:
            return True
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{user_id}\n")
        return True
    except OSError as exc:
        logger.warning(f"Failed to update whitelist file {path}: {exc}")
        return False


def _build_beta_keyboard(user_id: int) -> InlineKeyboardMarkup:
    approve = InlineKeyboardButton(
        "✅ Разрешить", callback_data=f"{BETA_REQUEST_PREFIX}:approve:{user_id}"
    )
    decline = InlineKeyboardButton(
        "❌ Отклонить", callback_data=f"{BETA_REQUEST_PREFIX}:decline:{user_id}"
    )
    return InlineKeyboardMarkup([[approve, decline]])


async def _notify_admin_about_request(update: Update, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> None:
    if not config.admin_chat_id:
        logger.warning("Beta is enabled but YTSAGE_ADMIN_CHAT_ID is not set")
        return
    user = update.effective_user
    if not user:
        return
    username = f"@{user.username}" if user.username else "без username"
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    name_line = f"{full_name} ({username})" if full_name else username
    text = (
        "Новая заявка на доступ к бете.\n"
        f"Пользователь: {name_line}\n"
        f"ID: {user.id}"
    )
    await context.bot.send_message(
        chat_id=config.admin_chat_id,
        text=text,
        reply_markup=_build_beta_keyboard(user.id),
    )


async def _ensure_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE, config: BotConfig) -> bool:
    if not config.beta_enabled:
        return True
    is_new_attempt = _record_attempt(update, context)
    if _is_allowed(update, context, config):
        return True
    message = update.effective_message
    if message:
        await message.reply_text(BETA_PENDING_MESSAGE)
    if is_new_attempt:
        await _notify_admin_about_request(update, context, config)
    return False


def _extract_url(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not host:
        return False
    return host.endswith("youtube.com") or host.endswith("youtu.be")


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


def _resolve_ffmpeg_path() -> Optional[str]:
    ffmpeg_path = get_ffmpeg_path()
    if isinstance(ffmpeg_path, Path):
        return str(ffmpeg_path) if ffmpeg_path.exists() else None
    resolved = shutil.which(str(ffmpeg_path))
    return resolved if resolved else None


def _is_telegram_video_ext(path: Path, option: Optional[dict]) -> bool:
    ext = ""
    if option:
        ext = (option.get("ext") or "").strip().lower()
    if not ext:
        ext = path.suffix.lstrip(".").lower()
    return ext in TELEGRAM_VIDEO_EXTS


def _convert_to_mp4_for_telegram(path: Path, reporter: ProgressReporter) -> Optional[Path]:
    ffmpeg = _resolve_ffmpeg_path()
    if not ffmpeg:
        return None
    target = path.with_suffix(".mp4")
    if target == path:
        return target
    if target.exists():
        target = path.with_name(f"{path.stem}_tg.mp4")
    reporter.force_update("Конвертирую в MP4 для Telegram…")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning(f"FFmpeg conversion failed: {result.stderr.strip()}")
            return None
        if target.exists():
            return target
    except Exception as exc:
        logger.warning(f"FFmpeg conversion error: {exc}")
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, context, config):
        return
    sent = await update.message.reply_text(WELCOME_MESSAGE)
    try:
        await context.bot.pin_chat_message(
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as exc:
        logger.info(f"Could not pin welcome message: {exc}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, context, config):
        return
    url = _extract_url(" ".join(context.args)) or _extract_url(update.message.text)
    if not url:
        await update.message.reply_text("Пришлите ссылку на YouTube.")
        return
    await _handle_url(update, context, url)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, context, config):
        return
    url = _extract_url(" ".join(context.args)) or _extract_url(update.message.text)
    if not url:
        await update.message.reply_text("Пришлите ссылку на YouTube.")
        return
    await _handle_url(update, context, url)


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _get_config(context)
    if not await _ensure_allowed(update, context, config):
        return
    url = _extract_url(update.message.text)
    if not url:
        return
    await _handle_url(update, context, url)


def _selection_store(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, dict]:
    store = context.application.bot_data.setdefault("format_selections", {})
    if not isinstance(store, dict):
        store = {}
        context.application.bot_data["format_selections"] = store
    return store


def _cleanup_selections(store: Dict[str, dict]) -> None:
    now = time.time()
    expired = [key for key, item in store.items() if now - item.get("created_at", now) > SELECTION_TTL_SECONDS]
    for key in expired:
        store.pop(key, None)


def _human_size(value: Optional[int]) -> Optional[str]:
    if value is None or value <= 0:
        return None
    size = float(value)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return None


def _format_error_for_user(error_text: str) -> str:
    normalized = (error_text or "").lower()
    if "playlist" in normalized:
        return "Плейлисты пока не поддерживаются. Пришлите ссылку на конкретное видео."
    if any(marker in normalized for marker in ["invalid url", "unsupported url", "no video found", "invalid"]):
        return "Не похоже на корректную ссылку YouTube. Проверьте URL и попробуйте снова."
    if any(marker in normalized for marker in ["private", "login_required", "sign in", "cookies"]):
        return "Видео требует авторизацию. Попробуйте другое видео или пришлите публичную ссылку."
    if any(marker in normalized for marker in ["age restricted", "confirm your age"]):
        return "Это видео с возрастными ограничениями. Нужен доступ к аккаунту."
    if any(marker in normalized for marker in ["not available in your country", "geo-blocked", "geo blocked"]):
        return "Видео недоступно в вашем регионе."
    if any(marker in normalized for marker in ["live stream", "livestream", "is live"]):
        return "Это прямая трансляция. Попробуйте после окончания эфира."
    if any(marker in normalized for marker in ["timeout", "connection", "network", "unable to download"]):
        return "Проблемы с сетью. Попробуйте снова чуть позже."
    return "Не удалось обработать ссылку. Проверьте, что видео доступно, и попробуйте снова."


def _build_format_keyboard(options: List[FormatOption], selection_id: str) -> InlineKeyboardMarkup:
    rows = []
    for idx, option in enumerate(options):
        label = option.label
        if option.is_audio_only:
            label = f"🎧 {label}"
        else:
            label = f"🎬 {label}"
        data = f"{SELECTION_PREFIX}:{selection_id}:{idx}"
        rows.append([InlineKeyboardButton(label, callback_data=data)])
    return InlineKeyboardMarkup(rows)


async def _handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    chat_id = update.effective_chat.id
    if not _is_youtube_url(url):
        await update.message.reply_text("Поддерживаются только ссылки на YouTube.")
        return
    status_message = await update.message.reply_text("Проверяю ссылку и доступные качества…")

    result = await asyncio.to_thread(list_formats, url, _get_config(context))
    if not result.ok:
        logger.error(f"Format listing failed for {url}: {result.error}")
        await status_message.edit_text(_format_error_for_user(result.error or "Unknown error"))
        return

    options = result.options
    if not options:
        await status_message.edit_text("Не удалось получить варианты качества. Попробуйте другую ссылку.")
        return

    store = _selection_store(context)
    _cleanup_selections(store)
    selection_id = secrets.token_urlsafe(8)
    store[selection_id] = {
        "url": url,
        "options": [option.__dict__ for option in options],
        "owner_id": update.effective_user.id if update.effective_user else None,
        "chat_id": chat_id,
        "message_id": status_message.message_id,
        "created_at": time.time(),
    }

    title_line = f"Видео: {result.title}" if result.title else "Видео найдено"
    duration_line = f"Длительность: {result.duration}" if result.duration else None
    lines = [title_line]
    if duration_line:
        lines.append(duration_line)
    lines.append("Выберите качество:")

    await status_message.edit_text(
        "\n".join(lines),
        reply_markup=_build_format_keyboard(options, selection_id),
    )


async def beta_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != BETA_REQUEST_PREFIX:
        return
    config = _get_config(context)
    if not config.beta_enabled:
        await query.answer("Бета отключена.", show_alert=True)
        return
    if not _is_admin(update, config):
        await query.answer("Недостаточно прав.", show_alert=True)
        return
    action = parts[1]
    try:
        user_id = int(parts[2])
    except ValueError:
        await query.answer("Некорректный ID.", show_alert=True)
        return

    if action == "approve":
        _runtime_allowed_ids(context).add(user_id)
        if config.allowed_chat_ids is not None:
            config.allowed_chat_ids.add(user_id)
        _append_to_whitelist(config.whitelist_path, user_id)
        try:
            await context.bot.send_message(chat_id=user_id, text=WELCOME_MESSAGE)
        except Exception as exc:
            logger.info(f"Failed to send welcome to approved user {user_id}: {exc}")
        await query.answer("Доступ выдан.")
        await query.edit_message_text(f"✅ Доступ выдан пользователю {user_id}.")
        return

    if action == "decline":
        await query.answer("Отклонено.")
        await query.edit_message_text(f"❌ Заявка отклонена для пользователя {user_id}.")
        return

    await query.answer()


async def format_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    config = _get_config(context)
    if config.beta_enabled:
        _record_attempt(update, context)
    if not _is_allowed(update, context, config):
        await query.answer("Доступ к боту ограничен.", show_alert=True)
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != SELECTION_PREFIX:
        return

    selection_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await query.answer()
        await query.edit_message_text("Не удалось обработать выбор. Попробуйте снова.")
        return

    store = _selection_store(context)
    _cleanup_selections(store)
    payload = store.get(selection_id)
    if not payload:
        await query.answer()
        await query.edit_message_text("Этот выбор устарел. Пришлите ссылку заново.")
        return

    owner_id = payload.get("owner_id")
    if owner_id and query.from_user and query.from_user.id != owner_id:
        await query.answer("Эта кнопка не для вас.", show_alert=True)
        return

    options = payload.get("options") or []
    if idx < 0 or idx >= len(options):
        await query.answer()
        await query.edit_message_text("Не удалось обработать выбор. Пришлите ссылку заново.")
        return

    option = options[idx]
    store.pop(selection_id, None)

    option_label = option.get("quality_label") or option.get("label") or "Выбранное качество"
    size_hint = _human_size(option.get("filesize"))
    eta_hint = f"Оценка размера: {size_hint}." if size_hint else None

    status_lines = [f"Скачивание началось: {option_label}."]
    status_lines.append("Обычно это занимает несколько минут. ETA появится при старте загрузки.")
    if eta_hint:
        status_lines.append(eta_hint)

    await query.answer()
    await query.edit_message_text("\n".join(status_lines))

    chat_id = payload.get("chat_id") or (query.message.chat_id if query.message else None)
    message_id = payload.get("message_id") or (query.message.message_id if query.message else None)
    url = payload.get("url")
    if chat_id is None or message_id is None or not url:
        await query.edit_message_text("Не удалось запустить загрузку. Пришлите ссылку заново.")
        return

    logger.info(f"Starting download for chat {chat_id} with format {option.get('format_id')} ({option_label})")

    await _handle_download(
        context=context,
        chat_id=chat_id,
        status_message_id=message_id,
        url=url,
        option=option,
    )


async def _handle_download(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message_id: int,
    url: str,
    option: dict,
) -> None:
    loop = asyncio.get_running_loop()
    reporter = ProgressReporter(context.application, chat_id, status_message_id, loop)
    config = _get_config(context)

    def on_status(text: str) -> None:
        reporter.status(text)

    def on_progress(value: float) -> None:
        reporter.progress(value)

    def on_details(text: str) -> None:
        reporter.details(text)

    result = await asyncio.to_thread(
        download_with_callbacks,
        url,
        config,
        bool(option.get("is_audio_only")),
        option.get("format_id"),
        bool(option.get("format_has_audio")),
        on_status,
        on_progress,
        on_details,
    )

    if not result.ok:
        error_text = result.error or "Download failed"
        logger.error(f"Download failed for {url}: {error_text}")
        reporter.force_update(_format_error_for_user(error_text))
        return

    file_path = result.file_path
    if not file_path:
        reporter.force_update("Загрузка завершилась, но файл не найден.")
        return

    if _file_too_large(file_path, config):
        reporter.force_update(
            f"Файл слишком большой для загрузки в Telegram ({file_path.stat().st_size / (1024 * 1024):.1f} MB)."
        )
        return

    reporter.force_update("Загружаю файл в Telegram…")
    caption = f"{BOT_SIGNATURE}\nКачество: {option.get('quality_label') or option.get('label')}"

    async def _send_audio(path: Path) -> None:
        with open(path, "rb") as f:
            input_file = InputFile(f, filename=path.name)
            await context.bot.send_audio(chat_id=chat_id, audio=input_file, caption=caption)

    async def _send_video(path: Path) -> None:
        dims = _probe_video_dimensions(path)
        with open(path, "rb") as f:
            input_file = InputFile(f, filename=path.name)
            await context.bot.send_video(
                chat_id=chat_id,
                video=input_file,
                supports_streaming=True,
                width=dims[0] if dims else None,
                height=dims[1] if dims else None,
                caption=caption,
            )

    async def _send_document(path: Path) -> None:
        with open(path, "rb") as f:
            input_file = InputFile(f, filename=path.name)
            await context.bot.send_document(chat_id=chat_id, document=input_file, caption=caption)

    cleanup_paths = [file_path]
    send_path = file_path
    if not option.get("is_audio_only"):
        if not _is_telegram_video_ext(file_path, option):
            converted = _convert_to_mp4_for_telegram(file_path, reporter)
            if converted and converted != file_path:
                cleanup_paths.append(converted)
                send_path = converted

    if _file_too_large(send_path, config):
        reporter.force_update(
            f"Файл слишком большой для загрузки в Telegram ({send_path.stat().st_size / (1024 * 1024):.1f} MB)."
        )
        return

    try:
        if option.get("is_audio_only"):
            await _send_audio(send_path)
        else:
            try:
                await _send_video(send_path)
            except Exception as send_video_error:
                logger.warning(
                    f"send_video failed for {send_path.name}, fallback to send_document: {send_video_error}"
                )
                await _send_document(send_path)
    except Exception as e:
        logger.exception(f"Failed to send file: {e}")
        reporter.force_update("Не удалось отправить файл в Telegram. Попробуйте ещё раз.")
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=status_message_id)
    except Exception as exc:
        logger.info(f"Failed to delete status message: {exc}")
        reporter.force_update("Готово ✅")

    if config.cleanup_after_send:
        for path in cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to cleanup file {path}: {e}")
