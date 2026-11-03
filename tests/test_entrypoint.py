from unittest.mock import Mock, patch

import bot as legacy_entrypoint
from bale_downloader.__main__ import main
from bale_downloader.errors import ConfigurationError


def test_legacy_entrypoint_preserves_public_imports():
    assert legacy_entrypoint.Bot is not None
    assert legacy_entrypoint.Bale is not None
    assert legacy_entrypoint.Storage is not None
    assert legacy_entrypoint.main is main
    assert legacy_entrypoint.parse_repo_url("https://github.com/owner/repo") == "owner/repo"


def test_missing_configuration_exits_without_network_or_state_changes():
    with patch("bale_downloader.__main__.Settings.from_env", side_effect=ConfigurationError("missing")):
        with patch("bale_downloader.__main__.Bale") as bale:
            assert main() == 1
    bale.assert_not_called()


def test_webhook_error_is_reported_before_loading_state():
    settings = Mock(reset_webhook=False)
    bale = Mock()
    bale.prepare_polling.side_effect = ConfigurationError("webhook active")
    with patch("bale_downloader.__main__.Settings.from_env", return_value=settings):
        with patch("bale_downloader.__main__.Bale", return_value=bale):
            with patch("bale_downloader.__main__.Storage") as storage:
                assert main() == 1
    storage.assert_not_called()
