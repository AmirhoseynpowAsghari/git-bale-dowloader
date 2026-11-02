"""Send changed GitHub repository files to a Bale group using GitHub Actions.

Uses the workflow repository's `bale-watcher-state` branch to persist the
last successfully handled target commit, with no external server or database.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import PurePosixPath
from urllib.parse import quote

import requests


API = "https://api.github.com"
STATE_BRANCH = "bale-watcher-state"
STATE_PATH = "bale-watcher-state.json"
# Intentionally below Bale's documented 50-MB per-file upload limit.
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
        response = self.session.request(method, f"{API}/{path.lstrip('/')}", timeout=45, **kwargs)
        if response.status_code == 404 and allow_404:
            return None
        if not response.ok:
            raise RuntimeError(
                f"GitHub API error ({response.status_code}) on {method} {path}: "
                f"{response.text[:350]}"
            )
        return response

    def get_json(self, path: str, *, allow_404: bool = False, params=None):
        response = self.request("GET", path, allow_404=allow_404, params=params)
        return None if response is None else response.json()

    def get_head(self, repo: str, branch: str) -> str:
        ref = self.get_json(f"repos/{repo}/branches/{quote(branch, safe='')}")
        return ref["commit"]["sha"]

    def get_state(self, bot_repo: str):
        response = self.get_json(
            f"repos/{bot_repo}/contents/{STATE_PATH}",
            params={"ref": STATE_BRANCH}, allow_404=True,
        )
        if response is None:
            return None, None
        if response.get("encoding") != "base64":
            raise RuntimeError("Unexpected state-file encoding")
        data = json.loads(base64.b64decode(response["content"]).decode("utf-8"))
        return data, response["sha"]

    def save_state(self, bot_repo: str, state: dict, blob_sha: str | None) -> str:
        # State is stored on a separate branch so the default branch stays clean.
        ref_path = f"repos/{bot_repo}/git/ref/heads/{STATE_BRANCH}"
        if self.get_json(ref_path, allow_404=True) is None:
            # The scheduled/manual workflow runs on the default branch.
            default_commit = require_env("GITHUB_SHA")
            self.request("POST", f"repos/{bot_repo}/git/refs", json={
                "ref": f"refs/heads/{STATE_BRANCH}", "sha": default_commit,
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
        response = self.request(
            "PUT", f"repos/{bot_repo}/contents/{STATE_PATH}", json=payload,
        ).json()
        return response["content"]["sha"]

    def compare(self, repo: str, before_sha: str, after_sha: str) -> dict:
        path = f"repos/{repo}/compare/{before_sha}...{after_sha}"
        # GitHub limits each comparison to 300 changed files.
        return self.get_json(path, params={"per_page": 100, "page": 1})

    def download_blob(self, repo: str, blob_sha: str) -> bytes:
        # GitHub's Git Blob endpoint returns exact blob bytes as Base64.
        data = self.get_json(f"repos/{repo}/git/blobs/{blob_sha}")
        if data.get("encoding") != "base64":
            raise RuntimeError("Unexpected GitHub blob encoding")
        return base64.b64decode(data["content"])



class BaleClient:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = chat_id
        self.base = f"https://tapi.bale.ai/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, *, data=None, files=None):
        # Do not use raise_for_status(): exception URLs could contain the bot token.
        try:
            response = self.session.post(
                f"{self.base}/{method}", data=data, files=files, timeout=120,
            )
        except requests.RequestException as exc:
            # Don't print request URLs that include the bot token.
            raise RuntimeError(f"Bale network error during {method}: {type(exc).__name__}") from None
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError(f"Bale returned non-JSON response ({response.status_code})")
        if not response.ok or not result.get("ok"):
            raise RuntimeError(
                f"Bale {method} failed ({response.status_code}): "
                f"{str(result.get('description', 'unknown error'))[:200]}"
            )
        return result.get("result")

    def send_message(self, message: str):
        # Bale's plain text messages should be kept compact.
        self.call("sendMessage", data={"chat_id": self.chat_id, "text": message[:3800]})

    def send_document(self, filename: str, content: bytes, caption: str):
        self.call("sendDocument", data={
            "chat_id": self.chat_id, "caption": caption[:3800],
        }, files={
            "document": (filename, io.BytesIO(content), "application/octet-stream"),
        })


def run():
    token = require_env("GITHUB_TOKEN")
    bale_token = require_env("BALE_BOT_TOKEN")
    bale_chat_id = require_env("BALE_CHAT_ID")
    bot_repo = require_env("GITHUB_REPOSITORY")
    target_repo = os.getenv("MONITOR_REPO", "").strip() or bot_repo
    if len(target_repo.split("/")) != 2 or any(not x for x in target_repo.split("/")):
        raise RuntimeError("MONITOR_REPO must look like 'owner/repo'")

    gh = GithubClient(token)
    bale = BaleClient(bale_token, bale_chat_id)

    branch = os.getenv("MONITOR_BRANCH", "").strip()
    if not branch:
        branch = gh.get_json(f"repos/{target_repo}")["default_branch"]
    head = gh.get_head(target_repo, branch)
    state, state_file_sha = gh.get_state(bot_repo)

    if not state or state.get("repo") != target_repo or state.get("branch") != branch:
        # Initial run is a baseline; do not flood the group with every existing file.
        bale.send_message(
            f"✅ پایش GitHub فعال شد.\nریپازیتوری: {target_repo}\n"
            f"شاخه: {branch}\nنسخه شروع: {head[:12]}\n"
            "از اجرای بعدی، فایل‌های جدید و تغییرکرده ارسال می‌شوند."
        )
        gh.save_state(bot_repo, {
            "repo": target_repo, "branch": branch, "last_sha": head,
        }, state_file_sha)
        print("Initial baseline saved; no files sent.")
        return

    before = state["last_sha"]
    if before == head:
        print("No new commits; nothing to send.")
        return

    comparison = gh.compare(target_repo, before, head)
    changed = comparison.get("files", [])
    if len(changed) >= 300:
        # Avoid silently omitting files beyond GitHub's comparison API limit.
        raise RuntimeError("Comparison contains >=300 changed files; handle in smaller batches.")

    sent, skipped, removed, renamed = 0, [], [], []
    for file in changed:
        path = file["filename"]
        status = file["status"]
        if status == "removed":
            removed.append(path)
            continue
        if status == "renamed" and file.get("previous_filename"):
            renamed.append(f"{file['previous_filename']} → {path}")
        file_sha = file.get("sha")
        if not file_sha:
            skipped.append(f"{path} (no downloadable blob)")
            continue
        # Check API-reported size before requesting an oversized file.
        if file.get("size", 0) > MAX_UPLOAD_BYTES:
            skipped.append(f"{path} (over 45 MiB)")
            continue
        content = gh.download_blob(target_repo, file_sha)
        if len(content) > MAX_UPLOAD_BYTES:
            skipped.append(f"{path} (over 45 MiB)")
            continue
        label = {"added": "جدید", "modified": "ویرایش‌شده", "renamed": "تغییرنام‌یافته"}.get(
            status, status
        )
        caption = (
            f"📁 {path}\nوضعیت: {label}\n"
            f"ریپازیتوری: {target_repo}\nنسخه: {head[:12]}"
        )
        bale.send_document(PurePosixPath(path).name, content, caption)
        sent += 1
        print(f"Sent: {path}")

    summary = [
        "✅ بررسی تغییرات GitHub انجام شد",
        f"ریپازیتوری: {target_repo}",
        f"شاخه: {branch}",
        f"نسخه: {before[:12]} → {head[:12]}",
        f"📎 فایل‌های ارسال‌شده: {sent}",
    ]
    if removed:
        summary.append("🗑 فایل‌های حذف‌شده:\n" + "\n".join(removed[:20]))
        if len(removed) > 20:
            summary.append(f"... و {len(removed) - 20} فایل دیگر")
    if renamed:
        summary.append("✏️ تغییرنام:\n" + "\n".join(renamed[:10]))
        if len(renamed) > 10:
            summary.append(f"... و {len(renamed) - 10} مورد دیگر")
    if skipped:
        summary.append("⚠️ موارد ارسال‌نشده:\n" + "\n".join(skipped[:10]))
        if len(skipped) > 10:
            summary.append(f"... و {len(skipped) - 10} مورد دیگر")
    # Even when multiple commits cancel each other out, advance the saved revision.
    if changed:
        bale.send_message("\n".join(summary))
    gh.save_state(bot_repo, {
        "repo": target_repo, "branch": branch, "last_sha": head,
    }, state_file_sha)
    print(f"Done; sent={sent}, removed={len(removed)}, skipped={len(skipped)}")


if __name__ == "__main__":
    run()
