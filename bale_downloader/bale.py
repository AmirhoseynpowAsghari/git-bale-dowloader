"""Small Bale Bot API client with secret-safe errors and legacy-client support."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import requests

from .config import MAX_UPDATES_PER_RUN
from .errors import ConfigurationError, FriendlyError

logger = logging.getLogger(__name__)


class Bale:
    def __init__(self, token: str):
        self._base = f"https://tapi.bale.ai/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, payload: dict[str, Any] | None = None, files: dict | None = None) -> Any:
        body = payload or {}
        try:
            if files:
                # Nested JSON must be serialized explicitly in multipart requests.
                form = {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value)
                    for key, value in body.items()
                }
                response = self.session.post(
                    self._base + "/" + method, data=form, files=files, timeout=(20, 150)
                )
            else:
                response = self.session.post(self._base + "/" + method, json=body, timeout=(15, 35))
        except requests.RequestException:
            # Request exception text can contain the token-bearing URL.
            raise FriendlyError("ارتباط با API بله ناموفق بود؛ اجرای بعدی دوباره تلاش می‌کند.") from None
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.ok or not isinstance(result, dict) or result.get("ok") is not True:
            if response.status_code in (401, 404):
                raise FriendlyError("توکن ربات بله معتبر نیست؛ BALE_BOT_TOKEN را بررسی کن.")
            if response.status_code == 429:
                raise FriendlyError("محدودیت ارسال بله فعال شده؛ کمی بعد دوباره تلاش می‌کنم.")
            raise FriendlyError(f"API بله درخواست {method} را نپذیرفت (HTTP {response.status_code}).")
        return result.get("result")

    def prepare_polling(self, *, reset_webhook: bool = False) -> None:
        """Never silently replace an existing webhook integration."""
        info = self.call("getWebhookInfo")
        if isinstance(info, dict) and info.get("url"):
            if not reset_webhook:
                raise ConfigurationError(
                    "A Bale webhook is active, so getUpdates cannot receive button clicks. "
                    "Remove it, or explicitly set BALE_RESET_WEBHOOK=true to use polling."
                )
            self.call("deleteWebhook", {"drop_pending_updates": False})
            logger.info("Webhook removed with explicit opt-in; pending updates preserved.")

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "limit": MAX_UPDATES_PER_RUN,
                "timeout": 0,
            },
        )
        if not isinstance(result, list):
            raise FriendlyError("فهرست پیام‌های دریافتی از بله معتبر نیست.")
        return [update for update in result if isinstance(update, dict)]

    def send(self, chat_id: str, text: str, buttons: dict[str, Any] | None = None) -> dict[str, Any]:
        if not 1 <= len(text) <= 4096:
            raise FriendlyError("طول پیام برای ارسال به بله مناسب نیست.")
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons is not None:
            body["reply_markup"] = buttons
        result = self.call("sendMessage", body)
        return result if isinstance(result, dict) else {}

    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str) -> dict[str, Any]:
        result = self.call(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1024]},
            {"document": (filename, io.BytesIO(data), "application/zip")},
        )
        return result if isinstance(result, dict) else {}

    def answer_callback(self, query_id: str, text: str = "") -> bool:
        """Acknowledgement is best-effort and must never block the button action.

        Bale documents IDs beginning with '1' as legacy clients that do not
        support answerCallbackQuery. Older servers can also reject the method.
        https://docs.bale.ai/#answercallbackquery
        """
        if not query_id or query_id.startswith("1"):
            return False
        try:
            self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": query_id,
                    "text": text[:200],
                },
            )
            return True
        except FriendlyError:
            logger.warning("Callback acknowledgement unavailable; continuing with the action.")
            return False

    def clear_inline(self, chat_id: str, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            self.call(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except FriendlyError:
            logger.warning("Could not remove an old inline keyboard; selection still applies.")
