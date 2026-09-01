"""「我的人设」API 测试：GET/POST /api/player/persona。"""

import pytest
from fastapi.testclient import TestClient

import repository.player_persona as pp
from server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "CHARACTERS_DIR", tmp_path)
    yield


def test_get_persona_default_is_empty():
    resp = client.get("/api/player/persona")
    assert resp.status_code == 200
    assert resp.json()["persona"] == ""


def test_post_then_get_roundtrip():
    body = {"persona": "<identity>程序员</identity>\n<voice>冷静</voice>"}
    resp = client.post("/api/player/persona", json=body)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "程序员" in resp.json()["persona"]

    got = client.get("/api/player/persona").json()["persona"]
    assert got == body["persona"]


def test_post_empty_is_noop():
    resp = client.post("/api/player/persona", json={"persona": "   "})
    assert resp.status_code == 200
    assert resp.json()["persona"] == ""  # 空文本不写文件，仍为空


def test_persona_file_persisted_on_disk(tmp_path):
    client.post("/api/player/persona", json={"persona": "<goal>变强</goal>"})
    saved = (tmp_path / ".player_persona.md").read_text(encoding="utf-8")
    assert "变强" in saved
