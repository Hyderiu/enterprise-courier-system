"""Start the real application with synthetic data and live ordering disabled."""
from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def configure_environment() -> None:
    os.environ["APP_HOST"] = "0.0.0.0"
    os.environ["APP_PORT"] = os.getenv("PORT", "26666")
    os.environ.setdefault("DATABASE_URL", "sqlite:///./demo.db")
    os.environ["DINGTALK_LOGIN_ENABLED"] = "false"
    os.environ["LOCAL_USER_ID"] = "1"
    os.environ["PREVIEW_SAFE_MODE"] = "true"
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["SESSION_COOKIE_SECURE"] = "true"
    os.environ["SF_MOCK_MODE"] = "true"
    os.environ["SF_LIVE_ORDER_ENABLED"] = "false"
    os.environ["SF_CITY_FEATURE_ENABLED"] = "false"
    os.environ["SF_CITY_LIVE_ORDER_ENABLED"] = "false"
    os.environ["DB_MOCK_MODE"] = "true"
    os.environ["DB_LIVE_ORDER_ENABLED"] = "false"
    os.environ["JDL_LIVE_ORDER_ENABLED"] = "false"
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        os.environ["PUBLIC_BASE_URL"] = external_url


def main() -> None:
    configure_environment()
    from scripts.create_demo_data import create_demo_database

    create_demo_database(reset=False)
    # The data generator is also used by local HTTP previews. Restore the
    # secure-cookie setting required by the public HTTPS deployment.
    os.environ["SESSION_COOKIE_SECURE"] = "true"

    from run import main as run_application

    run_application()


if __name__ == "__main__":
    main()
