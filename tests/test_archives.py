import io
import warnings
import zipfile

import pytest

from bale_downloader.archives import build_delta_zip, changes, parse_repo_url, safe_relative_path
from bale_downloader.errors import FriendlyError


def make_zip(files, root="owner-repo-sha"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(f"{root}/{path}", content)
    return output.getvalue()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://www.github.com/owner/repo.git", "owner/repo"),
        ("  <https://github.com/owner/repo/>  ", "owner/repo"),
        ("https://github.com/owner/repo?tab=readme", "owner/repo"),
    ],
)
def test_valid_repository_urls(url, expected):
    assert parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://github.com.evil.test/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/owner",
        "https://token@github.com/owner/repo",
        "https://github.com:444/owner/repo",
        "https://github.com/../repo",
        "https://github.com/owner/..",
        "https://[",
        "owner/repo",
        "file:///owner/repo",
    ],
)
def test_invalid_repository_urls(url):
    assert parse_repo_url(url) is None


def test_changes_include_added_modified_renamed_and_deleted():
    updated, deleted = changes(
        {"same": "a", "modified": "b", "removed": "c", "old-name": "d"},
        {"same": "a", "modified": "e", "added": "f", "new-name": "d"},
    )
    assert updated == ["added", "modified", "new-name"]
    assert deleted == ["old-name", "removed"]


def test_delta_contains_only_changed_files_and_report():
    source = make_zip({"unchanged.txt": "old", "src/app.py": "new", "added.txt": "added"})
    delta = build_delta_zip(source, ["src/app.py", "added.txt"], ["removed.txt"])
    with zipfile.ZipFile(io.BytesIO(delta)) as archive:
        assert set(archive.namelist()) == {"src/app.py", "added.txt", "__changes.txt"}
        assert archive.read("src/app.py") == b"new"
        report = archive.read("__changes.txt").decode()
        assert "removed.txt" in report
        assert "unchanged.txt" not in report


def test_change_report_does_not_overwrite_repository_file():
    source = make_zip({"__changes.txt": "repository content"})
    delta = build_delta_zip(source, ["__changes.txt"], [])
    with zipfile.ZipFile(io.BytesIO(delta)) as archive:
        assert archive.read("__changes.txt") == b"repository content"
        assert "Changed/new files:" in archive.read("__changes_1.txt").decode()


def test_missing_changed_file_fails_without_advancing():
    with pytest.raises(FriendlyError, match="پیدا نشد"):
        build_delta_zip(make_zip({"present": "x"}), ["missing"], [])


def test_duplicate_archive_path_rejected():
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name", category=UserWarning)
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("root/a.txt", "one")
            archive.writestr("root/a.txt", "two")
    with pytest.raises(FriendlyError, match="تکراری"):
        build_delta_zip(output.getvalue(), ["a.txt"], [])


def test_invalid_zip_rejected():
    with pytest.raises(FriendlyError):
        build_delta_zip(b"not a zip", ["a"], [])


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:\\escape", "a\\b", "a\x00b", ""])
def test_unsafe_paths_rejected(path):
    assert not safe_relative_path(path)
    with pytest.raises(FriendlyError):
        build_delta_zip(make_zip({"a": "b"}), [path], [])


def test_oversized_delta_rejected(monkeypatch):
    monkeypatch.setattr("bale_downloader.archives.MAX_ZIP_BYTES", 10)
    with pytest.raises(FriendlyError, match="۴۸"):
        build_delta_zip(make_zip({"a": "x" * 100}), ["a"], [])


def test_oversized_expanded_archive_rejected(monkeypatch):
    monkeypatch.setattr("bale_downloader.archives.MAX_REPO_BYTES", 10)
    with pytest.raises(FriendlyError):
        build_delta_zip(make_zip({"a": "x" * 100}), ["a"], [])
