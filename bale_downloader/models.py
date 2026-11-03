"""Validated repository snapshots and backward-compatible bot state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .config import STATE_VERSION


@dataclass(frozen=True)
class Snapshot:
    repo: str
    head: str
    files: dict[str, str]
    total_bytes: int


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "offset": 0, "chats": {}}


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate v1 state without losing existing downloads or subscriptions.

    Pending questions remain repository names, so state written by the original
    bot.py can still be answered after this upgrade.
    """
    if not isinstance(state, dict) or state.get("version", 1) not in (1, STATE_VERSION):
        raise ValueError("Unsupported state schema")
    chats = state.get("chats", {})
    if not isinstance(chats, dict):
        raise ValueError("Invalid chats in state")
    offset = state.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Invalid update offset")
    result: dict[str, Any] = {"version": STATE_VERSION, "offset": offset, "chats": {}}
    for chat_id, raw_chat in chats.items():
        if not isinstance(raw_chat, dict) or not isinstance(raw_chat.get("repos", {}), dict):
            raise ValueError("Invalid chat state")
        chat: dict[str, Any] = {"repos": {}, "pending": None}
        for repo, raw_record in raw_chat.get("repos", {}).items():
            if not isinstance(raw_record, dict):
                raise ValueError("Invalid repository state")
            files = raw_record.get("files")
            if (
                not isinstance(repo, str)
                or not isinstance(raw_record.get("sha"), str)
                or not isinstance(files, dict)
                or not all(isinstance(p, str) and isinstance(s, str) for p, s in files.items())
            ):
                raise ValueError("Invalid repository snapshot")
            follow = raw_record.get("follow")
            if follow is not None and not isinstance(follow, bool):
                raise ValueError("Invalid subscription flag")
            record = deepcopy(raw_record)
            record["follow"] = follow
            record.setdefault("requester", "")
            chat["repos"][repo] = record
        pending = raw_chat.get("pending")
        chat["pending"] = pending if isinstance(pending, str) and pending in chat["repos"] else None
        result["chats"][str(chat_id)] = chat
    return result
