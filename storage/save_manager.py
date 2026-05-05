"""存档与游戏状态管理"""

import asyncio
import glob
import hashlib
import json
import os
import re
import shutil
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from shared.config import CHARACTERS_DIR, PROJECT_ROOT, character_path, get_agent_names
from log_config.routing import routing_logger
from storage.agent_files import increment_turn_counter, read_agent_file
from storage.history import append_message, load_conversation_history
from memory.parser import (
    canonical_cn_date,
    extract_status_field,
    parse_jsonl_line,
    serialize_episode,
)

TEMPLATES_DIR = PROJECT_ROOT / "data" / "templates"


# =============================================================================
# 开场白 / 消息加载
# =============================================================================


def load_prompt_file(filename: str) -> str:
    """从 prompts 目录加载文本文件（全局配置，如 opening_intro.txt）"""
    file_path = PROJECT_ROOT / "prompts" / filename
    if file_path.exists():
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[警告] 读取文件失败 {filename}: {e}")
    return ""


def load_story_file(story_id: str, filename: str) -> str:
    """从故事模板目录加载文本文件（如 opening.txt）"""
    file_path = TEMPLATES_DIR / story_id / filename
    if file_path.exists():
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[警告] 读取故事文件失败 {story_id}/{filename}: {e}")
    return ""


# =============================================================================
# 存档检查 / 重置
# =============================================================================


def has_existing_save() -> bool:
    """检查是否有已存在的存档（narrator 有 jsonl 文件）"""
    raw_dir = character_path("narrator", "raw")
    if os.path.exists(raw_dir):
        if glob.glob(f"{raw_dir}/*.jsonl"):
            return True
    return False


def reset_logs():
    """重置日志文件 - 清空 logs 目录下所有 .log 和 .jsonl 文件"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        print(f"  [info] 日志目录不存在: {log_dir}", flush=True)
        return

    for root, _, files in os.walk(log_dir):
        for filename in files:
            if filename.endswith(".log") or filename.endswith(".jsonl"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "w"):
                        pass
                    print(f"  已清空: {filepath}", flush=True)
                except Exception as e:
                    print(f"  清空失败 {filepath}: {e}", flush=True)


async def reset_game(story_id: str = "school") -> tuple[str, str]:
    """重置游戏，从 templates/{story_id} 重新创建 characters 目录

    Args:
        story_id: 故事ID，对应 data/templates/ 下的子目录名

    Returns:
        (opening_intro 文本, opening 文本)
    """
    try:
        print(f"\n{'=' * 40}", flush=True)
        print("重置游戏...", flush=True)
        print(f"{'=' * 40}\n", flush=True)

        # 1. 删除向量记忆
        from storage.vector_store import vector_store

        all_agents = get_agent_names()
        if all_agents:
            print("[Reset] 清理向量记忆...", flush=True)
            await vector_store.delete_all_agents(all_agents)

        # 2. 删除整个 characters 目录（如果存在）
        characters_dir = str(CHARACTERS_DIR)
        if os.path.exists(characters_dir):
            try:
                shutil.rmtree(characters_dir)
                print(f"  已删除: {characters_dir}", flush=True)
            except Exception as e:
                print(f"  删除失败 {characters_dir}: {e}", flush=True)

        # 3. 从对应故事模板完整复制
        story_template_dir = str(TEMPLATES_DIR / story_id)
        if os.path.exists(story_template_dir):
            try:
                shutil.copytree(story_template_dir, characters_dir)
                print(f"  已复制: {story_template_dir} -> {characters_dir}", flush=True)
            except Exception as e:
                print(f"  复制失败: {e}", flush=True)
        else:
            print(f"  [警告] 故事模板目录不存在: {story_template_dir}", flush=True)

        # 4. 只为 narrator 创建 raw 目录（对话历史集中存储）
        raw_dir = character_path("narrator", "raw")
        os.makedirs(raw_dir, exist_ok=True)
        print(f"  已创建: {raw_dir}", flush=True)

        # 5. 写入 story_id 和 save_id 标记文件
        story_id_path = os.path.join(characters_dir, ".story_id")
        with open(story_id_path, "w", encoding="utf-8") as f:
            f.write(story_id)
        print(f"  已写入: {story_id_path}", flush=True)

        save_id = uuid.uuid4().hex[:8]
        _write_save_id(save_id)
        print(f"  已写入: .save_id ({save_id})", flush=True)

        # 6. 重置日志
        print("[日志]", flush=True)
        reset_logs()

        print(f"\n{'=' * 40}", flush=True)
        print("重置完成", flush=True)
        print(f"{'=' * 40}\n", flush=True)
    except Exception as e:
        print(f"[ERROR] reset_game 执行异常: {e}", flush=True)
        import traceback

        traceback.print_exc()

    # 加载两个开场白：玩法介绍从 prompts/ 取（全局），故事开场从故事目录取
    intro_text = load_prompt_file("opening_intro.txt")
    opening_text = load_story_file(story_id, "opening.txt")

    # 将故事开场旁白写入 narrator 的历史（玩法介绍不写入）
    visible_agents = get_agent_names(include_narrator=False)
    opening_message = {
        "role": "narrator",
        "content": opening_text,
        "visible_to": visible_agents + ["narrator"],
        "turn": increment_turn_counter(),
    }
    await append_message(opening_message)

    return intro_text, opening_text


# =============================================================================
# 存档列表 / 导入
# =============================================================================


def list_save_archives() -> list[dict]:
    """列出 saves/ 目录下所有存档，按时间倒序

    Returns:
        存档信息列表，每项包含 filename、display_time
    """
    save_dir = PROJECT_ROOT / "saves"
    if not save_dir.exists():
        return []

    saves = []
    for zip_file in save_dir.glob("*.zip"):
        entry: dict = {"filename": zip_file.name, "display_time": zip_file.name, "focus": ""}
        try:
            with zipfile.ZipFile(str(zip_file), "r") as zf:
                if "metadata.json" in zf.namelist():
                    meta = json.loads(zf.read("metadata.json").decode("utf-8"))
                    export_time = meta.get("export_time", "")
                    if export_time:
                        dt = datetime.fromisoformat(export_time)
                        entry["display_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                        entry["_sort_key"] = dt.isoformat()
                    entry["focus"] = meta.get("focus", "")
        except Exception:
            pass
        saves.append(entry)

    saves.sort(key=lambda s: s.get("_sort_key", ""), reverse=True)
    for s in saves:
        s.pop("_sort_key", None)
    return saves


def delete_save_archive(save_filename: str) -> bool:
    """删除指定存档文件，文件名非法或不存在时返回 False。"""
    if not _is_safe_save_filename(save_filename):
        routing_logger.warning("[delete_save] 非法文件名: %s", save_filename)
        return False
    save_path = PROJECT_ROOT / "saves" / save_filename
    try:
        save_path.unlink()
        routing_logger.info("[delete_save] 已删除存档: %s", save_filename)
        return True
    except FileNotFoundError:
        routing_logger.warning("[delete_save] 存档文件不存在: %s", save_path)
        return False


async def import_save_archive(save_filename: str) -> bool:
    """从指定存档文件恢复游戏状态

    Args:
        save_filename: 存档文件名（仅文件名，不含路径）

    Returns:
        成功返回 True，失败返回 False
    """
    if not _is_safe_save_filename(save_filename):
        print(f"[读档] 非法文件名: {save_filename}", flush=True)
        return False

    save_path = PROJECT_ROOT / "saves" / save_filename
    if not save_path.exists():
        print(f"[读档] 存档文件不存在: {save_path}", flush=True)
        return False

    started_at = time.perf_counter()
    last_step_at = started_at

    def log_step(label: str) -> None:
        nonlocal last_step_at
        now = time.perf_counter()
        print(
            f"[读档] {label} 用时 {now - last_step_at:.2f}s，累计 {now - started_at:.2f}s",
            flush=True,
        )
        last_step_at = now

    try:
        from storage.vector_store import vector_store

        old_agents = get_agent_names()
        log_step(f"扫描当前角色（{len(old_agents)} 个）")

        characters_dir = str(CHARACTERS_DIR)
        if os.path.exists(characters_dir):
            shutil.rmtree(characters_dir)
            print(f"[读档] 已删除: {characters_dir}", flush=True)
        log_step("删除旧角色目录")

        os.makedirs(characters_dir, exist_ok=True)
        with zipfile.ZipFile(str(save_path), "r") as zf:
            for member in zf.namelist():
                if member == "metadata.json":
                    continue
                if os.path.basename(member) == "milestones.md":
                    print(f"[读档] 跳过旧里程碑文件: {member}", flush=True)
                    continue
                zf.extract(member, characters_dir)
                print(f"[读档] 已恢复: {member}", flush=True)
        log_step("解压存档")

        save_id_path = os.path.join(characters_dir, ".save_id")
        if not os.path.exists(save_id_path):
            new_save_id = uuid.uuid4().hex[:8]
            with open(save_id_path, "w", encoding="utf-8") as f:
                f.write(new_save_id)
            print(f"[读档] 旧存档无 save_id，已生成新 id: {new_save_id}", flush=True)
        log_step("校验 save_id")

        restored_agents = get_agent_names()
        agents_to_clear = list(dict.fromkeys([*old_agents, *restored_agents]))
        if agents_to_clear:
            print(f"[读档] 清理向量记忆: {agents_to_clear}", flush=True)
            await vector_store.delete_all_agents(agents_to_clear)
        log_step(f"清理向量记忆（{len(agents_to_clear)} 个角色）")

        print("[读档] 重建向量库...", flush=True)
        from memory.indexer import rebuild_memory_index
        await rebuild_memory_index(clear_existing=False)
        log_step("重建向量库")

        print("[读档] 重建 Understanding 向量库...", flush=True)
        from memory.indexer import rebuild_understanding_index
        await rebuild_understanding_index()
        log_step("重建 Understanding 向量库")

        print(f"[读档] 读档完成: {save_filename}", flush=True)
        return True

    except Exception as e:
        print(f"[读档] 读档失败: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return False


# =============================================================================
# 存档导出
# =============================================================================


def _get_agent_save_files(agent_name: str) -> list[str]:
    """获取指定角色需要存档的所有文件路径"""
    files = []
    base = character_path(agent_name)

    core_files = ["soul.md", "status.md"]
    if agent_name != "narrator":
        core_files.extend([
            "memory.jsonl", "memory_draft.jsonl", "understanding.jsonl", "schedule.json",
        ])

    for filename in core_files:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    for filename in ["states.md", "events.md"]:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    for hidden_file in [".history_window_state.json"]:
        hidden_path = f"{base}/{hidden_file}"
        if os.path.exists(hidden_path):
            files.append(hidden_path)

    if agent_name == "narrator":
        raw_dir = character_path("narrator", "raw")
        if os.path.exists(raw_dir):
            for jsonl_file in glob.glob(f"{raw_dir}/*.jsonl"):
                files.append(jsonl_file)

    return files


def _read_save_id() -> str:
    """读取当前游戏来源标记。它不再参与存档文件名选择。"""
    save_id_path = CHARACTERS_DIR / ".save_id"
    try:
        return save_id_path.read_text(encoding="utf-8").strip()
    except Exception:
        return uuid.uuid4().hex[:8]


def _write_save_id(save_id: str) -> None:
    """更新当前游戏来源标记。"""
    save_id_path = CHARACTERS_DIR / ".save_id"
    save_id_path.write_text(save_id, encoding="utf-8")


def _read_story_theme() -> str:
    """读取 .story_id 标记文件，返回故事主题（如 school / modern）"""
    story_id_path = CHARACTERS_DIR / ".story_id"
    try:
        return story_id_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_narrator_focus() -> str:
    """读取叙事焦点写入 metadata.json，供存档列表展示。"""
    try:
        status_text = read_agent_file("narrator", "status.md")
        focus = extract_status_field(status_text, "叙事焦点")
        if focus:
            safe = re.sub(r'[/\\:*?"<>|\n]', "", focus)[:20].strip()
            return safe
    except Exception:
        pass
    return ""


def _is_safe_save_filename(filename: str) -> bool:
    """只允许覆盖 saves/ 下的单个 zip 文件名。"""
    return (
        bool(filename)
        and filename.endswith(".zip")
        and "/" not in filename
        and "\\" not in filename
    )


def _read_archive_save_id(save_path: Path) -> str:
    """读取旧档位的来源标记，缺失时用文件名兜底。"""
    try:
        with zipfile.ZipFile(str(save_path), "r") as zf:
            if "metadata.json" in zf.namelist():
                meta = json.loads(zf.read("metadata.json").decode("utf-8"))
                save_id = str(meta.get("save_id", "")).strip()
                if save_id:
                    return save_id
    except Exception:
        pass

    stem = save_path.stem
    return stem.rsplit("_", 1)[-1] if "_" in stem else stem


def _recall_state_by_content_hash(
    recall_state: dict[str, dict[str, str]],
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for entry in recall_state.values():
        date = canonical_cn_date(str(entry.get("date", ""))) or str(
            entry.get("date", "")
        ).strip()
        content_hash = str(entry.get("content_hash", "")).strip()
        recalled_at = canonical_cn_date(str(entry.get("last_recalled_at", ""))) or str(
            entry.get("last_recalled_at", "")
        ).strip()
        if date and content_hash and recalled_at:
            result[(date, content_hash)] = recalled_at
    return result


def _memory_jsonl_archive_payload(
    agent_name: str,
    recall_state: dict[str, dict[str, str]],
) -> str | None:
    """返回写入存档的 memory.jsonl 内容，并合并 DB 中最新 recall 状态。"""
    memory_path = Path(character_path(agent_name, "memory.jsonl"))
    if not memory_path.exists():
        return None

    raw_text = memory_path.read_text(encoding="utf-8")
    if not recall_state:
        return raw_text

    recall_by_hash = _recall_state_by_content_hash(recall_state)
    output_lines: list[str] = []

    for line in raw_text.splitlines():
        record = parse_jsonl_line(line)
        if record is None:
            output_lines.append(line)
            continue

        date = canonical_cn_date(record.date) or record.date
        content_hash = hashlib.sha1(record.content.encode("utf-8")).hexdigest()
        recalled_at = recall_by_hash.get((date, content_hash), record.last_recalled_at)
        recalled_at = canonical_cn_date(recalled_at) or recalled_at or date
        output_lines.append(
            serialize_episode(record.model_copy(update={"last_recalled_at": recalled_at}))
        )

    return "\n".join(output_lines) + "\n"


def _build_new_save_path(theme: str, save_dir: Path) -> tuple[str, Path, str]:
    """为新档位生成不冲突的 uuid 文件名。"""
    for _ in range(20):
        save_id = uuid.uuid4().hex[:8]
        filename = f"{theme}_{save_id}.zip" if theme else f"{save_id}.zip"
        save_path = save_dir / filename
        if not save_path.exists():
            return filename, save_path, save_id

    save_id = uuid.uuid4().hex
    filename = f"{theme}_{save_id}.zip" if theme else f"{save_id}.zip"
    return filename, save_dir / filename, save_id


async def export_save_archive_with_detail(
    target_filename: str | None = None,
) -> tuple[str | None, str | None]:
    """导出存档，并返回路径或可直接展示的错误详情。

    Args:
        target_filename: 指定时覆盖该 saves/ 下的 zip；为空时创建新档位。

    Returns:
        (save_path, error_detail)
    """
    theme = _read_story_theme()

    save_dir = PROJECT_ROOT / "saves"
    os.makedirs(save_dir, exist_ok=True)

    if target_filename is not None:
        if not _is_safe_save_filename(target_filename):
            return None, "非法存档文件名。"
        filename = target_filename
        save_path = save_dir / filename
        if not save_path.exists():
            return None, "目标存档不存在，无法覆盖。"
        save_id = _read_archive_save_id(save_path) or _read_save_id()
    else:
        filename, save_path, save_id = _build_new_save_path(theme, save_dir)
        print(f"[存档] 新建档位: {filename}")

    focus = _read_narrator_focus()

    all_agents = get_agent_names()
    if not all_agents:
        print("[存档] 没有找到任何角色")
        return None, "没有找到任何角色，无法创建存档。"

    temp_path = save_dir / f".{filename}.{uuid.uuid4().hex}.tmp"

    try:
        # 运行期 recall 真值在 DB；存档时合并进 zip 内的 memory.jsonl。
        from storage.vector_store import vector_store

        character_agents = [a for a in all_agents if a != "narrator"]
        export_results = await asyncio.gather(
            *(
                vector_store.export_recall_state(agent_name)
                for agent_name in character_agents
            )
        )
        recall_states = dict(zip(character_agents, export_results))

        with zipfile.ZipFile(str(temp_path), "w", zipfile.ZIP_DEFLATED) as zf:
            metadata = {
                "export_time": datetime.now().isoformat(),
                "save_id": save_id,
                "filename": filename,
                "theme": theme,
                "focus": focus,
                "agents": all_agents,
                "version": "1.0",
            }
            zf.writestr(
                "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2)
            )

            zf.writestr(".save_id", save_id)
            print(f"[存档] 已添加: .save_id")

            for marker in [".story_id", ".turn_counter.json"]:
                marker_path = CHARACTERS_DIR / marker
                if marker_path.exists():
                    zf.write(str(marker_path), marker)
                    print(f"[存档] 已添加: {marker}")

            for agent_name in all_agents:
                agent_files = _get_agent_save_files(agent_name)
                for filepath in agent_files:
                    if os.path.exists(filepath):
                        arcname = os.path.relpath(filepath, start=str(CHARACTERS_DIR))
                        if (
                            agent_name != "narrator"
                            and os.path.basename(filepath) == "memory.jsonl"
                        ):
                            payload = _memory_jsonl_archive_payload(
                                agent_name,
                                recall_states.get(agent_name, {}),
                            )
                            if payload is not None:
                                zf.writestr(arcname, payload)
                                print(f"[存档] 已添加: {filepath} (merged recall)")
                                continue
                        zf.write(filepath, arcname)
                        print(f"[存档] 已添加: {filepath}")

        os.replace(temp_path, save_path)
        _write_save_id(save_id)
        print(f"[存档] 导出完成: {save_path}")
        return str(save_path), None

    except Exception as e:
        temp_path.unlink(missing_ok=True)
        detail = f"{type(e).__name__}: {e}"
        print(f"[存档] 导出失败: {detail}")
        routing_logger.error("[save] 导出异常: %s\n%s", detail, traceback.format_exc())
        return None, detail


async def export_save_archive() -> str | None:
    """兼容旧接口：仅返回存档路径。"""
    save_path, _ = await export_save_archive_with_detail()
    return save_path
