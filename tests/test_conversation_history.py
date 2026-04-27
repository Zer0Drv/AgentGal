"""测试对话历史加载功能

测试 load_conversation_history 能否跨日期加载消息
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 设置项目根目录
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

from shared.config import character_path
from storage.history import load_conversation_history


@pytest.fixture
def temp_raw_dir(tmp_path, monkeypatch):
    """创建临时 raw 目录并 monkeypatch character_path"""
    raw_dir = tmp_path / "narrator" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # monkeypatch character_path 返回临时路径
    original_character_path = character_path
    
    def mock_character_path(agent_name: str, *subpaths: str) -> str:
        if agent_name == "narrator" and subpaths and subpaths[0] == "raw":
            return str(raw_dir / subpaths[1] if len(subpaths) > 1 else raw_dir)
        return original_character_path(agent_name, *subpaths)
    
    monkeypatch.setattr("storage.history.character_path", mock_character_path)
    return raw_dir


def test_load_conversation_history_single_day(temp_raw_dir):
    """测试加载单日对话历史"""
    # 创建今天的 jsonl 文件
    today = datetime.now().strftime("%Y-%m-%d")
    jsonl_file = temp_raw_dir / f"{today}.jsonl"
    
    messages = [
        {"role": "player", "content": "你好", "visible_to": ["narrator"]},
        {"role": "narrator", "content": "你好，欢迎", "visible_to": ["narrator"]},
    ]
    
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    
    # 加载历史
    result = load_conversation_history(limit=10)
    
    assert len(result) == 2, f"应该加载 2 条消息，实际 {len(result)}"
    assert result[0]["role"] == "player"
    assert result[1]["role"] == "narrator"


def test_load_conversation_history_multiple_days(temp_raw_dir):
    """测试跨日期加载对话历史（解决早上找不到最近对话的问题）"""
    # 创建昨天的 jsonl 文件
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    jsonl_file_yesterday = temp_raw_dir / f"{yesterday}.jsonl"
    
    messages_yesterday = [
        {"role": "player", "content": "昨天的消息", "visible_to": ["narrator"]},
        {"role": "narrator", "content": "昨天的回复", "visible_to": ["narrator"]},
    ]
    
    with open(jsonl_file_yesterday, "w", encoding="utf-8") as f:
        for msg in messages_yesterday:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    
    # 创建今天的 jsonl 文件
    today = datetime.now().strftime("%Y-%m-%d")
    jsonl_file_today = temp_raw_dir / f"{today}.jsonl"
    
    messages_today = [
        {"role": "player", "content": "今天的消息", "visible_to": ["narrator"]},
    ]
    
    with open(jsonl_file_today, "w", encoding="utf-8") as f:
        for msg in messages_today:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    
    # 加载最近 5 条（应该包含昨天和今天的）
    result = load_conversation_history(limit=5)
    
    assert len(result) == 3, f"应该加载 3 条消息，实际 {len(result)}"
    assert result[0]["content"] == "昨天的消息"
    assert result[1]["content"] == "昨天的回复"
    assert result[2]["content"] == "今天的消息"


def test_load_conversation_history_limit(temp_raw_dir):
    """测试 limit 参数正确限制返回数量"""
    today = datetime.now().strftime("%Y-%m-%d")
    jsonl_file = temp_raw_dir / f"{today}.jsonl"
    
    # 创建 10 条消息
    messages = [
        {"role": "player", "content": f"消息 {i}", "visible_to": ["narrator"]}
        for i in range(10)
    ]
    
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    
    # 只加载最近 3 条
    result = load_conversation_history(limit=3)
    
    assert len(result) == 3, f"应该加载 3 条消息，实际 {len(result)}"
    assert result[0]["content"] == "消息 7"
    assert result[1]["content"] == "消息 8"
    assert result[2]["content"] == "消息 9"


def test_load_conversation_history_all_when_no_args(temp_raw_dir):
    """不传 limit / turns 时返回全部历史"""
    today = datetime.now().strftime("%Y-%m-%d")
    jsonl_file = temp_raw_dir / f"{today}.jsonl"

    messages = [
        {"role": "player", "content": f"消息 {i}", "visible_to": ["narrator"]}
        for i in range(4)
    ]

    with open(jsonl_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    result = load_conversation_history()

    assert len(result) == 4
    assert result[0]["content"] == "消息 0"
    assert result[-1]["content"] == "消息 3"


def test_load_conversation_history_empty(temp_raw_dir):
    """测试空目录返回空列表"""
    result = load_conversation_history(limit=10)
    assert result == [], "空目录应该返回空列表"


def test_load_conversation_history_by_turns(temp_raw_dir):
    """turns 参数按 turn 号回溯，包含所有 turn 中的全部消息"""
    today = datetime.now().strftime("%Y-%m-%d")
    jsonl_file = temp_raw_dir / f"{today}.jsonl"

    # 5 个 turn，每 turn 2 条消息
    messages = []
    for turn in range(1, 6):
        messages.append(
            {"role": "player", "content": f"t{turn}-玩家", "turn": turn, "visible_to": ["narrator"]}
        )
        messages.append(
            {"role": "narrator", "content": f"t{turn}-旁白", "turn": turn, "visible_to": ["narrator"]}
        )

    with open(jsonl_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # 最近 2 个 turn = turn 4 + turn 5 = 4 条消息
    result = load_conversation_history(turns=2)
    assert [m["content"] for m in result] == [
        "t4-玩家",
        "t4-旁白",
        "t5-玩家",
        "t5-旁白",
    ]


def test_load_conversation_history_limit_and_turns_mutually_exclusive(temp_raw_dir):
    with pytest.raises(ValueError):
        load_conversation_history(limit=3, turns=1)
