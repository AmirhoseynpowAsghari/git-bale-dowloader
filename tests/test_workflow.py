from pathlib import Path

import pytest
import yaml

from bale_downloader.config import MAX_UPDATES_PER_RUN, Settings
from bale_downloader.errors import ConfigurationError


def test_only_active_workflow_directory_exists():
    assert Path(".github/workflows/bale-watcher.yml").is_file()
    assert not Path("github/workflows").exists()


def test_workflow_is_valid_and_tests_before_live_cycle():
    workflow = yaml.safe_load(Path(".github/workflows/bale-watcher.yml").read_text())
    assert "workflow_dispatch" in workflow[True]
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = workflow["jobs"]["serve-bale"]["steps"]
    commands = [step.get("run", "") for step in steps]
    assert next(i for i, command in enumerate(commands) if "pytest" in command) < next(
        i for i, command in enumerate(commands) if "python -m bale_downloader" in command
    )
    assert "BALE_BOT_TOKEN" in steps[-1]["env"]


def test_settings_missing_values_are_reported_without_secrets():
    with pytest.raises(ConfigurationError, match="BALE_BOT_TOKEN"):
        Settings.from_env({})


def test_settings_valid():
    settings = Settings.from_env(
        {
            "BALE_BOT_TOKEN": "bale",
            "GITHUB_TOKEN": "github",
            "GITHUB_REPOSITORY": "owner/host",
            "GITHUB_SHA": "abc",
            "BALE_RESET_WEBHOOK": "true",
        }
    )
    assert settings.reset_webhook is True
    assert settings.hosting_repo == "owner/host"


def test_bale_limit_is_supported():
    assert 1 <= MAX_UPDATES_PER_RUN <= 100
