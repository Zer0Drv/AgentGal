from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_narrator_prompt_excludes_growth_block():
    content = _read("prompts/narrator_prompt.txt")
    assert "<personality_growth>" not in content
    assert "{growth}" not in content


def test_agent_manager_only_loads_growth_for_characters():
    content = _read("engine/agent_manager.py")
    assert 'load_growth_for_prompt(agent_name) if agent_name != "narrator" else ""' in content


def test_consolidator_skips_growth_for_narrator():
    content = _read("memory/consolidator.py")
    assert "def _supports_growth(agent_name: str) -> bool:" in content
    assert 'return agent_name != "narrator"' in content
    assert "if self._supports_growth(agent_name):" in content
    assert "if not self._supports_growth(agent_name):" in content


def test_save_manager_excludes_narrator_growth_and_runtime_file_removed():
    content = _read("game/save_manager.py")
    assert 'narrator_growth_path = character_path("narrator", "growth.md")' in content
    assert 'core_files = ["soul.md", "memory.md", "status.md"]' in content
    assert 'core_files.extend(["user.md", "growth.md"])' in content
    assert not (ROOT / "data/characters/narrator/growth.md").exists()