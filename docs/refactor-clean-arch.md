# 重构计划：Clean Architecture 三分

> 状态：已规划，待执行
> 创建：2026-06-07
> 范围：全项目结构性重构（C 范围 = 完整三分，含运行时核心重写）

## 1. 背景与动机

最初是评估 `storage/` 与 `memory/` 的 SRP，挖出两个根因问题：

1. **`storage` ↔ `memory` 循环依赖**（破坏了项目自己声明的单向分层）：
   - `memory/retrieval.py`、`memory/indexer.py` → `import storage.vector_store`
   - `storage/vector_store.py`、`storage/history.py`、`storage/save_manager.py` → `import memory.parser`（`EpisodeMemory` / `canonical_cn_date`）
   - 根源：`memory/parser.py` 一个文件同时是**数据模型 + JSONL 持久化 + 日期工具 + 文本规范化**，被 21 个文件引用。它是震中。

2. **机制 / 策略混住**：`storage` 里塞满了领域规则（`extract_player_name` 的正则、事件队列 `【】` 规则、`update_status` 的"禁止覆盖打算字段"策略、embed 文本组装、jieba 分词）。`agent_files.py`(581 行) 是典型的"杂物抽屉"。

3. **`Character` / `Narrator` 是行为类而非领域类**：一个类同时承担领域身份 + 持久化（Active Record）+ 用例编排（Service）。基类竟叫 `BaseEntity`，却是全项目最不像 Entity 的东西（满身 I/O）。`conversation_flow.py` 沦为薄壳。

**目标**：按 Clean Architecture 把领域实体向心收拢到最内层 `models/`，机制与策略分离，依赖单向向内。

## 2. 改造后的同心依赖

```
models/        EpisodeMemory · Understanding · UnderstandingHistoryEntry(值对象)
               CharacterStatus · Character(纯聚合)
               └─ 只依赖 shared 的纯函数(date_utils/text_utils)，绝不碰 config
shared/        纯函数(date_utils, text_utils)  ╎  config(路径/环境 = 基础设施)
storage/       Repository + 机制：CharacterRepo · NarratorRepo · memory_store
               · status_file · vector_store(纯存储)           ← models + shared
agents/        LLM 边界：llm_schema.py(LLM* 契约) · runner · factory  ← models + shared
memory/        检索策略：retrieval · indexer · formatting        ← models + storage + llm
consolidation/                                                  ← models + storage + agents + memory
engine/        Service / 用例：ConversationService · NarratorService · character_factory
server.py      HTTP 边界
```

**核心收益**：`EpisodeMemory` 进 `models/` 后，`storage` 不再 `import memory` —— 循环依赖被结构性根除，不靠任何技巧。

## 3. 已定的设计决策（ADR，勿重复讨论）

1. **数据模型下沉 `models/`**，作为最内层；其余各层向心依赖它。
2. **`models/` 只依赖纯函数**（`shared/date_utils`、`shared/text_utils`），**绝不 import `shared/config`**（拼路径、读环境 = 基础设施）。用 import 守护红线（见 §6）。
3. **LLM 契约统一 `LLM` 前缀**，物理迁入 `agents/llm_schema.py`：

   | 旧名（agents/schema.py） | 新名（agents/llm_schema.py） |
   |---|---|
   | `CharacterOutput` | `LLMCharacterOutput` |
   | `NarratorOutput` | `LLMNarratorOutput` |
   | `NewCharacterRequest` | `LLMNewCharacterRequest` |
   | `NewCharacterProfile` | `LLMNewCharacterProfile` |
   | `StateUpdaterOutput` | `LLMStateUpdate` |
   | `ChoicesOutput` | `LLMChoices` |
   | `EpisodeMemoryBlock` | `LLMEpisodeMemory` |
   | `EpisodeClosureBoundary` | `LLMEpisodeClosureBoundary` |
   | `EpisodeClosureOutput` | `LLMEpisodeClosure` |
   | `UnderstandingEntry` | `LLMUnderstandingEntry` |
   | `UnderstandingPatchOutput` | `LLMUnderstandingPatch` |

4. **DTO ↔ Entity 不继承，各自独立**（2026-06-07 修订，推翻原"继承消重"方案）：
   - 原因：LLM 契约在 `agents/`、实体在 `models/`；若 `EpisodeMemory(LLMEpisodeMemory)`，
     会让 `models/ → agents/`，反向打穿 §2 的依赖方向（正是本次要根除的逆向依赖）。
     且二者今天本就是两个独立类、各带一份校验，继承反而是**新引入**耦合、违背 Phase 1
     "纯搬迁零行为变化"的承诺。
   - `models/EpisodeMemory`（全字段 + 自带 `_clean_keywords`/`_clamp_importance`）
     与 `agents/LLMEpisodeMemory`（LLM 子集 + 边界校验）独立并存；
     `models/Understanding` 与 `agents/LLMUnderstandingEntry` 同理。
   - 系统字段（UUID、owner、时间戳、原始对话）只在实体上，绝不暴露给 LLM；
     DTO → 实体的映射在 consolidation 流程显式完成（反腐层）。
5. **Character 建模**：`soul.md` 保持**只读文本资源**（它本质是 prompt，不硬塞 pydantic）；只有动态的 `status` 结构化成 `CharacterStatus`。`Character` 实体 = `name + soul_text + status`，纯数据零 I/O。
6. **`UnderstandingHistoryEntry` 保留**为 `Understanding` 聚合下的值对象，**不与 `EpisodeMemory` 合并**：
   - 二者只有 `date/title/content` 三个字段名重叠，且 `content` 语义不同——它装的是"理解更新后的内容"（`new_content`），`date/title/episode_id` 只是触发更新的那条 episode 的出处标注。
   - `content` 字段名易与 episode 混，但已序列化进 `understanding.jsonl`，改名破坏旧档（不写兼容代码），故**保留字段名，补注释**点明 `content = 理解快照而非事件内容`。
7. **全程不碰运行时 / 存档数据格式**，纯代码搬迁，旧存档向后可读。

## 4. 三分映射：行为类 → 三层

| 现在（`Character`/`Narrator` 一肩挑） | 拆后归属 | 层 |
|---|---|---|
| `name` / `soul` / `status` 数据 | `Character` 纯聚合 + `CharacterStatus` | **models/** |
| `soul`/`status` property 读文件、`_soul_cache` | `CharacterRepository.load()`（缓存也在这） | **storage/** |
| `append_memory` / `set_status_fields` / `add_event` / `mark_triggered` | `CharacterRepository` 写回方法 | **storage/** |
| `Character.run()`（搜记忆→prompt→LLM→写回） | `ConversationService.run_turn()` | **engine/** |
| `Narrator.route()` / `_write_scene_to_status` / `_filter_new_characters` | `NarratorService.route()` | **engine/** |
| `Narrator.update_state` / `_apply_state_updates` / `sync_player_relations` | `NarratorService.update_state()` | **engine/** |
| 全局 `_characters` registry / `narrator` 单例（`engine/character.py` 末尾） | 删除；Service 经 Repository 取纯实体 | — |

## 5. 分阶段执行（从内到外，风险递增；每阶段 = 一个 commit/PR，跑完测试才进下一阶段）

### Phase 1 — 立地基：`models/` + 纯函数层 + DTO 改名，断环 🔴 ✅ 已完成（2026-06-07）
- [x] 新建 `models/`：`EpisodeMemory` / `Understanding` / `UnderstandingHistoryEntry` 从 `memory/parser.py` 移入
- [x] 新建 `shared/date_utils.py`（移 `parse_cn_date`/`canonical_cn_date`/`game_day_number`/`game_day_diff`）
- [x] `normalize` / `extract_status_field` 并入 `shared/text_utils.py`
- [x] `EpisodeMemory.last_recalled_at` 默认值改依赖纯函数（`shared.date_utils.canonical_cn_date`，不反向依赖外层）
- [x] JSONL IO（`read_memory_jsonl`/`append_memory_records`/`parse_jsonl_line`/`serialize_episode`/understanding IO）移到 `storage/memory_store.py`
- [x] `agents/schema.py` → `agents/llm_schema.py`，按 §3 表全部加 `LLM` 前缀
- [x] Entity 与 DTO **保持独立、不继承**（§3.4 修订）
- [x] 改全部 `import memory.parser` 引用点（22 个文件，含 `engine/memory_query_builder.py`；不留兼容 shim），删除 `memory/parser.py`
- [x] 验证：`uv run pytest` → 188 passed / 28 skipped；2 个失败为**改动前既存**（`test_multiturn.py` 查询构建与 CLAUDE.md 描述失配，与本次无关，已 stash 原始 HEAD 复现确认）
- **风险**：影响面广，但纯搬迁/改名，无行为变化，风险低。**结果：行为零变化，达成。**

### Phase 2 — 拆 `storage`：`agent_files` 解散 + `CharacterStatus` 引入
- [ ] `agent_files.py`(581) 拆 → `status_file.py`（section 引擎 + 事件队列 + 白名单）、`runtime_state.py`（turn_counter + player_name）、`files.py`（基础 IO + backup）
- [ ] 新建 `models/CharacterStatus` + `status_file` 提供 `status.md ↔ CharacterStatus` 解析/写回
- [ ] 此阶段不动 `Character` 类，只归位 I/O 函数 + 引入结构化 status
- [ ] 验证：`test_agent_manager` / `test_message_router` / `test_consolidator`

### Phase 3 — Repository 成形
- [ ] `storage/character_repo.py` / `narrator_repo.py`：集中"文件 ↔ 实体"双向映射，soul 缓存搬这里
- [ ] `Character`/`Narrator` 类暂存，读写方法改为委托 Repository（过渡态，行为不变）
- [ ] 验证：`uv run pytest`

### Phase 4 — Service 成形：编排上移，实体退化为纯数据 🔴 C 的核心
- [ ] 新建 `engine/conversation_service.py`：吸收 `Character.run` + `conversation_flow.run_agent_in_scene` 的编排
- [ ] 新建 `engine/narrator_service.py`：吸收 `Narrator.route` / `update_state` / `sync_player_relations`
- [ ] `Character`/`Narrator` 退化为 `models/` 纯实体；删除全局 registry / 单例
- [ ] `server.py` / `conversation_flow` 改调 Service
- [ ] 验证：`uv run pytest` + 手动跑一轮对话冒烟（含新角色孵化）
- **风险**：最高（动主流程编排迁移）。

### Phase 5 — 收尾
- [ ] 校验 DTO↔Entity 继承消重
- [ ] 更新 `CLAUDE.md` 分层图与文件职责表
- [ ] recall sidecar 兼容逻辑（`export_recall_state` 横跨 vector_store↔save_manager）按需收敛

## 6. 风险与守护

- **依赖方向红线化**：加 import 守护（import-linter 或 CI 里 grep 断言），确保 `models/` 不 import `shared.config` / `storage` / `agents` / `memory`。否则向心架构会慢慢腐蚀。
- **一 Phase 一 PR**，独立可 `git revert`；全程不写兼容层，靠测试兜底。
- **Phase 1 是其余一切的地基**，必须先做。
- 不碰运行时/存档数据格式，旧存档向后可读。
