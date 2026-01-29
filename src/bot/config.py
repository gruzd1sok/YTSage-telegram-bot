import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from src.utils.ytsage_config_manager import ConfigManager
from src.utils.ytsage_logger import logger


@dataclass(frozen=True)
class BotConfig:
    token: str
    download_dir: Path
    max_upload_mb: int
    allowed_chat_ids: Optional[Set[int]]
    cleanup_after_send: bool
    default_resolution: str
    force_audio_format: bool
    preferred_audio_format: str
    force_output_format: bool
    preferred_output_format: str
    cookie_file: Optional[Path]
    browser_cookies: Optional[str]
    js_runtime: Optional[str]
    auto_setup_deno: bool


def _parse_int_set(raw: Optional[str]) -> Optional[Set[int]]:
    if not raw:
        return None
    values = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            logger.warning(f"Invalid chat id in YTSAGE_ALLOWED_CHAT_IDS: {part}")
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
    allowed_chat_ids = _parse_int_set(os.environ.get("YTSAGE_ALLOWED_CHAT_IDS"))
    cleanup_after_send = os.environ.get("YTSAGE_CLEANUP_AFTER_SEND", "true").lower() in {"1", "true", "yes"}

    default_resolution = os.environ.get("YTSAGE_DEFAULT_RESOLUTION", "720").strip() or "720"
    force_audio_format = os.environ.get("YTSAGE_FORCE_AUDIO_FORMAT", "false").lower() in {"1", "true", "yes"}
    preferred_audio_format = os.environ.get("YTSAGE_PREFERRED_AUDIO_FORMAT", "best").strip() or "best"
    force_output_format = os.environ.get("YTSAGE_FORCE_OUTPUT_FORMAT", "false").lower() in {"1", "true", "yes"}
    preferred_output_format = os.environ.get("YTSAGE_PREFERRED_OUTPUT_FORMAT", "mp4").strip() or "mp4"
    cookie_file_raw = os.environ.get("YTSAGE_COOKIE_FILE", "").strip()
    cookie_file = Path(cookie_file_raw).expanduser().resolve() if cookie_file_raw else None
    browser_cookies = os.environ.get("YTSAGE_COOKIES_FROM_BROWSER", "").strip() or None
    js_runtime = os.environ.get("YTSAGE_JS_RUNTIME", "").strip() or None
    auto_setup_deno = os.environ.get("YTSAGE_AUTO_SETUP_DENO", "true").lower() in {"1", "true", "yes"}

    return BotConfig(
        token=token,
        download_dir=download_dir,
        max_upload_mb=max_upload_mb,
        allowed_chat_ids=allowed_chat_ids,
        cleanup_after_send=cleanup_after_send,
        default_resolution=default_resolution,
        force_audio_format=force_audio_format,
        preferred_audio_format=preferred_audio_format,
        force_output_format=force_output_format,
        preferred_output_format=preferred_output_format,
        cookie_file=cookie_file,
        browser_cookies=browser_cookies,
        js_runtime=js_runtime,
        auto_setup_deno=auto_setup_deno,
    )
