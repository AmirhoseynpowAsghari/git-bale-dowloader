"""Interaction routing and subscription delivery, independent of HTTP transport."""

from __future__ import annotations

import logging
from typing import Any

from .archives import build_delta_zip, changes, parse_repo_url, readable_size
from .bale import Bale
from .config import MAX_REPOS_PER_CHAT, MAX_UPDATE_BATCHES, MAX_UPDATES_PER_RUN
from .errors import FriendlyError
from .github import GitHub
from .models import normalize_state
from .storage import Storage
from .ui import (
    FALLBACK_HINT,
    FOLLOW_QUESTION,
    follow_keyboard,
    parse_callback,
    parse_choice,
    remove_keyboard,
    reply_keyboard,
    repo_tag,
)

logger = logging.getLogger(__name__)

HELP = (
    "سلام! 👋 من دانلودر GitHub برای بله هستم.\n\n"
    "🔗 لینک اصلی یک ریپازیتوری عمومی را بفرست:\n"
    "https://github.com/owner/repo\n\n"
    "📦 نسخه کامل ZIP را دریافت می‌کنی؛ سپس انتخاب می‌کنی فقط دانلود باشد یا آپدیت هم بفرستم.\n"
    "🔄 آپدیت‌ها شامل فایل‌های جدید/تغییرکرده و گزارش حذف‌شده‌ها هستند، نه تاریخچه Git.\n"
    "📏 سقف مجموع فایل‌ها ۵۰ MB و سقف هر ZIP، ۴۸ MB است.\n\n"
    "/list — نمایش ریپازیتوری‌ها و وضعیت آپدیت\n"
    "/stop owner/repo — حذف ریپازیتوری و توقف آپدیت\n"
    "/cancel — بستن سؤال بازِ آپدیت\n"
    "/help — همین راهنما\n\n"
    "بررسی‌ها در اجرای بعدی GitHub Actions انجام می‌شوند؛ پاسخ ممکن است چند دقیقه طول بکشد."
)


class Bot:
    def __init__(self, github: GitHub, bale: Bale, storage: Storage, state: dict[str, Any]):
        self.github = github
        self.bale = bale
        self.storage = storage
        self.state = normalize_state(state)

    def chat(self, chat_id: str) -> dict[str, Any]:
        return self.state.setdefault("chats", {}).setdefault(str(chat_id), {"repos": {}, "pending": None})

    @staticmethod
    def owns(record: dict[str, Any], from_id: str) -> bool:
        return bool(from_id) and record.get("requester") == from_id

    def reply_follow(self, chat_id: str, repo: str, yes: bool) -> None:
        """Persist a choice before confirming it; repeated clicks are idempotent."""
        chat = self.chat(chat_id)
        record = chat["repos"].get(repo)
        if record is None:
            self.bale.send(
                chat_id, "این درخواست قدیمی است؛ لینک ریپازیتوری را دوباره بفرست.", remove_keyboard()
            )
            return
        already_confirmed = (
            record.get("follow") is yes
            and chat.get("pending") != repo
            and record.get("choice_confirmed", True)
        )
        if already_confirmed:
            self.bale.clear_inline(chat_id, record.get("prompt_message_id"))
            return
        record["follow"] = yes
        record["choice_confirmed"] = False
        if chat.get("pending") == repo:
            chat["pending"] = None
        # A failed confirmation must not undo the user's saved choice. On retry,
        # choice_confirmed=False ensures that confirmation is attempted again.
        self.storage.save(self.state)
        self.bale.clear_inline(chat_id, record.pop("prompt_message_id", None))
        if yes:
            text = (
                f"✅ آپدیت‌های {repo} فعال شد.\n"
                "پس از هر تغییر، فقط فایل‌های جدید/تغییرکرده و گزارش حذف‌شده‌ها را می‌فرستم.\n"
                f"برای توقف: /stop {repo}"
            )
        else:
            text = (
                f"❌ آپدیت خودکار {repo} غیرفعال است؛ فقط نسخه دانلودشده را دریافت می‌کنی.\n"
                "هر وقت خواستی، لینک را دوباره بفرست و دریافت آپدیت را فعال کن."
            )
        self.bale.send(chat_id, text, remove_keyboard())
        record["choice_confirmed"] = True

    def handle_link(self, chat_id: str, from_id: str, repo: str) -> None:
        chat = self.chat(chat_id)
        old = chat["repos"].get(repo)
        if old is None and len(chat["repos"]) >= MAX_REPOS_PER_CHAT:
            self.bale.send(chat_id, "حداکثر ۵ ریپازیتوری در هر گفتگو پشتیبانی می‌شود؛ از /stop استفاده کن.")
            return
        if old and old.get("follow") is True and not self.owns(old, from_id):
            self.bale.send(chat_id, "آپدیت این ریپازیتوری توسط کاربر دیگری فعال شده است.")
            return
        self.bale.send(chat_id, "🔍 در حال بررسی و دانلود ریپازیتوری…")
        try:
            snapshot = self.github.snapshot(repo)
            source = self.github.download_zip(snapshot)
        except FriendlyError as exc:
            self.bale.send(chat_id, f"❌ {exc}")
            return
        self.bale.send_document(
            chat_id,
            f"{repo.replace('/', '-')}-{snapshot.head[:8]}.zip",
            source,
            f"📦 نسخه کامل {repo}\n"
            f"commit: {snapshot.head[:8]} | محتوا: {readable_size(snapshot.total_bytes)}",
        )
        if old and old.get("follow") is True:
            # An explicit re-download must not silently disable a subscription.
            old.update(sha=snapshot.head, files=snapshot.files)
            old.pop("last_error", None)
            self.storage.save(self.state)
            self.bale.send(chat_id, f"✅ نسخه جدید ارسال شد؛ آپدیت‌های {repo} همچنان فعال هستند.")
            return
        chat["repos"][repo] = {
            "sha": snapshot.head,
            "files": snapshot.files,
            "follow": None,
            "requester": from_id,
            "choice_confirmed": False,
            "prompt_message_id": None,
        }
        chat["pending"] = repo
        self.storage.save(self.state)
        prompt: dict[str, Any] = {}
        try:
            prompt = self.bale.send(
                chat_id, FOLLOW_QUESTION + "\n\nمی‌توانی «بله» یا «نه» هم بنویسی.", follow_keyboard(repo)
            )
        except FriendlyError:
            logger.warning("Inline prompt unavailable; using the text-button fallback.")
        message_id = prompt.get("message_id")
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            chat["repos"][repo]["prompt_message_id"] = message_id
        try:
            self.bale.send(chat_id, FOLLOW_QUESTION if not prompt else FALLBACK_HINT, reply_keyboard())
        except FriendlyError:
            if not prompt:
                raise
            logger.warning("Reply keyboard unavailable; inline buttons and typed answers remain usable.")

    def handle_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        sender = message.get("from") or {}
        chat_info = message.get("chat") or {}
        if not isinstance(sender, dict) or not isinstance(chat_info, dict):
            return
        if sender.get("id") is None or chat_info.get("id") is None or sender.get("is_bot"):
            return
        chat_id, from_id = str(chat_info["id"]), str(sender["id"])
        body = message.get("text") or ""
        if not isinstance(body, str) or not body.strip():
            return
        body = body.strip()
        parts = body.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) == 2 else ""
        chat = self.chat(chat_id)
        if command in ("/start", "/help"):
            self.bale.send(chat_id, HELP)
            return
        if command in ("/list", "/status"):
            labels = {True: "🔄 آپدیت فعال", False: "📦 فقط دانلود", None: "⏳ انتخاب نشده"}
            items = [f"• {repo} — {labels[record.get('follow')]}" for repo, record in chat["repos"].items()]
            self.bale.send(
                chat_id,
                "ریپازیتوری‌های این گفتگو:\n" + "\n".join(items)
                if items
                else "هنوز ریپازیتوری ثبت نشده؛ لینک GitHub را بفرست.",
            )
            return
        if command == "/cancel":
            pending = chat.get("pending")
            if pending:
                record = chat["repos"][pending]
                if not self.owns(record, from_id):
                    self.bale.send(chat_id, "فقط فرستنده لینک می‌تواند این سؤال را ببندد.")
                    return
                record["follow"], record["choice_confirmed"] = False, True
                chat["pending"] = None
                self.bale.clear_inline(chat_id, record.pop("prompt_message_id", None))
            self.bale.send(chat_id, "سؤال باز بسته شد؛ آپدیت خودکار فعال نشده است.", remove_keyboard())
            return
        if command == "/stop":
            repo = parse_repo_url(argument) if argument.startswith("https://") else argument
            if not repo or parse_repo_url("https://github.com/" + repo) != repo:
                self.bale.send(chat_id, "برای توقف بنویس: /stop owner/repo\nفهرست: /list")
                return
            record = chat["repos"].get(repo)
            if record is None:
                self.bale.send(chat_id, "این ریپازیتوری در فهرست این گفتگو نیست.")
            elif not self.owns(record, from_id):
                self.bale.send(chat_id, "فقط فرستنده لینک می‌تواند این ریپازیتوری را حذف کند.")
            else:
                del chat["repos"][repo]
                if chat.get("pending") == repo:
                    chat["pending"] = None
                self.bale.send(chat_id, f"🛑 {repo} حذف شد و آپدیت خودکار آن متوقف شد.", remove_keyboard())
            return
        repo = parse_repo_url(body)
        if repo:
            self.handle_link(chat_id, from_id, repo)
            return
        choice = parse_choice(body)
        if choice is not None:
            pending = chat.get("pending")
            if not pending:
                self.bale.send(
                    chat_id,
                    "سؤال بازی وجود ندارد؛ برای دانلود و انتخاب آپدیت، لینک را بفرست.",
                    remove_keyboard(),
                )
            elif not self.owns(chat["repos"][pending], from_id):
                self.bale.send(chat_id, "فقط کسی که لینک را فرستاده می‌تواند این انتخاب را انجام دهد.")
            else:
                self.reply_follow(chat_id, pending, choice)
        elif "github.com" in body.lower():
            self.bale.send(chat_id, "لینک اصلی را بفرست؛ مثل https://github.com/owner/repo (بدون مسیر فایل).")

    def handle_callback(self, callback: dict[str, Any]) -> None:
        if not isinstance(callback, dict):
            return
        query_id = str(callback.get("id") or "")
        # Never let a legacy/unsupported acknowledgement prevent the actual action.
        self.bale.answer_callback(query_id)
        message = callback.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        chat_info = message.get("chat") or {}
        if not isinstance(chat_info, dict):
            chat_info = {}
        chat_id = str(chat_info.get("id", message.get("chat_id", "")))
        sender = callback.get("from") or {}
        from_id = str(sender.get("id", "")) if isinstance(sender, dict) else ""
        action = parse_callback(callback.get("data"))
        if not chat_id or action is None or not from_id:
            if chat_id:
                self.bale.send(chat_id, "این دکمه شناخته نشد؛ لینک ریپازیتوری را دوباره بفرست.")
            return
        chat = self.chat(chat_id)
        repo = next((name for name in chat["repos"] if repo_tag(name) == action.tag), None)
        if repo is None:
            self.bale.clear_inline(chat_id, message.get("message_id"))
            self.bale.send(chat_id, "این درخواست قدیمی یا حذف شده است؛ لینک را دوباره بفرست.")
            return
        if not self.owns(chat["repos"][repo], from_id):
            self.bale.send(chat_id, "فقط فرستنده لینک می‌تواند این گزینه را انتخاب کند.")
            return
        self.reply_follow(chat_id, repo, action.follow)

    def process_updates(self) -> None:
        """Checkpoint each successful update; failed updates remain retryable."""
        for _ in range(MAX_UPDATE_BATCHES):
            offset = int(self.state.get("offset", 0))
            updates = self.bale.get_updates(offset)
            valid = [
                item
                for item in updates
                if isinstance(item.get("update_id"), int)
                and not isinstance(item["update_id"], bool)
                and item["update_id"] >= offset
            ]
            for update in sorted(valid, key=lambda item: item["update_id"]):
                if "callback_query" in update:
                    self.handle_callback(update["callback_query"])
                elif "message" in update:
                    self.handle_message(update["message"])
                else:
                    logger.info("Ignoring unsupported update type %s.", update["update_id"])
                self.state["offset"] = update["update_id"] + 1
                self.storage.save(self.state)
                logger.info("Processed update %s.", update["update_id"])
            if len(updates) < MAX_UPDATES_PER_RUN:
                break

    def _send_deletion_report(self, chat_id: str, repo: str, deleted: list[str]) -> None:
        lines = [f"🗑 {len(deleted)} فایل در {repo} حذف شده است:"]
        lines.extend(path if len(path) <= 300 else path[:297] + "…" for path in deleted)
        chunk = ""
        for line in lines:
            if chunk and len(chunk) + len(line) + 1 > 3900:
                self.bale.send(chat_id, chunk)
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk:
            self.bale.send(chat_id, chunk)

    def deliver_updates(self) -> None:
        for chat_id, chat in list(self.state.get("chats", {}).items()):
            for repo, record in list(chat.get("repos", {}).items()):
                if record.get("follow") is not True:
                    continue
                try:
                    snapshot = self.github.snapshot(repo)
                    if record.get("sha") == snapshot.head and record.get("files") == snapshot.files:
                        if record.pop("last_error", None) is not None:
                            self.storage.save(self.state)
                        continue
                    updated, deleted = changes(record["files"], snapshot.files)
                    if updated:
                        source = self.github.download_zip(snapshot)
                        delta = build_delta_zip(source, updated, deleted)
                        self.bale.send_document(
                            chat_id,
                            f"{repo.replace('/', '-')}-updates-{snapshot.head[:8]}.zip",
                            delta,
                            f"🔄 آپدیت {repo}\n{len(updated)} فایل جدید/تغییرکرده، {len(deleted)} حذف",
                        )
                    elif deleted:
                        self._send_deletion_report(chat_id, repo, deleted)
                    record["sha"], record["files"] = snapshot.head, snapshot.files
                    record.pop("last_error", None)
                    # Advance this chat only after a successful upload/report.
                    self.storage.save(self.state)
                except FriendlyError as exc:
                    reason = str(exc)
                    logger.warning("Repository delivery failed (%s).", type(exc).__name__)
                    if record.get("last_error") != reason:
                        self.bale.send(chat_id, f"⚠️ بررسی {repo}: {reason}")
                        record["last_error"] = reason
                        self.storage.save(self.state)
