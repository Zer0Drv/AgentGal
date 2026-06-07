"""统一创建并缓存所有 Agent。"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agents.llm_schema import (
    LLMCharacterOutput,
    LLMChoices,
    LLMEpisodeClosure,
    LLMEpisodeMemory,
    LLMNarratorOutput,
    LLMNewCharacterProfile,
    LLMStateUpdate,
    LLMUnderstandingPatch,
)
from engine.prompt_builder import build_system_prompt
from llm.config import get_llm_config
from prompts.consolidation_prompts import (
    EPISODE_CLOSURE_DETECTOR,
    EPISODE_MEMORY_GENERATOR,
    UNDERSTANDING_PATCH,
)
from prompts.runtime_prompts import CHOICES, NARRATOR_OBSERVATION, STATE_UPDATER
from prompts.worldgen_prompts import CHARACTER_FACTORY
from shared.config import (
    CONSOLIDATION_MAX_TOKENS,
    CONSOLIDATION_TEMPERATURE,
    STATE_UPDATER_OUTPUT_RETRIES,
    STATE_UPDATER_TEMPERATURE,
    get_agent_names,
)
from storage.agent_files import read_agent_file

ConversationAgent = Agent[None, LLMCharacterOutput | LLMNarratorOutput]
StructuredAgent = Agent[None, object]

_conversation_agents: dict[str, ConversationAgent] = {}
_observation_narrator_agent: ConversationAgent | None = None
_choices_agent: Agent[None, LLMChoices] | None = None
_state_updater_agent: Agent[None, LLMStateUpdate] | None = None
_character_factory_agent: Agent[None, LLMNewCharacterProfile] | None = None
_consolidation_agents: dict[str, StructuredAgent] = {}


_GOOGLE_SAFETY_OFF = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
]

_MakeModel = Callable[[dict], Model]
_MakeSettings = Callable[[dict], ModelSettings]

_default_make_settings: _MakeSettings = lambda c: ModelSettings(temperature=c["temperature"])

# 新增 provider：加一行 tuple 即可
_PROVIDER_REGISTRY: dict[str, tuple[_MakeModel, _MakeSettings]] = {
    "openai": (
        lambda c: OpenAIChatModel(
            c["model_id"],
            provider=OpenAIProvider(base_url=c["api_url"] or None, api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
    "google": (
        lambda c: GoogleModel(
            c["model_id"],
            provider=GoogleProvider(api_key=c["api_key"]),
        ),
        lambda c: GoogleModelSettings(
            temperature=c["temperature"],
            google_safety_settings=_GOOGLE_SAFETY_OFF,
        ),
    ),
    "anthropic": (
        lambda c: AnthropicModel(
            c["model_id"],
            provider=AnthropicProvider(api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
    "deepseek": (
        lambda c: OpenAIChatModel(
            c["model_id"],
            provider=DeepSeekProvider(api_key=c["api_key"]),
        ),
        _default_make_settings,
    ),
}


def _make_sdk_model(config: dict) -> Model:
    entry = _PROVIDER_REGISTRY.get(config["provider"])
    if entry is None:
        raise ValueError(
            f"Unsupported LLM_PROVIDER: {config['provider']!r}. "
            f"Supported: {list(_PROVIDER_REGISTRY)}"
        )
    make_model, _ = entry
    return make_model(config)


def _build_model_settings(
    config: dict,
    *,
    max_tokens: int | None = None,
) -> ModelSettings:
    _, make_settings = _PROVIDER_REGISTRY[config["provider"]]
    settings = make_settings(config)
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    return settings


def _build_agent(
    *,
    name: str,
    instructions: str,
    config: dict,
    output_type: type,
    max_tokens: int | None = None,
    output_retries: int | None = None,
) -> StructuredAgent:
    return Agent(
        _make_sdk_model(config),
        name=name,
        instructions=instructions,
        model_settings=_build_model_settings(config, max_tokens=max_tokens),
        output_type=PromptedOutput(output_type),
        output_retries=output_retries,
    )


def initialize_conversation_agents() -> None:
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)


def reload_conversation_agent(name: str) -> None:
    global _choices_agent, _observation_narrator_agent

    soul = read_agent_file(name, "soul.md")
    config = get_llm_config()
    output_type = LLMNarratorOutput if name == "narrator" else LLMCharacterOutput
    _conversation_agents[name] = _build_agent(
        name=name,
        instructions=build_system_prompt(name, soul),
        config=config,
        output_type=output_type,
    )
    if name == "narrator":
        _choices_agent = None
        _observation_narrator_agent = None


def get_conversation_agent(name: str) -> ConversationAgent:
    if name not in _conversation_agents:
        reload_conversation_agent(name)
    return _conversation_agents[name]


def get_observation_narrator_agent() -> ConversationAgent:
    global _observation_narrator_agent
    if _observation_narrator_agent is None:
        soul = read_agent_file("narrator", "soul.md")
        config = get_llm_config()
        _observation_narrator_agent = _build_agent(
            name="narrator_observation",
            instructions=NARRATOR_OBSERVATION.format(soul=soul),
            config=config,
            output_type=LLMNarratorOutput,
        )
    return _observation_narrator_agent


def get_choices_agent() -> Agent[None, LLMChoices]:
    global _choices_agent

    if _choices_agent is None:
        config = get_llm_config()
        _choices_agent = _build_agent(
            name="choices",
            instructions=CHOICES,
            config=config,
            output_type=LLMChoices,
        )
    return _choices_agent


def get_state_updater_agent() -> Agent[None, LLMStateUpdate]:
    global _state_updater_agent

    if _state_updater_agent is None:
        config = get_llm_config(temperature=STATE_UPDATER_TEMPERATURE)
        _state_updater_agent = _build_agent(
            name="state_updater",
            instructions=STATE_UPDATER,
            config=config,
            output_type=LLMStateUpdate,
            output_retries=STATE_UPDATER_OUTPUT_RETRIES,
        )
    return _state_updater_agent


def get_character_factory_agent() -> Agent[None, LLMNewCharacterProfile]:
    global _character_factory_agent

    if _character_factory_agent is None:
        config = get_llm_config()
        _character_factory_agent = _build_agent(
            name="character_factory",
            instructions=CHARACTER_FACTORY,
            config=config,
            output_type=LLMNewCharacterProfile,
        )
    return _character_factory_agent


def _ensure_consolidation_agents() -> None:
    if _consolidation_agents:
        return

    config = get_llm_config(temperature=CONSOLIDATION_TEMPERATURE)
    _consolidation_agents["episode_memory_generator"] = _build_agent(
        name="episode_memory_generator",
        instructions=EPISODE_MEMORY_GENERATOR,
        config=config,
        output_type=LLMEpisodeMemory,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["episode_closure_detector"] = _build_agent(
        name="episode_closure_detector",
        instructions=EPISODE_CLOSURE_DETECTOR,
        config=config,
        output_type=LLMEpisodeClosure,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )
    _consolidation_agents["understanding_patch"] = _build_agent(
        name="understanding_patch",
        instructions=UNDERSTANDING_PATCH,
        config=config,
        output_type=LLMUnderstandingPatch,
        max_tokens=CONSOLIDATION_MAX_TOKENS,
    )


def _get_consolidation_agent(key: str) -> StructuredAgent:
    _ensure_consolidation_agents()
    return _consolidation_agents[key]


def get_episode_memory_generator_agent() -> Agent[None, LLMEpisodeMemory]:
    return _get_consolidation_agent("episode_memory_generator")


def get_episode_closure_detector_agent() -> Agent[None, LLMEpisodeClosure]:
    return _get_consolidation_agent("episode_closure_detector")


def get_understanding_patch_agent() -> Agent[None, LLMUnderstandingPatch]:
    return _get_consolidation_agent("understanding_patch")
