"""Run one bounded polling/delivery cycle (python -m bale_downloader)."""

from __future__ import annotations

import logging
import sys

from .bale import Bale
from .bot import Bot
from .config import Settings
from .errors import FriendlyError
from .github import GitHub
from .storage import Storage


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        settings = Settings.from_env()
        bale = Bale(settings.bale_token)
        bale.prepare_polling(reset_webhook=settings.reset_webhook)
        storage = Storage(
            settings.github_token, settings.hosting_repo, settings.bale_token, settings.github_sha
        )
        state = storage.load()
        app = Bot(GitHub(settings.github_token), bale, storage, state)
        app.process_updates()
        app.deliver_updates()
    except (FriendlyError, RuntimeError) as exc:
        # These messages are deliberately credential-safe; never log request URLs.
        logging.error("%s", exc)
        return 1
    logging.info("Cycle complete: %d chat(s) in state.", len(state.get("chats", {})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
