多 Agent 角色扮演 / 叙事游戏项目。当前实现以 **FastAPI + pydantic-ai + Pydantic 结构化输出 + 文件记忆 + sqlite-vec** 为核心，使用 `uv` 作为项目管理器。

## 核心设计

- **独立记忆**：角色维护自己的 `memory.md / status.md / user.md`，`narrator` 只维护 `status.md` 与 raw 历史
- **信息差**：消息按 `visible_to` 控制可见范围，未参与场景的角色不会看到该轮内容
- **旁白先行**：`narrator` 先做路由与场景推进，再顺序调用目标角色
- **结构化输出**：所有结构化 Agent 使用 `PromptedOutput`，不输出 XML；系统直接读取 typed 字段写回文件
- **双层记忆**：Markdown 文件可读可编辑，向量库负责检索

## 技术栈

- Python 3.11+
- FastAPI + SSE（服务端推送）
- pydantic-ai（`pydantic-ai`）—— `Agent` / `PromptedOutput` / `OpenAIChatModel` / `OpenAIProvider`
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
├── engine/                     # 应用层编排
│   ├── agent_factory.py        # Agent 创建、注册表与 SDK model 配置
│   ├── agent_runner.py         # SDK Runner 调用、Logfire trace 与 typed parse
│   ├── agent_schema.py         # Pydantic 结构化输出类型
│   ├── conversation_flow.py    # 单轮对话编排、路由、typed 输出写回
│   ├── consolidation_flow.py   # 记忆整理编排
│   ├── prompt_builder.py       # prompt / 历史窗口 / 整理输入构造
│   └── save_manager.py         # 存档 / 读档 / 重置 / 开场加载
├── llm/
│   ├── providers.py            # Provider 配置与 URL 解析（返回 api_url/api_key/model/temperature）
│   ├── embedding.py            # Embeddings 客户端（embed_async / embed_sync）
│   └── rerank.py               # Rerank API 客户端
├── log_config/                 # Logfire 与业务 logger 配置
├── memory/                     # 记忆规则与流程
│   ├── indexer.py              # 向量索引重建入口（从 memory.md 解析后写入 storage）
│   ├── parser.py               # memory.md 格式解析、事件切分、日期工具、记忆块操作
│   └── retrieval.py            # 完整检索 pipeline（融合、rerank、recency、召回状态更新）
├── shared/                     # 纯配置与无副作用工具函数
│   ├── config.py               # 路径、运行参数、character_path、get_agent_names
│   └── text_utils.py           # 文本清理、get_display_name
├── storage/                    # 持久化基础设施（文件 / JSONL / sqlite-vec）
│   ├── agent_files.py          # 角色目录文件操作（read/write soul/memory/status/user/growth/sidecar）
│   ├── history.py              # narrator raw JSONL 对话历史读取
│   ├── message_router.py       # 对话写入 / 可见性过滤
│   └── vector_store.py         # sqlite-vec 向量存储（write/delete + 原始候选检索）
├── prompts/                    # narrator / character / consolidation prompts
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
memory/          ← shared/ + storage/ + llm/
engine/          ← shared/ + storage/ + memory/ + llm/
server.py        ← shared/ + storage/ + engine/
```

## 运行时文件职责

### 角色文件

- `soul.md`：手写角色定义，只读
- `memory.md`：角色长期记忆，记录事件与情绪变化（仅角色有）
- `status.md`：当前状态；角色包含“打算”，旁白包含“待触发事件”
- `user.md`：角色对玩家的认知（仅角色有，`narrator` 无）
- `tmp_user.md`：`user.md` 的工作草稿；首次写入时复制正式档案，整理后删除
- `growth.md`：人格沉淀，由整理器维护并在角色 prompt 中注入（仅角色有）

### 历史文件

- 当前对话历史**只写入** `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- 每条消息带 `visible_to`
- 角色读取上下文时，通过可见性过滤出自己能看到的消息

### 其他运行时文件

- `data/characters/last_choices.json`：最新一组玩家选项，续档时恢复展示，重置时清除
- `data/characters/*/.history_window_state.json`：各 Agent 的对话历史高低水位窗口 sidecar
- `data/characters/*/.consolidation_state.json`：角色记忆整理进度 sidecar
- `data/characters/*/.memory_recall_state.json`：角色长期记忆 recall 快照（仅存档时从 DB 生成，运行期不维护）

## 消息路由

由 `narrator` 负责决定谁参与当前回合。

```text
用户输入 → narrator → targets: [“角色名”, ...]（NarratorOutput.targets）
```

### narrator 的职责

- 分析玩家输入，输出 `targets` 数组
- 描述时间、地点、在场信息与环境
- 推进剧情、切换场景、安排纯 NPC 行为
- **绝不替角色说话或决定角色行动**

## 单轮对话流程

```text
用户消息
  ↓
调用 narrator，得到 NarratorOutput（targets + content）
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
后台调用 state_updater，更新 narrator/status.md（场景、时间、待触发事件）
```

## Agent 输出与写回机制

所有结构化 Agent 使用 pydantic-ai 的 `PromptedOutput` 结构化输出，不再使用 XML `<update_notes>`：

- `CharacterOutput`：`content`, `memory`, `status`, `player`, `triggered`, `add_event`
- `NarratorOutput`：`content`, `targets`（仅路由与场景描述）
- `StateUpdaterOutput`：`status`, `triggered`, `add_event`（回合后后台维护 narrator 状态）
- `ChoicesOutput`：`choices`

`engine/conversation_flow.py` 的 `_apply_response_updates()` 读取 typed 字段写回文件。

### 写回规则

- `output.memory` → 追加/更新 `memory.md`
- `output.status` → 覆盖更新 `status.md` 对应字段
- `output.player` → 追加到 `tmp_user.md` 对应字段；首次写入时先复制 `user.md` 为工作草稿，整理后再回写 `user.md`
- `output.triggered` → 从 `status.md` 中移除已执行条目
- `output.add_event` → 向 `status.md` 中插入新条目

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
2. `prompts/character_prompt.txt`
3. 允许写回的字段白名单

`user` 消息按以下顺序拼装为**单条大消息**：

1. `growth.md`
2. 最近可见对话历史（从 raw JSONL 构建；按 `visible_to` 过滤；高低水位截断）
3. `user.md`（`tmp_user.md` 仅作为工作草稿参与整理，不直接注入 prompt）
4. `status.md`
5. `<relevant_memories>`（来自 `memory.md` 的长期记忆召回）
6. 本轮玩家输入

### narrator Agent

`system` 消息包含：

1. `soul.md`
2. `prompts/narrator_prompt.txt`

`user` 消息按以下顺序拼装为**单条大消息**：

1. 最近对话历史
2. `status.md`
3. 本轮玩家输入

`narrator` 不走向量召回；它依赖 `status.md` 中的场景、叙事焦点和待触发事件管理剧情推进。

narrator 支持独立 LLM 配置（`NARRATOR_LLM_*` 环境变量），未设置时回退到主 LLM。

### 选项生成

每轮角色回应后，调用 `generate_choices()` 生成 2-3 个玩家可选行动：

- prompt 来源：`prompts/choices_prompt.txt`
- 使用 narrator 的 LLM 配置
- 输出风格为玩家台词（可含括号动作描写），非行动指令
- 选项同时以文本和按钮形式展示，持久化到 `last_choices.json`

## 长期记忆检索

- 向量库只索引 `memory.md` 中的长期记忆事件，owner scope 固定为当前角色
- 默认检索路径是 memory-only；非 memory 检索已停用
- `memory/retrieval.py` 负责完整检索 pipeline：embedding → 向量/BM25 候选 → hybrid 融合 → (可选) rerank → recency 排序 → recall 状态更新
- `storage/vector_store.py` 只做存储层：提供 `get_vector_candidates` / `get_bm25_candidates` 原始候选，pipeline 逻辑不在此处
- `memory/indexer.py` 负责从 `memory.md` 重建向量索引（解析、过滤、元数据提取在此层，storage 只做 I/O）
- 召回排序为：向量相关性与 BM25 相关性先融合，rerank（可选）替换 relevance 信号，最后叠加游戏内时间 recency
- 已配置 Logfire 时，记忆检索会记录每轮 query 和 top 命中摘要，便于排查召回质量
- `last_recalled_at` 会在命中后更新到 DB；`.memory_recall_state.json` 仅在存档时从 DB 导出，读档重建时作为降级数据源
- `memory/indexer.rebuild_memory_index()` 会结合 `.consolidation_state.json` 恢复长期记忆索引；recall 状态优先从 DB 读取，DB 为空时降级读 `.memory_recall_state.json`

## 记忆整理

`engine/consolidation_flow.py` 负责角色后台整理：

- 组装整理流程，并调用 `engine/prompt_builder.py` 准备整理输入
- 归并 `memory.md`
- 提炼 / 更新 `growth.md`（仅角色）
- 去重压缩 `growth.md`（仅角色）
- 顺带精炼 `user.md`（仅角色）
- 按进度同步向量索引

`narrator` 不维护 `memory.md`，也不参与整理。

整理在对话历史窗口触发高水位截断时自动触发（事件驱动，无固定计数器）。

## 配置来源

### `.env`

- 放密钥、模型 ID、provider 和外部服务 URL
- `RERANK_ENABLED=true` 时才会真正启用 rerank 调用
- narrator / choices / consolidation 都支持各自的独立 LLM 配置，未设置时逐级回退

### `config.toml`

- 放运行时策略参数，例如 Agent temperature、超时、向量检索权重
- `[history]` 中的 `history_high` / `history_low` 控制多轮消息高低水位截断

## 存档与重置

由 `engine/save_manager.py` 负责，通过 FastAPI 接口暴露：

- `POST /api/save`：导出 zip 到 `saves/`
- `GET /api/saves`：列出存档
- `POST /api/load`：恢复存档并重建必要索引
- `POST /api/reset`：从 `data/templates/{story_id}` 重置运行时数据

存档会包含：

- 角色 markdown 文件（`narrator` 不含 `memory.md`）
- narrator 的 raw 历史
- 各 Agent `.history_window_state.json`
- 角色 `.consolidation_state.json`
- 角色 `.memory_recall_state.json`
- `last_choices.json`

当前内置故事模板：

- `school`：`lilith` / `mitsuki` / `narrator`
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
