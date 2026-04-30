Multi-Agent Roleplay / Narrative Game Project. The current implementation is built around **FastAPI + pydantic-ai + Pydantic structured output + file-based memory + sqlite-vec**, using `uv` as the project manager.

## Core Design

- **Independent Memory**: Characters maintain their own `memory.jsonl / status.md / user.md`; the `narrator` maintains `status.md` and raw history
- **Information Asymmetry**: Messages are scoped via `visible_to`; characters not present in a scene cannot see that round's content
- **Narrator-First**: The `narrator` handles routing and scene progression before sequentially invoking target characters
- **Structured Output**: All structured agents use `PromptedOutput`, not XML; the system reads typed fields directly and writes them back to files
- **Dual-Layer Memory**: Markdown files are human-readable and editable; the vector store handles retrieval

## Tech Stack

- Python 3.11+
- FastAPI + SSE (Server-Sent Events)
- pydantic-ai (`pydantic-ai`) — `Agent` / `PromptedOutput` / `OpenAIChatModel` / provider-specific `Provider`
- sqlite-vec + aiosqlite
- asyncio

## Current Project Structure

```text
agentgal-memos/
├── server.py                   # FastAPI entry point (UI adapter layer)
├── config.toml                 # Non-secret runtime parameters
├── data/
│   ├── characters/             # Runtime character data
│   ├── templates/              # Story templates (school / modern)
│   └── vectors.sqlite          # Vector store
├── engine/                     # Dialogue runtime orchestration
│   ├── character.py            # Character / Narrator runtime wrapper and typed output writeback
│   ├── character_factory.py    # New character incubation
│   ├── conversation_flow.py    # Single-round dialogue orchestration and UI adapter functions
│   └── prompt_builder.py       # Dialogue prompt / history window / schedule snapshot construction
├── agents/                     # SDK infrastructure (technical support layer)
│   ├── factory.py              # Agent creation, registry, and SDK model configuration
│   ├── runner.py               # SDK Runner invocation, Logfire trace, and typed parse
│   └── schema.py               # Pydantic structured output types
├── world/                      # World model (time / location)
│   └── schedule.py             # Character schedule queries, game time parsing, time-slot matching
├── consolidation/              # Background memory consolidation (independent process)
│   ├── flow.py                 # Consolidation orchestration: EpisodeMemoryGenerator / growth / user refinement
│   └── inputs.py               # Consolidation prompt assembly (memory_owner / raw_dialogue)
├── llm/
│   ├── providers.py            # Provider configuration and URL parsing (returns provider/api_url/api_key/model/temperature)
│   ├── embedding.py            # Embeddings client (embed_async / embed_sync)
│   └── rerank.py               # Rerank API client
├── log_config/                 # Logfire and business logger configuration
├── memory/                     # Memory rules and flows
│   ├── indexer.py              # Vector index rebuild entry point (reads from memory.jsonl, writes to storage)
│   ├── parser.py               # memory.jsonl structured record read/write, EpisodeMemory definition, date utilities
│   └── retrieval.py            # Full retrieval pipeline (fusion, rerank, recency, recall state update)
├── shared/                     # Pure configuration and side-effect-free utility functions
│   ├── config.py               # Paths, runtime parameters, character_path, get_agent_names
│   └── text_utils.py           # Text cleanup, get_display_name
├── storage/                    # Persistence infrastructure (files / JSONL / sqlite-vec / saves)
│   ├── agent_files.py          # Character directory file operations (read/write soul/memory/status/user/growth/sidecar)
│   ├── history.py              # Narrator raw JSONL dialogue history read
│   ├── message_router.py       # Dialogue write / visibility filtering
│   ├── save_manager.py         # Save / load / reset / opening load
│   └── vector_store.py         # sqlite-vec vector storage (write/delete + raw candidate retrieval)
├── prompts/                    # Prompt constant modules grouped by lifecycle
│   ├── consolidation_prompts.py  # Background consolidation: EpisodeClosureDetector / EpisodeMemoryGenerator / growth / user
│   ├── runtime_prompts.py        # Main dialogue: character / narrator / choices / state_updater
│   ├── worldgen_prompts.py       # Character incubation
│   └── opening_intro.txt         # Gameplay intro text (player-facing)
├── scripts/                    # Maintenance scripts
├── static/                     # Alpine.js + HTML/JS frontend
├── tests/                      # pytest tests
├── README.md
├── AGENTS.md
├── CLAUDE.md
└── .env
```

### Layered Dependency Direction

```
shared/          ← no internal dependencies
storage/         ← shared/
llm/             ← shared/
agents/          ← shared/                            # SDK base layer
memory/          ← shared/ + storage/ + llm/
world/           ← shared/ + storage/ + agents/
consolidation/   ← shared/ + storage/ + agents/ + memory/ + llm/
engine/          ← shared/ + storage/ + agents/ + memory/ + world/ + consolidation/
server.py        ← all
```

## Runtime File Responsibilities

### Character Files

- `soul.md`: Handwritten character definition, read-only; divided into five sections: `<identity>` / `<goal>` / `<dynamic>` / `<behavior>` / `<voice>`. The `<goal>` section describes the character's concrete long-term objectives for the story period (externally verifiable milestones + optional relationship vision), largely unchanged throughout the story period
- `memory.jsonl`: Character long-term memory, one structured `EpisodeMemory` per line (`id / date / time / location / participants / keywords / importance / content / memory_owner / title / raw_dialogue`), append-only, characters only; `id` is a stable UUID, and old data can be backfilled with `scripts/backfill_episode_ids.py`
- `memory_draft.jsonl`: On-disk buffer for each round's `output.memory` (characters only), each line is `{"turn": int, "text": str}`; after consolidation's `EpisodeClosureDetector` determines closure, slices are read by `until_turn` to produce structured `EpisodeMemory` appended to `memory.jsonl`. Merged entries are removed from draft; unclosed turn entries are retained
- `understanding.jsonl`: Stable understandings formed by the character, one structured `Understanding` per line (`id / memory_owner / subject / keywords / content / linked_episodes`); unlike `EpisodeMemory`, this records durable beliefs or interaction patterns rather than single events
- `status.md`: Current status; characters contain "Intentions" and "Relationship with Player", narrator contains "Pending Events", "Character Locations", and the derived field "Relationship with Player" (summarized from each character's status as `- Character Display Name: Relationship`, maintained by code, narrator does not generate it)
- `user.md`: Character's perception of the player (characters only, not `narrator`)
- `tmp_user.md`: Working draft of `user.md`; copies the official file on first write, deleted after consolidation
- `growth.md`: Behavioral drift records, maintained by the consolidator and injected into character prompts (characters only); each entry has a dimension tag (`Toward X: A→B` behavioral drift axis), format `[P001|Toward X: A→B] [Date] ...`, max 15 entries
- `relations.md`: Character's current perspective on other characters (excluding `player`); one section per `## {character}`; each round `output.relations[character]` overwrites the entire section (characters only)

### History Files

- Current dialogue history is **only written** to `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- Each message carries `visible_to`
- When characters read context, they filter messages by visibility

### Other Runtime Files

- `data/characters/last_choices.json`: Latest set of player options, restored on load, cleared on reset
- `data/characters/.turn_counter.json`: Global turn counter, incremented by 1 for each player message; raw JSONL and `memory_draft.jsonl` entries carry turn numbers for `EpisodeClosureDetector` closure detection; reset clears with characters directory, new game starts at 0
- `data/characters/narrator/tasks.md`: Optional story seed file; current main flow primarily syncs "Pending Events" from character "Intentions" via `state_updater`
- `data/characters/*/.history_window_state.json`: Per-agent dialogue history high/low water mark window sidecar
- `data/characters/*/.consolidation_state.json`: Character memory consolidation progress sidecar
- `data/characters/*/.memory_recall_state.json`: Character long-term memory recall snapshot (only generated from DB on save; not maintained at runtime)

## Message Routing

The `narrator` decides who participates in the current round.

```text
User Input → narrator → targets: ["existing character name", ...] (NarratorOutput.targets; may be empty if only introducing new characters this round)
```

### Narrator Responsibilities

- Analyze player input, output `targets` of currently existing characters who can respond this round; may be empty if only introducing new characters, the orchestration layer will add them after incubation
- Determine whether the player still wishes to interact with characters: if so, continue the current scene; if parting, skipping time, or no longer interacting, steer toward pending events or create equivalent immediate tension
- Every round must ensure at least one major character can perceive and respond to the player
- Describe time, location, present characters, environment, pure NPC behavior, and current hooks
- Do not add future events; future events are maintained by `state_updater` from character "Intentions"
- When the plot requires introducing a new character with relationship anchors, list `NewCharacterRequest` anchors via `NarratorOutput.new_characters` (`name_hint` is just an optional name hint); `engine/character_factory.py` generates `character_id` and incubates the directory; the orchestration layer automatically adds successfully incubated new characters to this round's response list. Pure passersby are not generated, described directly in content
- **Never speak for characters or decide their actions**

## Single-Round Dialogue Flow

```text
User Message
  ↓
Invoke narrator, get NarratorOutput (targets + content + new_characters)
  ↓
Incubate new_characters: `character_factory` generates `character_id`, writes soul/status/relations/memory/growth/user + `schedule.json` (skipped if LLM does not produce); successfully incubated new characters enter this round's final response list
  ↓
Write narrator content to single raw history (with visible_to)
  ↓
Sequentially invoke each target Agent (each agent response is written to history before the next can see it)
  ↓
After each Agent response: write back from CharacterOutput typed fields, broadcast to history
  ↓
Invoke choice generation (using narrator model), display 2-3 optional actions
  ↓
Persist latest options to last_choices.json (for load restoration)
  ↓
Background concurrently launch two tasks (both `asyncio.create_task`, non-blocking):
  1. state_updater → update narrator/status.md (scene, time, character locations, narrative focus, pending events; "Relationship with Player" is derived and synced from each character's status by code)
  2. detect_and_consolidate(current_turn) → determine episode closure and merge memories (see "Memory Consolidation")
  ↓
state_updater inputs in order: `schedule_snapshot` (renders each character's schedule default location by current game_time, missing schedule marked "(no schedule)"), character_intention, current_narrator_status, recent_history
  ↓
state_updater outputs full "Character Locations" snapshot each round; priority: recent_history facts > character_intention with location > old snapshot > schedule_snapshot defaults
  ↓
state_updater syncs public "Pending Events" from each character's "Intentions" (event names preserve character names)
```

## Agent Output and Writeback Mechanism

All structured agents use pydantic-ai's `PromptedOutput` structured output, no longer using XML `<update_notes>`:

- `CharacterOutput`: `content`, `memory`, `status`, `player`, `triggered`, `add_event`, `relations`
- `NarratorOutput`: `content`, `targets`, `new_characters` (routing, scene description, and dynamic character requests)
- `NewCharacterRequest` / `NewCharacterProfile`: New character incubation anchor (optional `name_hint`, no `character_id`) and character_factory's complete output (includes `character_id`, final `display_name`, `initial_status`, `initial_relations`)
- `EpisodeMemoryBlock`: Single long-term memory event output by `EpisodeMemoryGenerator` (`date / time / location / participants / keywords / importance / content / title`), completed with a stable `id` by the write path and injected with `memory_owner` and `raw_dialogue` (original dialogue trace, metadata only, not vector-indexed, not in recall text), then appended to character `memory.jsonl`
- `EpisodeClosureDetector` output type: `dict[str, list[EpisodeClosureBoundary]]` (key is the character's `agent_name` appearing in recent_history; value is all theme boundaries detected for that character in history, sorted by `end_turn` ascending, empty array means no boundaries. Each boundary contains `end_turn / old_theme / new_theme / reason`. Consumer only adopts local candidate characters, and takes the maximum `end_turn` from each array as this round's mergeable closure point)
- `StateUpdaterOutput`: `status`, `triggered`, `add_event` (post-round background narrator state maintenance)
- `ChoicesOutput`: `choices`

`engine/character.py`'s `Character` / `Narrator` both inherit from `BaseEntity`, encapsulating soul / status read/write and SDK invocation; writes go through entity methods (`set_status_fields` / `append_memory` / `add_event` / `mark_triggered` / `set_relation` / `set_user_profile_fields`), no longer allowing external direct calls to underlying `update_xxx`. `Narrator.route()` handles routing and scene description, `Narrator.update_state()` invokes `state_updater` at round end.

### Writeback Rules

- `output.memory` → append one record tagged with current global turn number to `memory_draft.jsonl` (after `EpisodeClosureDetector` determines closure turn, consolidation slices by `until_turn` to produce `EpisodeMemory` appended to `memory.jsonl`, merged entries removed from draft)
- `output.status` → overwrite corresponding fields in `status.md`
- `output.player` → append to corresponding fields in `tmp_user.md`; on first write, copy `user.md` as working draft first, then write back to `user.md` after consolidation
- `output.triggered` → remove executed entries from `status.md`
- `output.add_event` → insert new entries into `status.md`
- `output.relations` → overwrite the `## {target}` section in `relations.md` (target must be another character's display name, not self or `player`; long-term perspective on player goes through `user.md` and `status.md`'s "Relationship with Player", not written to relations; invalid targets are skipped with a warning logged)

Where:

- `narrator` operates on section: `Pending Events`
- Other characters operate on section: `Intentions`
- `Intentions` / `Pending Events` cannot be overwritten in bulk via `<status>`, only maintained item-by-item via `<triggered>` / `<add_event>`

## Prompt Composition

### Design Principles

- Keep system prompts stable, put dynamic content in user messages to improve prompt cache hit rate
- Do not arbitrarily adjust context block order; current order is specifically tuned for cache and retrieval hit rates

### Character Agent

`system` message contains:

1. `soul.md`
2. `prompts.runtime_prompts.CHARACTER`
3. Allowlisted fields for writeback

`user` message is assembled into a **single large message** in the following order:

1. `<my_schedule>` (renders character's `schedule.json`; most stable throughout the story period, placed first to anchor prompt cache)
2. `growth.md`
3. `user.md` (`tmp_user.md` only participates in consolidation as working draft, not directly injected into prompt)
4. Recent visible dialogue history (built from raw JSONL; filtered by `visible_to`; high/low water mark truncation; history contains all narrator messages)
5. `status.md`
6. `<relations>` (directly injects the character's own `relations.md`, covering all known major characters, regardless of presence. Player perspective does not go through relations)
7. `<relevant_memories>` (long-term memory recall from `memory.jsonl`, vector store side still renders as markdown for LLM reading)
8. Current round player input

### Narrator Agent

`system` message contains:

1. `soul.md`
2. `prompts.runtime_prompts.NARRATOR`

`user` message is assembled into a **single large message** in the following order:

1. Recent dialogue history (narrator keeps only the last entry)
2. `status.md`
3. Current round player input

`narrator` does not use vector recall; it relies on scene, narrative focus, pending events, and "Relationship with Player" in `status.md` to advance the current round. Pending events are primarily synced by `state_updater` from character "Intentions", event names preserve character names (e.g. `【Mitsuki: Promise to Walk Together】`). "Relationship with Player" is summarized from each character's status by code, format `- Mitsuki: Lover`.

> Note: `<world_now>` (derived projection of current time / real-time character locations) is currently disabled, to be restored after schedule mechanism is refined. During this period narrator only reads fields maintained by author/state_updater in `status.md`.

Narrator supports independent LLM configuration (`NARRATOR_LLM_*` env vars), falling back to main LLM when not set.

### Choice Generation

After each round of character responses, `generate_choices()` is called to generate 2-3 player-selectable actions:

- Prompt source: `prompts.runtime_prompts.CHOICES`
- Uses narrator's LLM configuration
- Output style is player dialogue (may include parenthetical action descriptions), not action instructions
- Options are displayed as both text and buttons, persisted to `last_choices.json`

## Long-Term Memory Retrieval

- Vector store only indexes long-term memory events from `memory.jsonl`, owner scope is fixed to current character
- Default retrieval path is memory-only; non-memory retrieval is disabled
- `memory/retrieval.py` handles the full retrieval pipeline: embedding → vector/BM25 candidates → hybrid fusion → (optional) rerank → recency sort → recall state update
- `storage/vector_store.py` is storage layer only: provides `get_vector_candidates` / `get_bm25_candidates` raw candidates, pipeline logic is not here
- `memory/indexer.py` reads `EpisodeMemory` records from `memory.jsonl` and appends them to the vector store
- Recall ranking: vector relevance and BM25 relevance are fused first, rerank (optional) replaces relevance signal, then in-game time recency is layered on top
- When Logfire is configured, memory retrieval logs each round's query and top hit summary for debugging recall quality
- `last_recalled_at` is updated to DB on hit; `.memory_recall_state.json` is only exported from DB on save, used as fallback data source on load rebuild
- `memory/indexer.rebuild_memory_index()` combines `.consolidation_state.json` to restore long-term memory index; recall state is read from DB first, falls back to `.memory_recall_state.json` when DB is empty

## Memory Consolidation

`consolidation/flow.py` handles character background consolidation:

- Triggered as a background task by `detect_and_consolidate(current_turn)` at the end of each round (concurrent with `state_updater`): first scans characters with `memory_draft.jsonl` as candidates, calls `EpisodeClosureDetector` to determine which characters have closed episodes this round (returns `{agent_name: closed_at_turn}`); closed characters execute `consolidate_agent(name, until_turn=closed_at_turn)` in parallel
- `consolidate_agent` slices draft entries + raw dialogue for the corresponding turn range from `memory_draft.jsonl` by `until_turn`, hands to `EpisodeMemoryGenerator` to produce a single structured `EpisodeMemory`; flow layer injects `memory_owner` and `raw_dialogue`, then appends to `memory.jsonl`. Merged entries are removed from draft; unclosed turn entries are retained. On failure, entire draft is retained for retry next round
- Growth stage uses the consolidated `EpisodeMemory` JSON array as LLM input, no longer pre-rendering to markdown
- Patches `growth.md` (add/update/remove, constrained 1:1 by dimension; update can update dimension along the same object/relationship axis of gradual change) (characters only)
- Also refines `user.md` (characters only)
- Syncs vector index by progress

`narrator` does not maintain `memory.jsonl`, nor does it participate in consolidation.

## Configuration Sources

### `.env`

- Holds secrets, model IDs, providers, and external service URLs
- Rerank calls are only truly enabled when `RERANK_MODEL` is configured
- narrator / choices / consolidation / character_factory / episode_closure_detector all support their own independent LLM configurations, falling back level by level when not set (`CHARACTER_FACTORY_LLM_*` falls back to narrator; `EPISODE_CLOSURE_DETECTOR_LLM_*` falls back to main LLM)

### `config.toml`

- Holds runtime strategy parameters, such as Agent temperature, character/consolidation/choice generation timeouts, embedding/rerank request timeouts, vector retrieval weights
- `[history]`'s `history_high` / `history_low` control multi-round dialogue high/low water mark truncation (in distinct turn counts, anchored by turn number, stored in `.history_window_state.json`'s `start_turn` field); `raw_scan_turns` limits how many turns narrator routing / character running / choice generation reads from raw history (consolidation flow slices by turn, not subject to this limit)

## Save and Reset

Handled by `storage/save_manager.py`, exposed via FastAPI endpoints:

- `POST /api/save`: Export zip to `saves/`; `{}` creates new uuid slot, `{"filename": "...zip"}` overwrites specified slot
- `GET /api/saves`: List saves
- `POST /api/load`: Restore save and rebuild necessary indexes
- `POST /api/reset`: Reset runtime data from `data/templates/{story_id}`

Save includes:

- Character markdown / jsonl files (`narrator` does not include `memory.jsonl` / `memory_draft.jsonl`)
- Character `memory.jsonl` (structured long-term memory, one `EpisodeMemory` per line, includes stable `id` and `raw_dialogue` trace field)
- Character `memory_draft.jsonl` (when present; each line `{"turn": int, "text": str}`, ensuring unclosed merge memories are not lost on save)
- Character `understanding.jsonl` (when present; stable beliefs linked back to EpisodeMemory ids)
- Character `schedule.json` (when present)
- Narrator raw history (each entry carries turn number)
- Per-agent `.history_window_state.json`
- Character `.consolidation_state.json`
- Character `.memory_recall_state.json`
- `last_choices.json`
- `.turn_counter.json` (global turn counter)

Currently built-in story templates:

- `school`: `mitsuki` / `narrator`
- `modern`: `chenxiao` / `guyining` / `narrator`

## Development Conventions

### Code Design

- Keep DRY, but don't abstract for abstraction's sake
- Prioritize simple, explicit, good-enough-for-now implementations
- One function does one thing, keep complexity controlled
- Complete type annotations (Python 3.11+)

### Error Handling

- LLM / embedding / database calls must retain contextual logs
- Check paths and existence before file operations
- Prohibit bare `except:`, catch specific exceptions

### Concurrency and Async

- All I/O operations use `async/await`
- Multi-character invocations use `asyncio.gather()` for parallel execution
- Consider concurrency protection for shared resources (files, vector store, consolidation tasks)

### Readability

- Variable and function names should be self-explanatory
- Comments explain "why", not restate surface meaning of code
- Sync documentation and prompts when structure changes

## Logging and Observability

- When Logfire is configured (local CLI or `LOGFIRE_TOKEN`), reports PydanticAI traces, token/cost, routing events, memory retrieval and consolidation events; silently skipped when not configured
- Routing and memory modules still use standard logger as business event entry points, but no longer write to local `logs/*.log` rotation files by default

## Testing Conventions

- Keep pure logic as unit-testable functions
- Use `pytest`
- Current tests include:
  - Dialogue history related tests
  - Formatting tests
  - Save consistency tests
  - Vector store tests
- Tests involving vector retrieval/embedding may depend on embedding configuration in `.env`
