多 Agent 角色扮演 / 叙事游戏项目。当前实现以 **FastAPI + pydantic-ai + Pydantic 结构化输出 + 文件记忆 + sqlite-vec** 为核心，使用 `uv` 作为项目管理器。

## 核心设计

- **独立记忆**：角色维护自己的 `memory.jsonl / status.md / user.md`，`narrator` 维护 `status.md` 与 raw 历史
- **信息差**：消息按 `visible_to` 控制可见范围，未参与场景的角色不会看到该轮内容
- **旁白先行**：`narrator` 先做路由与场景推进，再顺序调用目标角色
- **结构化输出**：所有结构化 Agent 使用 `PromptedOutput`，不输出 XML；系统直接读取 typed 字段写回文件
- **双层记忆**：Markdown 文件可读可编辑，向量库负责检索

## 技术栈

- Python 3.11+
- FastAPI + SSE（服务端推送）
- pydantic-ai（`pydantic-ai`）—— `Agent` / `PromptedOutput` / `OpenAIChatModel` / provider-specific `Provider`
- sqlite-vec + aiosqlite
- asyncio

## 当前项目结构

```text
agentgal-memos/
├── server.py                   # FastAPI 入口（UI 适配层）
├── config.toml                 # 非密钥运行参数
├── data/
│   ├── characters/             # 运行时角色数据
│   ├── templates/              # 故事模板（school / modern）
│   └── vectors.sqlite          # 向量库
├── engine/                     # 对话运行时编排
│   ├── character.py            # Character / Narrator 运行封装与 typed 输出写回
│   ├── character_factory.py    # 新角色孵化
│   ├── conversation_flow.py    # 单轮对话编排与 UI 适配函数
│   └── prompt_builder.py       # 对话 prompt / 历史窗口 / schedule 快照构造
├── agents/                     # SDK 基础设施（技术支撑层）
│   ├── factory.py              # Agent 创建、注册表与 SDK model 配置
│   ├── runner.py               # SDK Runner 调用、Logfire trace 与 typed parse
│   └── schema.py               # Pydantic 结构化输出类型
├── world/                      # 世界模型（时间 / 位置）
│   └── schedule.py             # 角色 schedule 查询、游戏时间解析、时段匹配
├── consolidation/              # 后台记忆整理（独立流程）
│   ├── flow.py                 # 整理编排：EpisodeMemoryGenerator / growth / user 精炼
│   └── inputs.py               # 整理 prompt 组装（memory_owner / raw_dialogue）
├── llm/
│   ├── providers.py            # Provider 配置与 URL 解析（返回 provider/api_url/api_key/model/temperature）
│   ├── embedding.py            # Embeddings 客户端（embed_async / embed_sync）
│   └── rerank.py               # Rerank API 客户端
├── log_config/                 # Logfire 与业务 logger 配置
├── memory/                     # 记忆规则与流程
│   ├── indexer.py              # 向量索引重建入口（从 memory.jsonl 读取后写入 storage）
│   ├── parser.py               # memory.jsonl 结构化记录读写、EpisodeMemory 定义、日期工具
│   └── retrieval.py            # 完整检索 pipeline（融合、rerank、recency、召回状态更新）
├── shared/                     # 纯配置与无副作用工具函数
│   ├── config.py               # 路径、运行参数、character_path、get_agent_names
│   └── text_utils.py           # 文本清理、get_display_name
├── storage/                    # 持久化基础设施（文件 / JSONL / sqlite-vec / 存档）
│   ├── agent_files.py          # 角色目录文件操作（read/write soul/memory/status/user/growth/sidecar）
│   ├── history.py              # narrator raw JSONL 对话历史读取
│   ├── message_router.py       # 对话写入 / 可见性过滤
│   ├── save_manager.py         # 存档 / 读档 / 重置 / 开场加载
│   └── vector_store.py         # sqlite-vec 向量存储（write/delete + 原始候选检索）
├── prompts/                    # 按生命周期分组的 prompt 常量模块
│   ├── consolidation_prompts.py  # 后台整理：EpisodeClosureDetector / EpisodeMemoryGenerator / growth / user
│   ├── runtime_prompts.py        # 对话主线：character / narrator / choices / state_updater
│   ├── worldgen_prompts.py       # 角色孵化
│   └── opening_intro.txt         # 玩法介绍开场文案（面向玩家）
├── scripts/                    # 维护脚本
├── static/                     # Alpine.js + HTML/JS 前端
├── tests/                      # pytest 测试
├── README.md
├── AGENTS.md
├── CLAUDE.md
└── .env
```

### 分层依赖方向

```
shared/          ← 无内部依赖
storage/         ← shared/
llm/             ← shared/
agents/          ← shared/                            # SDK 基础层
memory/          ← shared/ + storage/ + llm/
world/           ← shared/ + storage/ + agents/
consolidation/   ← shared/ + storage/ + agents/ + memory/ + llm/
engine/          ← shared/ + storage/ + agents/ + memory/ + world/ + consolidation/
server.py        ← 全部
```

## 运行时文件职责

### 角色文件

- `soul.md`：手写角色定义，只读；分 `<identity>` / `<goal>` / `<dynamic>` / `<behavior>` / `<voice>` 五段，其中 `<goal>` 写角色在故事期内要拿到的具体长期目标（外部可验证里程碑 + 可选的关系愿景），整个故事期大体不变
- `memory.jsonl`：角色长期记忆，每行一个结构化 `EpisodeMemory`（`date / time / location / participants / keywords / importance / content / memory_owner / title / raw_dialogue`），append-only，仅角色有
- `memory_draft.jsonl`：每轮 `output.memory` 的落盘缓冲（仅角色有），每行 `{"turn": int, "text": str}`；由 consolidation 在 `EpisodeClosureDetector` 判定闭合后，按 `until_turn` 切片读取并产出结构化 `EpisodeMemory` 追加到 `memory.jsonl`，已归并的条目从 draft 中移除，未闭合 turn 的条目保留
- `status.md`：当前状态；角色包含「打算」和「和玩家的关系」，旁白包含「待触发事件」「角色位置」和派生字段「和玩家的关系」（按 `- 角色显示名：关系` 汇总各角色 status，程序维护，narrator 不自行生成）
- `user.md`：角色对玩家的认知（仅角色有，`narrator` 无）
- `tmp_user.md`：`user.md` 的工作草稿；首次写入时复制正式档案，整理后删除
- `growth.md`：人格沉淀，由整理器维护并在角色 prompt 中注入（仅角色有）；每条带 dimension 标签（不可逆转移轴），格式 `[P001|对X：A→B] [日期] ...`，最多 20 条
- `relations.md`：角色对其他角色（不含 `player`）的当下视角；`## {character}` 一节一段；每轮 `output.relations[character]` 整段覆盖（仅角色有）。

### 历史文件

- 当前对话历史**只写入** `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- 每条消息带 `visible_to`
- 角色读取上下文时，通过可见性过滤出自己能看到的消息

### 其他运行时文件

- `data/characters/last_choices.json`：最新一组玩家选项，续档时恢复展示，重置时清除
- `data/characters/.turn_counter.json`：全局 turn 计数器，每条玩家消息递增 1；raw JSONL 与 `memory_draft.jsonl` 的每条记录都带 turn 号，供 `EpisodeClosureDetector` 判定闭合点；reset 随 characters 目录清除，新游戏从 0 起计
- `data/characters/narrator/tasks.md`：可选剧情种子文件；当前主流程主要由 `state_updater` 从角色 `打算` 同步 `待触发事件`
- `data/characters/*/.history_window_state.json`：各 Agent 的对话历史高低水位窗口 sidecar
- `data/characters/*/.consolidation_state.json`：角色记忆整理进度 sidecar
- `data/characters/*/.memory_recall_state.json`：角色长期记忆 recall 快照（仅存档时从 DB 生成，运行期不维护）

## 消息路由

由 `narrator` 负责决定谁参与当前回合。

```text
用户输入 → narrator → targets: [“现有角色名”, ...]（NarratorOutput.targets；若本轮只引入新角色，可暂时为空）
```

### narrator 的职责

- 分析玩家输入，输出当前已存在且本轮可回应的 `targets`；若本轮只引入新角色，可暂时为空，编排层会在孵化成功后补入
- 判断玩家是否仍有和角色互动的意愿：有则延续当前场景；分别、跳过时间或不再互动时，导向待触发事件或制造同等作用的即时张力
- 每轮都必须让至少一个主要角色当轮可感知玩家并回应
- 描述时间、地点、在场信息、环境、纯 NPC 行为和当前钩子
- 不新增未来事件；未来事件由 `state_updater` 从角色 `打算` 维护
- 当剧情需要引入有关系锚的新人物时，通过 `NarratorOutput.new_characters` 列出 `NewCharacterRequest` 锚点（`name_hint` 只是可选姓名提示），由 `engine/character_factory.py` 生成 `character_id` 并孵化目录；编排层会在孵化成功后自动补入本轮回应名单。纯路人不生成，直接在 content 中描写
- **绝不替角色说话或决定角色行动**

## 单轮对话流程

```text
用户消息
  ↓
调用 narrator，得到 NarratorOutput（targets + content + new_characters）
  ↓
孵化 new_characters：`character_factory` 生成 `character_id`，并写出 soul/status/relations/memory/growth/user + `schedule.json`（LLM 未产出时跳过）；孵化成功的新角色会进入本轮最终回应名单
  ↓
将 narrator 内容写入单一 raw 历史（带 visible_to）
  ↓
顺序调用各 target Agent（每个 agent 响应写入 history 后，下一个才能看到）
  ↓
每个 Agent 响应后：从 CharacterOutput typed 字段写回文件、广播到 history
  ↓
调用选项生成（使用 narrator 模型），展示 2-3 个可选行动
  ↓
持久化最新选项到 last_choices.json（供续档恢复）
  ↓
后台并发启动两个 task（均 `asyncio.create_task`，不阻塞主流程）：
  1. state_updater → 更新 narrator/status.md（场景、时间、角色位置、叙事焦点、待触发事件；「和玩家的关系」由程序从各角色 status 派生同步）
  2. detect_and_consolidate(current_turn) → 判定 episode 闭合并归并记忆（见「记忆整理」）
  ↓
state_updater 输入按顺序为：`schedule_snapshot`（按当前 game_time 渲染各角色 schedule 默认位置，缺日程标「（无日程）」）、character_intention、current_narrator_status、recent_history
  ↓
state_updater 每轮输出全量「角色位置」快照；优先级：recent_history 事实 > character_intention 中带地点的打算 > 旧快照 > schedule_snapshot 默认值
  ↓
state_updater 从各角色「打算」同步公共「待触发事件」（事件名保留角色名）
```

## Agent 输出与写回机制

所有结构化 Agent 使用 pydantic-ai 的 `PromptedOutput` 结构化输出，不再使用 XML `<update_notes>`：

- `CharacterOutput`：`content`, `memory`, `status`, `player`, `triggered`, `add_event`, `relations`
- `NarratorOutput`：`content`, `targets`, `new_characters`（路由、场景描述与动态角色请求）
- `NewCharacterRequest` / `NewCharacterProfile`：新角色孵化锚点（可选 `name_hint`，不含 `character_id`）与 character_factory 的完整输出（包含 `character_id`、最终 `display_name`、`initial_status`、`initial_relations`）
- `EpisodeMemoryBlock`：`EpisodeMemoryGenerator` 输出的单条长期记忆事件（`date / time / location / participants / keywords / importance / content / title`），由流程层注入 `memory_owner` 与 `raw_dialogue`（原始对话追溯，仅作 metadata，不进向量索引、不进召回文本）后追加到角色 `memory.jsonl`
- `EpisodeClosureDetector` 输出类型：`dict[str, list[EpisodeClosureBoundary]]`（key 是 recent_history 中出现过的角色 `agent_name`；value 是该角色在 history 中检测到的所有主题边界，按 `end_turn` 升序，空数组表示无边界。每条边界含 `end_turn / old_theme / new_theme / reason`。消费方只采纳本地候选角色，并取每个数组里 `end_turn` 最大的边界作为本轮可归并的闭合点）
- `StateUpdaterOutput`：`status`, `triggered`, `add_event`（回合后后台维护 narrator 状态）
- `ChoicesOutput`：`choices`

`engine/character.py` 的 `Character` / `Narrator` 均继承自 `BaseEntity`，封装 soul / status 的读写与 SDK 调用；写入统一走实体方法（`set_status_fields` / `append_memory` / `add_event` / `mark_triggered` / `set_relation` / `set_user_profile_fields`），不再让外部直接调用底层 `update_xxx`。`Narrator.route()` 负责路由与场景描述，`Narrator.update_state()` 在回合末调 `state_updater` 。

### 写回规则

- `output.memory` → 以当前全局 turn 号为标签，追加一条记录到 `memory_draft.jsonl`（后续 `EpisodeClosureDetector` 判定闭合 turn 后，consolidation 按 `until_turn` 切片产出 `EpisodeMemory` 追加到 `memory.jsonl`，已归并条目从 draft 中移除）
- `output.status` → 覆盖更新 `status.md` 对应字段
- `output.player` → 追加到 `tmp_user.md` 对应字段；首次写入时先复制 `user.md` 为工作草稿，整理后再回写 `user.md`
- `output.triggered` → 从 `status.md` 中移除已执行条目
- `output.add_event` → 向 `status.md` 中插入新条目
- `output.relations` → 覆盖 `relations.md` 的 `## {target}` 节（target 必须是其他角色的显示名，不能是自己或 `player`；对玩家的长期视角走 `user.md` 与 `status.md` 的「和玩家的关系」，不写进 relations；非法 target 跳过并记录 warning）

其中：

- `narrator` 操作区块：`待触发事件`
- 其他角色操作区块：`打算`
- `打算` / `待触发事件` 不能通过 `<status>` 整段覆盖，只能通过 `<triggered>` / `<add_event>` 逐条维护

## Prompt 组成

### 设计原则

- system prompt 尽量稳定，动态内容放进 user message，以提高 prompt cache 命中率
- 不要随意调整 context 块顺序；当前顺序是专门为缓存和检索命中率调过的

### 角色 Agent

`system` 消息包含：

1. `soul.md`
2. `prompts.runtime_prompts.CHARACTER`
3. 允许写回的字段白名单

`user` 消息按以下顺序拼装为**单条大消息**：

1. `<my_schedule>`（渲染角色 `schedule.json`；整个故事期间最稳定，放最前锚定 prompt cache）
2. `growth.md`
3. `user.md`（`tmp_user.md` 仅作为工作草稿参与整理，不直接注入 prompt）
4. 最近可见对话历史（从 raw JSONL 构建；按 `visible_to` 过滤；高低水位截断；历史中的旁白只保留最后一条）
5. `status.md`
6. `<relations>`（直接注入角色自己的 `relations.md`，涵盖所有已知主要角色，不分在场与否。对玩家的视角不走 relations）
7. `<relevant_memories>`（来自 `memory.jsonl` 的长期记忆召回，向量库侧仍渲染成 markdown 供 LLM 阅读）
8. 本轮玩家输入

### narrator Agent

`system` 消息包含：

1. `soul.md`
2. `prompts.runtime_prompts.NARRATOR`

`user` 消息按以下顺序拼装为**单条大消息**：

1. 最近对话历史（旁白只保留最后一条）
2. `status.md`
3. 本轮玩家输入

`narrator` 不走向量召回；它依赖 `status.md` 中的场景、叙事焦点、待触发事件和「和玩家的关系」索引推进当前回合。待触发事件主要由 `state_updater` 从各角色 `打算` 同步，事件名保留角色名（如 `【美月：顺路的约定】`）。「和玩家的关系」由程序从各角色 status 汇总，格式为 `- 美月：恋人`。

> 注：`<world_now>`（当前时间 / 各角色实时位置的派生投影）目前已停用，待 schedule 机制完善后再恢复。期间 narrator 只读 `status.md` 中作者/state_updater 维护的字段。

narrator 支持独立 LLM 配置（`NARRATOR_LLM_*` 环境变量），未设置时回退到主 LLM。

### 选项生成

每轮角色回应后，调用 `generate_choices()` 生成 2-3 个玩家可选行动：

- prompt 来源：`prompts.runtime_prompts.CHOICES`
- 使用 narrator 的 LLM 配置
- 输出风格为玩家台词（可含括号动作描写），非行动指令
- 选项同时以文本和按钮形式展示，持久化到 `last_choices.json`

## 长期记忆检索

- 向量库只索引 `memory.jsonl` 中的长期记忆事件，owner scope 固定为当前角色
- 默认检索路径是 memory-only；非 memory 检索已停用
- `memory/retrieval.py` 负责完整检索 pipeline：embedding → 向量/BM25 候选 → hybrid 融合 → (可选) rerank → recency 排序 → recall 状态更新
- `storage/vector_store.py` 只做存储层：提供 `get_vector_candidates` / `get_bm25_candidates` 原始候选，pipeline 逻辑不在此处
- `memory/indexer.py` 负责从 `memory.jsonl` 读取 `EpisodeMemory` 记录并逐条追加到向量库
- 召回排序为：向量相关性与 BM25 相关性先融合，rerank（可选）替换 relevance 信号，最后叠加游戏内时间 recency
- 已配置 Logfire 时，记忆检索会记录每轮 query 和 top 命中摘要，便于排查召回质量
- `last_recalled_at` 会在命中后更新到 DB；`.memory_recall_state.json` 仅在存档时从 DB 导出，读档重建时作为降级数据源
- `memory/indexer.rebuild_memory_index()` 会结合 `.consolidation_state.json` 恢复长期记忆索引；recall 状态优先从 DB 读取，DB 为空时降级读 `.memory_recall_state.json`

## 记忆整理

`consolidation/flow.py` 负责角色后台整理：

- 每回合末由 `detect_and_consolidate(current_turn)` 作为后台 task 触发（与 `state_updater` 并发）：先扫描有 `memory_draft.jsonl` 的角色作为候选，调用 `EpisodeClosureDetector` 判定哪些角色本轮 episode 已闭合（返回 `{agent_name: closed_at_turn}`）；闭合角色并行执行 `consolidate_agent(name, until_turn=closed_at_turn)`
- `consolidate_agent` 按 `until_turn` 从 `memory_draft.jsonl` 切片出本 episode 的草稿条目 + 对应 turn 区间的 raw 对话，交给 `EpisodeMemoryGenerator` 产出单条结构化 `EpisodeMemory`；流程层注入 `memory_owner` 与 `raw_dialogue` 后 append 到 `memory.jsonl`，已归并条目从 draft 中移除，未闭合 turn 的条目保留；失败则 draft 全部保留留待下一轮重试
- growth 阶段使用整理出的 `EpisodeMemory` JSON 数组作为 LLM 输入，不再先渲染成 markdown
- 对 `growth.md` 做 patch（add/update/remove，按 dimension 1:1 约束；update 可随同一对象/同一关系轴的渐进变化更新 dimension）（仅角色）
- 顺带精炼 `user.md`（仅角色）
- 按进度同步向量索引

`narrator` 不维护 `memory.jsonl`，也不参与整理。

## 配置来源

### `.env`

- 放密钥、模型 ID、provider 和外部服务 URL
- 配置 `RERANK_MODEL` 时才会真正启用 rerank 调用
- narrator / choices / consolidation / character_factory / episode_closure_detector 都支持各自的独立 LLM 配置，未设置时逐级回退（`CHARACTER_FACTORY_LLM_*` 未设置时回退到 narrator；`EPISODE_CLOSURE_DETECTOR_LLM_*` 未设置时回退到主 LLM）

### `config.toml`

- 放运行时策略参数，例如 Agent temperature、角色/整理/选项生成超时、embedding/rerank 请求超时、向量检索权重
- `[history]` 中的 `history_high` / `history_low` 控制多轮消息高低水位截断（基于 turn 号锚定，存于 `.history_window_state.json` 的 `start_turn` 字段）；`raw_scan_turns` 限制 narrator 路由 / 角色运行 / 选项生成读取 raw 历史时回溯的 turn 数（整理流程按 turn 切片，不受此限制）

## 存档与重置

由 `storage/save_manager.py` 负责，通过 FastAPI 接口暴露：

- `POST /api/save`：导出 zip 到 `saves/`；`{}` 新建 uuid 档位，`{"filename": "...zip"}` 覆盖指定档位
- `GET /api/saves`：列出存档
- `POST /api/load`：恢复存档并重建必要索引
- `POST /api/reset`：从 `data/templates/{story_id}` 重置运行时数据

存档会包含：

- 角色 markdown / jsonl 文件（`narrator` 不含 `memory.jsonl` / `memory_draft.jsonl`）
- 角色 `memory.jsonl`（结构化长期记忆，每行一条 `EpisodeMemory`，含 `raw_dialogue` 追溯字段）
- 角色 `memory_draft.jsonl`（存在时；每行 `{"turn": int, "text": str}`，确保未闭合归并的本轮 memory 不随存档丢失）
- 角色 `schedule.json`（存在时）
- narrator 的 raw 历史（每条带 turn 号）
- 各 Agent `.history_window_state.json`
- 角色 `.consolidation_state.json`
- 角色 `.memory_recall_state.json`
- `last_choices.json`
- `.turn_counter.json`（全局 turn 计数）

当前内置故事模板：

- `school`：`mitsuki` / `narrator`
- `modern`：`chenxiao` / `guyining` / `narrator`

## 开发约定

### 代码设计

- 保持 DRY，但不要为了抽象而抽象
- 优先简单、显式、当前够用的实现
- 一个函数只做一件事，尽量控制复杂度
- 类型注解要完整（Python 3.11+）

### 错误处理

- LLM / embedding / 数据库调用必须保留上下文日志
- 文件操作前先检查路径与存在性
- 禁止裸 `except:`，应捕获具体异常

### 并发与异步

- 所有 I/O 操作使用 `async/await`
- 多角色调用使用 `asyncio.gather()` 并行执行
- 对共享资源（文件、向量库、整理任务）要考虑并发保护

### 可读性

- 变量名与函数名优先自解释
- 注释写“为什么”，不要复述代码表面含义
- 结构变化时同步更新文档与 prompt

## 日志与观测

- 已配置 Logfire（本地 CLI 或 `LOGFIRE_TOKEN`）时，上报 PydanticAI traces、token/cost、路由事件、记忆检索与整理事件；未配置时静默跳过
- 路由与记忆模块仍使用标准 logger 作为业务事件入口，但默认不再写入本地 `logs/*.log` 轮转文件

## 测试约定

- 纯逻辑尽量做成可单测函数
- 使用 `pytest`
- 当前已有：
  - 对话历史相关测试
  - 格式化测试
  - 存档一致性测试
  - 向量库测试
- 涉及向量检索/embedding 的测试可能依赖 `.env` 中的 embedding 配置
