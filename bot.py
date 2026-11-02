"""Interactive Bale GitHub downloader, designed for scheduled GitHub Actions.

Only public GitHub repositories. State is Fernet-encrypted on a separate branch
of the hosting repository, using a key derived from the Bale bot token.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import urllib.parse
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken

MAX_REPO_BYTES = 50_000_000  # Sum of Git blob sizes, not the Git history.
MAX_ZIP_BYTES = 48_000_000  # Safety margin under Bale's 50 MB upload limit.
MAX_UPDATES_PER_RUN = 30
MAX_REPOS_PER_CHAT = 5
STATE_BRANCH = "bale-interactive-state"
STATE_FILE = "state.enc"
GITHUB_API = "https://api.github.com"
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class FriendlyError(Exception):
    """Expected error safe to show in a Bale chat."""


def parse_repo_url(message: str) -> str | None:
    """Accept only a URL that points to the root of a github.com repo."""
    message = message.strip().strip("<> ")
    try:
        url = urllib.parse.urlsplit(message)
    except ValueError:
        return None
    if url.scheme != "https" or url.hostname not in ("github.com", "www.github.com"):
        return None
    try:
        if url.username or url.password or url.port:
            return None
    except ValueError:
        return None
    pieces = [p for p in url.path.strip("/").split("/") if p]
    if len(pieces) != 2:
        return None
    owner, name = pieces
    if name.endswith(".git"):
        name = name[:-4]
    if not all(REPO_PART.fullmatch(p) and p not in (".", "..") for p in (owner, name)):
        return None
    return f"{owner}/{name}"


def readable_size(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB"


def changes(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    updated = sorted(p for p, sha in new.items() if old.get(p) != sha)
    deleted = sorted(p for p in old if p not in new)
    return updated, deleted


def build_delta_zip(source: bytes, changed: list[str], deleted: list[str]) -> bytes:
    """Produce a new archive with ONLY new/modified files and a change report."""
    output = io.BytesIO()
    needed = set(changed)
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            found: set[str] = set()
            for entry in original.infolist():
                if entry.is_dir() or "/" not in entry.filename:
                    continue
                relative = entry.filename.split("/", 1)[1]
                if relative not in needed:
                    continue
                # Archive contains only server-provided GitHub ZIP content.
                data = original.read(entry)
                target.writestr(relative, data)
                found.add(relative)
            missing = needed - found
            if missing:
                raise FriendlyError("چند فایل تغییرکرده در آرشیو GitHub پیدا نشد. در اجرای بعد دوباره تلاش می‌کنم.")
            text = "Changed/new files:\n" + "\n".join(changed) + "\n\nDeleted files:\n" + "\n".join(deleted) + "\n"
            target.writestr("__changes.txt", text)
    blob = output.getvalue()
    if len(blob) > MAX_ZIP_BYTES:
        raise FriendlyError("بسته آپدیت از سقف امن آپلود ۴۸ مگابایت بیشتر شده است.")
    return blob


@dataclass
class Snapshot:
    repo: str
    head: str
    files: dict[str, str]
    total_bytes: int


class GitHub:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BaleInteractiveGitHubBot",
        })
        self.cache: dict[str, Snapshot] = {}
        self.zip_cache: dict[str, bytes] = {}

    def get_json(self, path: str) -> dict:
        try:
            r = self.session.get(GITHUB_API + path, timeout=30)
        except requests.RequestException:
            raise FriendlyError("اتصال به GitHub برقرار نشد؛ بعداً دوباره تلاش کن.") from None
        if r.status_code == 404:
            raise FriendlyError("ریپازیتوری عمومی پیدا نشد. لینک و عمومی‌بودن آن را بررسی کن.")
        if r.status_code == 403 or r.status_code == 429:
            raise FriendlyError("محدودیت درخواست GitHub فعال شده؛ کمی بعد دوباره تلاش کن.")
        if not r.ok:
            raise FriendlyError(f"GitHub پاسخ ناموفق داد (HTTP {r.status_code}).")
        return r.json()

    def snapshot(self, repo: str) -> Snapshot:
        if repo in self.cache:
            return self.cache[repo]
        owner, name = repo.split("/")
        details = self.get_json(f"/repos/{owner}/{name}")
        if details.get("private"):
            raise FriendlyError("در این نسخه فقط ریپازیتوری‌های عمومی پشتیبانی می‌شوند.")
        branch = details.get("default_branch", "main")
        commit = self.get_json(f"/repos/{owner}/{name}/commits/{urllib.parse.quote(branch, safe='')}")
        tree_sha = commit["commit"]["tree"]["sha"]
        tree = self.get_json(f"/repos/{owner}/{name}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise FriendlyError("فهرست فایل‌های این ریپازیتوری بیش‌ازحد بزرگ است.")
        files: dict[str, str] = {}
        size = 0
        for item in tree.get("tree", []):
            if item.get("type") != "blob":  # Git submodules aren't downloadable normal files.
                continue
            blob_size = item.get("size", 0)
            if not isinstance(blob_size, int) or blob_size < 0:
                raise FriendlyError("حجم فایل‌ها قابل بررسی نیست.")
            size += blob_size
            if size >= MAX_REPO_BYTES:
                raise FriendlyError(f"مجموع محتوای ریپازیتوری {readable_size(size)} یا بیشتر است؛ سقف ما ۵۰ مگابایت است.")
            files[item["path"]] = item["sha"]
        snapshot = Snapshot(repo=repo, head=commit["sha"], files=files, total_bytes=size)
        self.cache[repo] = snapshot
        return snapshot

    def download_zip(self, snapshot: Snapshot) -> bytes:
        key = f"{snapshot.repo}@{snapshot.head}"
        if key in self.zip_cache:
            return self.zip_cache[key]
        owner, name = snapshot.repo.split("/")
        url = f"https://codeload.github.com/{owner}/{name}/zip/{snapshot.head}"
        try:
            with requests.get(url, timeout=(15, 90), stream=True, headers={"User-Agent": "BaleInteractiveGitHubBot"}) as r:
                if not r.ok:
                    raise FriendlyError("دانلود ZIP از GitHub ناموفق بود.")
                data = io.BytesIO()
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        if data.tell() + len(chunk) > MAX_ZIP_BYTES:
                            raise FriendlyError("حجم ZIP از سقف امن ۴۸ مگابایت بیشتر است و قابل ارسال نیست.")
                        data.write(chunk)
        except requests.RequestException:
            raise FriendlyError("در دانلود فایل از GitHub مشکلی رخ داد.") from None
        blob = data.getvalue()
        if not zipfile.is_zipfile(io.BytesIO(blob)):
            raise FriendlyError("آرشیو GitHub معتبر نیست.")
        self.zip_cache[key] = blob
        return blob


class Bale:
    def __init__(self, token: str):
        self.base = f"https://tapi.bale.ai/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, payload: dict | None = None, files: dict | None = None) -> Any:
        try:
            r = self.session.post(self.base + "/" + method,
                                  data=payload if files else None,
                                  json=None if files else (payload or {}),
                                  files=files, timeout=(20, 150) if files else (15, 35))
            result = r.json()
        except (requests.RequestException, ValueError):
            # Never print a request URL: it contains the bot's secret token.
            raise FriendlyError("ارتباط با API بله ناموفق بود.") from None
        if not r.ok or not result.get("ok"):
            description = result.get("description", "unknown")
            raise FriendlyError(f"خطای API بله (HTTP {r.status_code}): {str(description)[:160]}")
        return result.get("result")

    def get_updates(self, offset: int) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "limit": MAX_UPDATES_PER_RUN, "timeout": 0})

    def send(self, chat_id: str, text: str, buttons: dict | None = None) -> None:
        body: dict = {"chat_id": chat_id, "text": text}
        if buttons:
            body["reply_markup"] = buttons
        self.call("sendMessage", body)

    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str) -> None:
        self.call("sendDocument", {"chat_id": chat_id, "caption": caption},
                  {"document": (filename, io.BytesIO(data), "application/zip")})

    def answer_callback(self, query_id: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": query_id})


class Storage:
    """Store an encrypted JSON state using the hosting repo's contents API."""
    def __init__(self, token: str, hosting_repo: str, bot_token: str):
        self.repo = hosting_repo
        self.api = GITHUB_API + f"/repos/{hosting_repo}"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "BaleInteractiveGitHubBot"
        })
        derived = hashlib.sha256(("bale-state-v1:" + bot_token).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(derived))
        self.file_sha: str | None = None
        self.branch_exists = False

    def request(self, method: str, suffix: str, **kwargs):
        try:
            r = self.session.request(method, self.api + suffix, timeout=30, **kwargs)
        except requests.RequestException:
            raise RuntimeError("GitHub state request failed (network).") from None
        return r

    def _ensure_branch(self) -> None:
        if self.branch_exists:
            return
        r = self.request("GET", f"/git/ref/heads/{STATE_BRANCH}")
        if r.status_code == 404:
            # GITHUB_SHA points to a commit in this hosting repository.
            sha = os.environ["GITHUB_SHA"]
            creation = self.request("POST", "/git/refs", json={
                "ref": f"refs/heads/{STATE_BRANCH}", "sha": sha
            })
            if creation.status_code not in (200, 201, 422):
                raise RuntimeError(f"Could not create state branch: HTTP {creation.status_code}")
        elif not r.ok:
            raise RuntimeError(f"Cannot access state branch: HTTP {r.status_code}")
        self.branch_exists = True

    def load(self) -> dict:
        self._ensure_branch()
        r = self.request("GET", f"/contents/{STATE_FILE}", params={"ref": STATE_BRANCH})
        if r.status_code == 404:
            return {"offset": 0, "chats": {}}
        if not r.ok:
            raise RuntimeError(f"Cannot read bot state: HTTP {r.status_code}")
        content = r.json()
        self.file_sha = content["sha"]
        try:
            ciphertext = base64.b64decode(content["content"].replace("\n", ""))
            return json.loads(self.cipher.decrypt(ciphertext).decode("utf-8"))
        except InvalidToken:
            raise RuntimeError("State decryption failed. Was the BALE_BOT_TOKEN changed?") from None

    def save(self, state: dict) -> None:
        plaintext = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode()
        encrypted = self.cipher.encrypt(plaintext)
        payload = {
            "message": "Update encrypted Bale state [skip ci]",
            "branch": STATE_BRANCH,
            "content": base64.b64encode(encrypted).decode(),
        }
        if self.file_sha:
            payload["sha"] = self.file_sha
        r = self.request("PUT", f"/contents/{STATE_FILE}", json=payload)
        if not r.ok:
            raise RuntimeError(f"Cannot save bot state: HTTP {r.status_code}")
        self.file_sha = r.json()["content"]["sha"]


def follow_keyboard(repo: str) -> dict:
    tag = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return {"inline_keyboard": [[
        {"text": "✅ بله، آپدیت‌ها رو بفرست", "callback_data": f"f:1:{tag}"},
        {"text": "❌ نه، فقط دانلود", "callback_data": f"f:0:{tag}"},
    ]]}


class Bot:
    def __init__(self, github: GitHub, bale: Bale, storage: Storage, state: dict):
        self.github, self.bale, self.storage, self.state = github, bale, storage, state

    def chat(self, chat_id: str) -> dict:
        return self.state.setdefault("chats", {}).setdefault(chat_id, {"repos": {}, "pending": None})

    def reply_follow(self, chat_id: str, repo: str, yes: bool) -> None:
        chat = self.chat(chat_id)
        record = chat["repos"].get(repo)
        if not record:
            self.bale.send(chat_id, "این درخواست قدیمی است؛ لینک ریپازیتوری را دوباره بفرست.")
            return
        record["follow"] = yes
        if chat.get("pending") == repo:
            chat["pending"] = None
        if yes:
            self.bale.send(chat_id, f"✅ آپدیت‌های {repo} فعال شد. هر بار تغییر جدیدی تشخیص داده شود، فقط تغییرات را می‌فرستم.\nبرای توقف: /stop {repo}")
        else:
            self.bale.send(chat_id, f"باشه! فقط نسخه فعلی {repo} ارسال شد. برای دریافت دوباره لینک را بفرست.")

    def handle_link(self, chat_id: str, from_id: str, repo: str) -> None:
        chat = self.chat(chat_id)
        old = chat["repos"].get(repo)
        if old and old.get("follow") is True:
            self.bale.send(chat_id, f"آپدیت‌های {repo} از قبل فعال هستند. برای توقف: /stop {repo}")
            return
        if chat.get("pending"):
            self.bale.send(chat_id, "اول به سؤال قبلی درباره آپدیت‌ها جواب بده؛ بعد لینک جدید بفرست.")
            return
        if repo not in chat["repos"] and len(chat["repos"]) >= MAX_REPOS_PER_CHAT:
            self.bale.send(chat_id, "حداکثر ۵ ریپازیتوری در هر گفتگو پشتیبانی می‌شود. از /stop برای حذف اشتراک استفاده کن.")
            return
        self.bale.send(chat_id, "🔍 در حال بررسی حجم ریپازیتوری… مجموع فایل‌ها باید کمتر یا مساوی ۵۰ مگابایت باشد.")
        try:
            snap = self.github.snapshot(repo)
            source = self.github.download_zip(snap)
        except FriendlyError as exc:
            self.bale.send(chat_id, f"❌ {exc}")
            return
        self.bale.send(chat_id, f"📦 حجم محتوای ریپو: {readable_size(snap.total_bytes)}\nحجم ZIP: {readable_size(len(source))}\nدر حال ارسال فایل…")
        self.bale.send_document(chat_id, f"{repo.replace('/', '-')}-{snap.head[:8]}.zip",
                                source, f"نسخه کامل {repo} | {snap.head[:8]}")
        chat["repos"][repo] = {
            "sha": snap.head, "files": snap.files, "follow": None, "requester": from_id,
        }
        chat["pending"] = repo
        self.bale.send(chat_id, f"✅ فایل کامل {repo} ارسال شد.\nمی‌خوای از این به بعد فقط آپدیت‌های همین ریپو هم برات ارسال بشن؟\n(دکمه را بزن یا «بله» / «خیر» بفرست)",
                       follow_keyboard(repo))

    def handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = str(chat["id"])
        from_id = str((message.get("from") or {}).get("id", ""))
        if (message.get("from") or {}).get("is_bot"):
            return
        body = (message.get("text") or "").strip()
        chat_data = self.chat(chat_id)
        if body.startswith("/start") or body.startswith("/help"):
            self.bale.send(chat_id, "سلام! 👋\nلینک یک ریپازیتوری عمومی GitHub رو بفرست، مثلاً:\nhttps://github.com/mzhoolideh/KNTU_CP_2026\n\n📌 مجموع حجم فایل‌های ریپو باید حداکثر ۵۰ مگابایت باشه. بعد از ارسال ZIP ازت می‌پرسم آپدیت‌ها رو هم می‌خوای یا نه.\n/list = اشتراک‌ها | /stop owner/repo = توقف")
            return
        if body.startswith("/list"):
            items = [f"• {repo}: {'فعال' if r.get('follow') else 'غیرفعال'}" for repo, r in chat_data["repos"].items()]
            self.bale.send(chat_id, "ریپازیتوری‌های این گفتگو:\n" + "\n".join(items) if items else "هنوز ریپازیتوری ثبت نشده است.")
            return
        if body.startswith("/stop"):
            parts = body.split(maxsplit=1)
            if len(parts) != 2:
                self.bale.send(chat_id, "برای توقف بنویس: /stop owner/repo")
                return
            repo = parse_repo_url(parts[1]) or parts[1].strip()
            if repo in chat_data["repos"]:
                if chat_data["repos"][repo].get("requester") != from_id:
                    self.bale.send(chat_id, "فقط فرستنده لینک می‌تواند این اشتراک را حذف کند.")
                    return
                del chat_data["repos"][repo]
                if chat_data.get("pending") == repo:
                    chat_data["pending"] = None
                self.bale.send(chat_id, f"🛑 اشتراک {repo} حذف شد.")
            else:
                self.bale.send(chat_id, "این ریپازیتوری در اشتراک‌های این گفتگو نیست.")
            return
        repo = parse_repo_url(body)
        if repo:
            self.handle_link(chat_id, from_id, repo)
            return
        pending = chat_data.get("pending")
        if body.lower() in ("بله", "آره", "اره", "yes", "y", "خیر", "نه", "no", "n") and pending:
            # Only the person who sent the link can answer in a group.
            if chat_data["repos"][pending].get("requester") != from_id:
                self.bale.send(chat_id, "فقط کسی که لینک را فرستاده می‌تواند این اشتراک را تأیید کند.")
                return
            yes = body.lower() in ("بله", "آره", "اره", "yes", "y")
            self.reply_follow(chat_id, pending, yes)
        elif body.startswith("https://") and "github.com" in body:
            self.bale.send(chat_id, "لطفاً لینک اصلی ریپازیتوری رو بفرست، مثل https://github.com/owner/repo (بدون مسیر فایل یا شاخه).")

    def handle_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        from_id = str((callback.get("from") or {}).get("id", ""))
        data = callback.get("data", "")
        query_id = callback.get("id")
        if query_id:
            self.bale.answer_callback(str(query_id))
        if not chat_id or not data.startswith("f:"):
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("0", "1"):
            return
        chat = self.chat(chat_id)
        repo = next((k for k in chat["repos"] if hashlib.sha256(k.encode()).hexdigest()[:16] == parts[2]), None)
        if not repo:
            return
        if chat["repos"][repo].get("requester") != from_id:
            self.bale.send(chat_id, "فقط فرستنده لینک می‌تواند این گزینه را انتخاب کند.")
            return
        self.reply_follow(chat_id, repo, parts[1] == "1")

    def process_updates(self) -> None:
        offset = int(self.state.get("offset", 0))
        updates = sorted(self.bale.get_updates(offset), key=lambda item: item["update_id"])
        for update in updates:
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update["message"])
            self.state["offset"] = int(update["update_id"]) + 1
            self.storage.save(self.state)

    def deliver_updates(self) -> None:
        # One snapshot per repo per run; each chat keeps its own last-delivered SHA.
        for chat_id, chat in list(self.state.get("chats", {}).items()):
            for repo, record in list(chat.get("repos", {}).items()):
                if record.get("follow") is not True:
                    continue
                try:
                    snap = self.github.snapshot(repo)
                    if record.get("sha") == snap.head and record.get("files") == snap.files:
                        if record.pop("last_error", None):
                            self.storage.save(self.state)
                        continue
                    updated, deleted = changes(record["files"], snap.files)
                    if updated:
                        full_zip = self.github.download_zip(snap)
                        delta = build_delta_zip(full_zip, updated, deleted)
                        self.bale.send_document(chat_id, f"{repo.replace('/', '-')}-updates-{snap.head[:8]}.zip",
                                                delta, f"آپدیت {repo}: {len(updated)} فایل جدید/تغییرکرده، {len(deleted)} حذف")
                    elif deleted:
                        report = "\n".join(deleted[:60])
                        self.bale.send(chat_id, f"🗑 {len(deleted)} فایل در {repo} حذف شدند:\n{report}")
                    # If only commit metadata changed, just advance HEAD.
                    record["sha"], record["files"] = snap.head, snap.files
                    record.pop("too_large", None)
                    self.storage.save(self.state)
                except FriendlyError as exc:
                    # Avoid notifying every five minutes for the same error.
                    reason = str(exc)
                    if record.get("last_error") != reason:
                        self.bale.send(chat_id, f"⚠️ بررسی {repo}: {reason}")
                        record["last_error"] = reason
                        self.storage.save(self.state)
                    print(f"Skipped {repo}: {reason}")
                else:
                    if record.pop("last_error", None):
                        self.storage.save(self.state)


def main():
    bale_token = os.environ["BALE_BOT_TOKEN"]
    gh_token = os.environ["GITHUB_TOKEN"]
    hosting_repo = os.environ["GITHUB_REPOSITORY"]
    storage = Storage(gh_token, hosting_repo, bale_token)
    state = storage.load()
    app = Bot(GitHub(gh_token), Bale(bale_token), storage, state)
    app.process_updates()
    app.deliver_updates()
    print("Interactive Bale bot cycle completed.")


if __name__ == "__main__":
    main()
