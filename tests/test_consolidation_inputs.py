"""测试记忆归并输入构造与 raw 对话重标记。"""

import os
import sys
from pathlib import Path

# 设置项目根目录
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

from consolidation.inputs import (
    build_episode_memory_generator_payload,
    build_memory_owner_block,
    format_raw_dialogue_for_owner,
)


def test_format_raw_dialogue_for_character_owner(monkeypatch):
    """角色整理时应把自己/玩家/旁白/其他角色映射成统一视角。"""

    def mock_history(limit: int):
        assert limit == 20
        return [
            {
                "role": "narrator",
                "content": "**时间**：10月6日 星期五 11:42\n**地点**：舒芙蕾店",
                "visible_to": ["chenxiao", "narrator"],
            },
            {
                "role": "chenxiao",
                "content": "## 陈晓\n（看着他）……那我点了哦。",
                "visible_to": ["chenxiao", "narrator"],
            },
            {
                "role": "player",
                "content": "我猜啊，你吃不完的是我的",
                "visible_to": ["chenxiao", "narrator"],
            },
            {
                "role": "guyining",
                "content": "## 顾以宁\n先把文件发我。",
                "visible_to": ["chenxiao", "guyining", "narrator"],
            },
            {
                "role": "mitsuki",
                "content": "你们继续。",
                "visible_to": ["mitsuki", "narrator"],
            },
        ]

    monkeypatch.setattr("consolidation.inputs.load_conversation_history", mock_history)

    formatted = format_raw_dialogue_for_owner("chenxiao", 20)

    assert "旁白：**时间**：10月6日 星期五 11:42" in formatted
    assert "我：（看着他）……那我点了哦。" in formatted
    assert "他：我猜啊，你吃不完的是我的" in formatted
    assert "顾以宁：先把文件发我。" in formatted
    assert "mitsuki" not in formatted
    assert "## 陈晓" not in formatted
    assert "## 顾以宁" not in formatted


def test_build_episode_memory_generator_payload_includes_owner_before_memory(monkeypatch):
    """EpisodeMemoryGenerator payload 应先注入记忆主体，再附带 memory 与 raw 对话。"""

    monkeypatch.setattr(
        "consolidation.inputs.read_agent_file",
        lambda agent_name, filename: "<role>陈晓</role>" if filename == "soul.md" else "",
    )

    payload = build_episode_memory_generator_payload(
        "chenxiao",
        "## 10月6日\n- **时间**：10月6日 上午\n- **地点**：公司\n- **在场**：我、他\n- **内容**：测试内容。",
        raw_dialogue="旁白：**时间**：10月6日 星期五 11:42",
    )

    assert payload.index("<memory_owner>") < payload.index("<memory_entries>")
    assert "<raw_dialogue>" in payload
    assert "当前整理对象：陈晓（agent_name=chenxiao）" in payload
    assert "- 我 = 陈晓" in payload
    assert "- 他 = 玩家" in payload
    assert "旁白：**时间**：10月6日 星期五 11:42" in payload
