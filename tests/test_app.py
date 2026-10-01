import os
import sys
from unittest.mock import MagicMock

import pytest
import requests

os.environ["YANDEX_TOKEN"] = "test-token"
os.environ["WEBHOOK_TOKEN"] = "secret"
os.environ["ALERTMANAGER_BASE_URL"] = "https://alertmanager.example.com"
os.environ["BACKEND_CHAT_ID"] = "chat-1"
os.environ["DATA_PLATFORM_CHAT_ID"] = "chat-2, chat-3"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import app as proxy  # noqa: E402

AUTH = {"Authorization": "Bearer secret"}


def alert(team="backend", status="firing", annotations=None, **labels):
    return {
        "status": status,
        "labels": {"alertname": "HighErrorRate", "severity": "critical", "team": team, **labels},
        "annotations": annotations or {},
    }


@pytest.fixture
def client():
    return proxy.app.test_client()


@pytest.fixture
def post(monkeypatch):
    mock = MagicMock(return_value=MagicMock(raise_for_status=MagicMock()))
    monkeypatch.setattr(proxy.session, "post", mock)
    return mock


def test_health(client):
    assert client.get("/health").status_code == 200


def test_rejects_missing_or_wrong_token(client, post):
    assert client.post("/alert", json={"alerts": [alert()]}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/alert", json={"alerts": [alert()]}, headers=wrong).status_code == 401
    post.assert_not_called()


def test_invalid_json(client, post):
    r = client.post("/alert", data="not json", headers=AUTH)
    assert r.status_code == 400
    post.assert_not_called()


def test_sends_to_team_chat(client, post):
    r = client.post("/alert", json={"alerts": [alert()]}, headers=AUTH)
    assert r.status_code == 200
    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["json"]["chat_id"] == "chat-1"
    assert kwargs["headers"]["Authorization"] == "OAuth test-token"
    assert "🔴 FIRING" in kwargs["json"]["text"]
    assert "Alert: HighErrorRate" in kwargs["json"]["text"]


def test_multiple_teams_and_chats(client, post):
    r = client.post("/alert", json={"alerts": [alert(team="backend, data-platform")]}, headers=AUTH)
    assert r.status_code == 200
    sent = sorted(c.kwargs["json"]["chat_id"] for c in post.call_args_list)
    assert sent == ["chat-1", "chat-2", "chat-3"]


def test_unknown_team_is_skipped(client, post):
    r = client.post("/alert", json={"alerts": [alert(team="nobody")]}, headers=AUTH)
    assert r.status_code == 200
    post.assert_not_called()


def test_all_failed_returns_500(client, post):
    post.side_effect = requests.ConnectionError("boom")
    r = client.post("/alert", json={"alerts": [alert()]}, headers=AUTH)
    assert r.status_code == 500


def test_partial_failure_returns_207(client, post):
    ok = MagicMock(raise_for_status=MagicMock())
    post.side_effect = [ok, requests.ConnectionError("boom")]
    r = client.post("/alert", json={"alerts": [alert(team="data-platform")]}, headers=AUTH)
    assert r.status_code == 207


def test_grafana_url_gets_namespace():
    url = proxy.get_grafana_url(
        {"grafana_url": "https://grafana.example.com/d/abc?orgId=1"}, {"namespace": "shop"}
    )
    assert url == "https://grafana.example.com/d/abc?orgId=1&var-namespace=shop"


def test_silence_url_generated_from_labels():
    url = proxy.get_silence_url({}, {"alertname": "A B", "app": "api"}, "https://am.example.com")
    assert url == (
        "https://am.example.com/#/silences/new?filter=%7Balertname%3D%22A%20B%22%2Capp%3D%22api%22%7D"
    )


def test_silence_url_omitted_without_base_url():
    assert proxy.get_silence_url({}, {"alertname": "A"}, "") is None


def test_silence_url_from_annotation_wins():
    assert proxy.get_silence_url({"silence_url": "https://x"}, {"alertname": "A"}, "") == "https://x"


def test_message_format():
    text = proxy.format_message(
        "resolved",
        {"alertname": "DiskFull", "severity": "warning", "team": "backend", "env": "prod"},
        {"summary": "Disk is full", "description": "Usage 91.23456%", "runbook": "https://wiki.example.com/disk"},
    )
    assert text.startswith("🟢 RESOLVED\nAlert: DiskFull\nSeverity: WARNING\n")
    assert "Env: prod" in text
    assert "Description: Usage 91.235%" in text
    assert "Runbook: https://wiki.example.com/disk" in text
