"""Repository URL validation and bounded, delta-only ZIP construction."""

from __future__ import annotations

import io
import re
import urllib.parse
import zipfile
import zlib
from pathlib import PurePosixPath, PureWindowsPath

from .config import MAX_REPO_BYTES, MAX_ZIP_BYTES
from .errors import FriendlyError

REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_repo_url(message: str) -> str | None:
    """Accept an HTTPS GitHub repository root, including an optional .git suffix."""
    try:
        url = urllib.parse.urlsplit(message.strip().strip("<> "))
        if (
            url.scheme != "https"
            or url.hostname not in ("github.com", "www.github.com")
            or url.username
            or url.password
            or url.port
        ):
            return None
    except ValueError:
        return None
    pieces = [part for part in url.path.split("/") if part]
    if len(pieces) != 2:
        return None
    owner, name = pieces
    if name.endswith(".git"):
        name = name[:-4]
    if not all(REPO_PART.fullmatch(part) and part not in (".", "..") for part in (owner, name)):
        return None
    return f"{owner}/{name}"


def readable_size(size: int) -> str:
    return f"{size / 1_000_000:.2f} MB"


def changes(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    updated = sorted(path for path, sha in new.items() if old.get(path) != sha)
    deleted = sorted(path for path in old if path not in new)
    return updated, deleted


def safe_relative_path(path: str) -> bool:
    return (
        bool(path)
        and "\\" not in path
        and not any(ord(character) < 32 for character in path)
        and not PurePosixPath(path).is_absolute()
        and not PureWindowsPath(path).drive
        and ".." not in PurePosixPath(path).parts
    )


def build_delta_zip(source: bytes, changed: list[str], deleted: list[str]) -> bytes:
    """Include only changed file contents; deleted paths appear in the report."""
    needed = set(changed)
    if not needed or not all(safe_relative_path(path) for path in needed | set(deleted)):
        raise FriendlyError("فهرست تغییرات برای ساخت آرشیو معتبر نیست.")
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as original:
            entries = [entry for entry in original.infolist() if not entry.is_dir()]
            roots = {entry.filename.split("/", 1)[0] for entry in entries}
            if len(roots) != 1 or any("/" not in entry.filename for entry in entries):
                raise FriendlyError("ساختار آرشیو GitHub معتبر نیست.")
            if sum(entry.file_size for entry in entries) > MAX_REPO_BYTES:
                raise FriendlyError("حجم استخراج‌شده آرشیو از سقف امن بیشتر است.")
            relative_names: set[str] = set()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
                found: set[str] = set()
                extracted = 0
                for entry in entries:
                    relative = entry.filename.split("/", 1)[1]
                    if relative in relative_names:
                        raise FriendlyError("آرشیو GitHub شامل مسیر تکراری است.")
                    relative_names.add(relative)
                    if relative not in needed:
                        continue
                    if entry.flag_bits & 0x1:
                        raise FriendlyError("فایل رمزگذاری‌شده در آرشیو پشتیبانی نمی‌شود.")
                    data = original.read(entry)
                    extracted += len(data)
                    if extracted > MAX_REPO_BYTES:
                        raise FriendlyError("حجم آپدیت از سقف امن بیشتر است.")
                    target.writestr(PurePosixPath(relative).as_posix(), data)
                    found.add(relative)
                if needed != found:
                    raise FriendlyError(
                        "چند فایل تغییرکرده در آرشیو GitHub پیدا نشد. بعداً دوباره تلاش می‌کنم."
                    )
                report = (
                    "Changed/new files:\n"
                    + "\n".join(changed)
                    + "\n\nDeleted files:\n"
                    + "\n".join(deleted)
                    + "\n"
                )
                report_name = "__changes.txt"
                suffix = 1
                while report_name in relative_names:
                    report_name = f"__changes_{suffix}.txt"
                    suffix += 1
                target.writestr(report_name, report)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise FriendlyError("آرشیو GitHub قابل خواندن نیست.") from exc
    blob = output.getvalue()
    if len(blob) > MAX_ZIP_BYTES:
        raise FriendlyError("بسته آپدیت از سقف امن آپلود ۴۸ مگابایت بیشتر شده است.")
    return blob
