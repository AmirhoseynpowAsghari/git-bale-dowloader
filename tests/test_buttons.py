from copy import deepcopy

import pytest
from conftest import CHAT, REPO, USER, callback, message

from bale_downloader.errors import FriendlyError
from bale_downloader.ui import follow_keyboard, remove_keyboard, repo_tag


@pytest.mark.parametrize("follow", [True, False])
def test_inline_button_selects_and_persists_correct_choice(pending_bot, bale, storage, follow):
    button = follow_keyboard(REPO)["inline_keyboard"][0][0 if follow else 1]
    pending_bot.handle_callback(callback(button["callback_data"])["callback_query"])
    record = pending_bot.chat(CHAT)["repos"][REPO]
    assert record["follow"] is follow
    assert record["choice_confirmed"] is True
    assert pending_bot.chat(CHAT)["pending"] is None
    assert "prompt_message_id" not in record
    assert storage.save.call_count == 1
    saved = storage.save.call_args.args[0]
    assert saved["chats"][CHAT]["repos"][REPO]["follow"] is follow
    bale.answer_callback.assert_called_once_with("23456789")
    bale.clear_inline.assert_called_once_with(CHAT, 99)
    assert bale.send.call_args.args[2] == remove_keyboard()


@pytest.mark.parametrize(
    "text,follow",
    [
        ("نه فقط دانلود", False),
        ("بله آپدیت هارو بفرست", True),
        ("❌ نه، فقط دانلود", False),
        ("✅ بله، آپدیت‌ها رو بفرست", True),
        ("خیر", False),
        ("بله", True),
    ],
)
def test_reply_keyboard_and_typed_fallback(pending_bot, storage, text, follow):
    pending_bot.handle_message(message(text)["message"])
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is follow
    assert pending_bot.chat(CHAT)["pending"] is None
    storage.save.assert_called_once()


@pytest.mark.parametrize("follow", [True, False])
def test_repeat_click_is_idempotent(pending_bot, bale, storage, follow):
    query = callback(f"follow:{int(follow)}:{repo_tag(REPO)}")["callback_query"]
    pending_bot.handle_callback(query)
    bale.send.reset_mock()
    storage.save.reset_mock()
    pending_bot.handle_callback(query)
    bale.send.assert_not_called()
    storage.save.assert_not_called()
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is follow


def test_legacy_callback_still_executes_without_unsupported_ack(pending_bot, bale):
    # The Bot routes the action regardless of client version. The real Bale
    # client's legacy acknowledgement behavior is tested in test_bale.py.
    bale.answer_callback.return_value = False
    pending_bot.handle_callback(callback("f:1:" + repo_tag(REPO), query_id="123456")["callback_query"])
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is True


def test_callback_from_other_group_member_cannot_change_subscription(pending_bot, bale, storage):
    before = deepcopy(pending_bot.state)
    pending_bot.handle_callback(callback("follow:1:" + repo_tag(REPO), sender="999")["callback_query"])
    assert pending_bot.state == before
    storage.save.assert_not_called()
    assert "فقط فرستنده" in bale.send.call_args.args[1]


def test_text_from_other_group_member_cannot_change_subscription(pending_bot, storage):
    pending_bot.handle_message(message("بله", sender="999")["message"])
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is None
    storage.save.assert_not_called()


def test_expired_callback_gets_visible_feedback(bot, bale):
    bot.handle_callback(callback("follow:1:abcdef0123456789")["callback_query"])
    assert "قدیمی" in bale.send.call_args.args[1]
    bale.clear_inline.assert_called_once_with(CHAT, 99)


def test_unknown_callback_gets_feedback(bot, bale):
    bot.handle_callback(callback("unrecognized")["callback_query"])
    assert "شناخته نشد" in bale.send.call_args.args[1]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": "234", "data": "follow:1:abcdef0123456789"},
        {"id": "234", "message": None, "data": "bad"},
        {"id": "234", "message": {"chat": []}, "data": "bad"},
        {"id": "234", "message": {"chat": {"id": 1}}, "from": [], "data": "bad"},
    ],
)
def test_malformed_callback_does_not_crash(bot, payload):
    bot.handle_callback(payload)


def test_choice_survives_confirmation_failure_and_is_retried(pending_bot, bale, storage):
    bale.send.side_effect = FriendlyError("Temporary send failure")
    with pytest.raises(FriendlyError):
        pending_bot.reply_follow(CHAT, REPO, True)
    saved = storage.save.call_args.args[0]
    assert saved["chats"][CHAT]["repos"][REPO]["follow"] is True
    assert saved["chats"][CHAT]["repos"][REPO]["choice_confirmed"] is False
    bale.send.side_effect = None
    pending_bot.reply_follow(CHAT, REPO, True)
    assert pending_bot.chat(CHAT)["repos"][REPO]["choice_confirmed"] is True


def test_storage_failure_does_not_claim_success(pending_bot, bale, storage):
    storage.save.side_effect = RuntimeError("state conflict")
    with pytest.raises(RuntimeError):
        pending_bot.reply_follow(CHAT, REPO, True)
    bale.send.assert_not_called()


def test_cancel_disables_pending_updates_without_deleting_download(pending_bot):
    pending_bot.handle_message(message("/cancel")["message"])
    assert pending_bot.chat(CHAT)["pending"] is None
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is False


def test_cancel_requires_original_requester(pending_bot):
    pending_bot.handle_message(message("/cancel", sender="999")["message"])
    assert pending_bot.chat(CHAT)["pending"] == REPO


def test_text_choice_without_pending_has_feedback(bot, bale):
    bot.handle_message(message("نه فقط دانلود")["message"])
    assert "سؤال بازی وجود ندارد" in bale.send.call_args.args[1]


def test_stop_removes_only_authorized_subscription(pending_bot):
    pending_bot.handle_message(message(f"/stop {REPO}", sender="999")["message"])
    assert REPO in pending_bot.chat(CHAT)["repos"]
    pending_bot.handle_message(message(f"/stop https://github.com/{REPO}")["message"])
    assert REPO not in pending_bot.chat(CHAT)["repos"]
    assert pending_bot.chat(CHAT)["pending"] is None


def test_old_state_pending_question_can_be_answered(bot, storage):
    bot.state["chats"][CHAT] = {
        "repos": {REPO: {"sha": "a" * 40, "files": {}, "follow": None, "requester": USER}},
        "pending": REPO,
    }
    bot.handle_message(message("بله آپدیت هارو بفرست")["message"])
    assert bot.chat(CHAT)["repos"][REPO]["follow"] is True
    storage.save.assert_called_once()
