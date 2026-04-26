"""统一创建并缓存所有 Agent。"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings

from agents.schema import (
    CharacterOutput,
    ChoicesOutput,
    EpisodeMemoryBlock,
    GrowthDedupOutput,
    GrowthExtractOutput,
    NarratorOutput,
    NewCharacterProfile,
    StateUpdaterOutput,
)
from engine.prompt_builder import build_system_prompt
from llm.providers import (
    get_character_factory_llm_config,
    get_choices_llm_config,
    get_consolidation_llm_config,
    get_episode_closure_detector_llm_config,
    get_llm_config,
    get_narrator_llm_config,
)
from prompts.consolidation_prompts import (
    EPISODE_CLOSURE_DETECTOR,
    EPISODE_MEMORY_GENERATOR,
    GROWTH_DEDUPE,
    GROWTH_EXTRACT,
    PLAYER_PROFILE,
)
from prompts.runtime_prompts import CHOICES, STATE_UPDATER
from prompts.worldgen_prompts import CHARACTER_FACTORY
from shared.config import (
    CONSOLIDATION_MAX_TOKENS,
    CONSOLIDATION_PLAYER_PROFILE_MAX_TOKENS,
    CONSOLIDATION_TEMPERATURE,
    get_agent_names,
)
from storage.agent_files import read_agent_file

ConversationAgent = Agent[None, CharacterOutput | NarratorOutput]
StructuredAgent = Agent[None, object]
TextAgent = Agent[None, str]

_conversation_agents: dict[str, ConversationAgent] = {}
_choices_agent: Agent[None, ChoicesOutput] | None = None
_state_updater_agent: Agent[None, StateUpdaterOutput] | None = None
_character_factory_agent: Agent[None, NewCharacterProfile] | None = None
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
        _choices_agent = _build_agent(
            name="choices",
            instructions=CHOICES,
            config=config,
            output_type=ChoicesOutput,
        )
    return _choices_agent


def get_state_updater_agent() -> Agent[None, StateUpdaterOutput]:
    global _state_updater_agent

    if _state_updater_agent is None:
        config = get_narrator_llm_config()
        _state_updater_agent = _build_agent(
            name="state_updater",
            instructions=STATE_UPDATER,
            config=config,
            output_type=StateUpdaterOutput,
        )
    return _state_updater_agent


def get_character_factory_agent() -> Agent[None, NewCharacterProfile]:
    global _character_factory_agent

    if _character_factory_agent is None:
        config = get_character_factory_llm_config()
        _character_factory_agent = _build_agent(
            name="character_factory",
            instructions=CHARACTER_FACTORY,
            config=config,
            output_type=NewCharacterProfile,
        )
    return _character_factory_agent


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
    _consolidation_agents["episode_memory_generator"] = _build_agent(
        name="episode_memory_generator",
        instructions=EPISODE_MEMORY_GENERATOR,
        config=config,
        output_type=EpisodeMemoryBlock,
        max_tokens=consolidation_max_tokens,
    )
    closure_config = get_episode_closure_detector_llm_config(
        temperature=CONSOLIDATION_TEMPERATURE
    )
    closure_max_tokens = _resolve_openrouter_safe_max_tokens(
        closure_config,
        CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["episode_closure_detector"] = _build_agent(
        name="episode_closure_detector",
        instructions=EPISODE_CLOSURE_DETECTOR,
        config=closure_config,
        output_type=dict[str, int],
        max_tokens=closure_max_tokens,
    )
    _consolidation_agents["growth_extract"] = _build_agent(
        name="growth_extract",
        instructions=GROWTH_EXTRACT,
        config=config,
        output_type=GrowthExtractOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["growth_dedup"] = _build_agent(
        name="growth_dedup",
        instructions=GROWTH_DEDUPE,
        config=config,
        output_type=GrowthDedupOutput,
        max_tokens=consolidation_max_tokens,
    )
    _consolidation_agents["player_profile"] = _build_agent(
        name="player_profile",
        instructions=PLAYER_PROFILE,
        config=config,
        max_tokens=player_profile_max_tokens,
    )


def _get_consolidation_agent(key: str) -> StructuredAgent | TextAgent:
    _ensure_consolidation_agents()
    return _consolidation_agents[key]


def get_episode_memory_generator_agent() -> Agent[None, EpisodeMemoryBlock]:
    return _get_consolidation_agent("episode_memory_generator")


def get_episode_closure_detector_agent() -> Agent[None, dict[str, int]]:
    return _get_consolidation_agent("episode_closure_detector")


def get_growth_extract_agent() -> Agent[None, GrowthExtractOutput]:
    return _get_consolidation_agent("growth_extract")


def get_growth_dedup_agent() -> Agent[None, GrowthDedupOutput]:
    return _get_consolidation_agent("growth_dedup")


def get_player_profile_agent() -> TextAgent:
    return _get_consolidation_agent("player_profile")
