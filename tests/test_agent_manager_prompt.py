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


_TEMPLATE_VARS = "{agent_name} {display_name} {soul} {status_fields} {player_fields} {characters_scene_list} {valid_targets}"


def test_character_system_prompt_reads_character_template(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_prompt.txt").write_text("CHARACTER " + _TEMPLATE_VARS, encoding="utf-8")
    (prompts_dir / "narrator_prompt.txt").write_text("NARRATOR " + _TEMPLATE_VARS, encoding="utf-8")

    monkeypatch.setattr(prompt_builder_module, "PROJECT_ROOT", tmp_path)
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


def test_narrator_system_prompt_reads_narrator_template(tmp_path, monkeypatch):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_prompt.txt").write_text("CHARACTER " + _TEMPLATE_VARS, encoding="utf-8")
    (prompts_dir / "narrator_prompt.txt").write_text("NARRATOR " + _TEMPLATE_VARS, encoding="utf-8")

    monkeypatch.setattr(prompt_builder_module, "PROJECT_ROOT", tmp_path)
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

    assert result.startswith("NARRATOR narrator")
