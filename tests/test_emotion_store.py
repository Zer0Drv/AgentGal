"""情绪系统测试：emotion_store（emotions.jsonl 轨迹读写）。"""

import json

import repository.emotion_store as store_module
from repository.emotion_store import append_emotion, read_all_emotions, read_recent_emotions


def _patch_paths(tmp_path, monkeypatch):
    def fake_path(name, *subpaths):
        return str(tmp_path / name / "/".join(subpaths))

    monkeypatch.setattr(store_module, "character_path", fake_path)


def test_append_and_read_all(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    append_emotion("mitsuki", "开心", turn=3, date="2月20日", time="傍晚", reason="他来了")
    append_emotion("mitsuki", "有点害羞", turn=4, date="2月20日", time="夜", reason="他夸我")

    records = read_all_emotions("mitsuki")
    assert len(records) == 2
    assert records[0] == {
        "turn": 3,
        "date": "2月20日",
        "time": "傍晚",
        "emotion": "开心",
        "reason": "他来了",
    }
    assert records[1]["emotion"] == "有点害羞"


def test_read_recent_returns_newest_first(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    for i in range(5):
        append_emotion("mitsuki", f"情绪{i}", turn=i)

    recent = read_recent_emotions("mitsuki", limit=3)
    assert [r["emotion"] for r in recent] == ["情绪4", "情绪3", "情绪2"]


def test_read_empty_when_no_file(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    assert read_all_emotions("mitsuki") == []
    assert read_recent_emotions("mitsuki") == []


def test_skip_corrupted_lines(tmp_path, monkeypatch):
    _patch_paths(tmp_path, monkeypatch)
    path = store_module.emotions_path("mitsuki")
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"turn": 1, "emotion": "开心"}, ensure_ascii=False) + "\n")
        f.write("not-json\n")
        f.write(json.dumps({"turn": 2, "emotion": "难过"}, ensure_ascii=False) + "\n")

    records = read_all_emotions("mitsuki")
    assert [r["emotion"] for r in records] == ["开心", "难过"]
