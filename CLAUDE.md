# CLAUDE.md

多 Agent 角色扮演 / 叙事游戏项目。当前实现以 **Chainlit + OpenAI 兼容 LLM + 文件记忆 + sqlite-vec** 为核心，使用 `uv` 作为项目管理器。

## 核心设计

- **独立记忆**：角色维护自己的 `memory.md / status.md / user.md`，`narrator` 只维护 `status.md` 与 raw 历史
- **信息差**：消息按 `visible_to` 控制可见范围，未参与场景的角色不会看到该轮内容
- **旁白先行**：`narrator` 先做路由与场景推进，再并行调用目标角色
- **结构化更新**：Agent 不直接调用“记忆工具”写文件，而是输出 `<update_notes>`，由系统解析并写回
- **双层记忆**：Markdown 文件可读可编辑，向量库负责检索

## 技术栈

- Python 3.11+
- Chainlit
- OpenAI-compatible LLM client（支持 `openai` / `deepseek` / `openrouter`）
- sqlite-vec + aiosqlite
- asyncio

## 当前项目结构

```text
agentgal-memos/
├── app.py                      # Chainlit 入口
├── config.toml                 # 非密钥运行参数
├── data/
│   ├── characters/             # 运行时角色数据
│   ├── templates/              # 故事模板（school / modern）
│   └── vectors.sqlite          # 向量库
├── engine/
│   ├── agent_manager.py        # Agent prompt 构建、LLM 调用、结果写回
│   ├── config.py               # 路径与运行配置
│   ├── message_router.py       # 对话写入 / 可见性过滤
│   ├── response_parser.py      # 解析 <update_notes>
│   └── text_utils.py           # 文本清理
├── game/
│   └── save_manager.py         # 存档 / 读档 / 重置 / 开场加载
├── llm/
│   ├── llm_parser.py           # OpenAI 兼容客户端
│   └── providers.py            # Provider 配置与 URL 解析
├── log_config/                 # 路由、记忆、调用日志
├── memory/
│   ├── consolidator.py         # 记忆整理器
│   ├── consolidation_inputs.py # 整理 step1 输入构造与 raw 对话视角对齐
│   ├── file_ops.py             # md 文件读写工具
│   ├── retrieval.py            # 完整检索 pipeline（融合、rerank、recency、召回状态更新）
│   └── vector_store.py         # 向量索引存储层（write/delete/rebuild + 原始候选检索）
├── prompts/                    # narrator / character / consolidation prompts
├── scripts/                    # 维护脚本
├── tests/                      # pytest 测试
├── README.md
├── CLAUDE.md
└── .env
```

## 运行时文件职责

### 角色文件

- `soul.md`：手写角色定义，只读
- `memory.md`：角色长期记忆，记录事件与情绪变化（仅角色有）
- `status.md`：当前状态；角色包含“打算”，旁白包含“待触发事件”
- `user.md`：角色对玩家的认知（仅角色有，`narrator` 无）
- `growth.md`：人格沉淀，由整理器维护并在角色 prompt 中注入（仅角色有）

### 历史文件

- 当前对话历史**只写入** `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- 每条消息带 `visible_to`
- 角色读取上下文时，通过可见性过滤出自己能看到的消息

### 其他运行时文件

- `data/characters/last_choices.json`：最新一组玩家选项，续档时恢复展示，重置时清除
- `data/characters/*/.history_window_state.json`：各 Agent 的对话历史高低水位窗口 sidecar
- `data/characters/*/.consolidation_state.json`：角色记忆整理进度 sidecar
- `data/characters/*/.memory_recall_state.json`：角色长期记忆 recall sidecar

## 消息路由

由 `narrator` 负责决定谁参与当前回合。

```text
用户输入 → narrator → TARGETS: [角色列表]
```

### narrator 的职责

- 分析玩家输入，输出 `TARGETS: [...]`
- 描述时间、地点、在场信息与环境
- 推进剧情、切换场景、安排纯 NPC 行为
- **绝不替角色说话或决定角色行动**（`response_parser.py` 中有防御性截断：检测到角色名开头的行时自动截断）

## 单轮对话流程

```text
用户消息
  ↓
调用 narrator，得到 TARGETS + 旁白内容
  ↓
将 narrator 内容写入单一 raw 历史（带 visible_to）
  ↓
并行调用各 target Agent
  ↓
解析每个 Agent 的 <update_notes>
  ↓
写回角色 memory.md / status.md / user.md
  ↓
广播回应并展示给玩家
  ↓
调用选项生成（使用 narrator 模型），展示 2-3 个可选行动
  ↓
持久化最新选项到 last_choices.json（供续档恢复）
```

## Agent 输出与写回机制

当前实现不是“Tools 直接修改文件”，而是：

1. Agent 输出正常回复正文
2. 同时在末尾输出 `<update_notes>`
3. `engine/response_parser.py` 解析以下标签：
   - `<memory>`
   - `<status>`
   - `<player>`
   - `<triggered>`
   - `<add_event>`
4. `engine/agent_manager.py` 将解析结果写回文件

### 写回规则

- 角色 `<memory>` → 追加/更新 `memory.md`
- `<status>` → 覆盖更新 `status.md` 对应字段
- `<player>` → 更新 `user.md` 对应字段
- `<triggered>` → 从 `status.md` 中移除已执行条目
- `<add_event>` → 向 `status.md` 中插入新条目

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
3. `user.md`
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
- `memory/vector_store.py` 只做存储层：提供 `get_vector_candidates` / `get_bm25_candidates` 原始候选，pipeline 逻辑不在此处
- 召回排序为：向量相关性与 BM25 相关性先融合，rerank（可选）替换 relevance 信号，最后叠加游戏内时间 recency
- `logs/memory/memory.log` 会记录每轮检索 query 和 top 命中摘要，便于排查召回质量
- `last_recalled_at` 会在命中后更新，并同步写回 `.memory_recall_state.json`
- `rebuild()` 会结合 `.consolidation_state.json` 和 `.memory_recall_state.json` 恢复长期记忆索引与 recall 状态

## 记忆整理

`memory/consolidator.py` 负责角色后台整理：

- 组装整理流程，并调用 `memory/consolidation_inputs.py` 准备 step1 输入
- 归并 `memory.md`
- 提炼 / 更新 `growth.md`（仅角色）
- 去重压缩 `growth.md`（仅角色）
- 顺带精炼 `user.md`（仅角色）
- 按进度同步向量索引

`narrator` 不维护 `memory.md`，也不参与整理。

默认按 `config.toml` 中的 `[memory].consolidation_interval` 控制触发频率。

## 配置来源

### `.env`

- 放密钥、模型 ID、provider 和外部服务 URL
- `RERANK_ENABLED=true` 时才会真正启用 rerank 调用
- narrator / choices / consolidation 都支持各自的独立 LLM 配置，未设置时逐级回退

### `config.toml`

- 放运行时策略参数，例如 Agent temperature、超时、整理频率、向量检索权重
- `[history]` 中的 `history_high` / `history_low` 控制多轮消息高低水位截断

## 存档与重置

由 `game/save_manager.py` 负责：

- `/save`：导出 zip 到 `saves/`
- `/load list`：列出存档
- `/load <序号>`：恢复存档并重建必要索引
- `/reset`：从 `data/templates/{story_id}` 重置运行时数据

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

- `logs/agent/agent_calls_readable.log`：可读调用日志
- `logs/agent/agent_calls.jsonl`：结构化调用日志
- 调用日志会记录 token 用量，以及 provider 支持时的 prompt cache hit / miss / ratio

## 测试约定

- 纯逻辑尽量做成可单测函数
- 使用 `pytest`
- 当前已有：
  - 对话历史相关测试
  - 格式化测试
  - 存档一致性测试
  - 向量库测试
- 涉及向量检索/embedding 的测试可能依赖 `.env` 中的 embedding 配置
