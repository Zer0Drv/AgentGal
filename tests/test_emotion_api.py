"""情绪系统测试：/api/characters 返回情绪与状态字段（前端数据源）。"""

import pytest
from fastapi.testclient import TestClient

import repository.emotion_store as store
import server as server_module
from server import app

client = TestClient(app)

_FAKE_STATUS = (
    "# 我的状态\n"
    "## 身份\n偶像\n"
    "## 心境\n有点紧张\n"
    "## 在意的事\n他今天怎么了\n"
)


@pytest.fixture
def fake_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server_module, "get_agent_names", lambda include_narrator=True: ["mitsuki"]
    )
    monkeypatch.setattr(
        server_module, "read_agent_file", lambda name, ftype: _FAKE_STATUS
    )
    monkeypatch.setattr(server_module, "_get_agent_display_name", lambda name: "一之濑美月")
    monkeypatch.setattr(store, "emotions_path", lambda name: str(tmp_path / name / "emotions.jsonl"))
    return store


def test_api_characters_includes_emotion_and_status_fields(fake_environment):
    fake_environment.append_emotion("mitsuki", "开心", turn=2)
    fake_environment.append_emotion("mitsuki", "害羞", turn=3)

    resp = client.get("/api/characters")
    assert resp.status_code == 200
    chars = {c["name"]: c for c in resp.json()["characters"]}
    assert "mitsuki" in chars
    mitsuki = chars["mitsuki"]
    assert mitsuki["emotion"] == "害羞"
    assert mitsuki["emotion_history"] == ["开心", "害羞"]
    assert mitsuki["mood"] == "有点紧张"
    assert mitsuki["concern"] == "他今天怎么了"


def test_api_characters_empty_emotion_fields(fake_environment):
    resp = client.get("/api/characters")
    assert resp.status_code == 200
    mitsuki = {c["name"]: c for c in resp.json()["characters"]}["mitsuki"]
    assert mitsuki["emotion"] == ""
    assert mitsuki["emotion_history"] == []
