"""统一创建并缓存所有 Agent。"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings

from engine.agent_schema import (
    CharacterOutput,
    ChoicesOutput,
    GrowthDedupOutput,
    GrowthExtractOutput,
    MemoryMergeOutput,
    MemoryMetadataOutput,
    NarratorOutput,
    NewCharacterCreation,
    OffstageMemoryBlock,
    StateUpdaterOutput,
)
from engine.prompt_builder import build_system_prompt
from llm.providers import (
    get_character_factory_llm_config,
    get_choices_llm_config,
    get_consolidation_llm_config,
    get_llm_config,
    get_narrator_llm_config,
    get_offstage_synthesizer_llm_config,
)
from shared.config import (
    CONSOLIDATION_MAX_TOKENS,
    CONSOLIDATION_PLAYER_PROFILE_MAX_TOKENS,
    CONSOLIDATION_TEMPERATURE,
    PROJECT_ROOT,
    get_agent_names,
)
from storage.agent_files import load_text, read_agent_file

_MEMORY_MERGE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "memory_scene_merge.txt"
_MEMORY_METADATA_PROMPT_PATH = PROJECT_ROOT / "prompts" / "memory_chunk_metadata.txt"
_GROWTH_EXTRACT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "growth_extract.txt"
_GROWTH_DEDUP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "growth_dedupe.txt"
_PLAYER_PROFILE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "player_profile_consolidation_prompt.txt"
_CHOICES_PROMPT_PATH = PROJECT_ROOT / "prompts" / "choices_prompt.txt"
_STATE_UPDATER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "state_updater_prompt.txt"
_CHARACTER_FACTORY_PROMPT_PATH = PROJECT_ROOT / "prompts" / "character_factory_prompt.txt"
_OFFSTAGE_SYNTH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "offstage_synth_prompt.txt"

ConversationAgent = Agent[None, CharacterOutput | NarratorOutput]
StructuredAgent = Agent[None, object]
TextAgent = Agent[None, str]

_conversation_agents: dict[str, ConversationAgent] = {}
_choices_agent: Agent[None, ChoicesOutput] | None = None
_state_updater_agent: Agent[None, StateUpdaterOutput] | None = None
_character_factory_agent: Agent[None, NewCharacterCreation] | None = None
_offstage_synth_agent: Agent[None, OffstageMemoryBlock] | None = None
_consolidation_agents: dict[str, StructuredAgent | TextAgent] = {}
_OPENROUTER_SAFE_DEFAULT_MAX_TOKENS = 4096


def _make_sdk_model(config: dict) -> OpenAIChatModel:
    provider_name = config.get("provider", "openai")
    provider_class = infer_provider_class(provider_name)
    provider = provider_class(
        openai_client=AsyncOpenAI(
            base_url=config["api_url"],
            api_key=config["api_key"],
        )
    )
    return OpenAIChatModel(
        config["model"],
        provider=provider,
    )


def _build_model_settings(
    config: dict,
    *,
    max_tokens: int | None = None,
) -> ModelSettings:
    settings: ModelSettings = {"temperature": config["temperature"]}
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    return settings


def _build_agent(
    *,
    name: str,
    instructions: str,
    config: dict,
    output_type: type | None = None,
    max_tokens: int | None = None,
) -> Agent[None, object] | Agent[None, str]:
    agent_output = PromptedOutput(output_type) if output_type is not None else str
    return Agent(
        _make_sdk_model(config),
        name=name,
        instructions=instructions,
        model_settings=_build_model_settings(config, max_tokens=max_tokens),
        output_type=agent_output,
    )


def _resolve_openrouter_safe_max_tokens(
    config: dict,
    requested_max_tokens: int | None,
) -> int | None:
    """避免 OpenRouter 在未显式限制时默认申请超大输出。"""
    if requested_max_tokens is not None:
        return requested_max_tokens
    if config.get("provider") == "openrouter":
        return _OPENROUTER_SAFE_DEFAULT_MAX_TOKENS
    return None


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


def get_conversation_agent(name: str) -> ConversationAgent:
    if name not in _conversation_agents:
        reload_conversation_agent(name)
    return _conversation_agents[name]


def get_choices_agent() -> Agent[None, ChoicesOutput]:
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


def get_state_updater_agent() -> Agent[None, StateUpdaterOutput]:
    global _state_updater_agent

    if _state_updater_agent is None:
        config = get_narrator_llm_config()
        instructions = _STATE_UPDATER_PROMPT_PATH.read_text(encoding="utf-8")
        _state_updater_agent = _build_agent(
            name="state_updater",
            instructions=instructions,
            config=config,
            output_type=StateUpdaterOutput,
        )
    return _state_updater_agent


def get_character_factory_agent() -> Agent[None, NewCharacterCreation]:
    global _character_factory_agent

    if _character_factory_agent is None:
        config = get_character_factory_llm_config()
        instructions = _CHARACTER_FACTORY_PROMPT_PATH.read_text(encoding="utf-8")
        _character_factory_agent = _build_agent(
            name="character_factory",
            instructions=instructions,
            config=config,
            output_type=NewCharacterCreation,
        )
    return _character_factory_agent


def get_offstage_synthesizer_agent() -> Agent[None, OffstageMemoryBlock]:
    global _offstage_synth_agent

    if _offstage_synth_agent is None:
        config = get_offstage_synthesizer_llm_config()
        instructions = _OFFSTAGE_SYNTH_PROMPT_PATH.read_text(encoding="utf-8")
        _offstage_synth_agent = _build_agent(
            name="offstage_synthesizer",
            instructions=instructions,
            config=config,
            output_type=OffstageMemoryBlock,
        )
    return _offstage_synth_agent


def _ensure_consolidation_agents() -> None:
    if _consolidation_agents:
        return

    config = get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE)
    consolidation_max_tokens = _resolve_openrouter_safe_max_tokens(
        config,
        CONSOLIDATION_MAX_TOKENS,
    )
    player_profile_max_tokens = _resolve_openrouter_safe_max_tokens(
        config,
        CONSOLIDATION_PLAYER_PROFILE_MAX_TOKENS,
    )
    _consolidation_agents["memory_merge"] = _build_agent(
        name="memory_merge",
        instructions=load_text(_MEMORY_MERGE_PROMPT_PATH),
        config=config,
        output_type=MemoryMergeOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["memory_metadata"] = _build_agent(
        name="memory_metadata",
        instructions=load_text(_MEMORY_METADATA_PROMPT_PATH),
        config=config,
        output_type=MemoryMetadataOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["growth_extract"] = _build_agent(
        name="growth_extract",
        instructions=load_text(_GROWTH_EXTRACT_PROMPT_PATH),
        config=config,
        output_type=GrowthExtractOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["growth_dedup"] = _build_agent(
        name="growth_dedup",
        instructions=load_text(_GROWTH_DEDUP_PROMPT_PATH),
        config=config,
        output_type=GrowthDedupOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["player_profile"] = _build_agent(
        name="player_profile",
        instructions=load_text(_PLAYER_PROFILE_PROMPT_PATH),
        config=config,
        max_tokens=player_profile_max_tokens,
    )


def _get_consolidation_agent(key: str) -> StructuredAgent | TextAgent:
    _ensure_consolidation_agents()
    return _consolidation_agents[key]


def get_memory_merge_agent() -> Agent[None, MemoryMergeOutput]:
    return _get_consolidation_agent("memory_merge")


def get_memory_metadata_agent() -> Agent[None, MemoryMetadataOutput]:
    return _get_consolidation_agent("memory_metadata")


def get_growth_extract_agent() -> Agent[None, GrowthExtractOutput]:
    return _get_consolidation_agent("growth_extract")


def get_growth_dedup_agent() -> Agent[None, GrowthDedupOutput]:
    return _get_consolidation_agent("growth_dedup")


def get_player_profile_agent() -> TextAgent:
    return _get_consolidation_agent("player_profile")
