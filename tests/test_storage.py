import base64
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet

from bale_downloader.models import empty_state, normalize_state
from bale_downloader.storage import Storage


def response(data=None, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = data
    return resp


@pytest.fixture
def storage():
    storage = Storage("GH_SECRET", "owner/host", "BALE_SECRET", "head-sha")
    storage.session = Mock()
    storage.branch_exists = True
    return storage


def encrypted(storage, state):
    return storage.cipher.encrypt(json.dumps(state).encode())


def test_encrypted_state_round_trip_uses_blob_sha(storage):
    state = empty_state()
    state["chats"]["1"] = {
        "repos": {
            "owner/repo": {
                "sha": "head",
                "files": {" Persian.txt": "sha"},
                "follow": True,
                "requester": "42",
            }
        },
        "pending": None,
    }
    cipher = encrypted(storage, state)
    storage.session.request.side_effect = [
        response(
            {
                "sha": "old-sha",
                "size": len(cipher),
                "encoding": "base64",
                "content": base64.b64encode(cipher).decode(),
            }
        ),
        response({"content": {"sha": "new-sha"}}),
    ]
    assert storage.load() == state
    storage.save(state)
    payload = storage.session.request.call_args.kwargs["json"]
    assert payload["sha"] == "old-sha"
    assert "owner/repo" not in payload["content"]
    assert (
        storage.cipher.decrypt(base64.b64decode(payload["content"]))
        == json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert storage.file_sha == "new-sha"


def test_missing_state_file_initializes_empty_state(storage):
    storage.session.request.return_value = response(status=404)
    assert storage.load() == empty_state()


def test_existing_v1_state_migrates_without_losing_choice(storage):
    old = {
        "offset": 12,
        "chats": {
            "1": {
                "repos": {
                    "owner/repo": {
                        "sha": "head",
                        "files": {},
                        "follow": True,
                        "requester": "42",
                    }
                },
                "pending": None,
            }
        },
    }
    cipher = encrypted(storage, old)
    storage.session.request.return_value = response(
        {
            "sha": "blob",
            "size": len(cipher),
            "encoding": "base64",
            "content": base64.b64encode(cipher).decode(),
        }
    )
    loaded = storage.load()
    assert loaded["version"] == 2
    assert loaded["offset"] == 12
    assert loaded["chats"]["1"]["repos"]["owner/repo"]["follow"] is True


def test_large_state_uses_blob_api(storage):
    cipher = encrypted(storage, empty_state())
    storage.session.request.side_effect = [
        response({"sha": "large-blob", "size": 2_000_000, "encoding": "none", "content": ""}),
        response({"encoding": "base64", "content": base64.b64encode(cipher).decode()}),
    ]
    assert storage.load() == empty_state()
    assert "/git/blobs/large-blob" in storage.session.request.call_args.args[1]


def test_wrong_bot_token_does_not_overwrite_state(storage):
    cipher = Fernet(Fernet.generate_key()).encrypt(b"{}")
    storage.session.request.return_value = response(
        {
            "sha": "blob",
            "size": len(cipher),
            "encoding": "base64",
            "content": base64.b64encode(cipher).decode(),
        }
    )
    with pytest.raises(RuntimeError, match="decryption"):
        storage.load()
    assert not any(call.args[0] == "PUT" for call in storage.session.request.call_args_list)


def test_state_conflict_fails_safely(storage):
    storage.file_sha = "stale"
    storage.session.request.return_value = response(status=409)
    with pytest.raises(RuntimeError, match="409"):
        storage.save(empty_state())


def test_branch_creation_race_is_verified(storage):
    storage.branch_exists = False
    storage.session.request.side_effect = [
        response(status=404),
        response(status=422),
        response({"ref": "refs/heads/state"}),
    ]
    storage._ensure_branch()
    assert storage.branch_exists is True
    assert storage.session.request.call_count == 3


def test_invalid_state_is_not_silently_reset(storage):
    cipher = encrypted(storage, {"version": 999})
    storage.session.request.return_value = response(
        {
            "sha": "blob",
            "size": len(cipher),
            "encoding": "base64",
            "content": base64.b64encode(cipher).decode(),
        }
    )
    with pytest.raises(RuntimeError, match="invalid"):
        storage.load()


@pytest.mark.parametrize("pending", ["missing", [], {}, 1])
def test_normalize_does_not_mutate_original(pending):
    old = {"offset": 0, "chats": {"1": {"repos": {}, "pending": pending}}}
    before = deepcopy(old)
    normalized = normalize_state(old)
    assert old == before
    assert normalized["chats"]["1"]["pending"] is None


@pytest.mark.parametrize(
    "state",
    [
        {"offset": -1},
        {"offset": True},
        {"chats": []},
        {"version": 99},
        {"chats": {"1": {"repos": {"x": {"sha": 1, "files": {}}}}}},
    ],
)
def test_invalid_state_shapes_rejected(state):
    with pytest.raises(ValueError):
        normalize_state(state)
