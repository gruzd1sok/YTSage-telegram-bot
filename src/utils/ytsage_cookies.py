from __future__ import annotations

import inspect
import subprocess
import threading
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Callable, Iterable, Optional

from src.utils.ytsage_logger import logger


DEFAULT_COOKIE_DOMAINS = ("youtube.com", "google.com", "googlevideo.com")
_REFRESH_LOCK = threading.Lock()


@dataclass(frozen=True)
class CookieRefreshResult:
    refreshed: bool
    reason: Optional[str]
    error: Optional[str]


def parse_browser_option(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    raw = value.strip()
    if not raw:
        return None, None
    if ":" in raw:
        browser, profile = raw.split(":", 1)
        browser = browser.strip().lower() or None
        profile = profile.strip() or None
        return browser, profile
    return raw.lower(), None


def _normalize_domain(domain: str) -> str:
    domain = domain.strip()
    if domain.startswith("#HttpOnly_"):
        domain = domain[len("#HttpOnly_") :]
    domain = domain.lstrip(".")
    return domain.lower()


def _domain_matches(domain: str, domain_suffixes: Iterable[str]) -> bool:
    normalized = _normalize_domain(domain)
    for suffix in domain_suffixes:
        suffix_norm = suffix.lstrip(".").lower()
        if normalized == suffix_norm or normalized.endswith(f".{suffix_norm}"):
            return True
    return False


def _cookie_file_has_valid_entries(
    cookie_file: Path,
    domain_suffixes: Iterable[str],
    now_ts: int,
) -> tuple[bool, bool]:
    has_relevant = False
    has_valid = False
    try:
        with cookie_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain = parts[0]
                if domain_suffixes and not _domain_matches(domain, domain_suffixes):
                    continue
                has_relevant = True
                try:
                    expiry = int(parts[4])
                except ValueError:
                    continue
                if expiry == 0 or expiry > now_ts:
                    has_valid = True
                    break
    except Exception as exc:
        logger.warning(f"Failed to read cookies from {cookie_file}: {exc}")
    return has_relevant, has_valid


def is_cookie_file_expired(
    cookie_file: Path,
    max_age_seconds: Optional[int] = None,
    domain_suffixes: Iterable[str] = DEFAULT_COOKIE_DOMAINS,
) -> bool:
    if not cookie_file.exists():
        return True
    if max_age_seconds is not None:
        try:
            age_seconds = time.time() - cookie_file.stat().st_mtime
            if age_seconds > max_age_seconds:
                return True
        except OSError as exc:
            logger.warning(f"Failed to stat cookie file {cookie_file}: {exc}")
            return False
    now_ts = int(time.time())
    has_relevant, has_valid = _cookie_file_has_valid_entries(cookie_file, domain_suffixes, now_ts)
    if not has_relevant:
        return True
    return not has_valid


def _format_refresh_command(command: str, cookie_file: Path, browser: Optional[str], profile: Optional[str]) -> str:
    try:
        return command.format(
            cookie_file=str(cookie_file),
            cookie_path=str(cookie_file),
            browser=browser or "",
            profile=profile or "",
        )
    except Exception:
        return command


def _run_refresh_command(command: str, cookie_file: Path, browser: Optional[str], profile: Optional[str]) -> CookieRefreshResult:
    formatted = _format_refresh_command(command, cookie_file, browser, profile)
    logger.info("Refreshing cookies using custom command")
    try:
        result = subprocess.run(
            formatted,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return CookieRefreshResult(refreshed=False, reason="command", error=str(exc))

    if result.returncode != 0:
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        message = stderr or stdout or f"Command exited with {result.returncode}"
        return CookieRefreshResult(refreshed=False, reason="command", error=message)

    if cookie_file.exists() and cookie_file.stat().st_size > 0:
        return CookieRefreshResult(refreshed=True, reason="command", error=None)

    return CookieRefreshResult(refreshed=False, reason="command", error="Command finished but cookie file is empty")


def _export_cookies_from_browser(
    cookie_file: Path,
    browser: str,
    profile: Optional[str],
    domain_suffixes: Iterable[str],
) -> CookieRefreshResult:
    try:
        import browser_cookie3
    except Exception as exc:
        return CookieRefreshResult(
            refreshed=False,
            reason="browser",
            error=f"browser_cookie3 not available: {exc}",
        )

    loader = getattr(browser_cookie3, browser, None)
    if not loader:
        return CookieRefreshResult(
            refreshed=False,
            reason="browser",
            error=f"Unsupported browser for cookie refresh: {browser}",
        )

    kwargs = {}
    try:
        signature = inspect.signature(loader)
        if profile:
            if "profile" in signature.parameters:
                kwargs["profile"] = profile
            elif "profile_name" in signature.parameters:
                kwargs["profile_name"] = profile
    except Exception:
        pass

    try:
        jar = loader(**kwargs)
    except Exception as exc:
        return CookieRefreshResult(refreshed=False, reason="browser", error=str(exc))

    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    mozilla_jar = MozillaCookieJar(str(cookie_file))
    kept = 0
    for cookie in jar:
        if domain_suffixes and not _domain_matches(cookie.domain, domain_suffixes):
            continue
        mozilla_jar.set_cookie(cookie)
        kept += 1
    try:
        mozilla_jar.save(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        return CookieRefreshResult(refreshed=False, reason="browser", error=str(exc))

    if kept == 0:
        return CookieRefreshResult(
            refreshed=False,
            reason="browser",
            error="No matching cookies found in browser",
        )
    return CookieRefreshResult(refreshed=True, reason="browser", error=None)


def refresh_cookie_file(
    cookie_file: Path,
    refresh_command: Optional[str],
    browser: Optional[str],
    profile: Optional[str],
    domain_suffixes: Iterable[str] = DEFAULT_COOKIE_DOMAINS,
) -> CookieRefreshResult:
    if refresh_command:
        return _run_refresh_command(refresh_command, cookie_file, browser, profile)
    if browser:
        return _export_cookies_from_browser(cookie_file, browser, profile, domain_suffixes)
    return CookieRefreshResult(refreshed=False, reason=None, error="No refresh method configured")


def refresh_cookies_now(
    cookie_file: Optional[Path],
    refresh_command: Optional[str],
    browser_option: Optional[str],
    on_status: Optional[Callable[[str], None]] = None,
) -> CookieRefreshResult:
    if not cookie_file:
        return CookieRefreshResult(refreshed=False, reason=None, error="No cookie file configured")
    browser, profile = parse_browser_option(browser_option)
    if on_status:
        on_status("Refreshing cookies...")
    return refresh_cookie_file(
        cookie_file=cookie_file,
        refresh_command=refresh_command,
        browser=browser,
        profile=profile,
    )


def ensure_fresh_cookies(
    cookie_file: Optional[Path],
    refresh_enabled: bool,
    max_age_seconds: Optional[int],
    refresh_command: Optional[str],
    browser_option: Optional[str],
    on_status: Optional[Callable[[str], None]] = None,
) -> None:
    if not refresh_enabled or not cookie_file:
        return

    if not is_cookie_file_expired(cookie_file, max_age_seconds=max_age_seconds):
        return

    with _REFRESH_LOCK:
        if not is_cookie_file_expired(cookie_file, max_age_seconds=max_age_seconds):
            return

        reason = "expired" if cookie_file.exists() else "missing"
        logger.info(f"Cookie file needs refresh ({reason}): {cookie_file}")
        result = refresh_cookies_now(
            cookie_file=cookie_file,
            refresh_command=refresh_command,
            browser_option=browser_option,
            on_status=on_status,
        )

        if result.refreshed:
            logger.info("Cookie file refreshed successfully")
            return

        logger.warning(f"Cookie refresh failed ({result.reason}): {result.error}")
        if on_status:
            if cookie_file.exists():
                on_status("Cookie refresh failed; using existing cookies")
            else:
                on_status("Cookie refresh failed; proceeding without cookies")
