"""存档与游戏状态管理"""

import glob
import json
import os
import re
import shutil
import zipfile
from datetime import datetime

from engine.config import CHARACTERS_DIR, PROJECT_ROOT, character_path, get_agent_names

TEMPLATES_DIR = PROJECT_ROOT / "data" / "templates"


# =============================================================================
# 路径工具
# =============================================================================


def agent_path(agent_name: str) -> str:
    """获取角色基础目录路径"""
    return character_path(agent_name)


def narrator_raw_dir() -> str:
    """获取 narrator 的 raw 目录路径（只有 narrator 拥有对话历史）"""
    return character_path("narrator", "raw")


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


def load_conversation_history(limit: int = 10) -> list:
    """从 narrator 的 raw/ 目录加载最近的对话历史（跨所有日期的 jsonl 文件）

    narrator 拥有上帝视角，包含所有消息。返回原始消息列表（未过滤、未格式化）。

    Args:
        limit: 返回最近多少条消息

    Returns:
        最近 limit 条消息的列表，每条是 dict（包含 role, content, visible_to 等字段）
    """
    raw_dir = character_path("narrator", "raw")
    if not os.path.exists(raw_dir):
        return []

    # 按日期排序所有 jsonl 文件
    jsonl_files = sorted(glob.glob(f"{raw_dir}/*.jsonl"))
    if not jsonl_files:
        return []

    # 从所有文件中收集消息
    all_messages = []
    for filepath in jsonl_files:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        all_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue

    # 返回最后 limit 条
    return all_messages[-limit:]


# =============================================================================
# 存档检查 / 重置
# =============================================================================


def has_existing_save() -> bool:
    """检查是否有已存在的存档（narrator 有 jsonl 文件）"""
    raw_dir = narrator_raw_dir()
    if os.path.exists(raw_dir):
        jsonl_files = glob.glob(f"{raw_dir}/*.jsonl")
        if jsonl_files:
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

        # 2. 从对应故事模板完整复制
        story_template_dir = str(TEMPLATES_DIR / story_id)
        if os.path.exists(story_template_dir):
            try:
                shutil.copytree(story_template_dir, characters_dir)
                print(f"  已复制: {story_template_dir} -> {characters_dir}", flush=True)
            except Exception as e:
                print(f"  复制失败: {e}", flush=True)
        else:
            print(f"  [警告] 故事模板目录不存在: {story_template_dir}", flush=True)

        # 3. 只为 narrator 创建 raw 目录（对话历史集中存储）
        raw_dir = narrator_raw_dir()
        os.makedirs(raw_dir, exist_ok=True)
        print(f"  已创建: {raw_dir}", flush=True)

        # 4. 写入 story_id 标记文件，供存档命名使用
        story_id_path = os.path.join(characters_dir, ".story_id")
        with open(story_id_path, "w", encoding="utf-8") as f:
            f.write(story_id)
        print(f"  已写入: {story_id_path}", flush=True)

        # 4. 重置日志
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

    # 返回两个独立的开场白（玩法介绍、故事开始）
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
    # 新格式：<主题>_<焦点>_YYYYMMDD_HHMMSS.zip；时间戳永远在末尾，用正则定位
    ts_re = re.compile(r"^(.+_)?(\d{8}_\d{6})$")
    for zip_file in sorted(save_dir.glob("*.zip"), reverse=True):
        try:
            m = ts_re.match(zip_file.stem)
            if not m:
                continue
            prefix = (m.group(1) or "").rstrip("_")  # "school_午休时间" 或 ""
            ts_str = m.group(2)                       # "20260302_153000"
            dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            display_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            display_time = zip_file.name
            prefix = ""
        saves.append({"filename": zip_file.name, "display_time": display_time, "focus": prefix})

    return saves


async def import_save_archive(save_filename: str) -> bool:
    """从指定存档文件恢复游戏状态

    Args:
        save_filename: 存档文件名（仅文件名，不含路径）

    Returns:
        成功返回 True，失败返回 False
    """
    # 安全检查：防止路径遍历
    if not save_filename.endswith(".zip") or os.sep in save_filename or "/" in save_filename:
        print(f"[读档] 非法文件名: {save_filename}", flush=True)
        return False

    save_path = PROJECT_ROOT / "saves" / save_filename
    if not save_path.exists():
        print(f"[读档] 存档文件不存在: {save_path}", flush=True)
        return False

    try:
        # 1. 清空向量记忆
        from memory.vector_store import vector_store

        all_agents = get_agent_names()
        if all_agents:
            print("[读档] 清理向量记忆...", flush=True)
            await vector_store.delete_all_agents(all_agents)

        # 2. 删除当前 characters 目录
        characters_dir = str(CHARACTERS_DIR)
        if os.path.exists(characters_dir):
            shutil.rmtree(characters_dir)
            print(f"[读档] 已删除: {characters_dir}", flush=True)

        # 3. 解压存档到 characters 目录（跳过 metadata.json）
        os.makedirs(characters_dir, exist_ok=True)
        with zipfile.ZipFile(str(save_path), "r") as zf:
            for member in zf.namelist():
                if member == "metadata.json":
                    continue
                zf.extract(member, characters_dir)
                print(f"[读档] 已恢复: {member}", flush=True)

        # 4. 重建向量库（从 jsonl 历史重新索引）
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
    """获取指定角色需要存档的所有文件路径

    Args:
        agent_name: 角色名称

    Returns:
        文件路径列表（相对于 data/characters/ 目录）
    """
    files = []
    base = agent_path(agent_name)

    # 核心记忆文件（soul.md 虽只读，但存档需自包含；growth.md 由 consolidator 运行时生成）
    for filename in ["soul.md", "memory.md", "user.md", "status.md", "growth.md"]:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    # 可选的状态文件
    for filename in ["states.md", "events.md", "tasks.md"]:
        filepath = f"{base}/{filename}"
        if os.path.exists(filepath):
            files.append(filepath)

    # 整理状态文件
    consolidation_file = f"{base}/.consolidation_state.json"
    if os.path.exists(consolidation_file):
        files.append(consolidation_file)

    # narrator 的 raw/ 对话历史（只有 narrator 有）
    if agent_name == "narrator":
        raw_dir = narrator_raw_dir()
        if os.path.exists(raw_dir):
            for jsonl_file in glob.glob(f"{raw_dir}/*.jsonl"):
                files.append(jsonl_file)

    return files


def _read_story_theme() -> str:
    """读取 .story_id 标记文件，返回故事主题（如 school / modern / ancient）"""
    story_id_path = os.path.join(str(CHARACTERS_DIR), ".story_id")
    try:
        return open(story_id_path, encoding="utf-8").read().strip()
    except Exception:
        return ""


def _read_narrator_focus() -> str:
    """从 narrator/status.md 读取叙事焦点，用于存档命名"""
    status_path = character_path("narrator", "status.md")
    try:
        lines = open(status_path, encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            if "叙事焦点" in line:
                for j in range(i + 1, len(lines)):
                    content = lines[j].strip()
                    if content:
                        # 过滤文件名中不合法的字符，限制长度
                        safe = re.sub(r'[/\\:*?"<>|\n]', "", content)[:20].strip()
                        return safe if safe else ""
    except Exception:
        pass
    return ""


async def export_save_archive() -> str | None:
    """导出存档文件，返回存档文件路径

    Returns:
        存档文件路径，如果失败返回 None
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    theme = _read_story_theme()
    focus = _read_narrator_focus()
    # 格式：<主题>_<焦点>_YYYYMMDD_HHMMSS.zip，缺失时降级
    prefix = "_".join(part for part in [theme, focus] if part)
    filename = f"{prefix}_{timestamp}.zip" if prefix else f"{timestamp}.zip"

    save_dir = PROJECT_ROOT / "saves"
    os.makedirs(save_dir, exist_ok=True)

    save_path = str(save_dir / filename)

    all_agents = get_agent_names()
    if not all_agents:
        print("[存档] 没有找到任何角色")
        return None

    try:
        with zipfile.ZipFile(save_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 添加元数据文件
            metadata = {
                "export_time": datetime.now().isoformat(),
                "agents": all_agents,
                "version": "1.0",
            }
            zf.writestr(
                "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2)
            )

            # 添加 .story_id 标记文件（解压后主题仍可读）
            story_id_path = str(CHARACTERS_DIR / ".story_id")
            if os.path.exists(story_id_path):
                zf.write(story_id_path, ".story_id")
                print("[存档] 已添加: .story_id")

            # 添加每个角色的文件
            for agent_name in all_agents:
                agent_files = _get_agent_save_files(agent_name)
                for filepath in agent_files:
                    if os.path.exists(filepath):
                        # 在 zip 中保持相对路径结构
                        arcname = os.path.relpath(filepath, start=str(CHARACTERS_DIR))
                        zf.write(filepath, arcname)
                        print(f"[存档] 已添加: {filepath}")

        print(f"[存档] 导出完成: {save_path}")
        return save_path

    except Exception as e:
        print(f"[存档] 导出失败: {e}")
        return None
