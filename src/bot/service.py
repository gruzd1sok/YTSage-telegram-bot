from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.bot.config import BotConfig
from src.core.ytsage_downloader import DownloadCallbacks, DownloadThread
from src.core.ytsage_yt_dlp import check_ytdlp_installed, download_ytdlp
from src.core.ytsage_ffmpeg import check_ffmpeg_installed
from src.core.ytsage_deno import check_deno_installed, download_deno, get_deno_path
from src.utils.ytsage_logger import logger


@dataclass
class DownloadResult:
    ok: bool
    file_path: Optional[Path]
    error: Optional[str]


class DownloadObserver:
    def __init__(self) -> None:
        self.error_message: Optional[str] = None

    def set_error(self, message: str) -> None:
        self.error_message = message


def download_with_callbacks(
    url: str,
    config: BotConfig,
    is_audio_only: bool,
    on_status=None,
    on_progress=None,
    on_details=None,
) -> DownloadResult:
    observer = DownloadObserver()

    if not check_ytdlp_installed():
        try:
            if on_status:
                on_status("yt-dlp not found, downloading...")
            download_ytdlp()
        except Exception as exc:
            return DownloadResult(ok=False, file_path=None, error=f"Failed to set up yt-dlp: {exc}")

    js_runtimes = config.js_runtime
    if not js_runtimes and config.auto_setup_deno:
        try:
            if not check_deno_installed():
                if on_status:
                    on_status("Deno not found, downloading...")
                download_deno()
            deno_path = get_deno_path()
            if isinstance(deno_path, Path):
                js_runtimes = f"deno:{deno_path}"
            else:
                js_runtimes = "deno"
        except Exception as exc:
            logger.warning(f"Failed to set up Deno JS runtime: {exc}")

    callbacks = DownloadCallbacks(
        on_status=on_status,
        on_progress=on_progress,
        on_details=on_details,
        on_error=observer.set_error,
    )

    force_output_format = False
    preferred_output_format = config.preferred_output_format
    format_selector = None
    if not is_audio_only:
        if config.force_output_format and check_ffmpeg_installed():
            force_output_format = True
            if preferred_output_format.lower() == "mp4":
                res_value = config.default_resolution.strip()
                height_filter = ""
                if res_value.isdigit():
                    height_filter = f"[height<={res_value}]"
                format_selector = (
                    f"bestvideo[vcodec~='avc1']{height_filter}+bestaudio[ext=m4a]/"
                    f"best[ext=mp4]/best"
                )
        elif config.force_output_format and on_status:
            on_status("FFmpeg not found; cannot force output format")

    worker = DownloadThread(
        url=url,
        path=config.download_dir,
        format_id="",
        is_audio_only=is_audio_only,
        format_has_audio=False,
        subtitle_langs=None,
        is_playlist=False,
        merge_subs=False,
        enable_sponsorblock=False,
        sponsorblock_categories=None,
        resolution=config.default_resolution,
        playlist_items=None,
        save_description=False,
        embed_chapters=False,
        cookie_file=config.cookie_file,
        browser_cookies=config.browser_cookies,
        rate_limit=None,
        download_section=None,
        force_keyframes=False,
        proxy_url=None,
        geo_proxy_url=None,
        force_output_format=force_output_format,
        preferred_output_format=preferred_output_format,
        format_selector=format_selector,
        force_audio_format=config.force_audio_format if is_audio_only else False,
        preferred_audio_format=config.preferred_audio_format,
        js_runtimes=js_runtimes,
        callbacks=callbacks,
    )

    logger.info(f"Starting download for URL: {url}")
    worker.run()

    if observer.error_message:
        return DownloadResult(ok=False, file_path=None, error=observer.error_message)

    if worker.last_file_path:
        file_path = Path(worker.last_file_path)
        if file_path.exists():
            return DownloadResult(ok=True, file_path=file_path, error=None)

    return DownloadResult(ok=False, file_path=None, error="Download finished but file was not found")
