"""动态生成新角色：narrator 请求时给新人搭骨架。

流程：校验锚点 → 调 character_factory agent 生成 role/identity/dynamic/behavior/voice/status/relations → 写文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agents.factory import get_character_factory_agent, reload_conversation_agent
from agents.runner import run_structured_agent
from agents.schema import CharacterSchedule, NewCharacterCreation, NewCharacterSpec
from llm.providers import get_character_factory_llm_config
from log_config.routing import routing_logger
from memory.parser import extract_status_field
from shared.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    CHARACTERS_DIR,
    get_agent_names,
)
from shared.text_utils import extract_identity, get_display_name
from storage.agent_files import (
    read_agent_file,
    resolve_agent_display_name,
    update_status,
    write_sidecar_json,
)


_RESERVED_NAMES = {"player", "narrator", ""}
_AGENT_NAME_MAX_LEN = 32
_STATUS_ORDER = [
    "身份",
    "心境",
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


def _build_schedule_template_block() -> str:
    """从首个已有角色 schedule.json 取 period start/end/name，给 LLM 一个可参照的时间范围模板。

    只暴露 period 元数据（不暴露其他角色的具体地点），LLM 自己根据新角色身份填 slots。
    没有任何已有 schedule 时返回空串，由 LLM 自行决定起止日期。
    """
    for agent in get_agent_names(include_narrator=False):
        sched_path = CHARACTERS_DIR / agent / "schedule.json"
        if not sched_path.exists():
            continue
        try:
            data = json.loads(sched_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        periods = data.get("periods") or []
        if not periods:
            continue
        rows = []
        for p in periods:
            start = (p.get("start") or "").strip()
            end = (p.get("end") or "").strip()
            name = (p.get("name") or "").strip()
            if not start or not end:
                continue
            label = f"{name}（{start} 至 {end}）" if name else f"{start} 至 {end}"
            rows.append(f"- {label}")
        if rows:
            return "<schedule_template>\n" + "\n".join(rows) + "\n</schedule_template>"
    return ""


def _build_factory_user_message(spec: NewCharacterSpec) -> str:
    narrator_status = read_agent_file("narrator", "status.md")
    current_time = extract_status_field(narrator_status, "当前时间").strip() or "（未知）"
    scene = extract_status_field(narrator_status, "场景").strip() or "（未知）"
    existing_agents = _format_existing_agents()
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
    schedule_template = _build_schedule_template_block()
    if schedule_template:
        blocks.append(schedule_template)
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
    fields.pop("当前位置", None)
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
    relation_to_display = (
        resolve_agent_display_name(spec.relation_to) if spec.relation_to != "player" else None
    )
    sections: dict[str, str] = {}
    for k, v in relations.items():
        if not k or not v or not v.strip() or k == "player":
            continue
        sections[resolve_agent_display_name(k)] = v.strip()

    if relation_to_display and relation_to_display not in sections:
        sections[relation_to_display] = spec.relation_description.strip()

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


def _append_to_narrator_locations(display_name: str, location: str) -> None:
    """把新角色的初始位置追加到 narrator/status.md 的「角色位置」列表。"""
    narrator_status = read_agent_file("narrator", "status.md")
    current = extract_status_field(narrator_status, "角色位置").strip()
    new_entry = f"- {display_name}：{location}"
    new_value = f"{current}\n{new_entry}" if current else new_entry
    update_status("narrator", "角色位置", new_value)


def _write_schedule_json(agent_dir: Path, schedule: CharacterSchedule | None) -> bool:
    """写新角色 schedule.json；schedule 为空或所有 period 都没有 slots 时跳过并返回 False。"""
    if schedule is None or not schedule.periods:
        return False
    if not any(p.slots for p in schedule.periods):
        return False
    payload = schedule.model_dump(mode="json")
    (agent_dir / "schedule.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


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

    if not _write_schedule_json(agent_dir, creation.schedule):
        routing_logger.warning(
            f"[character_factory] {spec.character_id!r} 未生成 schedule.json，"
            "schedule_snapshot 将显示「（无日程）」"
        )

    narrator_status = read_agent_file("narrator", "status.md")
    last_seen = extract_status_field(narrator_status, "当前时间").strip()
    if last_seen:
        write_sidecar_json(spec.character_id, ".last_seen.json", {"last_seen": last_seen})

    if spec.initial_location.strip():
        _append_to_narrator_locations(creation.role, spec.initial_location.strip())


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
