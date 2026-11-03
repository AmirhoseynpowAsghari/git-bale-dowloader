import json
from unittest.mock import Mock

import pytest
import requests

from bale_downloader.bale import Bale
from bale_downloader.errors import ConfigurationError, FriendlyError


def api_response(*, ok=True, result=None, status=200):
    resp = Mock()
    resp.ok = ok
    resp.status_code = status
    resp.json.return_value = {"ok": ok, "result": result}
    return resp


@pytest.fixture
def client():
    client = Bale("SECRET_TOKEN")
    client.session = Mock()
    client.session.post.return_value = api_response(result=True)
    return client


@pytest.mark.parametrize("query_id", ["123456789", "1"])
def test_legacy_clients_skip_unsupported_ack(client, query_id):
    assert client.answer_callback(query_id) is False
    client.session.post.assert_not_called()


def test_modern_callback_is_acknowledged(client):
    assert client.answer_callback("23456789", "saved") is True
    args, kwargs = client.session.post.call_args
    assert args[0].endswith("/answerCallbackQuery")
    assert kwargs["json"] == {"callback_query_id": "23456789", "text": "saved"}


def test_ack_error_is_nonfatal(client):
    client.session.post.return_value = api_response(ok=False, status=400)
    assert client.answer_callback("23456789") is False


def test_empty_callback_id_is_ignored(client):
    assert client.answer_callback("") is False
    client.session.post.assert_not_called()


def test_inline_keyboard_sent_as_json_object(client):
    markup = {"inline_keyboard": [[{"text": "Yes", "callback_data": "follow:1:abcdef0123456789"}]]}
    client.send("1", "Choose", markup)
    assert client.session.post.call_args.kwargs["json"]["reply_markup"] == markup


def test_multipart_nested_markup_is_json_serialized(client):
    client.send_document("1", "repo.zip", b"zip", "caption")
    kwargs = client.session.post.call_args.kwargs
    assert kwargs["data"]["chat_id"] == "1"
    assert kwargs["files"]["document"][1].read() == b"zip"


def test_network_error_never_leaks_token(client):
    client.session.post.side_effect = requests.ConnectionError(
        "https://tapi.bale.ai/botSECRET_TOKEN/getUpdates"
    )
    with pytest.raises(FriendlyError) as error:
        client.get_updates(0)
    assert "SECRET_TOKEN" not in str(error.value)


def test_api_error_does_not_echo_untrusted_description(client):
    resp = api_response(ok=False, status=400)
    resp.json.return_value["description"] = "SECRET_TOKEN"
    client.session.post.return_value = resp
    with pytest.raises(FriendlyError) as error:
        client.send("1", "hello")
    assert "SECRET_TOKEN" not in str(error.value)


def test_polling_requests_current_offset(client):
    client.session.post.return_value = api_response(result=[{"update_id": 10}])
    assert client.get_updates(10) == [{"update_id": 10}]
    assert client.session.post.call_args.kwargs["json"]["offset"] == 10


def test_invalid_update_response_rejected(client):
    client.session.post.return_value = api_response(result={"not": "a list"})
    with pytest.raises(FriendlyError):
        client.get_updates(0)


def test_active_webhook_blocks_polling_without_opt_in(client):
    client.session.post.return_value = api_response(result={"url": "https://example.org/hook"})
    with pytest.raises(ConfigurationError, match="webhook"):
        client.prepare_polling()
    assert client.session.post.call_count == 1


def test_webhook_reset_requires_explicit_opt_in_and_preserves_pending(client):
    client.session.post.side_effect = [
        api_response(result={"url": "https://example.org/hook"}),
        api_response(result=True),
    ]
    client.prepare_polling(reset_webhook=True)
    args, kwargs = client.session.post.call_args
    assert args[0].endswith("/deleteWebhook")
    assert kwargs["json"] == {"drop_pending_updates": False}


def test_polling_without_webhook_does_not_delete_anything(client):
    client.session.post.return_value = api_response(result={"url": ""})
    client.prepare_polling()
    assert client.session.post.call_count == 1


def test_clear_keyboard_error_is_nonfatal(client):
    client.session.post.return_value = api_response(ok=False, status=400)
    client.clear_inline("1", 99)


def test_send_reply_markup_survives_json_serialization(client):
    client.send("1", "test", {"remove_keyboard": True})
    body = client.session.post.call_args.kwargs["json"]
    assert json.loads(json.dumps(body))["reply_markup"] == {"remove_keyboard": True}
