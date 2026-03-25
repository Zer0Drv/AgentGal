"""响应解析器 - 从 Agent 输出中提取 XML 格式的更新指令"""

import json
import re
from dataclasses import dataclass
from typing import Optional

from log_config.routing import routing_logger


@dataclass
class ParsedResponse:
    """解析后的响应结构"""

    content: str  # 清理后的对话内容（不含 XML 标签）
    memory: Optional[str] = None  # 需要追加到 memory.md 的内容
    status: Optional[dict] = None  # 需要更新到 status.md 的字段
    player: Optional[dict] = None  # 需要追加到 user.md 的字段
    triggered: Optional[list[str]] = None  # 需要标记为已触发的事件名列表
    add_event: Optional[list[str]] = None  # 需要插入到待触发事件队列的新事件描述列表


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
    # 提取 XML 块（处理闭合和未闭合的情况）
    xml_content, clean_content = _extract_update_notes(raw_response)

    if xml_content is None:
        # 没有 update_notes 标签，返回原始内容
        return ParsedResponse(content=raw_response.strip())

    # 解析各个字段
    memory = _extract_xml_field(xml_content, "memory")
    status = _extract_xml_field(xml_content, "status")
    player = _extract_xml_field(xml_content, "player")
    triggered_raw = _extract_xml_field(xml_content, "triggered")
    add_event_list = _extract_all_xml_tags(xml_content, "add_event")

    # 解析 JSON 字段
    status_dict = _parse_json_field(status, "status", agent_name)
    player_dict = _parse_json_field(player, "player", agent_name)

    # 解析 triggered：逗号或换行分隔的事件名列表
    triggered_list = _parse_triggered_field(triggered_raw, agent_name)

    # 记录解析结果
    has_updates = any([memory, status_dict, player_dict, triggered_list, add_event_list])
    if has_updates:
        routing_logger.info(
            f"[{agent_name}] 解析到更新: memory={'有' if memory else '无'}, "
            f"status={list(status_dict.keys()) if status_dict else '无'}, "
            f"player={list(player_dict.keys()) if player_dict else '无'}, "
            f"triggered={triggered_list or '无'}, "
            f"add_event={add_event_list or '无'}"
        )

    return ParsedResponse(
        content=clean_content,
        memory=memory.strip() if memory else None,
        status=status_dict,
        player=player_dict,
        triggered=triggered_list,
        add_event=add_event_list,
    )


def _extract_update_notes(raw_response: str) -> tuple[Optional[str], str]:
    """
    提取 update_notes 标签内容，处理闭合和未闭合的情况。

    Returns:
        (xml_content, clean_content): 标签内容和移除标签块后的干净内容
        如果没有找到标签，返回 (None, "")
    """
    # 查找 <update_notes> 开始标签
    start_match = re.search(r"<update_notes>\s*", raw_response, re.DOTALL)
    if not start_match:
        return None, ""

    start_pos = start_match.start()  # <update_notes> 标签开始的位置
    content_start = start_match.end()  # <update_notes> 标签之后的内容开始位置

    # 尝试查找闭合标签 </update_notes>
    end_match = re.search(r"</update_notes>", raw_response[content_start:], re.DOTALL)

    if end_match:
        # 正常闭合的情况
        xml_content = raw_response[content_start : content_start + end_match.start()]
        # 移除整个块（包括可能紧跟的多余 </update_notes>）
        block_end = content_start + end_match.end()
        # 跳过紧跟的多余 </update_notes> 标签（LLM 偶尔会输出双重闭合）
        while True:
            extra_close = re.match(r"\s*</update_notes>", raw_response[block_end:])
            if extra_close:
                block_end += extra_close.end()
            else:
                break
        clean_content = (raw_response[:start_pos] + raw_response[block_end:]).strip()
    else:
        # 未闭合的情况：提取到字符串末尾或下一个大段落分隔符
        remaining = raw_response[content_start:]
        # 查找可能的结束标记（如分隔线、metrics 等）
        separator_match = re.search(r"\n\s*-{10,}|\n\s*\[metrics\]|\n\s*$", remaining)
        if separator_match:
            xml_content = remaining[: separator_match.start()]
        else:
            xml_content = remaining

        routing_logger.warning(
            f"检测到未闭合的 <update_notes> 标签，从位置 {start_pos} 提取"
        )
        clean_content = raw_response[:start_pos].strip()

    return xml_content.strip(), clean_content


def _extract_xml_field(xml_content: str, field_name: str) -> Optional[str]:
    """从 XML 内容中提取指定字段（仅第一个匹配）"""
    pattern = rf"<{field_name}>(.*?)</{field_name}>"
    match = re.search(pattern, xml_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _extract_all_xml_tags(xml_content: str, tag_name: str) -> Optional[list[str]]:
    """从 XML 内容中提取所有同名标签的内容列表（支持多次出现）"""
    pattern = rf"<{tag_name}>(.*?)</{tag_name}>"
    matches = re.findall(pattern, xml_content, re.DOTALL)
    results = [m.strip() for m in matches if m.strip()]
    return results if results else None


def _parse_triggered_field(
    content: Optional[str], agent_name: str
) -> Optional[list[str]]:
    """解析 triggered 字段，返回事件名列表（支持逗号或换行分隔）"""
    if not content:
        return None
    # 支持逗号或换行分隔，去除空项和多余空白
    names = [name.strip() for name in re.split(r"[,\n]", content) if name.strip()]
    if not names:
        routing_logger.warning(f"[{agent_name}] triggered 字段内容为空")
        return None
    return names


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


