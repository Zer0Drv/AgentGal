"""测试动态生成新角色：narrator 的 new_characters 过滤 + character_factory bootstrap。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.character as character_module
    import engine.character_factory as character_factory_module
    import engine.conversation_flow as conversation_flow_module
    from engine.agent_schema import (
        NarratorOutput,
        NewCharacterCreation,
        NewCharacterSpec,
    )
    from engine.character import Narrator
except ModuleNotFoundError as exc:
    pytest.skip(f"skip character_factory tests: missing dependency ({exc})", allow_module_level=True)


# ---------------------------------------------------------------------------
# Narrator._filter_new_characters
# ---------------------------------------------------------------------------


def test_filter_new_characters_keeps_valid_anchors():
    specs = [
        NewCharacterSpec(
            name="mitsuki_mom",
            relation_to="mitsuki",
            relation_description="美月的妈妈，温柔但严厉",
        ),
        NewCharacterSpec(
            name="player_cousin",
            relation_to="player",
            relation_description="玩家的表姐，大两岁",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert [s.name for s in kept] == ["mitsuki_mom", "player_cousin"]


def test_filter_new_characters_rejects_unknown_anchor(caplog):
    specs = [
        NewCharacterSpec(
            name="ghost",
            relation_to="not_exist",
            relation_description="无名路人",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_rejects_reserved_and_existing_names():
    specs = [
        NewCharacterSpec(name="player", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(name="narrator", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(name="mitsuki", relation_to="mitsuki", relation_description="x"),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_rejects_empty_description():
    specs = [
        NewCharacterSpec(
            name="mitsuki_mom",
            relation_to="mitsuki",
            relation_description="   ",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_dedupes_names():
    specs = [
        NewCharacterSpec(
            name="twin",
            relation_to="mitsuki",
            relation_description="美月的哥哥",
        ),
        NewCharacterSpec(
            name="twin",
            relation_to="player",
            relation_description="重复项",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert len(kept) == 1
    assert kept[0].relation_to == "mitsuki"


# ---------------------------------------------------------------------------
# Narrator.route 透传 new_characters 并把新名加入 targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrator_route_passes_new_characters(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    monkeypatch.setattr(character_module, "read_agent_file", lambda *_args: "# soul")
    monkeypatch.setattr(character_module, "get_display_name", lambda *_args: "美月")

    async def fake_run_narrator(self, *_args, **_kwargs):
        return NarratorOutput(
            targets=["mitsuki", "mitsuki_mom"],
            content="场景",
            new_characters=[
                NewCharacterSpec(
                    name="mitsuki_mom",
                    relation_to="mitsuki",
                    relation_description="美月的妈妈",
                )
            ],
        )

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, _scene, new_chars, is_valid = await Narrator().route("来一个妈妈")

    assert targets == ["mitsuki", "mitsuki_mom"]
    assert [s.name for s in new_chars] == ["mitsuki_mom"]
    assert is_valid is True


# ---------------------------------------------------------------------------
# character_factory._validate_spec
# ---------------------------------------------------------------------------


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    monkeypatch.setattr(character_factory_module, "CHARACTERS_DIR", tmp_path)
    return tmp_path


def _seed(root: Path, name: str, soul: str = "", status: str = "") -> None:
    agent = root / name
    agent.mkdir(parents=True, exist_ok=True)
    if soul:
        (agent / "soul.md").write_text(soul, encoding="utf-8")
    if status:
        (agent / "status.md").write_text(status, encoding="utf-8")


def test_validate_spec_accepts_anchor_in_existing_agents(character_dir):
    _seed(character_dir, "mitsuki", soul="# 美月")
    spec = NewCharacterSpec(
        name="mitsuki_mom",
        relation_to="mitsuki",
        relation_description="妈妈",
    )
    assert character_factory_module._validate_spec(spec) is None


def test_validate_spec_rejects_unknown_anchor(character_dir):
    _seed(character_dir, "mitsuki")
    spec = NewCharacterSpec(
        name="ghost",
        relation_to="not_exist",
        relation_description="x",
    )
    assert character_factory_module._validate_spec(spec) is not None


def test_validate_spec_allows_player_anchor(character_dir):
    spec = NewCharacterSpec(
        name="player_cousin",
        relation_to="player",
        relation_description="表姐",
    )
    assert character_factory_module._validate_spec(spec) is None


def test_validate_spec_rejects_duplicate_name(character_dir):
    _seed(character_dir, "mitsuki")
    _seed(character_dir, "dup")
    spec = NewCharacterSpec(
        name="dup",
        relation_to="mitsuki",
        relation_description="x",
    )
    assert character_factory_module._validate_spec(spec) is not None


def test_validate_spec_rejects_non_ascii_name(character_dir):
    _seed(character_dir, "mitsuki")
    spec = NewCharacterSpec(
        name="美月妈妈",
        relation_to="mitsuki",
        relation_description="妈妈",
    )
    assert character_factory_module._validate_spec(spec) is not None


# ---------------------------------------------------------------------------
# create_character 端到端：mock LLM，确认写入目录结构
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_character_bootstraps_all_files(character_dir, monkeypatch):
    _seed(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 当前位置\n教室\n",
    )
    _seed(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n",
    )

    async def fake_run_structured_agent(**_kwargs):
        return NewCharacterCreation(
            soul=(
                "<role>桥本志津</role>\n<anchor>美月的妈妈</anchor>\n"
                "<flavor>- 语气温和</flavor>\n<examples><example>"
                "<context>接美月放学</context><response>「今天辛苦了」</response>"
                "</example></examples>"
            ),
            status={
                "身份": "全职主妇",
                "心境": "挂念美月",
                "当前位置": "教室走廊",
                "和玩家的关系": "听说过",
                "在意的事": "女儿练习太累",
                "打算": "- [ ] 【等美月】在教室外等她下课",
            },
            relations={
                "mitsuki": "女儿，最近显得疲惫",
                "player": "女儿同班同学，还没正式认识",
            },
        )

    monkeypatch.setattr(
        character_factory_module,
        "run_structured_agent",
        fake_run_structured_agent,
    )
    monkeypatch.setattr(
        character_factory_module,
        "get_character_factory_agent",
        lambda: object(),
    )
    monkeypatch.setattr(
        character_factory_module,
        "get_character_factory_llm_config",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(
        character_factory_module,
        "reload_conversation_agent",
        lambda _name: None,
    )

    spec = NewCharacterSpec(
        name="mitsuki_mom",
        relation_to="mitsuki",
        relation_description="美月的妈妈",
        initial_location="教室走廊",
    )
    ok = await character_factory_module.create_character(spec)
    assert ok is True

    agent_dir = character_dir / "mitsuki_mom"
    assert (agent_dir / "soul.md").read_text(encoding="utf-8").startswith("<role>桥本志津</role>")
    status = (agent_dir / "status.md").read_text(encoding="utf-8")
    assert "## 当前位置\n教室走廊" in status
    assert "## 打算\n- [ ] 【等美月】" in status

    relations = (agent_dir / "relations.md").read_text(encoding="utf-8")
    assert "## mitsuki" in relations
    assert "女儿，最近显得疲惫" in relations
    # 对玩家的视角走 status."和玩家的关系"，不再出现在 relations.md
    assert "## player" not in relations
    assert "## 和玩家的关系\n听说过" in status

    assert (agent_dir / "memory.md").exists()
    assert (agent_dir / "growth.md").read_text(encoding="utf-8").startswith("# 心路历程")
    assert (agent_dir / "user.md").read_text(encoding="utf-8").startswith("# 眼中的玩家")

    last_seen = (agent_dir / ".last_seen.json").read_text(encoding="utf-8")
    assert "4月3日 星期一 8:23" in last_seen


@pytest.mark.asyncio
async def test_create_character_validates_before_calling_llm(character_dir, monkeypatch):
    _seed(character_dir, "mitsuki")
    called = False

    async def fake_run_structured_agent(**_kwargs):
        nonlocal called
        called = True
        return NewCharacterCreation(soul="x", status={}, relations={})

    monkeypatch.setattr(
        character_factory_module,
        "run_structured_agent",
        fake_run_structured_agent,
    )

    spec = NewCharacterSpec(
        name="mitsuki_mom",
        relation_to="ghost",
        relation_description="x",
    )
    ok = await character_factory_module.create_character(spec)
    assert ok is False
    assert called is False


@pytest.mark.asyncio
async def test_create_character_seeds_relation_to_when_llm_omits(character_dir, monkeypatch):
    _seed(character_dir, "mitsuki")
    _seed(character_dir, "narrator", status="## 当前时间\n4月3日 星期一 8:23\n")

    async def fake_run_structured_agent(**_kwargs):
        return NewCharacterCreation(
            soul="<role>x</role>",
            status={"身份": "x", "心境": "x", "和玩家的关系": "x"},
            relations={"player": "陌生人"},
        )

    monkeypatch.setattr(character_factory_module, "run_structured_agent", fake_run_structured_agent)
    monkeypatch.setattr(character_factory_module, "get_character_factory_agent", lambda: object())
    monkeypatch.setattr(
        character_factory_module,
        "get_character_factory_llm_config",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr(character_factory_module, "reload_conversation_agent", lambda _name: None)

    spec = NewCharacterSpec(
        name="fallback_char",
        relation_to="mitsuki",
        relation_description="美月的邻居",
    )
    ok = await character_factory_module.create_character(spec)
    assert ok is True

    relations = (character_dir / "fallback_char" / "relations.md").read_text(encoding="utf-8")
    assert "## mitsuki" in relations
    assert "美月的邻居" in relations  # fallback 描述进入 relations.md


# ---------------------------------------------------------------------------
# conversation_flow.bootstrap_new_characters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_new_characters_keeps_only_targeted_successes(monkeypatch):
    async def fake_create_character(spec):
        return spec.name != "bad"

    monkeypatch.setattr(conversation_flow_module, "create_character", fake_create_character)

    specs = [
        NewCharacterSpec(name="good1", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(name="bad", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(name="good2", relation_to="mitsuki", relation_description="x"),
    ]
    targets, created = await conversation_flow_module.bootstrap_new_characters(
        specs, ["mitsuki", "good1", "bad"]
    )
    assert targets == ["mitsuki", "good1"]
    assert created == ["good1", "good2"]


@pytest.mark.asyncio
async def test_bootstrap_new_characters_does_not_auto_target_created(monkeypatch):
    async def fake_create_character(spec):
        return True

    monkeypatch.setattr(conversation_flow_module, "create_character", fake_create_character)

    specs = [
        NewCharacterSpec(name="good1", relation_to="mitsuki", relation_description="x"),
    ]
    targets, created = await conversation_flow_module.bootstrap_new_characters(
        specs, ["mitsuki"]
    )
    assert targets == ["mitsuki"]
    assert created == ["good1"]


@pytest.mark.asyncio
async def test_bootstrap_new_characters_no_specs_is_noop():
    targets, created = await conversation_flow_module.bootstrap_new_characters([], ["mitsuki"])
    assert targets == ["mitsuki"]
    assert created == []
