"""Bale keyboards and a single parser for inline and text-button choices."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

YES_BUTTON = "✅ بله، آپدیت‌ها رو بفرست"
NO_BUTTON = "❌ نه، فقط دانلود"
FOLLOW_QUESTION = (
    "فایل کامل ارسال شد. می‌خواهی تغییرات بعدی این ریپازیتوری هم ارسال شوند؟\n\n"
    "✅ بله: فقط فایل‌های جدید یا تغییرکرده، همراه با گزارش حذف‌شدن فایل‌ها.\n"
    "❌ نه: فقط همین دانلود؛ آپدیت خودکار ارسال نمی‌شود."
)
FALLBACK_HINT = "اگر دکمه‌های زیر پیام کار نمی‌کنند، از دو دکمه پایین صفحه استفاده کن."


def repo_tag(repo: str) -> str:
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]


def follow_keyboard(repo: str) -> dict[str, Any]:
    tag = repo_tag(repo)
    return {
        "inline_keyboard": [
            [
                {"text": YES_BUTTON, "callback_data": f"follow:1:{tag}"},
                {"text": NO_BUTTON, "callback_data": f"follow:0:{tag}"},
            ]
        ]
    }


def reply_keyboard() -> dict[str, Any]:
    # These buttons send ordinary messages, providing a fallback for clients
    # whose inline callback acknowledgement is unsupported or unreliable.
    return {
        "keyboard": [[{"text": YES_BUTTON}, {"text": NO_BUTTON}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard() -> dict[str, Any]:
    return {"remove_keyboard": True}


def normalize_choice(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"ي": "ی", "ك": "ک", "آ": "ا", "أ": "ا", "إ": "ا"}))
    text = re.sub(r"[\u200c\u200d\u200e\u200f]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


_YES_CHOICES = {
    "بله",
    "اره",
    "آره",
    "yes",
    "y",
    "بله آپدیت ها رو بفرست",
    "بله آپدیت هارو بفرست",
    "بله اپدیت هارو بفرست",
    "بله اپدیت ها رو بفرست",
    "بله آپدیت‌ها رو بفرست",
}
_NO_CHOICES = {
    "نه",
    "خیر",
    "no",
    "n",
    "نه فقط دانلود",
    "فقط دانلود",
    "نه فقط دانلود کن",
}
_YES_CHOICES = {normalize_choice(value) for value in _YES_CHOICES}
_NO_CHOICES = {normalize_choice(value) for value in _NO_CHOICES}


def parse_choice(text: str) -> bool | None:
    normalized = normalize_choice(text)
    if normalized in _YES_CHOICES:
        return True
    if normalized in _NO_CHOICES:
        return False
    return None


@dataclass(frozen=True)
class FollowCallback:
    tag: str
    follow: bool


def parse_callback(data: Any) -> FollowCallback | None:
    if not isinstance(data, str):
        return None
    # Accept old f:1:<tag> buttons already present in users' chats.
    match = re.fullmatch(r"(?:follow|f):([01]):([a-f0-9]{16})", data)
    if match is None:
        return None
    return FollowCallback(tag=match.group(2), follow=match.group(1) == "1")
