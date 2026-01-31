import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import requests

from src.core.ytsage_ffmpeg import get_file_sha256
from src.utils.ytsage_constants import (
    OS_NAME,
    SUBPROCESS_CREATIONFLAGS,
    YTDLP_APP_BIN_PATH,
    YTDLP_DOWNLOAD_URL,
    YTDLP_SHA256_URL,
)
from src.utils.ytsage_logger import logger


def _curl_path() -> Optional[str]:
    return shutil.which("curl")


def _download_text_with_curl(url: str, timeout: int = 30) -> str:
    curl = _curl_path()
    if not curl:
        raise RuntimeError("curl is not available")
    result = subprocess.run(
        [curl, "-fL", url],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr.strip()}")
    return result.stdout


def _download_file_with_curl(url: str, dest: Path, timeout: int = 120) -> None:
    curl = _curl_path()
    if not curl:
        raise RuntimeError("curl is not available")
    result = subprocess.run(
        [curl, "-fL", "-o", str(dest), url],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr.strip()}")


def verify_ytdlp_sha256(file_path: Path, download_url: str) -> bool:
    """
    Verify yt-dlp file SHA256 hash against official checksums.

    Args:
        file_path: Path to the downloaded yt-dlp file
        download_url: The URL used to download the file (to determine the filename)

    Returns:
        bool: True if verification successful, False otherwise
    """
    try:
        logger.info(f"Downloading SHA256 checksums from: {YTDLP_SHA256_URL}")
        try:
            response = requests.get(YTDLP_SHA256_URL, timeout=10)
            response.raise_for_status()
            checksum_content = response.text
        except requests.RequestException as exc:
            logger.warning(f"Failed to download checksums via requests: {exc}")
            try:
                checksum_content = _download_text_with_curl(YTDLP_SHA256_URL, timeout=30)
            except Exception as curl_exc:
                logger.error(f"Failed to download checksums via curl: {curl_exc}")
                return False

        filename = download_url.split("/")[-1]
        logger.info(f"Looking for checksum for file: {filename}")

        expected_hash = None
        for line in checksum_content.strip().split("\n"):
            if filename in line:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == filename:
                    expected_hash = parts[0]
                    break

        if not expected_hash:
            logger.error(f"Could not find SHA256 hash for {filename} in checksums file")
            return False

        logger.info("Calculating SHA256 hash of downloaded file...")
        actual_hash = get_file_sha256(file_path)

        if actual_hash.lower() == expected_hash.lower():
            logger.info("SHA256 verification successful")
            return True

        logger.error("SHA256 verification failed")
        logger.error(f"Expected: {expected_hash}")
        logger.error(f"Actual:   {actual_hash}")
        return False
    except requests.RequestException as e:
        logger.error(f"Failed to download SHA256 checksums: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error during SHA256 verification: {e}")
        return False


def download_ytdlp(progress_callback: Optional[Callable[[int], None]] = None) -> Path:
    """Download yt-dlp binary into the app bin directory with optional progress callbacks."""
    exe_path = YTDLP_APP_BIN_PATH
    logger.info(f"Downloading yt-dlp from: {YTDLP_DOWNLOAD_URL}")

    try:
        response = requests.get(YTDLP_DOWNLOAD_URL, stream=True, timeout=30)
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        block_size = 1024

        if total_size == 0 and progress_callback:
            progress_callback(100)

        with open(exe_path, "wb") as f:
            downloaded = 0
            for data in response.iter_content(block_size):
                f.write(data)
                downloaded += len(data)
                if total_size > 0 and progress_callback:
                    progress_callback(int(downloaded / total_size * 100))
    except requests.RequestException as exc:
        logger.warning(f"Failed to download yt-dlp via requests: {exc}")
        if exe_path.exists():
            exe_path.unlink()
        _download_file_with_curl(YTDLP_DOWNLOAD_URL, exe_path, timeout=120)
        if progress_callback:
            progress_callback(100)

    logger.info("Download complete, verifying SHA256 hash...")
    if not verify_ytdlp_sha256(exe_path, YTDLP_DOWNLOAD_URL):
        logger.error("SHA256 verification failed, removing downloaded file")
        if exe_path.exists():
            exe_path.unlink()
        raise RuntimeError("SHA256 verification failed for yt-dlp")

    if OS_NAME != "Windows":
        os.chmod(exe_path, 0o755)

    logger.info("yt-dlp downloaded and verified successfully")
    return exe_path


def check_ytdlp_binary() -> Optional[Path]:
    """Check if yt-dlp binary exists in the app's bin directory."""
    exe_path = YTDLP_APP_BIN_PATH
    if exe_path.exists():
        if OS_NAME != "Windows" and not os.access(exe_path, os.X_OK):
            try:
                os.chmod(exe_path, 0o755)
                logger.info(f"Fixed permissions on yt-dlp at {exe_path}")
            except Exception as e:
                logger.exception(f"Could not set executable permissions on {exe_path}: {e}")
        logger.info(f"Found yt-dlp in app bin directory: {exe_path}")
        return exe_path

    logger.warning(f"yt-dlp binary not found in app bin directory: {exe_path}")
    return None


def check_ytdlp_installed() -> bool:
    """Check if yt-dlp is installed and accessible."""
    try:
        ytdlp_path = check_ytdlp_binary()
        if ytdlp_path:
            result = subprocess.run(
                [str(ytdlp_path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=SUBPROCESS_CREATIONFLAGS,
            )
            return result.returncode == 0
        # Fallback: check system PATH
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_yt_dlp_path() -> Path:
    """Get the yt-dlp path from the app bin directory or fall back to PATH."""
    ytdlp_path = check_ytdlp_binary()
    if ytdlp_path:
        logger.info(f"Using yt-dlp from: {ytdlp_path}")
        return ytdlp_path

    logger.info("yt-dlp not found in app directory, falling back to command name")
    return "yt-dlp"  # type: ignore[return-value]
