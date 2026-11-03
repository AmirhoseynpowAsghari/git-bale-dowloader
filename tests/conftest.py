from unittest.mock import Mock

import pytest

from bale_downloader.bale import Bale
from bale_downloader.bot import Bot
from bale_downloader.github import GitHub
from bale_downloader.models import Snapshot, empty_state
from bale_downloader.storage import Storage

REPO = "octocat/Hello-World"
CHAT = "-1001"
USER = "42"


@pytest.fixture
def github():
    client = Mock(spec=GitHub)
    client.snapshot.return_value = Snapshot(
        REPO, "a" * 40, {"README.md": "blob-a", "src/app.py": "blob-b"}, 120
    )
    client.download_zip.return_value = b"PK-test-zip"
    return client


@pytest.fixture
def bale():
    client = Mock(spec=Bale)
    client.send.return_value = {"message_id": 99}
    client.send_document.return_value = {"message_id": 98}
    client.get_updates.return_value = []
    return client


@pytest.fixture
def storage():
    return Mock(spec=Storage)


@pytest.fixture
def state():
    return empty_state()


@pytest.fixture
def bot(github, bale, storage, state):
    return Bot(github, bale, storage, state)


@pytest.fixture
def pending_bot(bot):
    bot.chat(CHAT)["repos"][REPO] = {
        "sha": "a" * 40,
        "files": {"README.md": "blob-a", "src/app.py": "blob-b"},
        "follow": None,
        "requester": USER,
        "prompt_message_id": 99,
        "choice_confirmed": False,
    }
    bot.chat(CHAT)["pending"] = REPO
    return bot


def message(text, *, chat=CHAT, sender=USER):
    return {"message": {"chat": {"id": chat}, "from": {"id": sender}, "text": text}}


def callback(data, *, chat=CHAT, sender=USER, query_id="23456789", message_id=99):
    return {
        "callback_query": {
            "id": query_id,
            "from": {"id": sender},
            "data": data,
            "message": {"chat": {"id": chat}, "message_id": message_id},
        }
    }
