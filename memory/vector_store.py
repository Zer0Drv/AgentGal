"""向量存储 - EverMemOS 后端，只负责存入和搜索"""

import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from log_config.routing import routing_logger
from engine.config import character_path

# 标准角色名映射（避免多处硬编码）
SENDER_MAP: dict[str, str] = {
    "玩家": "player",
    "旁白": "narrator",
}

# 可选依赖：evermemos
try:
    from evermemos import EverMemOS

    _HAS_EVERMEMOS = True
except ImportError:
    _HAS_EVERMEMOS = False
    EverMemOS = None


class VectorStore:
    """EverMemOS 向量存储。职责：存入（add/schedule_add）和搜索（search）。

    约定：
    - 上下文隔离按 group_id=agent_name 划分；narrator 也有自己的 group。
    - 仅存“对话轮次原文”到向量库；不再存整理后的事件片段。
    - 群聊整轮写入可标注 scene="group_chat"，普通写入使用 scene="assistant"。
    """

    def __init__(self):
        self._client = None
        self._bg_tasks: set[asyncio.Task] = set()

    def _get_client(self):
        """获取 EverMemOS 客户端（延迟初始化）"""
        if self._client is None:
            if not _HAS_EVERMEMOS:
                raise ImportError("evermemos SDK not installed. Run: uv add evermemos")
            api_key = os.getenv("EVERMEMOS_API_KEY")
            if not api_key:
                raise ValueError("EVERMEMOS_API_KEY environment variable not set")
            base_url = os.getenv("EVERMEMOS_BASE_URL")
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = EverMemOS(**kwargs)
        return self._client

    # --- 存入 ---

    async def _add_one(
        self,
        target_group: str,
        message_id: str,
        content: str,
        *,
        sender: str,
        scene: str = "assistant",
        flush: bool = True,
    ) -> bool:
        """向 EverMemOS 写入单条事件（所有写入路径的统一入口）。"""
        try:
            response = self._get_client().v0.memories.add(
                message_id=message_id,
                create_time=datetime.utcnow().isoformat() + "Z",
                sender=sender,
                sender_name=sender,
                group_id=target_group,
                content=content,
                scene=scene,
                flush=flush,
            )
            # 兼容不同网关返回风格
            if getattr(response, "status", "") not in {"success", "ok"}:
                routing_logger.warning(
                    f"[EverMemOS] {target_group} {message_id} 写入失败: {getattr(response, message, unknown)}"
                )
                return False
            routing_logger.info(f"[EverMemOS] {target_group} {message_id} 写入成功")
            return True
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {target_group} {message_id} 写入异常: {e}")
            return False

    def schedule_add(
        self,
        target_group: str,
        message_id: str,
        content: str,
        *,
        sender: str,
        scene: str = "assistant",
    ):
        """非阻塞：将单条事件写入丢到后台执行。"""
        async def _runner():
            await self._add_one(target_group, message_id, content, sender=sender, scene=scene)

        task = asyncio.create_task(_runner())
        self._bg_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._bg_tasks.discard(t)
            try:
                exc = t.exception()
                if exc:
                    routing_logger.error(
                        f"[EverMemOS] 写入任务异常 {target_group}/{message_id}: {exc}"
                    )
            except Exception:
                # 不让回调本身抛出异常影响事件循环
                pass

        task.add_done_callback(_on_done)

    def schedule_add_round(self, targets: list[str], round_id: str, content: str):
        """每轮对话结束后调用：为每个参与角色后台写入该轮原始对话记录（逐条消息）。

        解析传入的整轮文本，按“玩家/旁白/角色名: 内容”逐行拆分为消息；
        每条消息写入到该轮的所有 targets（含 narrator）的 group 中，scene=group_chat；
        sender=真实说话人（player/narrator/角色名）。
        """
        # 1) 解析整轮文本 -> [(sender, text)]
        msgs: list[tuple[str, str]] = []
        for line in content.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            label, text = line.split(":", 1)
            sender = SENDER_MAP.get(label.strip(), label.strip())
            text = text.strip()
            if not text:
                continue
            msgs.append((sender, text))

        # 2) 写入到每个目标角色的 group
        for agent_name in targets:
            for idx, (sender, text) in enumerate(msgs):
                message_id = f"{agent_name}_{round_id}_{idx}"
                self.schedule_add(
                    agent_name,
                    message_id,
                    text,
                    sender=sender,
                    scene="group_chat",
                )

    async def rebuild(self, agent_name: str):
        """全量重建：基于 narrator/raw/*.jsonl，按“消息粒度”重导入。

        规则：
        - narrator 导入所有消息；其他角色仅导入 visible_to 中包含自身的消息。
        - sender=该条的 role；scene="group_chat"；message_id=f"{agent}_{date}_{idx}"。
        """
        import json, glob
        raw_dir = Path(character_path("narrator", "raw"))
        if not raw_dir.exists():
            routing_logger.warning(f"[EverMemOS] 重建: narrator 原始对话目录不存在: {raw_dir}")
            return
        files = sorted(glob.glob(str(raw_dir / "*.jsonl")))
        if not files:
            routing_logger.info("[EverMemOS] 重建: 未发现任何原始对话记录，跳过")
            return

        def _ensure_list(v):
            if isinstance(v, list):
                return v
            try:
                return json.loads(v) if isinstance(v, str) else []
            except Exception:
                return []

        total = 0
        for fp in files:
            date = Path(fp).stem
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    records = []
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            obj["visible_to"] = _ensure_list(obj.get("visible_to", []))
                            records.append(obj)
                        except Exception:
                            continue

                    for idx, obj in enumerate(records):
                        role = str(obj.get("role", "")).lower()
                        text = str(obj.get("content", ""))
                        vis = set(str(x).lower() for x in obj.get("visible_to", []))

                        should = (agent_name == "narrator") or (agent_name.lower() in vis)
                        if not should or not text.strip():
                            continue

                        message_id = f"{agent_name}_{date}_{idx}"
                        ok = await self._add_one(
                            agent_name,
                            message_id,
                            text,
                            sender=role or "unknown",
                            scene="group_chat",
                        )
                        if ok:
                            total += 1
            except Exception as e:
                routing_logger.error(f"[EverMemOS] 重建读取失败 {fp}: {e}")
        routing_logger.info(f"[EverMemOS] {agent_name} 全量重建完成: {total} 条")

    # --- 搜索 ---

    def search_sync(
        self, agent_name: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """同步语义搜索，供 Agno instructions 使用。"""
        try:
            response = self._get_client().v0.memories.search(
                # 基于 group_id 的隔离检索；仅召回该组的对话片段
                extra_query={
                    "query": query,
                    "group_id": agent_name,
                    "memory_scope": "group",
                    "data_source": "episode",
                    "retrieval_mode": "rrf",
                    "top_k": limit,
                }
            )
            results = []
            if response.result and response.result.memories:
                for mem in response.result.memories:
                    # SDK 返回 pydantic model；episode 优先，其次 summary
                    content = getattr(mem, "episode", "") or getattr(mem, "summary", "")
                    score = getattr(mem, "score", None)
                    results.append(
                        {
                            "id": getattr(mem, "id", ""),
                            "content": content,
                            "score": None if score is None else float(score),
                        }
                    )
            routing_logger.info(
                f"[EverMemOS] {agent_name} 搜索 '{query[:30]}...' 召回 {len(results)} 条"
            )
            return results
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 搜索失败: {e}")
            return []

    # --- 删除 ---

    async def delete(self, agent_name: str) -> bool:
        """删除 EverMemOS 中指定角色的所有记忆（group_id=agent_name）。"""
        try:
            response = self._get_client().v0.memories.delete(
                extra_body={"user_id": agent_name, "group_id": agent_name}
            )
            success = getattr(response, "status", "") in {"success", "ok"}
            if success:
                routing_logger.info(f"[EverMemOS] {agent_name} 记忆删除成功")
            else:
                routing_logger.warning(
                    f"[EverMemOS] {agent_name} 记忆删除失败: {getattr(response, 'message', 'unknown')}"
                )
            return success
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 删除记忆失败: {e}")
            return False

    async def delete_all_agents(self, agent_names: list[str]) -> dict[str, bool]:
        """并行删除多个角色的记忆。"""
        results = await asyncio.gather(*(self.delete(name) for name in agent_names))
        return dict(zip(agent_names, results))

    async def close(self):
        """等待后台任务完成"""
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)


# 全局实例
vector_store = VectorStore()
