"""Encrypted, versioned state on a separate GitHub branch, with optimistic locking."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken

from .config import GITHUB_API, STATE_BRANCH, STATE_FILE
from .models import empty_state, normalize_state


class Storage:
    MAX_STATE_BYTES = 8_000_000

    def __init__(self, token: str, hosting_repo: str, bot_token: str, github_sha: str = ""):
        self.repo = hosting_repo
        self.api = GITHUB_API + f"/repos/{hosting_repo}"
        self.github_sha = github_sha or os.environ.get("GITHUB_SHA", "")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "BaleGitHubDownloader/2",
            }
        )
        # Preserve the original encryption scheme so upgrades can read v1 state.
        derived = hashlib.sha256(("bale-state-v1:" + bot_token).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(derived))
        self.file_sha: str | None = None
        self.branch_exists = False

    def request(self, method: str, suffix: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.request(method, self.api + suffix, timeout=(10, 30), **kwargs)
        except requests.RequestException:
            raise RuntimeError("GitHub state request failed (network).") from None

    @staticmethod
    def checked(response: requests.Response, action: str, statuses: tuple[int, ...]) -> None:
        if response.status_code not in statuses:
            raise RuntimeError(f"{action}: HTTP {response.status_code}")

    def _ensure_branch(self) -> None:
        if self.branch_exists:
            return
        response = self.request("GET", f"/git/ref/heads/{STATE_BRANCH}")
        if response.status_code == 404:
            if not self.github_sha:
                raise RuntimeError("GITHUB_SHA is required to initialize the state branch.")
            creation = self.request(
                "POST",
                "/git/refs",
                json={
                    "ref": f"refs/heads/{STATE_BRANCH}",
                    "sha": self.github_sha,
                },
            )
            self.checked(creation, "Could not create state branch", (200, 201, 422))
            # A concurrent creator may have won the race; verify, don't assume.
            response = self.request("GET", f"/git/ref/heads/{STATE_BRANCH}")
        self.checked(response, "Cannot access state branch", (200,))
        self.branch_exists = True

    def _ciphertext(self, content: dict[str, Any]) -> bytes:
        size = content.get("size", 0)
        if not isinstance(size, int) or size > self.MAX_STATE_BYTES:
            raise RuntimeError("Encrypted state exceeds the safe size limit.")
        if content.get("encoding") == "base64" and content.get("content"):
            return base64.b64decode(content["content"].replace("\n", ""), validate=True)
        # Contents JSON omits content for files over 1 MiB; the blob API supports
        # larger state instead of silently treating it as an empty database.
        response = self.request("GET", f"/git/blobs/{content['sha']}")
        self.checked(response, "Cannot read state blob", (200,))
        blob = response.json()
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise RuntimeError("Invalid state blob encoding.")
        return base64.b64decode(blob["content"].replace("\n", ""), validate=True)

    def load(self) -> dict[str, Any]:
        self._ensure_branch()
        response = self.request("GET", f"/contents/{STATE_FILE}", params={"ref": STATE_BRANCH})
        if response.status_code == 404:
            return empty_state()
        self.checked(response, "Cannot read bot state", (200,))
        try:
            content = response.json()
            self.file_sha = content["sha"]
            ciphertext = self._ciphertext(content)
            state = json.loads(self.cipher.decrypt(ciphertext).decode("utf-8"))
            return normalize_state(state)
        except InvalidToken:
            raise RuntimeError("State decryption failed. Was BALE_BOT_TOKEN changed?") from None
        except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError):
            raise RuntimeError("Stored state is invalid or damaged; it was not overwritten.") from None

    def save(self, state: dict[str, Any]) -> None:
        self._ensure_branch()
        plaintext = json.dumps(normalize_state(state), separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        encrypted = self.cipher.encrypt(plaintext)
        if len(encrypted) > self.MAX_STATE_BYTES:
            raise RuntimeError("Encrypted state exceeds the safe size limit.")
        payload = {
            "message": "Update encrypted Bale state [skip ci]",
            "branch": STATE_BRANCH,
            "content": base64.b64encode(encrypted).decode("ascii"),
        }
        if self.file_sha:
            payload["sha"] = self.file_sha
        response = self.request("PUT", f"/contents/{STATE_FILE}", json=payload)
        self.checked(response, "Cannot save bot state", (200, 201))
        try:
            self.file_sha = response.json()["content"]["sha"]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("GitHub saved state but did not return its blob SHA.") from None
