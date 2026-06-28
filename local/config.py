"""Local MVP settings — read from environment or local/.env (Python 3.9, no deps)."""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    """Load KEY=VALUE pairs from local/.env into os.environ (without overriding)."""
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

# Product bot token (SEPARATE from the dev-comms bot). Required to run the bot.
BOT_TOKEN: str = os.environ.get("LOCAL_BOT_TOKEN", "")
# Owner chat id for admin notifications (optional).
ADMIN_CHAT_ID: int = int(os.environ.get("LOCAL_ADMIN_CHAT_ID", "0") or "0")
# SQLite database file.
DB_PATH: str = os.environ.get("LOCAL_DB_PATH", str(_ROOT / "hsc_local.db"))

PAGE_URL: str = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADLESS: bool = os.environ.get("LOCAL_HEADLESS", "false").lower() == "true"
SCAN_INTERVAL_MINUTES: int = int(os.environ.get("LOCAL_SCAN_INTERVAL_MINUTES", "180"))
# Comma-separated region values to scrape; empty = all. Default Vinnytsia ("2") for quick tests.
SCAN_REGIONS: str = os.environ.get("LOCAL_SCAN_REGIONS", "2")

# Server architecture: Mac scraper pushes results to the server's /ingest with this secret.
INGEST_SECRET: str = os.environ.get("LOCAL_INGEST_SECRET", "")
# Where the Mac scraper pushes scraped data (the server's public API base).
SERVER_INGEST_URL: str = os.environ.get("LOCAL_SERVER_INGEST_URL", "")

# API protection: only clients sending this key may read data (blocks third-party scrapers).
API_KEY: str = os.environ.get("LOCAL_API_KEY", "")
# Per-IP rate limit for the API (requests per minute).
API_RATE_PER_MIN: int = int(os.environ.get("LOCAL_API_RATE_PER_MIN", "120"))

# Periodic "base updated" re-engagement: how often the bot pushes a fresh menu to every user
# (bumps the chat back up Telegram's list). Hours; 0 disables. Default daily.
REFRESH_HOURS: int = int(os.environ.get("LOCAL_REFRESH_HOURS", "24"))

# Hourly main-menu broadcast: at the top of every hour from MENU_HOURS_START..MENU_HOURS_END
# (Kyiv time, inclusive) the bot sends a FRESH main menu to every user and wipes the old
# conversation below it — so the bot stays near the top of Telegram's chat list and the screen
# stays clean. Set MENU_BROADCAST=0 to disable. Supersedes REFRESH_HOURS when enabled.
MENU_BROADCAST: bool = os.environ.get("LOCAL_MENU_BROADCAST", "1") == "1"
MENU_HOURS_START: int = int(os.environ.get("LOCAL_MENU_HOURS_START", "7"))
MENU_HOURS_END: int = int(os.environ.get("LOCAL_MENU_HOURS_END", "21"))
# Send every N hours within the window (counted from MENU_HOURS_START). Default 4 → 07/11/15/19.
MENU_INTERVAL_HOURS: int = int(os.environ.get("LOCAL_MENU_INTERVAL_HOURS", "4"))

# Viber bot (optional second channel). Token from partners.viber.com. Webhook on the server.
VIBER_TOKEN: str = os.environ.get("LOCAL_VIBER_TOKEN", "")
VIBER_WEBHOOK_URL: str = os.environ.get("LOCAL_VIBER_WEBHOOK_URL", "https://34.123.136.171.nip.io/viber/webhook")

# Staging file for moderated DB updates (extension → /stage → admin commits, or auto after N h).
STAGE_PATH: str = os.environ.get("LOCAL_STAGE_PATH", str(_ROOT / "staging.jsonl"))
# Auto-commit staged data older than this many hours (if admin didn't commit manually).
STAGE_AUTOCOMMIT_HOURS: int = int(os.environ.get("LOCAL_STAGE_AUTOCOMMIT_HOURS", "12"))

# Database backend: "sqlite" (local MVP) or "postgres" (Supabase). Switch after migration.
DB_BACKEND: str = os.environ.get("LOCAL_DB_BACKEND", "sqlite")
# Postgres / Supabase connection (used when DB_BACKEND == "postgres").
PG_HOST: str = os.environ.get("LOCAL_PG_HOST", "")
PG_PORT: int = int(os.environ.get("LOCAL_PG_PORT", "5432"))
PG_DB: str = os.environ.get("LOCAL_PG_DB", "postgres")
PG_USER: str = os.environ.get("LOCAL_PG_USER", "")
PG_PASSWORD: str = os.environ.get("LOCAL_PG_PASSWORD", "")
