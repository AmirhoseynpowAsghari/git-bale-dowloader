import io
import zipfile
from copy import deepcopy

import pytest
from conftest import CHAT, REPO, USER, callback, message

from bale_downloader.config import MAX_UPDATES_PER_RUN
from bale_downloader.errors import FriendlyError
from bale_downloader.models import Snapshot
from bale_downloader.ui import repo_tag


def update(update_id, payload):
    return {"update_id": update_id, **payload}


def test_updates_sorted_and_offset_checkpointed_after_success(bot, bale, storage):
    bale.get_updates.return_value = [
        update(11, message("/help")),
        update(10, message("/start")),
    ]
    saved_offsets = []
    storage.save.side_effect = lambda state: saved_offsets.append(state["offset"])
    bot.process_updates()
    assert bot.state["offset"] == 12
    assert saved_offsets == [11, 12]


def test_failed_update_does_not_advance_offset(pending_bot, bale, storage):
    bale.get_updates.return_value = [
        update(10, callback("follow:1:" + repo_tag(REPO))),
        update(11, message("/help")),
    ]
    storage.save.side_effect = RuntimeError("GitHub unavailable")
    with pytest.raises(RuntimeError):
        pending_bot.process_updates()
    assert pending_bot.state["offset"] == 0
    assert bale.send.call_count == 0


def test_unsupported_update_does_not_block_queue(bot, bale, storage):
    bale.get_updates.return_value = [
        update(10, {"edited_message": {"text": "edited"}}),
        update(11, message("/help")),
    ]
    bot.process_updates()
    assert bot.state["offset"] == 12
    bale.send.assert_called_once()


def test_stale_and_malformed_updates_are_ignored(bot, bale, storage):
    bot.state["offset"] = 10
    bale.get_updates.return_value = [
        {"update_id": 9, "message": {}},
        {"update_id": True},
        {"update_id": "10"},
        {},
    ]
    bot.process_updates()
    assert bot.state["offset"] == 10
    storage.save.assert_not_called()


def test_polling_drains_bounded_full_batches(bot, bale, storage):
    bale.get_updates.side_effect = [
        [update(batch * MAX_UPDATES_PER_RUN + i, {"unsupported": {}}) for i in range(MAX_UPDATES_PER_RUN)]
        for batch in range(3)
    ]
    bot.process_updates()
    assert bale.get_updates.call_count == 3
    assert bot.state["offset"] == 3 * MAX_UPDATES_PER_RUN


def test_both_buttons_work_end_to_end_with_legacy_ack(bot, bale, storage):
    bale.get_updates.side_effect = [
        [update(0, message(f"https://github.com/{REPO}"))],
        [update(1, callback("f:1:" + repo_tag(REPO), query_id="123456"))],
        [update(2, message(f"https://github.com/{REPO}", chat="2", sender="43"))],
        [update(3, callback("follow:0:" + repo_tag(REPO), chat="2", sender="43", query_id="123457"))],
    ]
    # Model the real client's legacy-client behavior without making network calls.
    bale.answer_callback.side_effect = lambda query_id: not query_id.startswith("1")
    for _ in range(4):
        bot.process_updates()
    assert bot.chat(CHAT)["repos"][REPO]["follow"] is True
    assert bot.chat("2")["repos"][REPO]["follow"] is False
    assert bot.state["offset"] == 4
    assert bale.send_document.call_count == 2


def test_link_download_creates_both_inline_and_reply_buttons(bot, bale, storage):
    bot.handle_message(message(f"https://github.com/{REPO}")["message"])
    record = bot.chat(CHAT)["repos"][REPO]
    assert record["follow"] is None
    assert record["requester"] == USER
    assert record["prompt_message_id"] == 99
    assert bot.chat(CHAT)["pending"] == REPO
    bale.send_document.assert_called_once()
    markups = [call.args[2] for call in bale.send.call_args_list if len(call.args) == 3]
    assert any("inline_keyboard" in markup for markup in markups)
    assert any("keyboard" in markup for markup in markups)
    storage.save.assert_called_once()


def test_inline_send_failure_uses_reply_keyboard(bot, bale):
    def send(chat_id, text, buttons=None):
        if buttons and "inline_keyboard" in buttons:
            raise FriendlyError("inline unavailable")
        return {"message_id": 99}

    bale.send.side_effect = send
    bot.handle_link(CHAT, USER, REPO)
    assert bot.chat(CHAT)["pending"] == REPO
    assert any("keyboard" in call.args[2] for call in bale.send.call_args_list if len(call.args) == 3)


def test_download_failure_is_reported_without_creating_subscription(bot, bale, storage):
    bot.github.snapshot.side_effect = FriendlyError("repository not found")
    bot.handle_link(CHAT, USER, REPO)
    bale.send_document.assert_not_called()
    storage.save.assert_not_called()
    assert REPO not in bot.chat(CHAT)["repos"]


def test_active_repo_can_be_downloaded_again_without_disabling_updates(pending_bot, bale):
    pending_bot.chat(CHAT)["repos"][REPO]["follow"] = True
    pending_bot.chat(CHAT)["pending"] = None
    pending_bot.handle_link(CHAT, USER, REPO)
    assert pending_bot.chat(CHAT)["repos"][REPO]["follow"] is True
    bale.send_document.assert_called_once()


def test_list_distinguishes_pending_and_download_only(bot, bale):
    bot.chat(CHAT)["repos"] = {
        "owner/a": {"follow": True},
        "owner/b": {"follow": False},
        "owner/c": {"follow": None},
    }
    bot.handle_message(message("/list@MyBot")["message"])
    text = bale.send.call_args.args[1]
    assert "آپدیت فعال" in text
    assert "فقط دانلود" in text
    assert "انتخاب نشده" in text


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"chat": None, "from": None},
        {"chat": {"id": 1}, "from": {"id": 2}, "text": {}},
        {"chat": {"id": 1}, "from": {"id": 2, "is_bot": True}, "text": "/start"},
    ],
)
def test_invalid_messages_do_not_crash(bot, payload):
    bot.handle_message(payload)


def test_following_chat_receives_delta_and_advances_only_after_upload(pending_bot, github, bale, storage):
    record = pending_bot.chat(CHAT)["repos"][REPO]
    record["follow"] = True
    pending_bot.chat(CHAT)["pending"] = None
    github.snapshot.return_value = Snapshot(
        REPO, "b" * 40, {"README.md": "blob-a", "src/app.py": "changed"}, 120
    )
    source = io.BytesIO()

    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("root/README.md", "same")
        archive.writestr("root/src/app.py", "changed")
    github.download_zip.return_value = source.getvalue()
    pending_bot.deliver_updates()
    bale.send_document.assert_called_once()
    assert record["sha"] == "b" * 40
    storage.save.assert_called_once()


def test_download_only_chat_never_receives_automatic_updates(pending_bot, bale):
    pending_bot.chat(CHAT)["repos"][REPO]["follow"] = False
    pending_bot.deliver_updates()
    pending_bot.github.snapshot.assert_not_called()
    bale.send_document.assert_not_called()
    bale.send.assert_not_called()


def test_failed_delta_upload_keeps_baseline_for_retry(pending_bot, bale, storage):
    record = pending_bot.chat(CHAT)["repos"][REPO]
    record["follow"] = True
    before = deepcopy(record)
    pending_bot.github.snapshot.return_value = Snapshot(REPO, "b" * 40, {"new.txt": "new"}, 1)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("root/new.txt", "new")
    pending_bot.github.download_zip.return_value = archive.getvalue()
    bale.send_document.side_effect = FriendlyError("upload failed")
    pending_bot.deliver_updates()
    assert record["sha"] == before["sha"]
    assert record["files"] == before["files"]
    assert record["last_error"] == "upload failed"
    bale.send_document.side_effect = None
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("root/new.txt", "new")
    pending_bot.github.download_zip.return_value = source.getvalue()
    pending_bot.deliver_updates()
    assert record["sha"] == "b" * 40
    assert "last_error" not in record


def test_repeated_error_not_spammed(pending_bot, bale, storage):
    record = pending_bot.chat(CHAT)["repos"][REPO]
    record["follow"] = True
    pending_bot.github.snapshot.side_effect = FriendlyError("GitHub unavailable")
    pending_bot.deliver_updates()
    pending_bot.deliver_updates()
    assert bale.send.call_count == 1
    assert storage.save.call_count == 1


def test_delete_only_update_sends_complete_report(pending_bot, bale):
    record = pending_bot.chat(CHAT)["repos"][REPO]
    record.update(follow=True, files={"removed-" + str(i): "sha" for i in range(100)})
    pending_bot.github.snapshot.return_value = Snapshot(REPO, "b" * 40, {}, 0)
    pending_bot.deliver_updates()
    bale.send_document.assert_not_called()
    text = "\n".join(item.args[1] for item in bale.send.call_args_list)
    assert all(f"removed-{i}" in text for i in range(100))
    assert all(len(item.args[1]) <= 4096 for item in bale.send.call_args_list)
    assert record["files"] == {}


def test_no_change_does_not_send_or_save(pending_bot, bale, storage):
    pending_bot.chat(CHAT)["repos"][REPO]["follow"] = True
    pending_bot.deliver_updates()
    bale.send.assert_not_called()
    bale.send_document.assert_not_called()
    storage.save.assert_not_called()


def test_state_storage_error_is_not_swallowed_as_repository_error(pending_bot, storage):
    pending_bot.chat(CHAT)["repos"][REPO]["follow"] = True
    pending_bot.github.snapshot.return_value = Snapshot(REPO, "b" * 40, {}, 0)
    storage.save.side_effect = RuntimeError("conflict")
    with pytest.raises(RuntimeError, match="conflict"):
        pending_bot.deliver_updates()
