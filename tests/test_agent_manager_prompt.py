"""测试 agent_manager 的 prompt 模板选择。"""

import ast
import os
import re
from pathlib import Path

project_root = Path(__file__).parent.parent
os.chdir(project_root)


def _load_build_system_prompt():
    source_path = project_root / "engine" / "agent_manager.py"
    module_ast = ast.parse(source_path.read_text(encoding="utf-8"))
    selected_nodes = [
        node
        for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_get_display_name", "_build_system_prompt"}
    ]
    namespace: dict[str, object] = {"Path": Path, "re": re}
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=selected_nodes, type_ignores=[])),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    return namespace["_build_system_prompt"]


def test_character_system_prompt_reads_character_template(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_prompt.txt").write_text("CHARACTER {agent_name}", encoding="utf-8")
    (prompts_dir / "narrator_prompt.txt").write_text("NARRATOR {agent_name}", encoding="utf-8")

    build_system_prompt = _load_build_system_prompt()
    build_system_prompt.__globals__["PROJECT_ROOT"] = tmp_path
    build_system_prompt.__globals__["get_allowed_fields"] = lambda agent_name, field: []
    build_system_prompt.__globals__["get_agent_names"] = lambda include_narrator=False: []
    build_system_prompt.__globals__["read_agent_file"] = lambda agent_name, filename: ""

    result = build_system_prompt("mitsuki", "# 美月")

    assert result == "CHARACTER mitsuki"


def test_narrator_system_prompt_reads_narrator_template(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_prompt.txt").write_text("CHARACTER {agent_name}", encoding="utf-8")
    (prompts_dir / "narrator_prompt.txt").write_text("NARRATOR {agent_name}", encoding="utf-8")

    build_system_prompt = _load_build_system_prompt()
    build_system_prompt.__globals__["PROJECT_ROOT"] = tmp_path
    build_system_prompt.__globals__["get_allowed_fields"] = lambda agent_name, field: []
    build_system_prompt.__globals__["get_agent_names"] = lambda include_narrator=False: []
    build_system_prompt.__globals__["read_agent_file"] = lambda agent_name, filename: ""

    result = build_system_prompt("narrator", "# 旁白")

    assert result == "NARRATOR narrator"
