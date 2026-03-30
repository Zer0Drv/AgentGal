"""统一创建并缓存所有 Agent。"""

from agents import Agent, AgentOutputSchema, ModelSettings, OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from engine.agent_schema import (
    CharacterOutput,
    ChoicesOutput,
    GrowthDedupOutput,
    GrowthExtractOutput,
    MemoryMergeOutput,
    MemoryMetadataOutput,
    NarratorOutput,
)
from engine.prompt_builder import build_system_prompt
from llm.providers import (
    get_choices_llm_config,
    get_consolidation_llm_config,
    get_llm_config,
    get_narrator_llm_config,
)
from shared.config import CONSOLIDATION_MAX_TOKENS, CONSOLIDATION_TEMPERATURE, PROJECT_ROOT, get_agent_names
from storage.agent_files import load_text, read_agent_file

_MEMORY_MERGE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "memory_scene_merge.txt"
_MEMORY_METADATA_PROMPT_PATH = PROJECT_ROOT / "prompts" / "memory_chunk_metadata.txt"
_GROWTH_EXTRACT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "growth_extract.txt"
_GROWTH_DEDUP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "growth_dedupe.txt"
_PLAYER_PROFILE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "player_profile_consolidation_prompt.txt"
_CHOICES_PROMPT_PATH = PROJECT_ROOT / "prompts" / "choices_prompt.txt"

_conversation_agents: dict[str, Agent] = {}
_choices_agent: Agent | None = None
_consolidation_agents: dict[str, Agent] = {}


def _make_sdk_model(config: dict) -> OpenAIChatCompletionsModel:
    oai_client = AsyncOpenAI(base_url=config["api_url"], api_key=config["api_key"])
    return OpenAIChatCompletionsModel(
        model=config["model"],
        openai_client=oai_client,
    )


def _build_agent(
    *,
    name: str,
    instructions: str,
    config: dict,
    output_type: type | None = None,
    max_tokens: int | None = None,
) -> Agent:
    settings_kwargs: dict = {"temperature": config["temperature"]}
    # extra_body 覆盖 agents 自动生成的 json_schema，因为 deepseek 只支持 json_object
    if output_type is not None:
        settings_kwargs["extra_body"] = {"response_format": {"type": "json_object"}}
    if max_tokens is not None:
        settings_kwargs["max_tokens"] = max_tokens
    wrapped_output_type = AgentOutputSchema(output_type, strict_json_schema=False) if output_type is not None else None
    return Agent(
        name=name,
        instructions=instructions,
        model=_make_sdk_model(config),
        model_settings=ModelSettings(**settings_kwargs),
        output_type=wrapped_output_type,
    )


def initialize_conversation_agents() -> None:
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)


def reload_conversation_agent(name: str) -> None:
    global _choices_agent

    soul = read_agent_file(name, "soul.md")
    config = get_narrator_llm_config() if name == "narrator" else get_llm_config()
    output_type = NarratorOutput if name == "narrator" else CharacterOutput
    _conversation_agents[name] = _build_agent(
        name=name,
        instructions=build_system_prompt(name, soul),
        config=config,
        output_type=output_type,
    )
    if name == "narrator":
        _choices_agent = None


def get_conversation_agent(name: str) -> Agent:
    if name not in _conversation_agents:
        reload_conversation_agent(name)
    return _conversation_agents[name]


def get_choices_agent() -> Agent:
    global _choices_agent

    if _choices_agent is None:
        config = get_choices_llm_config()
        instructions = _CHOICES_PROMPT_PATH.read_text(encoding="utf-8")
        _choices_agent = _build_agent(
            name="choices",
            instructions=instructions,
            config=config,
            output_type=ChoicesOutput,
        )
    return _choices_agent


def _ensure_consolidation_agents() -> None:
    if _consolidation_agents:
        return

    config = get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE)
    _consolidation_agents["memory_merge"] = _build_agent(
        name="memory_merge",
        instructions=load_text(_MEMORY_MERGE_PROMPT_PATH),
        config=config,
        output_type=MemoryMergeOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["memory_metadata"] = _build_agent(
        name="memory_metadata",
        instructions=load_text(_MEMORY_METADATA_PROMPT_PATH),
        config=config,
        output_type=MemoryMetadataOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["growth_extract"] = _build_agent(
        name="growth_extract",
        instructions=load_text(_GROWTH_EXTRACT_PROMPT_PATH),
        config=config,
        output_type=GrowthExtractOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["growth_dedup"] = _build_agent(
        name="growth_dedup",
        instructions=load_text(_GROWTH_DEDUP_PROMPT_PATH),
        config=config,
        output_type=GrowthDedupOutput,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["player_profile"] = _build_agent(
        name="player_profile",
        instructions=load_text(_PLAYER_PROFILE_PROMPT_PATH),
        config=config,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )


def _get_consolidation_agent(key: str) -> Agent:
    _ensure_consolidation_agents()
    return _consolidation_agents[key]


def get_memory_merge_agent() -> Agent:
    return _get_consolidation_agent("memory_merge")


def get_memory_metadata_agent() -> Agent:
    return _get_consolidation_agent("memory_metadata")


def get_growth_extract_agent() -> Agent:
    return _get_consolidation_agent("growth_extract")


def get_growth_dedup_agent() -> Agent:
    return _get_consolidation_agent("growth_dedup")


def get_player_profile_agent() -> Agent:
    return _get_consolidation_agent("player_profile")
