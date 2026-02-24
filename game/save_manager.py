"""存档与游戏状态管理"""

import glob
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from engine.config import CHARACTERS_DIR, PROJECT_ROOT, character_path, get_agent_names


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


def load_opening_text() -> str:
    """从配置文件加载开场白"""
    opening_path = PROJECT_ROOT / "prompts" / "opening.txt"
    if opening_path.exists():
        try:
            return opening_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[警告] 读取开场白文件失败: {e}")
    return ""


def load_recent_raw_messages(limit: int = 10) -> list:
    """从 narrator 的 raw/ 目录加载最近的原始消息（narrator 拥有上帝视角，包含所有消息）"""
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


async def reset_game(show_opening: bool = True) -> str:
    """重置游戏，从 templates 重新创建 characters 目录

    Returns:
        开场白内容（如果 show_opening=True）或空字符串
    """
    try:
        print(f"\n{'=' * 40}", flush=True)
        print("重置游戏...", flush=True)
        print(f"{'=' * 40}\n", flush=True)

        # 1. 删除 EverMemOS 中的向量记忆
        from memory.vector_store import vector_store
        all_agents = get_agent_names()
        if all_agents:
            print("[EverMemOS] 清理向量记忆...", flush=True)
            await vector_store.delete_all_agents(all_agents)

        # 2. 删除整个 characters 目录（如果存在）
        characters_dir = "data/characters"
        if os.path.exists(characters_dir):
            try:
                shutil.rmtree(characters_dir)
                print(f"  已删除: {characters_dir}", flush=True)
            except Exception as e:
                print(f"  删除失败 {characters_dir}: {e}", flush=True)

        # 2. 从 templates 完整复制
        templates_dir = "data/templates"
        if os.path.exists(templates_dir):
            try:
                shutil.copytree(templates_dir, characters_dir)
                print(f"  已复制: {templates_dir} -> {characters_dir}", flush=True)
            except Exception as e:
                print(f"  复制失败: {e}", flush=True)
        else:
            print(f"  [警告] 模板目录不存在: {templates_dir}", flush=True)

        # 3. 只为 narrator 创建 raw 目录（对话历史集中存储）
        raw_dir = narrator_raw_dir()
        os.makedirs(raw_dir, exist_ok=True)
        print(f"  已创建: {raw_dir}", flush=True)

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

    opening_text = load_opening_text()

    # 将开场旁白写入 narrator 的历史
    timestamp = datetime.now().isoformat()
    all_agents = get_agent_names()
    visible_agents = [a for a in all_agents if a != "narrator"]
    opening_message = {
        "timestamp": timestamp,
        "role": "narrator",
        "content": opening_text,
        "visible_to": visible_agents + ["narrator"],
    }

    raw_path = character_path("narrator", "raw", f"{datetime.now().strftime('%Y-%m-%d')}.jsonl")
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")

    return opening_text if show_opening else ""


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

    # 核心记忆文件
    for filename in ["memory.md", "user.md", "status.md"]:
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


async def export_save_archive() -> str | None:
    """导出存档文件，返回存档文件路径

    Returns:
        存档文件路径，如果失败返回 None
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = PROJECT_ROOT / "saves"
    os.makedirs(save_dir, exist_ok=True)

    save_path = str(save_dir / f"save_{timestamp}.zip")

    all_agents = get_agent_names()
    if not all_agents:
        print("[存档] 没有找到任何角色")
        return None

    try:
        with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 添加元数据文件
            metadata = {
                "export_time": datetime.now().isoformat(),
                "agents": all_agents,
                "version": "1.0"
            }
            zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))

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
