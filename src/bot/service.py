from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, List, Optional

import json
import subprocess
import time

from src.bot.config import BotConfig
from src.core.ytsage_downloader import DownloadCallbacks, DownloadThread
from src.core.ytsage_yt_dlp import check_ytdlp_binary, check_ytdlp_installed, download_ytdlp
from src.core.ytsage_ffmpeg import check_ffmpeg_installed
from src.core.ytsage_deno import check_deno_installed, download_deno, get_deno_path
from src.utils.ytsage_logger import logger
from src.utils.ytsage_cookies import ensure_fresh_cookies, refresh_cookies_now
from src.core.ytsage_yt_dlp import get_yt_dlp_path


@dataclass
class DownloadResult:
    ok: bool
    file_path: Optional[Path]
    error: Optional[str]


@dataclass(frozen=True)
class FormatOption:
    format_id: str
    label: str
    quality_label: str
    is_audio_only: bool
    format_has_audio: bool
    ext: Optional[str]
    filesize: Optional[int]


@dataclass
class FormatListResult:
    ok: bool
    title: Optional[str]
    duration: Optional[str]
    options: List[FormatOption]
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
    format_id: Optional[str] = None,
    format_has_audio: bool = False,
    on_status=None,
    on_progress=None,
    on_details=None,
) -> DownloadResult:
    if not check_ytdlp_installed():
        try:
            if on_status:
                on_status("yt-dlp не найден, загружаю...")
            download_ytdlp()
        except Exception as exc:
            return DownloadResult(ok=False, file_path=None, error=f"Failed to set up yt-dlp: {exc}")

    js_runtimes = config.js_runtime
    if not js_runtimes and config.auto_setup_deno:
        try:
            if not check_deno_installed():
                if on_status:
                    on_status("Deno не найден, загружаю...")
                download_deno()
            deno_path = get_deno_path()
            if isinstance(deno_path, Path):
                js_runtimes = f"deno:{deno_path}"
            else:
                js_runtimes = "deno"
        except Exception as exc:
            logger.warning(f"Failed to set up Deno JS runtime: {exc}")

    ensure_fresh_cookies(
        cookie_file=config.cookie_file,
        refresh_enabled=config.cookie_auto_refresh,
        max_age_seconds=config.cookie_refresh_max_age_seconds,
        refresh_command=config.cookie_refresh_command,
        browser_option=config.browser_cookies,
        on_status=on_status,
    )

    cookie_file = config.cookie_file
    if cookie_file and not cookie_file.exists():
        logger.warning(f"Cookie file does not exist: {cookie_file}")
        cookie_file = None

    def _run_once() -> DownloadResult:
        observer = DownloadObserver()
        callbacks = DownloadCallbacks(
            on_status=on_status,
            on_progress=on_progress,
            on_details=on_details,
            on_error=observer.set_error,
        )

        force_output_format = False
        preferred_output_format = config.preferred_output_format
        format_selector = None
        if not is_audio_only and not format_id:
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
                on_status("FFmpeg не найден — не могу принудительно выставить формат.")

        if format_id:
            logger.info(
                f"Using explicit format id: {format_id} (audio_only={is_audio_only}, has_audio={format_has_audio})"
            )

        worker = DownloadThread(
            url=url,
            path=config.download_dir,
            format_id=format_id or "",
            is_audio_only=is_audio_only,
            format_has_audio=format_has_audio,
            subtitle_langs=None,
            is_playlist=False,
            merge_subs=False,
            enable_sponsorblock=False,
            sponsorblock_categories=None,
            resolution=config.default_resolution,
            playlist_items=None,
            save_description=False,
            embed_chapters=False,
            cookie_file=cookie_file,
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

    result = _run_once()
    if result.ok or not result.error:
        return result

    error_text = result.error.lower()
    invalid_cookie_markers = (
        "cookies are no longer valid",
        "sign in to confirm",
        "use --cookies-from-browser",
        "use --cookies for the authentication",
    )
    if config.cookie_auto_refresh and any(marker in error_text for marker in invalid_cookie_markers):
        logger.warning("Detected invalid cookies; attempting refresh and retry")
        refresh_result = refresh_cookies_now(
            cookie_file=cookie_file,
            refresh_command=config.cookie_refresh_command,
            browser_option=config.browser_cookies,
            on_status=on_status,
        )
        if refresh_result.refreshed:
            if on_status:
                on_status("Cookies обновлены, пробую ещё раз…")
            cookie_file_retry = config.cookie_file
            if cookie_file_retry and not cookie_file_retry.exists():
                cookie_file_retry = None
            cookie_file = cookie_file_retry
            return _run_once()

    return result


def _format_size_bytes(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return int(value)
    except Exception:
        return None
    return None


def _pick_best_by_quality(formats: Iterable[dict], prefer_progressive: bool) -> Optional[dict]:
    candidates = [f for f in formats if f.get("format_id")]
    if not candidates:
        return None

    def _score(fmt: dict) -> tuple:
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        tbr = fmt.get("tbr") or 0
        abr = fmt.get("abr") or 0
        has_audio = fmt.get("acodec") not in (None, "none")
        return (
            1 if (prefer_progressive and has_audio) else 0,
            size,
            tbr,
            abr,
        )

    return max(candidates, key=_score)


def _duration_to_string(duration_seconds: Optional[float]) -> Optional[str]:
    if not duration_seconds:
        return None
    try:
        total = int(duration_seconds)
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"
    except Exception:
        return None


def list_formats(url: str, config: BotConfig, on_status=None) -> FormatListResult:
    if not check_ytdlp_installed():
        try:
            if on_status:
                on_status("yt-dlp not found, downloading...")
            download_ytdlp()
        except Exception as exc:
            logger.exception("Failed to set up yt-dlp for format listing")
            if check_ytdlp_binary():
                logger.warning("Continuing with existing yt-dlp binary despite setup failure")
            else:
                return FormatListResult(
                    ok=False,
                    title=None,
                    duration=None,
                    options=[],
                    error=f"Failed to set up yt-dlp: {exc}",
                )

    ensure_fresh_cookies(
        cookie_file=config.cookie_file,
        refresh_enabled=config.cookie_auto_refresh,
        max_age_seconds=config.cookie_refresh_max_age_seconds,
        refresh_command=config.cookie_refresh_command,
        browser_option=config.browser_cookies,
        on_status=on_status,
    )

    cookie_file = config.cookie_file
    if cookie_file and not cookie_file.exists():
        logger.warning(f"Cookie file does not exist: {cookie_file}")
        cookie_file = None

    yt_dlp_path = get_yt_dlp_path()
    def _run_list(
        cookie_file_path: Optional[Path],
        allow_browser_cookies: bool,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        cmd = [str(yt_dlp_path), "--ignore-config", "--dump-json", "--no-warnings", "--no-playlist", url]
        if cookie_file_path:
            cmd.extend(["--cookies", str(cookie_file_path)])
        elif allow_browser_cookies and config.browser_cookies:
            cmd.extend(["--cookies-from-browser", config.browser_cookies])
        env = os.environ.copy()
        for key in ("YT_DLP_OPTS", "YTDLP_OPTS", "YTDL_OPTS", "YOUTUBE_DL_OPTS", "YOUTUBE_DL_ARGS", "YTDLP_ARGS"):
            env.pop(key, None)
        started_at = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        elapsed = time.monotonic() - started_at
        return result, elapsed

    def _run_list_checked(
        cookie_file_path: Optional[Path],
        allow_browser_cookies: bool,
    ) -> tuple[Optional[subprocess.CompletedProcess[str]], Optional[float], Optional[str]]:
        try:
            result, elapsed = _run_list(cookie_file_path, allow_browser_cookies)
            return result, elapsed, None
        except subprocess.TimeoutExpired:
            logger.exception("yt-dlp timed out while listing formats")
            return None, None, "Timeout while fetching video info"
        except Exception as exc:
            logger.exception("Failed to run yt-dlp for format listing")
            return None, None, f"Failed to run yt-dlp: {exc}"

    cookies_available = bool(cookie_file or config.browser_cookies)
    result, elapsed, run_error = _run_list_checked(None, False)
    if run_error:
        return FormatListResult(
            ok=False,
            title=None,
            duration=None,
            options=[],
            error=run_error,
        )

    used_cookies = False

    if result and result.returncode != 0 and cookies_available:
        error_text = result.stderr.strip().lower()
        auth_markers = (
            "sign in",
            "login required",
            "private",
            "members only",
            "members-only",
            "age restricted",
            "confirm your age",
            "cookies",
            "bot",
            "requested format is not available",
        )
        if any(marker in error_text for marker in auth_markers):
            logger.warning("Format listing failed without cookies; retrying with cookies")
            result, elapsed, run_error = _run_list_checked(cookie_file, True)
            used_cookies = True
            if run_error:
                return FormatListResult(
                    ok=False,
                    title=None,
                    duration=None,
                    options=[],
                    error=run_error,
                )

    if result.returncode != 0:
        error_text = result.stderr.strip()
        logger.error(
            f"yt-dlp format listing failed (code={result.returncode}, elapsed={elapsed:.2f}s): {error_text}"
        )
        invalid_cookie_markers = (
            "cookies are no longer valid",
            "sign in to confirm",
            "use --cookies-from-browser",
            "use --cookies for the authentication",
        )
        if used_cookies and config.cookie_auto_refresh and any(marker in error_text.lower() for marker in invalid_cookie_markers):
            logger.warning("Detected invalid cookies during format listing; attempting refresh and retry")
            refresh_result = refresh_cookies_now(
                cookie_file=cookie_file,
                refresh_command=config.cookie_refresh_command,
                browser_option=config.browser_cookies,
                on_status=on_status,
            )
            if refresh_result.refreshed:
                cookie_file_retry = config.cookie_file
                if cookie_file_retry and not cookie_file_retry.exists():
                    cookie_file_retry = None
                try:
                    result, elapsed = _run_list(cookie_file_retry)
                except subprocess.TimeoutExpired:
                    logger.exception("yt-dlp timed out while listing formats after refresh")
                    return FormatListResult(
                        ok=False,
                        title=None,
                        duration=None,
                        options=[],
                        error="Timeout while fetching video info",
                    )
                except Exception as exc:
                    logger.exception("Failed to run yt-dlp for format listing after refresh")
                    return FormatListResult(
                        ok=False,
                        title=None,
                        duration=None,
                        options=[],
                        error=f"Failed to run yt-dlp: {exc}",
                    )
                if result.returncode != 0:
                    error_text = result.stderr.strip()
                    logger.error(
                        f"yt-dlp format listing failed after refresh (code={result.returncode}, elapsed={elapsed:.2f}s): {error_text}"
                    )
                    return FormatListResult(
                        ok=False,
                        title=None,
                        duration=None,
                        options=[],
                        error=error_text or "yt-dlp failed",
                    )
            else:
                logger.warning(f"Cookie refresh failed during format listing: {refresh_result.error}")
                return FormatListResult(
                    ok=False,
                    title=None,
                    duration=None,
                    options=[],
                    error=error_text or "yt-dlp failed",
                )
        else:
            return FormatListResult(
                ok=False,
                title=None,
                duration=None,
                options=[],
                error=error_text or "yt-dlp failed",
            )

    json_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not json_lines:
        logger.error("yt-dlp returned no JSON data for format listing")
        return FormatListResult(ok=False, title=None, duration=None, options=[], error="No data returned")

    try:
        info = json.loads(json_lines[0])
    except json.JSONDecodeError as exc:
        logger.exception("Failed to parse yt-dlp JSON output")
        return FormatListResult(ok=False, title=None, duration=None, options=[], error=str(exc))

    if info.get("_type") == "playlist":
        logger.info("Playlist detected; not supported in bot flow")
        return FormatListResult(
            ok=False,
            title=None,
            duration=None,
            options=[],
            error="playlist",
        )

    formats = info.get("formats") or []
    if not formats:
        logger.warning("No formats found in yt-dlp output")
        return FormatListResult(
            ok=False,
            title=None,
            duration=None,
            options=[],
            error="No formats found",
        )

    audio_formats = [
        f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none")]

    options: List[FormatOption] = []

    if video_formats:
        heights = sorted({f.get("height") for f in video_formats if f.get("height")}, reverse=True)
        for height in heights:
            candidates = [f for f in video_formats if f.get("height") == height]
            best = _pick_best_by_quality(candidates, prefer_progressive=True)
            if not best:
                continue
            ext = best.get("ext")
            fps = best.get("fps")
            label = f"{height}p"
            if fps:
                try:
                    fps_value = int(round(float(fps)))
                    if fps_value >= 50:
                        label = f"{label} {fps_value}fps"
                except (TypeError, ValueError):
                    pass
            quality_label = label + (f" ({ext})" if ext else "")
            options.append(
                FormatOption(
                    format_id=str(best.get("format_id")),
                    label=label,
                    quality_label=quality_label,
                    is_audio_only=False,
                    format_has_audio=best.get("acodec") not in (None, "none"),
                    ext=ext,
                    filesize=_format_size_bytes(best.get("filesize") or best.get("filesize_approx")),
                )
            )
            if len(options) >= 8:
                break

    if audio_formats:
        best_audio = _pick_best_by_quality(audio_formats, prefer_progressive=False)
        if best_audio:
            ext = best_audio.get("ext")
            abr = best_audio.get("abr") or best_audio.get("tbr")
            label = "Аудио"
            if abr:
                try:
                    abr_value = int(round(float(abr)))
                    label = f"{label} {abr_value}k"
                except (TypeError, ValueError):
                    pass
            quality_label = "Аудио" + (f" ({ext})" if ext else "")
            options.append(
                FormatOption(
                    format_id=str(best_audio.get("format_id")),
                    label=label,
                    quality_label=quality_label,
                    is_audio_only=True,
                    format_has_audio=True,
                    ext=ext,
                    filesize=_format_size_bytes(best_audio.get("filesize") or best_audio.get("filesize_approx")),
                )
            )

    title = info.get("title")
    duration = info.get("duration_string") or _duration_to_string(info.get("duration"))

    if not options:
        return FormatListResult(
            ok=False,
            title=title,
            duration=duration,
            options=[],
            error="No suitable formats",
        )

    return FormatListResult(ok=True, title=title, duration=duration, options=options, error=None)
