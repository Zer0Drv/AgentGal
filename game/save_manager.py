"""存档与游戏状态管理"""

import glob
import json
import os
import shutil
import zipfile
from datetime import datetime

from engine.config import get_agent_names, character_path


# =============================================================================
# 路径工具
# =============================================================================


def agent_path(agent_name: str) -> str:
    """获取角色基础目录路径"""
    return character_path(agent_name)


def agent_raw_dir(agent_name: str) -> str:
    """获取角色 raw 目录路径"""
    return character_path(agent_name, "raw")


# =============================================================================
# 开场白 / 消息加载
# =============================================================================


def load_opening_text() -> str:
    """从配置文件加载开场白"""
    opening_path = "prompts/opening.txt"
    if os.path.exists(opening_path):
        try:
            with open(opening_path, "r", encoding="utf-8") as f:
                return f.read()
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
    """检查是否有已存在的存档（任一角色有 jsonl 文件）"""
    all_agents = get_agent_names()
    for agent_name in all_agents:
        raw_dir = agent_raw_dir(agent_name)
        if os.path.exists(raw_dir):
            jsonl_files = glob.glob(f"{raw_dir}/*.jsonl")
            if jsonl_files:
                return True
    return False


def reset_agent_memory(agent_name: str):
    """重置指定角色的所有记忆文件（保留 soul.md）"""
    base = agent_path(agent_name)
    template_path = f"data/templates/{agent_name}"

    # 1. 删除 raw/ 目录下的所有 jsonl 文件（对话历史）
    raw_dir = agent_raw_dir(agent_name)
    if os.path.exists(raw_dir):
        for jsonl_file in glob.glob(f"{raw_dir}/*.jsonl"):
            try:
                os.remove(jsonl_file)
                print(f"  已删除: {os.path.basename(jsonl_file)}")
            except Exception as e:
                print(f"  删除失败 {jsonl_file}: {e}")

    # 2. 清空 Memory.md（长期记忆）
    memory_path = f"{base}/Memory.md"
    if os.path.exists(memory_path):
        try:
            with open(memory_path, "w", encoding="utf-8") as f:
                f.write("")
            print(f"  已清空: memory.md")
        except Exception as e:
            print(f"  清空失败 memory.md: {e}")

    # 3. 从 templates 恢复初始状态文件
    for filename in ["status.md", "user.md"]:
        template_file = f"{template_path}/{filename}"
        target_file = f"{base}/{filename}"
        if os.path.exists(template_file):
            try:
                shutil.copy2(template_file, target_file)
                print(f"  已恢复: {filename}")
            except Exception as e:
                print(f"  恢复失败 {filename}: {e}")


def reset_logs():
    """重置日志文件 - 清空 logs 目录下所有 .log 和 .jsonl 文件"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        return

    for root, _, files in os.walk(log_dir):
        for filename in files:
            if filename.endswith(".log") or filename.endswith(".jsonl"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "w"):
                        pass
                    print(f"  已清空: {filepath}")
                except Exception as e:
                    print(f"  清空失败 {filepath}: {e}")


async def reset_game(show_opening: bool = True) -> str:
    """重置游戏，从 templates 重新创建 characters 目录

    Returns:
        开场白内容（如果 show_opening=True）或空字符串
    """
    print(f"\n{'=' * 40}")
    print("重置游戏...")
    print(f"{'=' * 40}\n")

    # 1. 删除整个 characters 目录（如果存在）
    characters_dir = "data/characters"
    if os.path.exists(characters_dir):
        try:
            shutil.rmtree(characters_dir)
            print(f"  已删除: {characters_dir}")
        except Exception as e:
            print(f"  删除失败 {characters_dir}: {e}")

    # 2. 从 templates 完整复制
    templates_dir = "data/templates"
    if os.path.exists(templates_dir):
        try:
            shutil.copytree(templates_dir, characters_dir)
            print(f"  已复制: {templates_dir} -> {characters_dir}")
        except Exception as e:
            print(f"  复制失败: {e}")
    else:
        print(f"  [警告] 模板目录不存在: {templates_dir}")

    # 3. 为每个角色创建 raw 目录
    all_agents = get_agent_names()
    for agent_name in all_agents:
        raw_dir = agent_raw_dir(agent_name)
        os.makedirs(raw_dir, exist_ok=True)
        print(f"  已创建: {raw_dir}")

    # 4. 重置日志
    print("[日志]")
    reset_logs()

    print(f"\n{'=' * 40}")
    print("重置完成")
    print(f"{'=' * 40}\n")

    opening_text = load_opening_text()

    # 将开场旁白写入 narrator 的历史
    timestamp = datetime.now().isoformat()
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
    for filename in ["Memory.md", "user.md", "status.md"]:
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

    # 所有 jsonl 对话历史文件
    raw_dir = agent_raw_dir(agent_name)
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
    save_dir = "saves"
    os.makedirs(save_dir, exist_ok=True)

    save_path = f"{save_dir}/save_{timestamp}.zip"

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
                        arcname = filepath.replace("data/characters/", "")
                        zf.write(filepath, arcname)
                        print(f"[存档] 已添加: {filepath}")

        print(f"[存档] 导出完成: {save_path}")
        return save_path

    except Exception as e:
        print(f"[存档] 导出失败: {e}")
        return None
