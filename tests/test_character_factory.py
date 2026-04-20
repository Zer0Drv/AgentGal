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
    from agents.schema import (
        CharacterSchedule,
        CharacterSchedulePeriod,
        CharacterScheduleSlot,
        NarratorOutput,
        NewCharacterCreation,
        NewCharacterSpec,
    )
    from engine.character import Narrator
    from engine.character_factory import CreatedCharacterInfo
except ModuleNotFoundError as exc:
    pytest.skip(f"skip character_factory tests: missing dependency ({exc})", allow_module_level=True)


# ---------------------------------------------------------------------------
# Narrator._filter_new_characters
# ---------------------------------------------------------------------------


def test_filter_new_characters_keeps_valid_anchors():
    specs = [
        NewCharacterSpec(
            character_id="mitsuki_mom",
            relation_to="mitsuki",
            relation_description="美月的妈妈，温柔但严厉",
        ),
        NewCharacterSpec(
            character_id="player_cousin",
            relation_to="player",
            relation_description="玩家的表姐，大两岁",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert [s.character_id for s in kept] == ["mitsuki_mom", "player_cousin"]


def test_filter_new_characters_rejects_unknown_anchor(caplog):
    specs = [
        NewCharacterSpec(
            character_id="ghost",
            relation_to="not_exist",
            relation_description="无名路人",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_rejects_reserved_and_existing_names():
    specs = [
        NewCharacterSpec(character_id="player", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(character_id="narrator", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(character_id="mitsuki", relation_to="mitsuki", relation_description="x"),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_rejects_empty_description():
    specs = [
        NewCharacterSpec(
            character_id="mitsuki_mom",
            relation_to="mitsuki",
            relation_description="   ",
        ),
    ]
    kept = Narrator._filter_new_characters(specs, ["mitsuki"])
    assert kept == []


def test_filter_new_characters_dedupes_names():
    specs = [
        NewCharacterSpec(
            character_id="twin",
            relation_to="mitsuki",
            relation_description="美月的哥哥",
        ),
        NewCharacterSpec(
            character_id="twin",
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
                    character_id="mitsuki_mom",
                    relation_to="mitsuki",
                    relation_description="美月的妈妈",
                )
            ],
        )

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, _scene, new_chars, is_valid = await Narrator().route("来一个妈妈")

    assert targets == ["mitsuki", "mitsuki_mom"]
    assert [s.character_id for s in new_chars] == ["mitsuki_mom"]
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
        character_id="mitsuki_mom",
        relation_to="mitsuki",
        relation_description="妈妈",
    )
    assert character_factory_module._validate_spec(spec) is None


def test_validate_spec_rejects_unknown_anchor(character_dir):
    _seed(character_dir, "mitsuki")
    spec = NewCharacterSpec(
        character_id="ghost",
        relation_to="not_exist",
        relation_description="x",
    )
    assert character_factory_module._validate_spec(spec) is not None


def test_validate_spec_allows_player_anchor(character_dir):
    spec = NewCharacterSpec(
        character_id="player_cousin",
        relation_to="player",
        relation_description="表姐",
    )
    assert character_factory_module._validate_spec(spec) is None


def test_validate_spec_rejects_duplicate_name(character_dir):
    _seed(character_dir, "mitsuki")
    _seed(character_dir, "dup")
    spec = NewCharacterSpec(
        character_id="dup",
        relation_to="mitsuki",
        relation_description="x",
    )
    assert character_factory_module._validate_spec(spec) is not None


def test_validate_spec_rejects_non_ascii_name(character_dir):
    _seed(character_dir, "mitsuki")
    spec = NewCharacterSpec(
        character_id="美月妈妈",
        relation_to="mitsuki",
        relation_description="妈妈",
    )
    assert character_factory_module._validate_spec(spec) is not None


def test_new_character_creation_normalizes_identity_to_single_line():
    creation = NewCharacterCreation(
        role="桥本志津",
        identity="美月的妈妈，\n来学校接她放学的家长。",
        dynamic="你牵挂着女儿的健康。\n\n每次去学校都忍不住多问几句。",
        behavior=["被女儿嫌弃时，先退一步再绕回来"],
        voice=["美月，你脸色怎么这么差？"],
        status={},
        relations={},
    )
    assert creation.identity == "美月的妈妈， 来学校接她放学的家长。"


def test_build_factory_user_message_omits_empty_optional_fields(character_dir):
    _seed(character_dir, "mitsuki", soul="# 美月\n", status="## 当前位置\n教室\n")
    _seed(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n",
    )

    message = character_factory_module._build_factory_user_message(
        NewCharacterSpec(
            character_id="mitsuki_mom",
            relation_to="mitsuki",
            relation_description="美月的妈妈",
        )
    )

    assert "agent_id: mitsuki_mom" in message
    assert "display_name:" not in message
    assert "initial_location:" not in message
    assert "relation_to: mitsuki" in message


# ---------------------------------------------------------------------------
# create_character 端到端：mock LLM，确认写入目录结构
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_character_bootstraps_all_files(character_dir, monkeypatch):
    _seed(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
    )
    _seed(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n\n## 角色位置\n- 美月：教室\n",
    )

    async def fake_run_structured_agent(**_kwargs):
        return NewCharacterCreation(
            role="桥本志津",
            identity="美月的妈妈，来学校接她放学的家长。",
            dynamic=(
                "你牵挂着女儿的每一次练习和每一场演出，可她越长大越不愿意让你看见她累。\n\n"
                "你嘴上只问她冷不冷、累不累，心里其实想知道她是不是还撑得住——"
                "但你知道追问只会让她躲得更远，所以总用『顺路接送』『顺手买点东西』这种借口守在她附近。"
            ),
            behavior=[
                "被美月嫌弃时先笑一下退一步，过会儿再绕回来",
                "只要美月脸色不对就忍不住多问一句，问完又怕自己越界",
                "见到和女儿走近的人时，先礼貌打量，再私下仔细留意这人靠不靠谱",
            ],
            voice=[
                "美月，今天累不累？妈妈路过顺便来看看你。",
                "吃口东西再走，就一口。",
                "你别硬撑。撑不住的时候要跟妈妈说。",
            ],
            status={
                "身份": "全职主妇",
                "心境": "挂念美月",
                "和玩家的关系": "听说过",
                "在意的事": "女儿练习太累",
                "打算": "- [ ] 【等美月】在教室外等她下课",
            },
            relations={
                "mitsuki": "女儿，最近显得疲惫",
                "player": "女儿同班同学，还没正式认识",
            },
            schedule=CharacterSchedule(
                periods=[
                    CharacterSchedulePeriod(
                        start="2026-04-01",
                        end="2026-07-31",
                        name="春学期",
                        slots=[
                            CharacterScheduleSlot(
                                days=["mon", "tue", "wed", "thu", "fri"],
                                time="上午",
                                location="家",
                            ),
                            CharacterScheduleSlot(
                                days=["sat", "sun"], time="全天", location="家"
                            ),
                        ],
                    )
                ]
            ),
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
        character_id="mitsuki_mom",
        relation_to="mitsuki",
        relation_description="美月的妈妈",
        initial_location="教室走廊",
    )
    created = await character_factory_module.create_character(spec)
    assert created == CreatedCharacterInfo(
        character_id="mitsuki_mom",
        display_name="桥本志津",
        identity="美月的妈妈，来学校接她放学的家长。",
    )

    agent_dir = character_dir / "mitsuki_mom"
    soul = (agent_dir / "soul.md").read_text(encoding="utf-8")
    assert soul.startswith("<role>桥本志津</role>")
    assert "<identity>\n美月的妈妈，来学校接她放学的家长。\n</identity>" in soul
    assert "<dynamic>" in soul and "</dynamic>" in soul
    assert "<behavior>" in soul and "- 被美月嫌弃时先笑一下退一步" in soul
    assert "<voice>" in soul and "美月，今天累不累？" in soul
    status = (agent_dir / "status.md").read_text(encoding="utf-8")
    assert status.startswith("# 桥本志津 的状态")
    assert "## 当前位置" not in status
    assert "## 打算\n- [ ] 【等美月】" in status

    narrator_status = (character_dir / "narrator" / "status.md").read_text(encoding="utf-8")
    assert "- 美月：教室" in narrator_status
    assert "- 桥本志津：教室走廊" in narrator_status

    relations = (agent_dir / "relations.md").read_text(encoding="utf-8")
    # relations.md 按 display_name 作为 section 标题（soul 首行 `# 美月` → "美月"）
    assert "## 美月" in relations
    assert "女儿，最近显得疲惫" in relations
    # 对玩家的视角走 status."和玩家的关系"，不再出现在 relations.md
    assert "## player" not in relations
    assert "## 和玩家的关系\n听说过" in status

    assert (agent_dir / "memory.md").exists()
    assert (agent_dir / "growth.md").read_text(encoding="utf-8").startswith("# 心路历程")
    assert (agent_dir / "user.md").read_text(encoding="utf-8").startswith("# 眼中的玩家")

    last_seen = (agent_dir / ".last_seen.json").read_text(encoding="utf-8")
    assert "4月3日 星期一 8:23" in last_seen

    import json as _json

    schedule_path = agent_dir / "schedule.json"
    assert schedule_path.exists()
    schedule_data = _json.loads(schedule_path.read_text(encoding="utf-8"))
    assert schedule_data["periods"][0]["name"] == "春学期"
    assert schedule_data["periods"][0]["slots"][0]["location"] == "家"


@pytest.mark.asyncio
async def test_create_character_skips_schedule_when_llm_omits(character_dir, monkeypatch):
    """LLM 没产出 schedule 时不写 schedule.json，但其他文件依然落盘。"""
    _seed(character_dir, "mitsuki", soul="# 美月\n")
    _seed(character_dir, "narrator", status="## 当前时间\n4月3日 星期一 8:23\n")

    async def fake_run_structured_agent(**_kwargs):
        return NewCharacterCreation(
            role="林晚",
            identity="美月的邻居。",
            dynamic="你偶尔撞见美月，会打招呼但没熟到能聊天。",
            behavior=["撞见邻居时先点头笑一下"],
            voice=["今天回得早呀。"],
            status={"身份": "邻居", "心境": "随和", "和玩家的关系": "陌生人"},
            relations={},
            schedule=None,
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
        character_id="neighbor",
        relation_to="mitsuki",
        relation_description="美月的邻居",
    )
    created = await character_factory_module.create_character(spec)
    assert created is not None

    agent_dir = character_dir / "neighbor"
    assert not (agent_dir / "schedule.json").exists()
    assert (agent_dir / "soul.md").exists()
    assert (agent_dir / "status.md").exists()


def test_build_schedule_template_block_uses_first_existing_schedule(character_dir, monkeypatch):
    """已有角色带 schedule 时，template block 应包含其 period 元数据，不暴露具体 slots。"""
    import json as _json

    _seed(character_dir, "mitsuki", soul="# 美月\n")
    (character_dir / "mitsuki" / "schedule.json").write_text(
        _json.dumps(
            {
                "periods": [
                    {
                        "start": "2026-04-01",
                        "end": "2026-07-31",
                        "name": "春学期",
                        "slots": [{"days": ["mon"], "time": "上午", "location": "教室"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    block = character_factory_module._build_schedule_template_block()
    assert block.startswith("<schedule_template>")
    assert "春学期（2026-04-01 至 2026-07-31）" in block
    assert "教室" not in block  # 不暴露已有角色的具体地点


def test_build_schedule_template_block_empty_when_no_existing_schedule(character_dir):
    """没有任何已有 schedule 时返回空串。"""
    _seed(character_dir, "mitsuki", soul="# 美月\n")
    assert character_factory_module._build_schedule_template_block() == ""


@pytest.mark.asyncio
async def test_create_character_validates_before_calling_llm(character_dir, monkeypatch):
    _seed(character_dir, "mitsuki")
    called = False

    async def fake_run_structured_agent(**_kwargs):
        nonlocal called
        called = True
        return NewCharacterCreation(
            role="x",
            identity="x",
            dynamic="x",
            behavior=["x"],
            voice=["x"],
            status={},
            relations={},
        )

    monkeypatch.setattr(
        character_factory_module,
        "run_structured_agent",
        fake_run_structured_agent,
    )

    spec = NewCharacterSpec(
        character_id="mitsuki_mom",
        relation_to="ghost",
        relation_description="x",
    )
    created = await character_factory_module.create_character(spec)
    assert created is None
    assert called is False


@pytest.mark.asyncio
async def test_create_character_seeds_relation_to_when_llm_omits(character_dir, monkeypatch):
    _seed(character_dir, "mitsuki")
    _seed(character_dir, "narrator", status="## 当前时间\n4月3日 星期一 8:23\n")

    async def fake_run_structured_agent(**_kwargs):
        return NewCharacterCreation(
            role="林晚",
            identity="美月的邻居。",
            dynamic="你偶尔撞见美月从家里出来，会打个招呼，但没熟到能聊天。",
            behavior=["撞见邻居时先点头笑一下，再决定要不要搭话"],
            voice=["今天回得早呀。"],
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
        character_id="fallback_char",
        relation_to="mitsuki",
        relation_description="美月的邻居",
    )
    created = await character_factory_module.create_character(spec)
    assert created == CreatedCharacterInfo(
        character_id="fallback_char",
        display_name="林晚",
        identity="美月的邻居。",
    )

    relations = (character_dir / "fallback_char" / "relations.md").read_text(encoding="utf-8")
    assert "## mitsuki" in relations
    assert "美月的邻居" in relations  # fallback 描述进入 relations.md


# ---------------------------------------------------------------------------
# conversation_flow.bootstrap_new_characters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_new_characters_keeps_only_targeted_successes(monkeypatch):
    async def fake_create_character(spec):
        if spec.character_id == "bad":
            return None
        return CreatedCharacterInfo(
            character_id=spec.character_id,
            display_name=spec.character_id.upper(),
            identity=f"{spec.character_id}-identity",
        )

    monkeypatch.setattr(conversation_flow_module, "create_character", fake_create_character)

    specs = [
        NewCharacterSpec(character_id="good1", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(character_id="bad", relation_to="mitsuki", relation_description="x"),
        NewCharacterSpec(character_id="good2", relation_to="mitsuki", relation_description="x"),
    ]
    targets, created = await conversation_flow_module.bootstrap_new_characters(
        specs, ["mitsuki", "good1", "bad"]
    )
    assert targets == ["mitsuki", "good1", "good2"]
    assert [item.character_id for item in created] == ["good1", "good2"]
    assert [item.identity for item in created] == ["good1-identity", "good2-identity"]


@pytest.mark.asyncio
async def test_bootstrap_new_characters_auto_targets_created(monkeypatch):
    async def fake_create_character(spec):
        return CreatedCharacterInfo(
            character_id=spec.character_id,
            display_name="Good One",
            identity="新来的角色",
        )

    monkeypatch.setattr(conversation_flow_module, "create_character", fake_create_character)

    specs = [
        NewCharacterSpec(character_id="good1", relation_to="mitsuki", relation_description="x"),
    ]
    targets, created = await conversation_flow_module.bootstrap_new_characters(
        specs, ["mitsuki"]
    )
    assert targets == ["mitsuki", "good1"]
    assert [item.character_id for item in created] == ["good1"]


@pytest.mark.asyncio
async def test_bootstrap_new_characters_no_specs_is_noop():
    targets, created = await conversation_flow_module.bootstrap_new_characters([], ["mitsuki"])
    assert targets == ["mitsuki"]
    assert created == []
