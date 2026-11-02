"""Send a full GitHub snapshot on first run, then only file changes to a Bale group.

The workflow repository keeps a versioned checkpoint on a separate state branch.
First-run files are ALL staged locally before any document is uploaded.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests

API = "https://api.github.com"
STATE_BRANCH = "bale-watcher-state"
STATE_PATH = "bale-watcher-state.json"
STATE_VERSION = 2  # v1 only created a baseline. v2 uploads all files the first time.
# Leave headroom below Bale's documented 50-MB limit.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


class GithubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bale-github-watcher",
        })

    def request(self, method: str, path: str, *, allow_404: bool = False, **kwargs):
        # GitHub URLs have no Bale bot token. Avoid printing the GitHub token.
        try:
            response = self.session.request(
                method, f"{API}/{path.lstrip('/')}", timeout=90, **kwargs,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"GitHub network error: {type(exc).__name__}") from None
        if response.status_code == 404 and allow_404:
            return None
        if not response.ok:
            raise RuntimeError(
                f"GitHub API error ({response.status_code}) on {method} {path}: "
                f"{response.text[:250]}"
            )
        return response

    def get_json(self, path: str, *, allow_404: bool = False, params=None):
        response = self.request("GET", path, allow_404=allow_404, params=params)
        return None if response is None else response.json()

    def get_head(self, repo: str, branch: str) -> str:
        info = self.get_json(f"repos/{repo}/branches/{quote(branch, safe='')}")
        return info["commit"]["sha"]

    def list_files(self, repo: str, commit_sha: str) -> tuple[dict, list]:
        """Return all tracked files in one snapshot, with Git blob SHA and size.

        A recursive Git tree listing is necessary for the first full transfer,
        and tree-vs-tree comparison avoids the compare API's 300-file cap.
        """
        commit = self.get_json(f"repos/{repo}/git/commits/{commit_sha}")
        tree_sha = commit["tree"]["sha"]
        result = self.get_json(
            f"repos/{repo}/git/trees/{tree_sha}", params={"recursive": "1"},
        )
        if result.get("truncated"):
            raise RuntimeError(
                "GitHub tree listing was truncated (large repository). "
                "Stopping to avoid silently skipping files."
            )
        files = {}
        unsupported = {}
        for item in result["tree"]:
            if item["type"] == "blob":
                files[item["path"]] = {
                    "sha": item["sha"],
                    "size": item.get("size"),
                    "mode": item.get("mode", "100644"),
                }
            elif item["type"] == "commit":
                # Git submodules refer to another repository, not included here.
                unsupported[item["path"]] = item["sha"]
        return files, unsupported

    def download_blob(self, repo: str, sha: str) -> bytes:
        data = self.get_json(f"repos/{repo}/git/blobs/{sha}")
        if data.get("encoding") != "base64":
            raise RuntimeError("Unexpected GitHub blob encoding")
        return base64.b64decode(data["content"])

    def get_state(self, bot_repo: str):
        response = self.get_json(
            f"repos/{bot_repo}/contents/{STATE_PATH}",
            params={"ref": STATE_BRANCH}, allow_404=True,
        )
        if response is None:
            return None, None
        if response.get("encoding") != "base64":
            raise RuntimeError("Unexpected state file encoding")
        state = json.loads(base64.b64decode(response["content"]).decode("utf-8"))
        return state, response["sha"]

    def save_state(self, bot_repo: str, state: dict, blob_sha: str | None):
        ref_path = f"repos/{bot_repo}/git/ref/heads/{STATE_BRANCH}"
        if self.get_json(ref_path, allow_404=True) is None:
            self.request("POST", f"repos/{bot_repo}/git/refs", json={
                "ref": f"refs/heads/{STATE_BRANCH}",
                "sha": require_env("GITHUB_SHA"),
            })
        payload = {
            "message": "chore: update Bale watcher state [skip ci]",
            "branch": STATE_BRANCH,
            "content": base64.b64encode(
                (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            ).decode("ascii"),
        }
        if blob_sha:
            payload["sha"] = blob_sha
        self.request("PUT", f"repos/{bot_repo}/contents/{STATE_PATH}", json=payload)


class BaleClient:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = chat_id
        self.base = f"https://tapi.bale.ai/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, *, data=None, files=None):
        # IMPORTANT: never leak a request URL: it contains the Bale token.
        try:
            response = self.session.post(
                f"{self.base}/{method}", data=data, files=files, timeout=180,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Bale network error during {method}: {type(exc).__name__}"
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"Bale response was not JSON ({response.status_code})") from None
        if not response.ok or not body.get("ok"):
            description = str(body.get("description", "unknown error"))
            raise RuntimeError(
                f"Bale {method} failed ({response.status_code}): {description[:180]}"
            )
        return body.get("result")

    def send_message(self, message: str):
        self.call("sendMessage", data={"chat_id": self.chat_id, "text": message[:3700]})

    def send_document(self, filename: str, content: bytes, caption: str):
        self.call("sendDocument", data={
            "chat_id": self.chat_id, "caption": caption[:3700],
        }, files={
            "document": (filename, io.BytesIO(content), "application/octet-stream"),
        })


def select_files_to_upload(current: dict, previous: dict | None):
    """Return (path, metadata, label) for full import or changed blobs only."""
    entries = []
    for path in sorted(current):
        meta = current[path]
        old = previous.get(path) if previous is not None else None
        if previous is None:
            label = "اجرای اول: تمام فایل‌ها"
        elif old is None:
            label = "فایل جدید"
        elif old["sha"] != meta["sha"]:
            label = "فایل ویرایش‌شده"
        else:
            continue
        entries.append((path, meta, label))
    return entries


def stage_downloads(gh: GithubClient, repo: str, selected: list, folder: Path):
    """Download ALL eligible files to runner storage before any Bale upload."""
    staged = []
    skipped = []
    for index, (path, meta, label) in enumerate(selected):
        if meta["mode"] == "120000":
            skipped.append(f"{path} (symbolic link)")
            continue
        size = meta.get("size")
        if size is not None and size > MAX_UPLOAD_BYTES:
            skipped.append(f"{path} (over 45 MiB)")
            continue
        content = gh.download_blob(repo, meta["sha"])
        if len(content) > MAX_UPLOAD_BYTES:
            skipped.append(f"{path} (over 45 MiB)")
            continue
        local_path = folder / f"{index:08d}.blob"
        local_path.write_bytes(content)
        staged.append((path, label, local_path))
        print(f"Downloaded ({len(staged)}): {path}")
    return staged, skipped


def make_summary(*, repo, branch, head, first_run, sent, deleted, skipped, submodules):
    lines = [
        "✅ ارسال اولیه همه فایل‌ها پایان یافت" if first_run else "✅ بررسی تغییرات GitHub انجام شد",
        f"ریپازیتوری: {repo}",
        f"شاخه: {branch}",
        f"نسخه: {head[:12]}",
        f"📎 فایل‌های ارسال‌شده: {sent}",
    ]
    if deleted:
        lines.append("🗑 فایل‌های حذف‌شده:\n" + "\n".join(deleted[:25]))
        if len(deleted) > 25:
            lines.append(f"... و {len(deleted) - 25} فایل دیگر")
    if skipped:
        lines.append("⚠️ فایل‌های ارسال‌نشده:\n" + "\n".join(skipped[:15]))
        if len(skipped) > 15:
            lines.append(f"... و {len(skipped) - 15} فایل دیگر")
    if submodules:
        lines.append("⚠️ زیرماژول‌های Git (محتوای آن‌ها ارسال نمی‌شود):\n" + "\n".join(submodules[:10]))
    return "\n".join(lines)


def run():
    token = require_env("GITHUB_TOKEN")
    bale_token = require_env("BALE_BOT_TOKEN")
    bale_chat_id = require_env("BALE_CHAT_ID")
    bot_repo = require_env("GITHUB_REPOSITORY")
    target_repo = os.getenv("MONITOR_REPO", "").strip() or bot_repo
    if len(target_repo.split("/")) != 2 or any(not p for p in target_repo.split("/")):
        raise RuntimeError("MONITOR_REPO must look like owner/repo")

    gh = GithubClient(token)
    bale = BaleClient(bale_token, bale_chat_id)
    branch = os.getenv("MONITOR_BRANCH", "").strip()
    if not branch:
        branch = gh.get_json(f"repos/{target_repo}")["default_branch"]

    head = gh.get_head(target_repo, branch)
    state, state_sha = gh.get_state(bot_repo)
    first_run = (
        not state
        or state.get("version") != STATE_VERSION
        or state.get("repo") != target_repo
        or state.get("branch") != branch
    )
    if not first_run and state["last_sha"] == head:
        print("No new commits; nothing to send.")
        return

    current, current_submodules = gh.list_files(target_repo, head)
    previous = None
    previous_submodules = {}
    if not first_run:
        previous, previous_submodules = gh.list_files(target_repo, state["last_sha"])
    selected = select_files_to_upload(current, previous)
    deleted = sorted(set(previous or {}) - set(current)) if not first_run else []
    changed_submodules = sorted(
        path for path in set(current_submodules) | set(previous_submodules)
        if first_run or current_submodules.get(path) != previous_submodules.get(path)
    )

    print(f"Run mode: {'FULL FIRST UPLOAD' if first_run else 'INCREMENTAL'}")
    print(f"Eligible files to download: {len(selected)}")
    with tempfile.TemporaryDirectory(prefix="bale-watcher-") as temp_dir:
        # Download phase is complete before any sends to Bale.
        staged, skipped = stage_downloads(gh, target_repo, selected, Path(temp_dir))
        print(f"All downloads staged: {len(staged)}; skipped: {len(skipped)}")

        if first_run:
            bale.send_message(
                f"🚀 ارسال اولیه ریپازیتوری شروع شد\n"
                f"ریپازیتوری: {target_repo}\nشاخه: {branch}\n"
                f"تعداد فایل‌های آماده ارسال: {len(staged)}"
            )
        for path, label, local_path in staged:
            caption = (
                f"📁 {path}\nوضعیت: {label}\n"
                f"ریپازیتوری: {target_repo}\nنسخه: {head[:12]}"
            )
            bale.send_document(PurePosixPath(path).name, local_path.read_bytes(), caption)
            print(f"Uploaded to Bale: {path}")

    # Do not spam the group on empty changes; but report removals/skips.
    if first_run or selected or deleted or changed_submodules:
        bale.send_message(make_summary(
            repo=target_repo, branch=branch, head=head, first_run=first_run,
            sent=len(staged), deleted=deleted, skipped=skipped, submodules=changed_submodules,
        ))

    # Persist state only once every eligible document + summary succeeded.
    gh.save_state(bot_repo, {
        "version": STATE_VERSION,
        "repo": target_repo,
        "branch": branch,
        "last_sha": head,
    }, state_sha)
    print(f"Done: uploaded={len(staged)}, skipped={len(skipped)}, deleted={len(deleted)}")


if __name__ == "__main__":
    run()
