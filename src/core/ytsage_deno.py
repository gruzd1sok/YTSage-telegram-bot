import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Union

import requests

from src.core.ytsage_ffmpeg import get_file_sha256
from src.utils.ytsage_constants import (
    APP_BIN_DIR,
    DENO_APP_BIN_PATH,
    DENO_DOWNLOAD_URL,
    DENO_SHA256_URL,
    OS_NAME,
    SUBPROCESS_CREATIONFLAGS,
)
from src.utils.ytsage_logger import logger


def verify_deno_sha256(file_path: Path, sha256_url: str) -> bool:
    """Verify Deno file SHA256 hash against official checksums."""
    try:
        logger.info(f"Downloading SHA256 checksum from: {sha256_url}")
        response = requests.get(sha256_url, timeout=10)
        response.raise_for_status()
        checksum_content = response.text

        expected_hash = None
        for line in checksum_content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            if len(line) >= 64 and (" " in line or "\t" in line):
                potential_hash = line.split()[0]
                if len(potential_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in potential_hash):
                    expected_hash = potential_hash
                    break

            if line.startswith("Hash"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    expected_hash = parts[1].strip()
                    break

        if not expected_hash:
            logger.error("Could not find SHA256 hash in checksum file")
            logger.debug(f"Checksum file content: {checksum_content}")
            return False

        actual_hash = get_file_sha256(file_path)
        if actual_hash.lower() == expected_hash.lower():
            logger.info("SHA256 verification successful")
            return True

        logger.error("SHA256 verification failed")
        logger.error(f"Expected: {expected_hash}")
        logger.error(f"Actual:   {actual_hash}")
        return False
    except requests.RequestException as e:
        logger.error(f"Failed to download SHA256 checksum: {e}")
        return False
    except Exception as e:
        logger.exception(f"Error during SHA256 verification: {e}")
        return False


def download_deno() -> Path:
    """Download and extract Deno into the app bin directory."""
    temp_zip_path = None
    try:
        temp_zip_fd, temp_zip_path = tempfile.mkstemp(suffix=".zip")
        os.close(temp_zip_fd)

        logger.info(f"Downloading Deno from: {DENO_DOWNLOAD_URL}")
        response = requests.get(DENO_DOWNLOAD_URL, stream=True, timeout=30)
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        block_size = 8192

        with open(temp_zip_path, "wb") as f:
            downloaded = 0
            for data in response.iter_content(block_size):
                f.write(data)
                downloaded += len(data)
                if total_size > 0 and downloaded % (block_size * 128) == 0:
                    percent = int(downloaded / total_size * 100)
                    logger.debug(f"Deno download {percent}%")

        logger.info("Download complete, verifying SHA256 hash...")
        if not verify_deno_sha256(Path(temp_zip_path), DENO_SHA256_URL):
            logger.error("SHA256 verification failed, removing downloaded file")
            if Path(temp_zip_path).exists():
                Path(temp_zip_path).unlink()
            raise RuntimeError("SHA256 verification failed for Deno")

        logger.info("Extracting Deno executable...")
        with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
            executable_name = "deno.exe" if OS_NAME == "Windows" else "deno"
            if executable_name not in zip_ref.namelist():
                raise RuntimeError(f"Executable '{executable_name}' not found in zip")
            zip_ref.extract(executable_name, APP_BIN_DIR)

        exe_path = DENO_APP_BIN_PATH
        if not exe_path.exists():
            raise RuntimeError("Extraction failed: Deno binary missing")

        if OS_NAME != "Windows":
            os.chmod(exe_path, 0o755)

        if temp_zip_path and Path(temp_zip_path).exists():
            Path(temp_zip_path).unlink()

        logger.info("Deno downloaded, verified, and extracted successfully")
        return exe_path
    except Exception as e:
        logger.exception(f"Error downloading/extracting Deno: {e}")
        if temp_zip_path and Path(temp_zip_path).exists():
            Path(temp_zip_path).unlink()
        raise


def check_deno_binary() -> Optional[Path]:
    """Check if Deno binary exists in the app's bin directory."""
    exe_path = DENO_APP_BIN_PATH
    if exe_path.exists():
        if OS_NAME != "Windows" and not os.access(exe_path, os.X_OK):
            try:
                os.chmod(exe_path, 0o755)
                logger.info(f"Fixed permissions on Deno at {exe_path}")
            except Exception as e:
                logger.exception(f"Could not set executable permissions on {exe_path}: {e}")
        logger.info(f"Found Deno in app bin directory: {exe_path}")
        return exe_path

    logger.warning(f"Deno binary not found in app bin directory: {exe_path}")
    return None


def check_deno_installed() -> bool:
    """Check if Deno is installed and accessible."""
    try:
        deno_path = check_deno_binary() or "deno"
        result = subprocess.run(
            [str(deno_path), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=SUBPROCESS_CREATIONFLAGS,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_deno_path() -> Union[Path, str]:
    """Get the Deno path from the app bin directory or fall back to PATH."""
    deno_path = check_deno_binary()
    if deno_path:
        logger.info(f"Using Deno from: {deno_path}")
        return deno_path
    logger.info("Deno not found in app directory, falling back to command name")
    return "deno"


def get_deno_version_direct(deno_path: Optional[str] = None) -> str:
    """Get the version of Deno directly from the binary."""
    try:
        deno_cmd = deno_path or get_deno_path()
        result = subprocess.run(
            [str(deno_cmd), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            creationflags=SUBPROCESS_CREATIONFLAGS,
            check=False,
        )
        if result.returncode != 0:
            return "Unknown"
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        if first_line.startswith("deno "):
            return first_line.split()[1]
        return first_line.strip() or "Unknown"
    except Exception as e:
        logger.exception(f"Error getting Deno version: {e}")
        return "Unknown"


def get_latest_deno_version() -> Optional[str]:
    """Fetch the latest Deno version from GitHub API."""
    try:
        response = requests.get("https://api.github.com/repos/denoland/deno/releases/latest", timeout=10)
        response.raise_for_status()
        data = response.json()
        version = data.get("tag_name", "").lstrip("v")
        if version:
            logger.info(f"Latest Deno version: {version}")
            return version
        return None
    except requests.RequestException as e:
        logger.error(f"Failed to fetch latest Deno version: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error fetching Deno version: {e}")
        return None


def compare_deno_versions(current: str, latest: str) -> bool:
    """Return True if latest is newer than current."""
    try:
        current_tuple = tuple(int(part) for part in current.split("."))
        latest_tuple = tuple(int(part) for part in latest.split("."))
        return latest_tuple > current_tuple
    except Exception as e:
        logger.warning(f"Could not compare Deno versions: {e}")
        return False


def upgrade_deno() -> tuple[bool, str]:
    """Upgrade Deno to the latest version using 'deno upgrade'."""
    try:
        deno_path = get_deno_path()
        if isinstance(deno_path, Path) and not deno_path.exists():
            return False, f"Deno binary not found at: {deno_path}"

        logger.info(f"Upgrading Deno using: {deno_path}")
        result = subprocess.run(
            [str(deno_path), "upgrade"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            creationflags=SUBPROCESS_CREATIONFLAGS,
            check=False,
        )
        if result.returncode == 0:
            return True, "Deno upgrade successful"
        return False, f"Deno upgrade failed with code {result.returncode}: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "Deno upgrade timed out after 5 minutes"
    except Exception as e:
        return False, f"Error upgrading Deno: {str(e)}"


def check_deno_update() -> tuple[bool, str, str]:
    """Check if a Deno update is available."""
    current_version = get_deno_version_direct()
    latest_version = get_latest_deno_version() or ""
    if not latest_version:
        return False, current_version, ""

    is_update_available = compare_deno_versions(current_version, latest_version)
    return is_update_available, current_version, latest_version
