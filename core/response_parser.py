"""响应解析器 - 从 Agent 输出中提取 XML 格式的更新指令"""

import json
import re
from dataclasses import dataclass
from typing import Optional

from .routing_logger import routing_logger


@dataclass
class ParsedResponse:
    """解析后的响应结构"""

    content: str  # 清理后的对话内容（不含 XML 标签）
    memory: Optional[str] = None  # 需要追加到 memory.md 的内容
    status: Optional[dict] = None  # 需要更新到 status.md 的字段
    player: Optional[dict] = None  # 需要追加到 user.md 的字段


def parse_agent_response(raw_response: str, agent_name: str) -> ParsedResponse:
    """
    解析 Agent 响应，提取 XML 格式的更新指令。

    格式示例：
        ## 角色名
        对话内容...

        <update_notes>
        <memory>
        ## 2月20日
        - 今天他说了...
        </memory>
        <status>
        {"心境": "开心"}
        </status>
        <player>
        {"了解到的事": "喜欢咖啡"}
        </player>
        </update_notes>

    Args:
        raw_response: Agent 原始响应
        agent_name: 角色名称（用于日志）

    Returns:
        ParsedResponse: 解析后的内容和更新指令
    """
    # 提取 XML 块
    xml_pattern = r"<update_notes>(.*?)</update_notes>"
    xml_match = re.search(xml_pattern, raw_response, re.DOTALL)

    if not xml_match:
        # 没有 update_notes 标签，返回原始内容
        return ParsedResponse(content=raw_response.strip())

    xml_content = xml_match.group(1)

    # 移除 XML 块后的干净内容
    clean_content = raw_response[: xml_match.start()].strip()

    # 解析各个字段
    memory = _extract_xml_field(xml_content, "memory")
    status = _extract_xml_field(xml_content, "status")
    player = _extract_xml_field(xml_content, "player")

    # 解析 JSON 字段
    status_dict = _parse_json_field(status, "status", agent_name)
    player_dict = _parse_json_field(player, "player", agent_name)

    # 记录解析结果
    has_updates = any([memory, status_dict, player_dict])
    if has_updates:
        routing_logger.info(
            f"[{agent_name}] 解析到更新: memory={'有' if memory else '无'}, "
            f"status={list(status_dict.keys()) if status_dict else '无'}, "
            f"player={list(player_dict.keys()) if player_dict else '无'}"
        )

    return ParsedResponse(
        content=clean_content,
        memory=memory.strip() if memory else None,
        status=status_dict,
        player=player_dict,
    )


def _extract_xml_field(xml_content: str, field_name: str) -> Optional[str]:
    """从 XML 内容中提取指定字段"""
    pattern = rf"<{field_name}>(.*?)</{field_name}>"
    match = re.search(pattern, xml_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _parse_json_field(
    content: Optional[str], field_name: str, agent_name: str
) -> Optional[dict]:
    """解析 JSON 字段，出错时返回 None 并记录日志"""
    if not content:
        return None

    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            # 清理键名：去除多余引号（处理 LLM 输出 "\"场景\"" 的情况）
            cleaned = {}
            for k, v in parsed.items():
                clean_key = k.strip().strip('"').strip("'")
                cleaned[clean_key] = v
            return cleaned
        routing_logger.warning(
            f"[{agent_name}] {field_name} 解析结果不是字典: {type(parsed)}"
        )
        return None
    except json.JSONDecodeError as e:
        routing_logger.warning(
            f"[{agent_name}] {field_name} JSON 解析失败: {e}, 内容: {content[:100]}..."
        )
        return None
