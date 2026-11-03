"""Backward-compatible entry point; implementation lives in bale_downloader."""

from bale_downloader.__main__ import main
from bale_downloader.archives import build_delta_zip, changes, parse_repo_url, readable_size
from bale_downloader.bale import Bale
from bale_downloader.bot import Bot
from bale_downloader.config import (
    GITHUB_API,
    MAX_REPO_BYTES,
    MAX_REPOS_PER_CHAT,
    MAX_UPDATES_PER_RUN,
    MAX_ZIP_BYTES,
    STATE_BRANCH,
    STATE_FILE,
)
from bale_downloader.errors import FriendlyError
from bale_downloader.github import GitHub
from bale_downloader.models import Snapshot
from bale_downloader.storage import Storage
from bale_downloader.ui import follow_keyboard

__all__ = [
    "Bale",
    "Bot",
    "FriendlyError",
    "GitHub",
    "GITHUB_API",
    "MAX_REPOS_PER_CHAT",
    "MAX_REPO_BYTES",
    "MAX_UPDATES_PER_RUN",
    "MAX_ZIP_BYTES",
    "STATE_BRANCH",
    "STATE_FILE",
    "Snapshot",
    "Storage",
    "build_delta_zip",
    "changes",
    "follow_keyboard",
    "main",
    "parse_repo_url",
    "readable_size",
]

if __name__ == "__main__":
    raise SystemExit(main())
