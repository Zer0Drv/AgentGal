"""角色对话编排：搜记忆 → 构建 prompt → 跑 SDK → 经 repo 写回。

吸收原 engine.character.Character 的 run / _build_prompt / _apply_updates。
实体（Character）只承载 name + soul；I/O 走 CharacterRepository。
"""

from agents.factory import get_conversation_agent
from agents.llm_schema import LLMCharacterOutput
from agents.runner import run_app_agent
from engine.memory_query_builder import build_retrieval_queries
from engine.prompt_builder import build_user_message
from llm.config import get_llm_config
from llm.embedding import embed_sync
from log_config.routing import log_file_updates
from memory.retrieval import search_memories, search_understandings
from models import Character
from shared.config import HISTORY_RAW_SCAN_TURNS
from storage.character_repo import CharacterRepository, character_repo
from storage.history import load_conversation_history
from storage.status_file import FileUpdateResult


class ConversationService:
    """单个角色的一次对话编排。无状态，I/O 经注入的 CharacterRepository。"""

    def __init__(self, repo: CharacterRepository) -> None:
        self._repo = repo

    async def run_turn(
        self,
        character: Character,
        user_input: str,
        raw_messages: list[dict] | None = None,
        *,
        observation_mode: bool = False,
    ) -> LLMCharacterOutput:
        """搜记忆 → 构建 prompt → 运行 SDK → 写回文件，返回 LLMCharacterOutput。"""
        if raw_messages is None:
            raw_messages = load_conversation_history(turns=HISTORY_RAW_SCAN_TURNS)

        user_message = self._build_prompt(
            character, user_input, raw_messages, observation_mode=observation_mode
        )
        config = get_llm_config()
        output = await run_app_agent(
            get_conversation_agent(character.name),
            user_message,
            LLMCharacterOutput,
            workflow_name="agentgal_turn",
            usage_agent=character.name,
            model_name=config["model_id"],
        )
        self._apply_updates(character.name, output)
        return output

    def _build_prompt(
        self,
        character: Character,
        user_input: str,
        raw_messages: list[dict],
        *,
        observation_mode: bool = False,
    ) -> str:
        """组装角色 user message（含记忆与长期判断召回前缀）。"""
        name = character.name
        queries = build_retrieval_queries(name, user_input, raw_messages)

        # 两条 query 相同则只 embed 一次，复用同一个向量
        same_query = queries.understanding == queries.episode
        texts = [queries.episode] if same_query else [queries.episode, queries.understanding]
        try:
            vecs = embed_sync(texts)
            memory_qvec = vecs[0]
            understanding_qvec = vecs[0] if same_query else vecs[1]
        except Exception:
            memory_qvec = understanding_qvec = None

        relevant_memories = search_memories(
            name,
            queries.episode,
            qvec=memory_qvec,
            bm25_query=queries.episode_bm25,
        )
        relevant_understandings = search_understandings(
            name,
            queries.understanding,
            qvec=understanding_qvec,
            bm25_query=queries.understanding_bm25,
        )
        memories_block = (
            f"<relevant_memories>\n{relevant_memories}\n</relevant_memories>"
            if relevant_memories
            else ""
        )
        understandings_block = (
            f"<relevant_understandings>\n{relevant_understandings}\n</relevant_understandings>"
            if relevant_understandings
            else ""
        )
        message, _ = build_user_message(
            name,
            user_input,
            memories_block,
            understandings_prefix=understandings_block,
            raw_messages=raw_messages,
            observation_mode=observation_mode,
        )
        return message

    def _apply_updates(self, name: str, output: LLMCharacterOutput) -> None:
        """把 LLMCharacterOutput 的所有字段经 repo 落盘，并一次性记录结构化日志。"""
        results: list[FileUpdateResult] = []

        if output.memory:
            r = self._repo.append_memory(name, output.memory)
            if r is not None:
                results.append(r)

        results.extend(self._repo.apply_status_fields(name, output.status))

        for event_name in output.triggered:
            r = self._repo.mark_triggered(name, event_name)
            if r is not None:
                results.append(r)

        for event_desc in output.add_event:
            r = self._repo.add_event(name, event_desc)
            if r is not None:
                results.append(r)

        log_file_updates(name, results)


conversation_service = ConversationService(character_repo)
