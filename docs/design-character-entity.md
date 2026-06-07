# 设计草案：Character 实体 + Repository + Service（Phase 3/4）

> 状态：**已采纳并实施（2026-06-07，D1-D7 全部按推荐通过）**
> 创建：2026-06-07
> 目的：定清楚"Character 实体长什么样"，进而决定 Phase 3（Repository）/ Phase 4（Service）怎么落
> 落地：`models/character.py` · `models/status_fields.py` · `storage/character_repo.py` · `storage/narrator_repo.py` · `engine/conversation_service.py` · `engine/narrator_service.py`；`engine/character.py` 已删除。

## 0. 核心发现（先看这个——它把结论变轻了）

读完 `engine/character.py`(684) + `conversation_flow.py` + `server._chat_stream` 后，两个事实改变了设计走向：

**事实 1：当前"实体"几乎不持有状态。**
- `soul` = 缓存的**只读文本**（本质是 prompt 资源），整个存档生命周期不变
- `status` = 每次访问**实时读盘**（`@property status → read_agent_file`）
- memory = 在 `memory.jsonl` / 向量库，不在实体里

所以今天的 `Character` 实质只有 `name` + 一份 soul 缓存。它不是"贫血模型"，是"近乎无数据模型"。

**事实 2：status 没有任何消费方需要"结构化对象"。**
全项目 status 的用法只有两类（已逐一核对）：
- **整段原文**塞进 prompt：`<status>\n{status_content}\n</status>`（`prompt_builder`）
- **单字段取值**：`extract_status_field(status, "在意的事"/"场景"/"叙事焦点"/...)` —— 散落在 server.py、memory/retrieval、save_manager、memory_query_builder、character_factory、character.py

没有任何地方把 status 当成"有 N 个属性的对象"来用。

→ **结论：不引入 `CharacterStatus`（不是推迟，是砍掉）。** 它即便到 Phase 4 也没有消费者。
真正值得做的小改进是把散落的**字段名魔法字符串收拢成常量**（见 §3.2），而不是造一个没人用的类型。

## 1. 当前的三重混淆（要拆开的根因）

`BaseEntity / Character / Narrator` 一个类同时是：
1. **领域身份**：`name` / `soul` / `display_name`
2. **持久化（Active Record）**：`set_status_fields` / `append_memory` / `add_event` / `mark_triggered` / `_write_scene_to_status` / `sync_player_relations` —— 全是文件 I/O
3. **用例编排（Service）**：`Character.run()`（搜记忆→prompt→LLM→写回）、`Narrator.route()`、`Narrator.update_state()`

外加两个毛病：
- **全局可变单例** `_characters` / `narrator`，靠 `reset_entities()` 手动刷新生命周期，难测、隐藏状态
- **编排散落三处**：`server._chat_stream`（回合循环）、`conversation_flow`（薄壳）、实体方法（route/run/update_state）

**这次重构的真正收益不在"实体变丰富"（它本就薄），而在：杀全局单例 + 收拢散落编排 + 隔离 I/O。**

## 2. 目标三分映射

| 现在（`engine/character.py` 一肩挑） | 拆后归属 | 层 |
|---|---|---|
| `name` / `soul` / `display_name` | `models/Character`（冻结数据类，零 I/O） | **models/** |
| 字段名魔法字符串（"在意的事"/"场景"/...） | `models/` 常量（§3.2） | **models/** |
| `soul` property 读文件 + `_soul_cache` | `CharacterRepository.load()`（缓存在这） | **storage/** |
| `set_status_fields` / `append_memory` / `add_event` / `mark_triggered` 的**写回策略** | `CharacterRepository` 写回方法 | **storage/** |
| `_write_scene_to_status` / `_apply_state_updates` / `sync_player_relations` | `NarratorRepository` 写回方法 | **storage/** |
| `Character.run()` + `conversation_flow.run_agent_in_scene` | `ConversationService.run_turn()` | **engine/** |
| `Narrator.route()` / `_filter_new_characters` / `_sanitize_scene_description` | `NarratorService.route()` | **engine/** |
| `Narrator.update_state()` / `_build_state_updater_input` / `_format_characters_status` | `NarratorService.update_state()` | **engine/** |
| 全局 `_characters` / `narrator` 单例 + `reset_entities` | 删除；Service 经 Repository 取实体；soul 缓存失效改 Repository 提供 | — |

> 注意：`server._chat_stream` 的 **SSE 回合循环本身留在 server.py**（它与流式输出强耦合：逐角色 yield、choices/state/consolidation 并行调度）。Service 替换的是"实体方法"，不是把整个回合塞进一个 god-service。

## 3. 各层具体形状（草图）

### 3.1 `models/character.py` —— 极薄实体

```python
from dataclasses import dataclass
from shared.text_utils import get_display_name  # 纯函数，允许

@dataclass(frozen=True)
class Character:
    """游戏角色领域实体：只承载本存档生命周期内不变的数据（name + 只读 soul）。

    可变/共享状态（status.md、memory）不进实体——它们回合中被多方改写、且人工可编辑，
    持有快照会过期。这些经 CharacterRepository 按需读写。
    """
    name: str
    soul: str  # soul.md 全文，只读 prompt 资源

    @property
    def display_name(self) -> str:
        return get_display_name(self.name, self.soul)
```

- 用 `frozen dataclass` 而非 pydantic：无需校验/序列化（soul 是自由文本，永不写 JSON）。是否要 pydantic 见决策 D7。

### 3.2 `models/status_fields.py` —— 收拢字段名常量（替代 CharacterStatus）

```python
# 角色 status.md 的已知语义字段（作者仍可自定义其他 ## 字段，白名单仍由文件驱动）
IDENTITY = "身份"
MOOD = "心境"
CONCERN = "在意的事"
PLANS = "打算"            # 事件段，逐条维护
PLAYER_RELATION = "和玩家的关系"
# narrator status.md
CURRENT_TIME = "当前时间"
SCENE = "场景"
CHARACTER_LOCATIONS = "角色位置"
NARRATIVE_FOCUS = "叙事焦点"
RECENT_WORLD_EVENT = "最近世界事件"
PENDING_EVENTS = "待触发事件"   # 事件段
```

消费方继续用 `extract_status_field(status, status_fields.CONCERN)`，但不再散落字符串。

### 3.3 `storage/character_repo.py` —— 角色文件 ↔ 实体

```python
class CharacterRepository:
    def __init__(self) -> None:
        self._soul_cache: dict[str, str] = {}     # 替代实体里的 _soul_cache

    def load(self, name: str) -> Character: ...     # 读 soul（带缓存），返回实体
    def read_status_text(self, name: str) -> str: ... # 实时 status.md 原文（给 prompt）
    def invalidate(self, name: str | None = None) -> None: ...  # reset/load 后清缓存

    # 写回策略（吸收原实体方法的"value-add"，非薄包装）：
    def apply_status_fields(self, name, fields) -> list[FileUpdateResult]: ...  # 原 set_status_fields：滤事件段/空值
    def append_memory_draft(self, name, text, turn) -> FileUpdateResult | None: ...  # 原 append_memory：normalize + turn 标记
    def add_event(self, name, desc) -> ...        # 原 add_event：跳"无"
    def mark_triggered(self, name, event) -> ...
```

底层仍调 `status_file` / `memory_store`；Repository 装的是"如何正确持久化一个角色的回合产出"的策略。

### 3.4 `storage/narrator_repo.py` —— narrator 文件 ↔ 状态

narrator 与角色差异大（无 memory/draft、字段另一套、要管 world_schedule.json），单独一个 Repository：
```python
class NarratorRepository:
    def load_soul(self) -> str: ...
    def read_status_text(self) -> str: ...
    def write_scene(self, output: LLMNarratorOutput) -> ...      # 原 _write_scene_to_status
    def apply_state_update(self, output: LLMStateUpdate) -> ...  # 原 _apply_state_updates（含 world_schedule json 合并）
    def sync_player_relations(self) -> ...                       # 原 sync_player_relations + _format_player_relations
    def add_event / mark_triggered ...
```

### 3.5 `engine/conversation_service.py` / `engine/narrator_service.py` —— 编排

```python
class ConversationService:
    def __init__(self, repo: CharacterRepository): ...
    async def run_turn(self, character: Character, user_input, raw_messages, *, observation_mode) -> LLMCharacterOutput:
        # 原 Character.run + _build_prompt + _apply_updates：搜记忆→prompt→SDK→经 repo 写回

class NarratorService:
    def __init__(self, repo: NarratorRepository): ...
    async def route(self, user_input, *, observation_mode) -> tuple[LLMNarratorOutput | None, bool]: ...
    async def update_state(self) -> None: ...
```

- 无全局单例；server.py 在启动/请求处持有 Service 实例（或一个轻量 `engine/__init__` 装配点）。
- `reset_entities()` → 改为 `repo.invalidate()`；server 的 reset/load 调它。

## 4. 要 Hucci 拍板的决策

- **D1（核心）**：**砍掉 `CharacterStatus`**，改为 §3.2 字段名常量 + 沿用 `extract_status_field`。理由：无消费者。
  → 推荐：砍。
- **D2**：实体**不持有 status 快照**，status 经 Repository 按需读写。理由：status 回合中被多方改写 + 人工可编辑 + 事件段是外科式 splice 写入，快照会过期/冲掉他人改动。
  → 推荐：不持有，实体只含 `name + soul`。
- **D3**：Service 粒度 = **每角色/旁白 Service**（替换实体方法）；**SSE 回合循环留在 server.py**。不做"一个 god-service 吞整个回合"。
  → 推荐：如上。
- **D4**：**Narrator 不单独建数据实体**（它的数据也只有 name+soul，与 Character 重复）；narrator 的 soul 由 `NarratorRepository` 提供给 `NarratorService`。
  → 推荐：不建 Narrator 实体；只有 `models/Character`。
- **D5**：Repository 拆成 `CharacterRepository` + `NarratorRepository`（二者文件家族/写回字段差异大）。
  → 推荐：拆开。
- **D6**：**Phase 3 + Phase 4 合并执行**（Repository 形状依赖实体，Service 依赖二者，强耦合，分两步反而要造过渡态）。
  → 推荐：合并为一个 Phase。
- **D7**：实体用 `frozen dataclass`（推荐，无校验/序列化需求） vs pydantic（与 models/ 现有风格一致）。

## 5. 风险

- 这是整个重构**风险最高**的一步：动主回合编排（`server._chat_stream` 调用面）+ 删全局单例。
- 缓解：保持纯搬迁、行为不变；`server._chat_stream` 的事件序列/时序一字不改，只把"调实体方法"换成"调 Service"；测试兜底（test_agent_manager / test_conversation_flow / test_server_state_updater）。
