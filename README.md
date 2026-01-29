# YTSage Telegram Bot

Fork of **oop7/YTSage** adapted to run as a Telegram bot while reusing the original downloader core. This keeps the MIT license and credits the original author.

## Features
- Download YouTube videos via Telegram commands
- Audio-only mode
- Reuses YTSage core download logic and config paths

## Setup
```bash
pip install -r requirements.txt
export YTSAGE_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
python main.py
```

## Commands
- `/download <url>`: download video
- `/audio <url>`: download audio only
- Paste a YouTube URL directly to download video

## Environment Variables
- `YTSAGE_BOT_TOKEN` (required): Telegram bot token
- `YTSAGE_DOWNLOAD_DIR` (optional): download directory (defaults to config or ~/Downloads)
- `YTSAGE_MAX_UPLOAD_MB` (optional): max upload size for Telegram (default: 49)
- `YTSAGE_ALLOWED_CHAT_IDS` (optional): comma-separated chat IDs allowlist
- `YTSAGE_CLEANUP_AFTER_SEND` (optional): delete files after upload (default: true)
- `YTSAGE_DEFAULT_RESOLUTION` (optional): default resolution for video (default: 720)
- `YTSAGE_FORCE_AUDIO_FORMAT` (optional): convert audio on download (default: false)
- `YTSAGE_PREFERRED_AUDIO_FORMAT` (optional): audio format if conversion enabled (default: best)
- `YTSAGE_FORCE_OUTPUT_FORMAT` (optional): force video container format (requires FFmpeg)
- `YTSAGE_PREFERRED_OUTPUT_FORMAT` (optional): output container if forced (default: mp4)
- `YTSAGE_COOKIE_FILE` (optional): path to cookies.txt for age/region restricted content
- `YTSAGE_COOKIES_FROM_BROWSER` (optional): browser cookie source string for yt-dlp (e.g. `chrome`, `chrome:Default`)
- `YTSAGE_JS_RUNTIME` (optional): JS runtime string for yt-dlp (e.g. `deno:/path/to/deno`)
- `YTSAGE_AUTO_SETUP_DENO` (optional): auto-download Deno for JS runtime (default: true)

## Notes
- The bot relies on `yt-dlp` and `ffmpeg` available on PATH or in the YTSage app bin directory.
- Some YouTube videos require a JS runtime or cookies; set the env vars above if you see HTTP 403 errors.
- This repository intentionally removes the GUI entrypoint and focuses on the Telegram interface.

## License
MIT (same as upstream). Original author: **oop7**.
