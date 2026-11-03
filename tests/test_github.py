import io
import zipfile
from unittest.mock import Mock, patch

import pytest
import requests

from bale_downloader.errors import FriendlyError
from bale_downloader.github import GitHub
from bale_downloader.models import Snapshot


def response(data=None, status=200):
    resp = Mock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = data
    return resp


@pytest.fixture
def client():
    client = GitHub("SECRET")
    client.session = Mock()
    return client


def test_snapshot_uses_default_branch_and_caches(client):
    client.session.get.side_effect = [
        response({"private": False, "default_branch": "main"}),
        response({"sha": "head", "commit": {"tree": {"sha": "tree"}}}),
        response(
            {
                "tree": [
                    {"type": "blob", "path": "a.txt", "sha": "a", "size": 10},
                    {"type": "tree", "path": "src"},
                    {"type": "commit", "path": "submodule", "sha": "sub"},
                ]
            }
        ),
    ]
    snapshot = client.snapshot("owner/repo")
    assert snapshot == Snapshot("owner/repo", "head", {"a.txt": "a"}, 10)
    assert client.snapshot("owner/repo") is snapshot
    assert client.session.get.call_count == 3


def test_private_repo_rejected(client):
    client.session.get.return_value = response({"private": True})
    with pytest.raises(FriendlyError, match="عمومی"):
        client.snapshot("owner/private")


def test_truncated_tree_rejected(client):
    client.session.get.side_effect = [
        response({"default_branch": "main"}),
        response({"sha": "head", "commit": {"tree": {"sha": "tree"}}}),
        response({"truncated": True, "tree": []}),
    ]
    with pytest.raises(FriendlyError):
        client.snapshot("owner/repo")


def test_exact_fifty_mb_limit_is_allowed(client, monkeypatch):
    monkeypatch.setattr("bale_downloader.github.MAX_REPO_BYTES", 50)
    client.session.get.side_effect = [
        response({"default_branch": "main"}),
        response({"sha": "head", "commit": {"tree": {"sha": "tree"}}}),
        response({"tree": [{"type": "blob", "path": "a", "sha": "a", "size": 50}]}),
    ]
    assert client.snapshot("owner/repo").total_bytes == 50


def test_rate_limit_is_friendly(client):
    client.session.get.return_value = response(status=429)
    with pytest.raises(FriendlyError, match="محدودیت"):
        client.get_json("/repos/owner/repo")


def test_network_error_is_secret_safe(client):
    client.session.get.side_effect = requests.ConnectionError("SECRET")
    with pytest.raises(FriendlyError) as error:
        client.get_json("/repos/owner/repo")
    assert "SECRET" not in str(error.value)


def test_zip_download_is_bounded(monkeypatch):
    monkeypatch.setattr("bale_downloader.github.MAX_ZIP_BYTES", 3)
    resp = Mock()
    resp.ok = True
    resp.iter_content.return_value = [b"1234"]
    entered = Mock()
    entered.__enter__ = Mock(return_value=resp)
    entered.__exit__ = Mock(return_value=False)
    with patch("bale_downloader.github.requests.get", return_value=entered) as get:
        with pytest.raises(FriendlyError):
            GitHub("SECRET").download_zip(Snapshot("owner/repo", "head", {}, 0))
    assert "headers" in get.call_args.kwargs
    assert "Authorization" not in get.call_args.kwargs["headers"]


def test_zip_download_validates_and_caches():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("root/a", "content")
    resp = Mock(ok=True)
    resp.iter_content.return_value = [data.getvalue()]
    context = Mock()
    context.__enter__ = Mock(return_value=resp)
    context.__exit__ = Mock(return_value=False)
    client = GitHub("SECRET")
    snapshot = Snapshot("owner/repo", "head", {}, 7)
    with patch("bale_downloader.github.requests.get", return_value=context) as get:
        assert client.download_zip(snapshot) == data.getvalue()
        assert client.download_zip(snapshot) == data.getvalue()
    assert get.call_count == 1
