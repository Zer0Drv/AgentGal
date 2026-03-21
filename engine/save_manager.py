"""存档与游戏状态管理"""

import glob
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime

from engine.config import CHARACTERS_DIR, PROJECT_ROOT, character_path, get_agent_names
from engine.agent_files import read_agent_file
from engine.history import load_conversation_history
from memory.parser import extract_status_field

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
        from memory.vector_store import vector_store

        all_agents = get_agent_names()
        if all_agents:
            print("[Reset] 清理向量记忆...", flush=True)
            await vector_store.delete_all_agents(all_agents)

        # 2. 删除整个 characters 目录（如果存在）
        characters_dir = "data/characters"
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
        save_id_path = os.path.join(characters_dir, ".save_id")
        with open(save_id_path, "w", encoding="utf-8") as f:
            f.write(save_id)
        print(f"  已写入: {save_id_path} ({save_id})", flush=True)

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
    }

    raw_path = character_path(
        "narrator", "raw", f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    )
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")

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


async def import_save_archive(save_filename: str) -> bool:
    """从指定存档文件恢复游戏状态

    Args:
        save_filename: 存档文件名（仅文件名，不含路径）

    Returns:
        成功返回 True，失败返回 False
    """
    if not save_filename.endswith(".zip") or os.sep in save_filename or "/" in save_filename:
        print(f"[读档] 非法文件名: {save_filename}", flush=True)
        return False

    save_path = PROJECT_ROOT / "saves" / save_filename
    if not save_path.exists():
        print(f"[读档] 存档文件不存在: {save_path}", flush=True)
        return False

    try:
        from memory.vector_store import vector_store

        all_agents = get_agent_names()
        if all_agents:
            print("[读档] 清理向量记忆...", flush=True)
            await vector_store.delete_all_agents(all_agents)

        characters_dir = str(CHARACTERS_DIR)
        if os.path.exists(characters_dir):
            shutil.rmtree(characters_dir)
            print(f"[读档] 已删除: {characters_dir}", flush=True)

        os.makedirs(characters_dir, exist_ok=True)
        with zipfile.ZipFile(str(save_path), "r") as zf:
            for member in zf.namelist():
                if member == "metadata.json":
                    continue
                zf.extract(member, characters_dir)
                print(f"[读档] 已恢复: {member}", flush=True)

        save_id_path = os.path.join(characters_dir, ".save_id")
        if not os.path.exists(save_id_path):
            new_save_id = uuid.uuid4().hex[:8]
            with open(save_id_path, "w", encoding="utf-8") as f:
                f.write(new_save_id)
            print(f"[读档] 旧存档无 save_id，已生成新 id: {new_save_id}", flush=True)

        print("[读档] 重建向量库...", flush=True)
        await vector_store.rebuild("narrator")
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
        core_files.extend(["memory.md", "user.md", "growth.md"])

    for filename in core_files:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    for filename in ["states.md", "events.md", "tasks.md"]:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    hidden_files = [".history_window_state.json"]
    if agent_name != "narrator":
        hidden_files.extend([".consolidation_state.json", ".memory_recall_state.json"])

    for hidden_file in hidden_files:
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
    """读取当前游戏的 save_id，用于确定覆盖哪个存档文件"""
    save_id_path = CHARACTERS_DIR / ".save_id"
    try:
        return save_id_path.read_text(encoding="utf-8").strip()
    except Exception:
        return uuid.uuid4().hex[:8]


def _read_story_theme() -> str:
    """读取 .story_id 标记文件，返回故事主题（如 school / modern）"""
    story_id_path = CHARACTERS_DIR / ".story_id"
    try:
        return story_id_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_narrator_focus() -> str:
    """从 narrator/status.md 读取叙事焦点，用于存档命名"""
    try:
        status_text = read_agent_file("narrator", "status.md")
        focus = extract_status_field(status_text, "叙事焦点")
        if focus:
            safe = re.sub(r'[/\\:*?"<>|\n]', "", focus)[:20].strip()
            return safe
    except Exception:
        pass
    return ""


async def export_save_archive() -> str | None:
    """导出存档文件，返回存档文件路径

    Returns:
        存档文件路径，如果失败返回 None
    """
    save_id = _read_save_id()
    theme = _read_story_theme()
    focus = _read_narrator_focus()
    filename = f"{theme}_{save_id}.zip" if theme else f"{save_id}.zip"

    save_dir = PROJECT_ROOT / "saves"
    os.makedirs(save_dir, exist_ok=True)
    save_path = str(save_dir / filename)

    all_agents = get_agent_names()
    if not all_agents:
        print("[存档] 没有找到任何角色")
        return None

    try:
        with zipfile.ZipFile(save_path, "w", zipfile.ZIP_DEFLATED) as zf:
            metadata = {
                "export_time": datetime.now().isoformat(),
                "save_id": save_id,
                "theme": theme,
                "focus": focus,
                "agents": all_agents,
                "version": "1.0",
            }
            zf.writestr(
                "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2)
            )

            for marker in [".story_id", ".save_id"]:
                marker_path = CHARACTERS_DIR / marker
                if marker_path.exists():
                    zf.write(str(marker_path), marker)
                    print(f"[存档] 已添加: {marker}")

            for agent_name in all_agents:
                agent_files = _get_agent_save_files(agent_name)
                for filepath in agent_files:
                    if os.path.exists(filepath):
                        arcname = os.path.relpath(filepath, start=str(CHARACTERS_DIR))
                        zf.write(filepath, arcname)
                        print(f"[存档] 已添加: {filepath}")

        print(f"[存档] 导出完成: {save_path}")
        return save_path

    except Exception as e:
        print(f"[存档] 导出失败: {e}")
        return None
