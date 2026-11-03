import json

import pytest

from bale_downloader.ui import (
    NO_BUTTON,
    YES_BUTTON,
    follow_keyboard,
    parse_callback,
    parse_choice,
    remove_keyboard,
    reply_keyboard,
    repo_tag,
)


@pytest.mark.parametrize(
    "text",
    [
        "بله",
        "آره",
        "اره",
        "YES",
        " y ",
        "✅ بله، آپدیت‌ها رو بفرست",
        "بله آپدیت هارو بفرست",
        "بله اپدیت ها رو بفرست",
        "بله آپدیت‌ها رو بفرست",
        "بله، آپديت ها رو بفرست",
        YES_BUTTON,
    ],
)
def test_yes_text_variants(text):
    assert parse_choice(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "نه",
        "خیر",
        "NO",
        "n",
        "❌ نه، فقط دانلود",
        "نه فقط دانلود",
        "نه، فقط دانلود",
        "نه فقط دانلود کن",
        "فقط دانلود",
        NO_BUTTON,
    ],
)
def test_no_text_variants(text):
    assert parse_choice(text) is False


@pytest.mark.parametrize("text", ["", "نه ممنون شاید بعدا", "please download", "بله نه"])
def test_ambiguous_text_is_not_a_choice(text):
    assert parse_choice(text) is None


def test_keyboard_payload_is_valid_and_callback_fits_bale_limit():
    keyboard = follow_keyboard("owner/repository")
    json.dumps(keyboard)
    buttons = keyboard["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [YES_BUTTON, NO_BUTTON]
    assert all(1 <= len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert parse_callback(buttons[0]["callback_data"]).follow is True
    assert parse_callback(buttons[1]["callback_data"]).follow is False


def test_text_labels_round_trip_through_fallback_parser():
    for row in reply_keyboard()["keyboard"]:
        for button in row:
            assert parse_choice(button["text"]) is not None
    assert remove_keyboard() == {"remove_keyboard": True}


@pytest.mark.parametrize("follow", [True, False])
def test_old_inline_buttons_still_work(follow):
    tag = repo_tag("owner/repo")
    parsed = parse_callback(f"f:{int(follow)}:{tag}")
    assert parsed.follow is follow
    assert parsed.tag == tag


@pytest.mark.parametrize(
    "data", [None, "", "follow:2:abcdef0123456789", "f:1:short", "other", {}, "f:1:ABCDEF0123456789"]
)
def test_invalid_callback_rejected(data):
    assert parse_callback(data) is None
