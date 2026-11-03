"""Read-only GitHub API client for public repository snapshots and ZIP files."""

from __future__ import annotations

import io
import urllib.parse
import zipfile
from typing import Any

import requests

from .archives import parse_repo_url, safe_relative_path
from .config import GITHUB_API, MAX_REPO_BYTES, MAX_ZIP_BYTES
from .errors import FriendlyError
from .models import Snapshot


class GitHub:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "BaleGitHubDownloader/2",
            }
        )
        self.cache: dict[str, Snapshot] = {}
        self.zip_cache: dict[str, bytes] = {}

    def get_json(self, path: str) -> dict[str, Any]:
        try:
            response = self.session.get(GITHUB_API + path, timeout=(10, 30))
        except requests.RequestException:
            raise FriendlyError("اتصال به GitHub برقرار نشد؛ بعداً دوباره تلاش کن.") from None
        if response.status_code == 404:
            raise FriendlyError("ریپازیتوری عمومی پیدا نشد؛ لینک و عمومی‌بودن آن را بررسی کن.")
        if response.status_code in (401, 403, 429):
            raise FriendlyError("دسترسی یا محدودیت درخواست GitHub فعال شده؛ کمی بعد تلاش کن.")
        if not response.ok:
            raise FriendlyError(f"GitHub پاسخ ناموفق داد (HTTP {response.status_code}).")
        try:
            result = response.json()
        except ValueError:
            raise FriendlyError("پاسخ GitHub معتبر نیست.") from None
        if not isinstance(result, dict):
            raise FriendlyError("ساختار پاسخ GitHub معتبر نیست.")
        return result

    def snapshot(self, repo: str) -> Snapshot:
        if parse_repo_url("https://github.com/" + repo) != repo:
            raise FriendlyError("نام ریپازیتوری معتبر نیست.")
        if repo in self.cache:
            return self.cache[repo]
        owner, name = repo.split("/")
        prefix = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        details = self.get_json(prefix)
        if details.get("private") or details.get("visibility") == "private":
            raise FriendlyError("در این نسخه فقط ریپازیتوری‌های عمومی پشتیبانی می‌شوند.")
        branch = details.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise FriendlyError("شاخه پیش‌فرض ریپازیتوری مشخص نیست.")
        commit = self.get_json(prefix + "/commits/" + urllib.parse.quote(branch, safe=""))
        try:
            head = commit["sha"]
            tree_sha = commit["commit"]["tree"]["sha"]
            if not isinstance(head, str) or not isinstance(tree_sha, str):
                raise TypeError
        except (KeyError, TypeError):
            raise FriendlyError("اطلاعات commit این ریپازیتوری کامل نیست.") from None
        tree = self.get_json(prefix + f"/git/trees/{urllib.parse.quote(tree_sha, safe='')}?recursive=1")
        if tree.get("truncated") or not isinstance(tree.get("tree"), list):
            raise FriendlyError("فهرست فایل‌های این ریپازیتوری بیش‌ازحد بزرگ یا نامعتبر است.")
        files: dict[str, str] = {}
        size = 0
        for item in tree["tree"]:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue  # Submodule gitlinks are references, not downloadable files.
            path, sha, blob_size = item.get("path"), item.get("sha"), item.get("size", 0)
            if not isinstance(path, str) or not safe_relative_path(path) or not isinstance(sha, str):
                raise FriendlyError("مسیر فایل‌ها در GitHub معتبر نیست.")
            if isinstance(blob_size, bool) or not isinstance(blob_size, int) or blob_size < 0:
                raise FriendlyError("حجم فایل‌ها قابل بررسی نیست.")
            size += blob_size
            if size > MAX_REPO_BYTES:
                raise FriendlyError("مجموع فایل‌های ریپازیتوری بیشتر از سقف ۵۰ مگابایت است.")
            files[path] = sha
        snapshot = Snapshot(repo=repo, head=head, files=files, total_bytes=size)
        self.cache[repo] = snapshot
        return snapshot

    def download_zip(self, snapshot: Snapshot) -> bytes:
        key = f"{snapshot.repo}@{snapshot.head}"
        if key in self.zip_cache:
            return self.zip_cache[key]
        owner, name = snapshot.repo.split("/")
        url = f"https://codeload.github.com/{owner}/{name}/zip/{urllib.parse.quote(snapshot.head, safe='')}"
        data = io.BytesIO()
        try:
            # Do not forward the hosting repository's token to the download host.
            with requests.get(
                url, timeout=(15, 90), stream=True, headers={"User-Agent": "BaleGitHubDownloader/2"}
            ) as response:
                if not response.ok:
                    raise FriendlyError("دانلود ZIP از GitHub ناموفق بود.")
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    if data.tell() + len(chunk) > MAX_ZIP_BYTES:
                        raise FriendlyError("ZIP از سقف امن آپلود ۴۸ مگابایت بیشتر است.")
                    data.write(chunk)
        except requests.RequestException:
            raise FriendlyError("در دانلود فایل از GitHub مشکلی رخ داد.") from None
        blob = data.getvalue()
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > MAX_REPO_BYTES:
                    raise FriendlyError("حجم استخراج‌شده آرشیو از سقف امن بیشتر است.")
        except (zipfile.BadZipFile, EOFError):
            raise FriendlyError("آرشیو GitHub معتبر نیست.") from None
        self.zip_cache[key] = blob
        return blob
