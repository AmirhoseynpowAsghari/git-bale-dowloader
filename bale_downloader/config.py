"""Deployment limits and validated environment configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ConfigurationError

MAX_REPO_BYTES = 50_000_000
MAX_ZIP_BYTES = 48_000_000
MAX_UPDATES_PER_RUN = 100
MAX_UPDATE_BATCHES = 3
MAX_REPOS_PER_CHAT = 5
STATE_BRANCH = "bale-interactive-state"
STATE_FILE = "state.enc"
STATE_VERSION = 2
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class Settings:
    bale_token: str
    github_token: str
    hosting_repo: str
    github_sha: str
    reset_webhook: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        required = ("BALE_BOT_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_SHA")
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise ConfigurationError("Missing configuration: " + ", ".join(missing))
        reset = values.get("BALE_RESET_WEBHOOK", "").strip().lower()
        if reset not in ("", "true", "false", "1", "0"):
            raise ConfigurationError("BALE_RESET_WEBHOOK must be true or false.")
        return cls(
            bale_token=values["BALE_BOT_TOKEN"].strip(),
            github_token=values["GITHUB_TOKEN"].strip(),
            hosting_repo=values["GITHUB_REPOSITORY"].strip(),
            github_sha=values["GITHUB_SHA"].strip(),
            reset_webhook=reset in ("true", "1"),
        )
