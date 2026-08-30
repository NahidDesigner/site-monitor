import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every variable Settings reads. Real environment variables intentionally win
# over .env (Coolify injects them that way), so tests must start from a clean
# environment or one test's .env leaks into the next.
APP_ENV_VARS = (
    "SITES_FILE",
    "DATABASE_PATH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SITE_CONCURRENCY",
    "PAGE_CONCURRENCY",
    "ASSET_CONCURRENCY",
    "REQUEST_TIMEOUT",
    "MAX_RETRIES",
    "RETRY_BACKOFF",
    "USER_AGENT",
    "MAX_PAGES_PER_SITE",
    "LOG_LEVEL",
    "DRY_RUN",
    "PORT",
    "HOST",
)


@pytest.fixture(autouse=True)
def clean_app_env():
    saved = {name: os.environ.pop(name, None) for name in APP_ENV_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
