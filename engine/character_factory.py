"""动态生成新角色：narrator 请求时给新人搭骨架。

流程：校验锚点 → 调 character_factory agent 生成 role/identity/dynamic/behavior/voice/status/relations → 写文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engine.agent_factory import get_character_factory_agent, reload_conversation_agent
from engine.agent_runner import run_structured_agent
from engine.agent_schema import NewCharacterCreation, NewCharacterSpec
from llm.providers import get_character_factory_llm_config
from log_config.routing import routing_logger
from memory.parser import extract_status_field
from shared.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    CHARACTERS_DIR,
    get_agent_names,
)
from shared.text_utils import extract_identity, get_display_name
from storage.agent_files import read_agent_file, write_sidecar_json


_RESERVED_NAMES = {"player", "narrator", ""}
_AGENT_NAME_MAX_LEN = 32
_STATUS_ORDER = [
    "身份",
    "心境",
    "当前位置",
    "和玩家的关系",
    "在意的事",
    "打算",
]
_USER_MD_SKELETON = (
    "# 眼中的玩家\n\n"
    "## 基本信息\n- 姓名：\n- 年龄：\n- 性别/称呼：\n- 身份：\n\n"
    "## 对方是什么人\n\n\n"
    "## 我们怎么相处\n"
)


@dataclass(frozen=True, slots=True)
class CreatedCharacterInfo:
    character_id: str
    display_name: str
    identity: str


def _is_valid_agent_name(name: str) -> bool:
    if not name or len(name) > _AGENT_NAME_MAX_LEN:
        return False
    if name in _RESERVED_NAMES:
        return False
    return all((c.isascii() and c.isalnum()) or c in "_-" for c in name)


def _format_existing_agents() -> str:
    rows: list[str] = []
    for agent in get_agent_names(include_narrator=False):
        soul = read_agent_file(agent, "soul.md")
        display = get_display_name(agent, soul) if soul else agent
        identity = extract_identity(soul) if soul else ""
        label = f"{agent} / {display}" if display and display != agent else agent
        rows.append(f"- {label}：{identity}" if identity else f"- {label}")
    return "\n".join(rows) if rows else "（暂无）"


def _build_relation_to_context(relation_to: str) -> str:
    if relation_to == "player":
        return "relation_to 是玩家（player）；参考已有角色对玩家的既有描写推断。"

    soul = read_agent_file(relation_to, "soul.md")
    status = read_agent_file(relation_to, "status.md")
    parts: list[str] = []
    if soul:
        parts.append(f"<soul>\n{soul.strip()}\n</soul>")
    if status:
        parts.append(f"<status>\n{status.strip()}\n</status>")
    return "\n\n".join(parts) if parts else "（relation_to 无 soul / status 资料）"


def _build_factory_user_message(spec: NewCharacterSpec) -> str:
    narrator_status = read_agent_file("narrator", "status.md")
    current_time = extract_status_field(narrator_status, "当前时间").strip() or "（未知）"
    scene = extract_status_field(narrator_status, "场景").strip() or "（未知）"
    existing_agents = _format_existing_agents()
    relation_to_context = _build_relation_to_context(spec.relation_to)
    story_setting = read_agent_file("narrator", "soul.md").strip()

    spec_lines = [
        "<spec>",
        f"agent_id: {spec.character_id}",
        f"relation_to: {spec.relation_to}",
        f"relation_description: {spec.relation_description}",
        f"background_hint: {spec.background_hint or '（无）'}",
    ]
    if spec.display_name.strip():
        spec_lines.append(f"display_name: {spec.display_name.strip()}")
    if spec.initial_location.strip():
        spec_lines.append(f"initial_location: {spec.initial_location.strip()}")
    spec_lines.append("</spec>")
    spec_block = "\n".join(spec_lines)

    blocks: list[str] = [spec_block]
    if story_setting:
        blocks.append(f"<story_setting>\n{story_setting}\n</story_setting>")
    blocks.append(
        "<world_now>\n"
        f"当前时间：{current_time}\n"
        f"当前场景：{scene}\n"
        f"已有角色（agent_id / 显示名）：{existing_agents}\n"
        "</world_now>"
    )
    blocks.append(
        "<relation_to_context>\n"
        f"{relation_to_context}\n"
        "</relation_to_context>"
    )
    return "\n\n".join(blocks)


def _validate_spec(spec: NewCharacterSpec) -> str | None:
    """返回错误描述；None 表示校验通过。"""
    character_id = spec.character_id.strip()
    if not _is_valid_agent_name(character_id):
        return f"非法 agent_id: {spec.character_id!r}"
    existing = set(get_agent_names(include_narrator=True))
    if character_id in existing:
        return f"agent_id 已存在: {character_id}"
    valid_anchors = set(get_agent_names(include_narrator=False)) | {"player"}
    anchor = spec.relation_to.strip()
    if anchor not in valid_anchors:
        return f"relation_to 不在已有角色中: {anchor!r}"
    if not spec.relation_description.strip():
        return "relation_description 为空"
    return None


def _write_status_md(
    agent_dir: Path,
    status: dict[str, str],
    spec: NewCharacterSpec,
    display_name: str,
) -> None:
    """按 _STATUS_ORDER 顺序写 status.md；缺失字段用合理默认补齐。"""
    fields = {k: (v or "").strip() for k, v in status.items()}
    if not fields.get("当前位置") and spec.initial_location:
        fields["当前位置"] = spec.initial_location
    if not fields.get("和玩家的关系") and spec.relation_to == "player":
        fields["和玩家的关系"] = spec.relation_description.strip()
    if not fields.get("打算"):
        target = "玩家" if spec.relation_to == "player" else spec.relation_to
        fields["打算"] = f"- [ ] 【见到{target}】找到合适的时机自然出现"

    ordered_keys = list(_STATUS_ORDER)
    for key in fields:
        if key not in ordered_keys:
            ordered_keys.append(key)

    lines = [f"# {display_name} 的状态", ""]
    for key in ordered_keys:
        lines.append(f"## {key}")
        lines.append(fields.get(key, ""))
        lines.append("")
    (agent_dir / "status.md").write_text("\n".join(lines), encoding="utf-8")


def _write_relations_md(
    agent_dir: Path,
    relations: dict[str, str],
    spec: NewCharacterSpec,
) -> None:
    sections = {
        k: v.strip()
        for k, v in relations.items()
        if k and v and v.strip() and k != "player"
    }
    if spec.relation_to != "player" and spec.relation_to not in sections:
        sections[spec.relation_to] = spec.relation_description.strip()

    lines = ["# 我眼中的其他人", ""]
    for target, body in sections.items():
        lines.append(f"## {target}")
        lines.append(body)
        lines.append("")
    (agent_dir / "relations.md").write_text("\n".join(lines), encoding="utf-8")


def _format_bulleted_block(items: list[str]) -> str:
    """渲染 behavior 列表：每条前缀 '- '；空条目跳过。"""
    lines: list[str] = []
    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        lines.append(text if text.startswith("- ") else f"- {text}")
    return "\n".join(lines)


def _format_voice_block(items: list[str]) -> str:
    """渲染 voice 样例：每句独立一行；空条目跳过。"""
    return "\n".join(item.strip() for item in items if item and item.strip())


def _build_soul_md(creation: NewCharacterCreation) -> str:
    """按美月模板结构拼装 soul.md：role / identity / dynamic / behavior / voice。"""
    behavior_block = _format_bulleted_block(creation.behavior)
    voice_block = _format_voice_block(creation.voice)

    parts = [
        f"<role>{creation.role}</role>",
        "",
        "<identity>",
        creation.identity,
        "</identity>",
        "",
        "<dynamic>",
        creation.dynamic,
        "</dynamic>",
    ]
    if behavior_block:
        parts.extend(["", "<behavior>", behavior_block, "</behavior>"])
    if voice_block:
        parts.extend(["", "<voice>", voice_block, "</voice>"])
    return "\n".join(parts).strip() + "\n"


def _write_bootstrap_files(
    spec: NewCharacterSpec,
    creation: NewCharacterCreation,
    soul_content: str,
) -> None:
    agent_dir = CHARACTERS_DIR / spec.character_id
    agent_dir.mkdir(parents=True, exist_ok=True)

    (agent_dir / "soul.md").write_text(soul_content, encoding="utf-8")
    (agent_dir / "memory.md").write_text("", encoding="utf-8")
    (agent_dir / "growth.md").write_text("# 心路历程\n\n", encoding="utf-8")
    (agent_dir / "user.md").write_text(_USER_MD_SKELETON, encoding="utf-8")

    _write_status_md(agent_dir, creation.status, spec, creation.role)
    _write_relations_md(agent_dir, creation.relations, spec)

    last_seen = extract_status_field(
        read_agent_file("narrator", "status.md"), "当前时间"
    ).strip()
    if last_seen:
        write_sidecar_json(spec.character_id, ".last_seen.json", {"last_seen": last_seen})


async def create_character(spec: NewCharacterSpec) -> CreatedCharacterInfo | None:
    """孵化新角色；成功返回 CreatedCharacterInfo，失败返回 None 并记录日志。"""
    error = _validate_spec(spec)
    if error:
        routing_logger.warning(f"[character_factory] 拒绝生成 {spec.character_id!r}：{error}")
        return None

    config = get_character_factory_llm_config()
    try:
        creation = await run_structured_agent(
            agent=get_character_factory_agent(),
            user_input=_build_factory_user_message(spec),
            output_type=NewCharacterCreation,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name="agentgal_character_factory",
            trace_metadata={"agent_name": "character_factory", "target": spec.character_id},
            usage_agent="character_factory",
            usage_phase="agent_run",
            model_name=config["model"],
        )
    except Exception as e:
        routing_logger.error(f"[character_factory] 生成 {spec.character_id!r} 失败: {e}")
        return None

    soul_content = _build_soul_md(creation)
    try:
        _write_bootstrap_files(spec, creation, soul_content)
    except Exception as e:
        routing_logger.error(f"[character_factory] 写入 {spec.character_id!r} 文件失败: {e}")
        return None

    # 新角色进入目录后，narrator 的 system prompt 里的 valid_targets 需要刷新
    try:
        reload_conversation_agent("narrator")
    except Exception as e:
        routing_logger.warning(f"[character_factory] 刷新 narrator agent 失败: {e}")

    routing_logger.info(
        f"[character_factory] 生成 {spec.character_id!r}（锚点 relation_to={spec.relation_to}）"
    )
    return CreatedCharacterInfo(
        character_id=spec.character_id,
        display_name=get_display_name(spec.character_id, soul_content),
        identity=extract_identity(soul_content) or creation.identity.strip(),
    )
