import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from src.utils.ytsage_config_manager import ConfigManager
from src.utils.ytsage_logger import logger


ID_SPLIT_RE = re.compile(r"[,\s]+")
DEFAULT_WHITELIST_PATH = Path(__file__).resolve().parents[2] / "whitelist.txt"
DEFAULT_ATTEMPTS_LOG_PATH = Path(__file__).resolve().parents[2] / "attempts.txt"


@dataclass(frozen=True)
class BotConfig:
    token: str
    download_dir: Path
    max_upload_mb: int
    allowed_chat_ids: Optional[Set[int]]
    whitelist_path: Path
    attempts_log_path: Path
    cleanup_after_send: bool
    beta_enabled: bool
    admin_chat_id: Optional[int]
    default_resolution: str
    force_audio_format: bool
    preferred_audio_format: str
    force_output_format: bool
    preferred_output_format: str
    cookie_file: Optional[Path]
    browser_cookies: Optional[str]
    cookie_auto_refresh: bool
    cookie_refresh_max_age_seconds: Optional[int]
    cookie_refresh_command: Optional[str]
    js_runtime: Optional[str]
    auto_setup_deno: bool
    telegram_media_write_timeout: float


def _parse_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid {name} value; using default {default}")
        return default


def _parse_id_tokens(raw: str, source: str) -> Set[int]:
    values = set()
    for part in ID_SPLIT_RE.split(raw):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            logger.warning(f"Invalid user id in {source}: {part}")
    return values


def _parse_int_set(raw: Optional[str]) -> Optional[Set[int]]:
    if not raw:
        return None
    values = _parse_id_tokens(raw, "YTSAGE_ALLOWED_CHAT_IDS")
    return values or None


def _load_whitelist(path: Path) -> Optional[Set[int]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Failed to read whitelist file {path}: {exc}")
        return None
    values = _parse_id_tokens(raw, str(path))
    return values or None


def _default_download_dir() -> Path:
    config_path = ConfigManager.get("download_path")
    if config_path:
        return Path(str(config_path))
    return Path.home() / "Downloads"


def load_config() -> BotConfig:
    token = os.environ.get("YTSAGE_BOT_TOKEN", "").strip()
    download_dir = Path(os.environ.get("YTSAGE_DOWNLOAD_DIR", "").strip() or _default_download_dir())
    download_dir = download_dir.expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    max_upload_mb = int(os.environ.get("YTSAGE_MAX_UPLOAD_MB", "49"))
    whitelist_path_raw = os.environ.get("YTSAGE_WHITELIST_PATH", "").strip()
    whitelist_path = (
        Path(whitelist_path_raw).expanduser().resolve()
        if whitelist_path_raw
        else DEFAULT_WHITELIST_PATH
    )
    allowed_chat_ids_env = _parse_int_set(os.environ.get("YTSAGE_ALLOWED_CHAT_IDS"))
    allowed_chat_ids_file = _load_whitelist(whitelist_path)
    if allowed_chat_ids_env or allowed_chat_ids_file:
        allowed_chat_ids = set()
        if allowed_chat_ids_file:
            allowed_chat_ids |= allowed_chat_ids_file
        if allowed_chat_ids_env:
            allowed_chat_ids |= allowed_chat_ids_env
    else:
        allowed_chat_ids = None
    beta_enabled = os.environ.get("YTSAGE_BETA_ENABLED", "false").lower() in {"1", "true", "yes"}
    admin_chat_id_raw = os.environ.get("YTSAGE_ADMIN_CHAT_ID", "").strip()
    admin_chat_id: Optional[int] = None
    if admin_chat_id_raw:
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError:
            logger.warning("Invalid YTSAGE_ADMIN_CHAT_ID value; ignoring")
    attempts_log_path_raw = os.environ.get("YTSAGE_ATTEMPTS_LOG_PATH", "").strip()
    attempts_log_path = (
        Path(attempts_log_path_raw).expanduser().resolve()
        if attempts_log_path_raw
        else DEFAULT_ATTEMPTS_LOG_PATH
    )
    cleanup_after_send = os.environ.get("YTSAGE_CLEANUP_AFTER_SEND", "true").lower() in {"1", "true", "yes"}

    default_resolution = os.environ.get("YTSAGE_DEFAULT_RESOLUTION", "720").strip() or "720"
    force_audio_format = os.environ.get("YTSAGE_FORCE_AUDIO_FORMAT", "false").lower() in {"1", "true", "yes"}
    preferred_audio_format = os.environ.get("YTSAGE_PREFERRED_AUDIO_FORMAT", "best").strip() or "best"
    force_output_format = os.environ.get("YTSAGE_FORCE_OUTPUT_FORMAT", "false").lower() in {"1", "true", "yes"}
    preferred_output_format = os.environ.get("YTSAGE_PREFERRED_OUTPUT_FORMAT", "mp4").strip() or "mp4"
    cookie_file_raw = os.environ.get("YTSAGE_COOKIE_FILE", "").strip()
    cookie_file = Path(cookie_file_raw).expanduser().resolve() if cookie_file_raw else None
    browser_cookies = os.environ.get("YTSAGE_COOKIES_FROM_BROWSER", "").strip() or None
    cookie_auto_refresh = os.environ.get("YTSAGE_COOKIE_AUTO_REFRESH", "false").lower() in {"1", "true", "yes"}
    cookie_refresh_command = os.environ.get("YTSAGE_COOKIE_REFRESH_COMMAND", "").strip() or None
    cookie_refresh_max_age_raw = os.environ.get("YTSAGE_COOKIE_REFRESH_MAX_AGE_HOURS", "").strip()
    cookie_refresh_max_age_seconds: Optional[int] = None
    if cookie_refresh_max_age_raw:
        try:
            cookie_refresh_max_age_seconds = int(float(cookie_refresh_max_age_raw) * 3600)
        except ValueError:
            logger.warning("Invalid YTSAGE_COOKIE_REFRESH_MAX_AGE_HOURS value; ignoring")
    js_runtime = os.environ.get("YTSAGE_JS_RUNTIME", "").strip() or None
    auto_setup_deno = os.environ.get("YTSAGE_AUTO_SETUP_DENO", "true").lower() in {"1", "true", "yes"}
    telegram_media_write_timeout = _parse_float_env("YTSAGE_TELEGRAM_MEDIA_WRITE_TIMEOUT", 120.0)

    return BotConfig(
        token=token,
        download_dir=download_dir,
        max_upload_mb=max_upload_mb,
        allowed_chat_ids=allowed_chat_ids,
        whitelist_path=whitelist_path,
        attempts_log_path=attempts_log_path,
        cleanup_after_send=cleanup_after_send,
        beta_enabled=beta_enabled,
        admin_chat_id=admin_chat_id,
        default_resolution=default_resolution,
        force_audio_format=force_audio_format,
        preferred_audio_format=preferred_audio_format,
        force_output_format=force_output_format,
        preferred_output_format=preferred_output_format,
        cookie_file=cookie_file,
        browser_cookies=browser_cookies,
        cookie_auto_refresh=cookie_auto_refresh,
        cookie_refresh_max_age_seconds=cookie_refresh_max_age_seconds,
        cookie_refresh_command=cookie_refresh_command,
        js_runtime=js_runtime,
        auto_setup_deno=auto_setup_deno,
        telegram_media_write_timeout=telegram_media_write_timeout,
    )
