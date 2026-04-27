"""测试 prompt_builder 的 prompt 模板选择。"""

import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.prompt_builder as prompt_builder_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip prompt builder tests: missing dependency ({exc})", allow_module_level=True)


_TEMPLATE_VARS = "{agent_name} {display_name} {soul} {status_fields} {player_fields} {valid_targets}"


def _patch_templates(monkeypatch, character: str, narrator: str) -> None:
    monkeypatch.setattr(prompt_builder_module, "CHARACTER", character)
    monkeypatch.setattr(prompt_builder_module, "NARRATOR", narrator)


def test_character_system_prompt_reads_character_template(monkeypatch):
    _patch_templates(monkeypatch, "CHARACTER " + _TEMPLATE_VARS, "NARRATOR " + _TEMPLATE_VARS)
    monkeypatch.setattr(prompt_builder_module, "get_allowed_fields", lambda agent_name, field: [])
    monkeypatch.setattr(
        prompt_builder_module,
        "get_agent_names",
        lambda include_narrator=False: [],
    )
    monkeypatch.setattr(
        prompt_builder_module,
        "read_agent_file",
        lambda agent_name, filename: "",
    )

    result = prompt_builder_module.build_system_prompt("mitsuki", "# 美月")

    assert result.startswith("CHARACTER mitsuki")


def test_narrator_system_prompt_reads_narrator_template(monkeypatch):
    _patch_templates(monkeypatch, "CHARACTER " + _TEMPLATE_VARS, "NARRATOR {soul}")
    monkeypatch.setattr(prompt_builder_module, "get_allowed_fields", lambda agent_name, field: [])
    monkeypatch.setattr(
        prompt_builder_module,
        "get_agent_names",
        lambda include_narrator=False: [],
    )
    monkeypatch.setattr(
        prompt_builder_module,
        "read_agent_file",
        lambda agent_name, filename: "",
    )

    result = prompt_builder_module.build_system_prompt("narrator", "# 旁白")

    assert result == "NARRATOR # 旁白"


def test_character_system_prompt_uses_other_character_display_names_for_relations(monkeypatch):
    _patch_templates(monkeypatch, "{valid_targets}", "{valid_targets}")
    monkeypatch.setattr(prompt_builder_module, "get_allowed_fields", lambda agent_name, field: [])
    monkeypatch.setattr(
        prompt_builder_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki", "lilith"],
    )
    monkeypatch.setattr(
        prompt_builder_module,
        "read_agent_file",
        lambda agent_name, filename: {"mitsuki": "# 美月\n", "lilith": "# 莉莉丝\n"}.get(
            agent_name, ""
        ),
    )

    result = prompt_builder_module.build_system_prompt("mitsuki", "# 美月")

    assert result == "莉莉丝"


