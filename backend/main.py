"""
Main entry point — starts the API server and optionally auto-starts all enabled voters.

Usage:
    python -m backend.main                   # API only, start engines via /bot/start-all
    python -m backend.main --autostart       # API + auto-start all enabled voters
"""
import argparse
import logging
import os
import uvicorn

from backend.config import API_HOST, API_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="CurationBot")
    parser.add_argument("--autostart", action="store_true", help="Auto-start all enabled voters on boot")
    args = parser.parse_args()

    if args.autostart:
        from backend.services.bot_manager import BotManager
        result = BotManager().start_all_enabled()
        logger.info(f"Auto-start result: {result}")

    reload = os.getenv("RELOAD", "false").lower() in ("1", "true", "yes")
    uvicorn.run("backend.app:app", host=API_HOST, port=API_PORT, reload=reload)


if __name__ == "__main__":
    main()
