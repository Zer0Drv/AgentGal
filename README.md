# AgentGal

多 Agent 角色扮演 / 叙事游戏系统。项目围绕 **旁白路由 + 角色独立记忆 + 结构化写回 + 向量检索** 构建，以 **FastAPI + Alpine.js 静态前端** 作为交互入口，支持 SSE 流式对话与可视化存档管理。

## 项目特点

- **独立记忆**：角色维护自己的 `memory.jsonl / status.md / user.md`，`narrator` 维护 `status.md` 和单一 raw 历史
- **真实信息差**：消息通过 `visible_to` 控制可见性，不在场的角色不会自动知情
- **旁白驱动路由**：`narrator` 决定谁参与当前回合，并推进场景与时间
- **结构化更新**：Agent 使用 Pydantic 结构化输出，系统直接读取 typed 字段写回文件
- **双层记忆**：Markdown 文件负责可读存储，`sqlite-vec` 负责检索

## 当前技术栈

- Python 3.11+
- FastAPI + SSE（服务端推送）
- Alpine.js + marked（CDN 前端依赖）
- pydantic-ai（统一使用 `PromptedOutput` 结构化输出）
- sqlite-vec + aiosqlite
- asyncio
- `uv` 包管理

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少需要检查 / 配置以下变量：

- `LLM_API_KEY`
- `LLM_MODEL_ID`
- `LLM_PROVIDER`：可选；内置支持 `openai` / `deepseek` / `openrouter`。如果设置了 `LLM_API_URL` 指向 OpenAI-compatible endpoint，可留空。

如果需要向量记忆检索，还应配置：

- `EMBEDDING_API_URL`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIM`
- `EMBEDDING_API_KEY`
- `RERANK_ENABLED`（可选，开启后才会调用 rerank）
- `RERANK_MODEL` / `RERANK_API_URL` / `RERANK_API_KEY`（可选）

> 说明：如果你想使用 Anthropic 模型，请通过 `openrouter` 路由，而不是把 `LLM_PROVIDER` 直接写成 `anthropic`。

### 3. 启动服务

```bash
uv run uvicorn server:app --reload
```

默认可在浏览器打开 `http://localhost:8000`。

## 使用方式

启动后，系统会让你选择故事。当前内置两套模板：校园故事和现代都市故事。

然后直接在聊天界面输入消息即可。每轮角色回应后，系统会生成 2-3 个可选行动供点击，也可以忽略选项直接输入自由文本。

## 聊天命令

- `/save`：导出当前存档到 `saves/`
- `/load list`：查看存档列表
- `/load <序号>`：加载指定存档
- `/reset`：重置当前游戏并重新选择故事模板

## 当前运行机制

### 1. narrator 先路由

每轮先调用 `narrator` 判断玩家是否仍有和角色互动的意愿：有互动意愿就延续当前场景；玩家和角色分别、跳过时间或不再互动时，把玩家导向已有待触发事件，或制造同等作用的即时张力。两种推进都必须让至少一个主要角色当轮可回应。`narrator` 输出：

- `TARGETS: [角色列表]`，至少 1 个有效角色
- 当前时间、地点、在场信息
- 必要的环境描述或纯 NPC 行为

`narrator` 只能决定谁参与、环境如何变化，**不能替角色说话或行动**，也不负责新增未来事件。

### 2. 单一历史源 + 可见性过滤

- 对话统一写入 `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- 每条消息记录 `visible_to`
- 各角色在读取上下文时，按 `visible_to` 过滤出自己能看到的内容
- 对话 prompt 中的“最近对话历史”仍保留玩家和角色消息，但旁白只保留最后一条可见旁白；完整 raw 历史仍用于存档、窗口截断和记忆整理

### 3. 结构化写回

所有结构化 Agent 使用 Pydantic 结构化输出（`PromptedOutput`），系统直接读取 typed 字段写回文件：

- `output.memory` → 追加到 `memory_draft.md`，由后续 consolidation 产出 `EpisodeMemory` 并 append 到 `memory.jsonl`
- `output.status` → `status.md`
- `output.player` → 追加到 `tmp_user.md`；首次写入时先复制 `user.md` 作为工作草稿，整理后再回写 `user.md`
- `output.relations` → 用其他角色**名称**作为 key，覆盖 `relations.md` 中对应角色 section（不写自己，也不写 `player`）
- `output.triggered` / `output.add_event` → `status.md` 中的事件区块

其中：

- `narrator` 使用 `待触发事件`
- 角色使用 `打算`

### 4. state_updater 维护公共状态

回合末后台调用 `state_updater`，它负责更新 narrator 的场景状态、清理已发生/过期的待触发事件，并持续从各角色的 `打算` 中同步公共 `待触发事件`。从角色打算同步出的事件名必须保留角色名，例如 `【美月：顺路的约定】...`。

### 5. 记忆整理

`consolidation/flow.py` 负责角色后台记忆整理：

- 通过 `consolidation/inputs.py` 组装整理输入，并把 raw 对话对齐到当前角色视角
- 读取 `memory_draft.md` + 最近 raw 对话，EpisodeMemoryGenerator 产出完整结构化 `EpisodeMemory` 后 append 到 `memory.jsonl`（append-only），成功即清空 draft
- growth 阶段使用整理出的 `EpisodeMemory` JSON 数组作为 LLM 输入，不再先渲染成 markdown
- 提炼、更新并去重压缩 `growth.md`（仅角色）
- 顺带精炼 `user.md`（仅角色）
- 按进度同步向量索引

`narrator` 不维护 `memory.jsonl`，也不参与整理。整理在角色对话历史窗口触发高水位截断时自动启动（事件驱动，无固定计数器）。

### 6. 长期记忆检索

- 只有角色会做向量召回，`narrator` 依赖 `status.md` 中的场景状态和待触发事件推进当前回合；待触发事件主要由 `state_updater` 从角色打算同步
- 向量库只索引 `memory.jsonl` 中的长期记忆事件，不再混入其他来源；入库时直接保存 `EpisodeMemory` 结构字段，召回时再格式化为 LLM 可读块
- `memory/retrieval.py` 负责完整检索 pipeline：embedding → 向量/BM25 候选 → hybrid 融合 → 可选 rerank → recency 排序 → recall 状态更新
- `storage/vector_store.py` 只做存储层：提供向量与 BM25 原始候选，pipeline 逻辑不放在 storage 层
- 检索默认走 hybrid search：向量相关性 + BM25 关键字相关性，可选 rerank 替换 relevance 信号，最后叠加游戏内时间 recency
- 已配置 Logfire 时，记忆检索会记录每轮的 query 与 top 命中摘要，便于调试召回效果
- `last_recalled_at` 会在命中后更新到 DB；`.memory_recall_state.json` 仅在存档时从 DB 导出，读档重建时作为降级数据源

## 关键目录说明

```text
.
├── server.py
├── data/
│   ├── characters/
│   └── templates/
├── engine/
├── agents/
├── world/
├── consolidation/
├── llm/
├── log_config/
├── memory/
├── prompts/
├── scripts/
├── shared/
├── static/
├── storage/
└── tests/
```

### 运行时数据

- `data/characters/`：当前游戏状态
- `data/templates/`：故事模板
- `data/vectors.sqlite`：长期记忆向量库
- `saves/`：导出的 zip 存档
- 本地 `logs/` 不再默认写入；观测信息通过 Logfire 上报（配置后启用）

### 角色文件职责

- `soul.md`：角色定义，只读；分 `<identity>` / `<goal>` / `<dynamic>` / `<behavior>` / `<voice>` 五段，其中 `<goal>` 用来写角色在故事期内要拿到的具体长期目标
- `memory.jsonl`：角色长期记忆，每行一个结构化 `EpisodeMemory`（`date / time / location / participants / keywords / importance / content / memory_owner / title`），append-only，仅角色有
- `status.md`：当前状态 / 打算 / 待触发事件
- `user.md`：角色对玩家的认知（仅角色有）
- `tmp_user.md`：`user.md` 的工作草稿；由 typed `player` 字段增量写入，整理后删除
- `relations.md`：角色对其他角色（不含 `player`）的当下视角；对玩家的长期视角走 `user.md` 与角色 `status.md` 的「和玩家的关系」，narrator `status.md` 会派生汇总为 `- 角色显示名：关系`
- `growth.md`：整理器维护的人格沉淀（仅角色有）
- `tasks.md`：可选的 narrator 剧情种子文件；当前主流程主要通过 `state_updater` 从角色 `打算` 同步 `待触发事件`
- `.history_window_state.json`：对话历史高低水位窗口 sidecar
- `.consolidation_state.json`：角色整理进度 sidecar
- `.memory_recall_state.json`：角色记忆 recall 快照（仅存档时从 DB 生成，运行期不维护）

## 配置速览

### `.env`

密钥、模型和外部服务地址放在 `.env`。常用变量如下：

| 变量 | 必需 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | 否 | `openai` / `deepseek` / `openrouter`；设置 `LLM_API_URL` 时可留空并按 OpenAI-compatible endpoint 调用 |
| `LLM_API_KEY` | 是 | 主模型 API Key |
| `LLM_MODEL_ID` | 是 | 主模型 ID |
| `LLM_API_URL` | 否 | 自定义 OpenAI 兼容端点，已知 provider 可留空自动解析 |
| `CONSOLIDATION_LLM_PROVIDER` | 否 | 记忆整理器 provider，已知 provider 可省略 URL |
| `CONSOLIDATION_LLM_API_KEY` | 否 | 记忆整理器 API Key，不填则复用 `LLM_API_KEY` |
| `CONSOLIDATION_LLM_MODEL_ID` | 否 | 记忆整理器模型 ID |
| `CONSOLIDATION_LLM_API_URL` | 否 | 记忆整理器端点 URL |
| `NARRATOR_LLM_PROVIDER` | 否 | 旁白 provider，已知 provider 可省略 URL |
| `NARRATOR_LLM_API_KEY` | 否 | 旁白 API Key，不填则复用 `LLM_API_KEY` |
| `NARRATOR_LLM_MODEL_ID` | 否 | 旁白模型 ID |
| `NARRATOR_LLM_API_URL` | 否 | 旁白端点 URL |
| `EPISODE_CLOSURE_DETECTOR_LLM_PROVIDER` | 否 | episode 闭合检测器 provider，已知 provider 可省略 URL |
| `EPISODE_CLOSURE_DETECTOR_LLM_API_KEY` | 否 | episode 闭合检测器 API Key，不填则复用 `LLM_API_KEY` |
| `EPISODE_CLOSURE_DETECTOR_LLM_MODEL_ID` | 否 | episode 闭合检测器模型 ID |
| `EPISODE_CLOSURE_DETECTOR_LLM_API_URL` | 否 | episode 闭合检测器端点 URL |
| `EMBEDDING_API_URL` | 否 | embedding 接口地址 |
| `EMBEDDING_MODEL` | 否 | embedding 模型（默认 `BAAI/bge-m3`） |
| `EMBEDDING_DIM` | 否 | 向量维度（默认 1024） |
| `EMBEDDING_API_KEY` | 否 | embedding API Key |
| `RERANK_ENABLED` | 否 | `true/false`，开启后才会调用 rerank |
| `RERANK_MODEL` | 否 | rerank 模型，未开启时忽略 |
| `RERANK_API_URL` | 否 | rerank 端点 URL |
| `RERANK_API_KEY` | 否 | rerank API Key |
| `LOGFIRE_TOKEN` | 否 | 已配置时上报 Logfire traces，未配置则静默跳过 |
| `LOGFIRE_SEND_TO_LOGFIRE` | 否 | 覆盖 Logfire 是否发送数据 |
| `LOGFIRE_ENVIRONMENT` | 否 | Logfire 环境标签 |

### `config.toml`

运行策略和调参项放在 `config.toml`：

| 键 | 说明 |
|---|---|
| `[consolidation].temperature` | 整理模型温度 |
| `[consolidation].max_tokens` | 整理输出上限；设为 `0` 时不显式传入，但 OpenRouter 整理器会自动回落到 `4096`，避免默认申请超大输出 |
| `[consolidation].player_profile_max_tokens` | `user.md` 整理输出上限，默认用较小值避免高价模型因无限制输出直接失败 |
| `[consolidation].growth_dedup_threshold` | `growth.md` 去重阈值 |
| `[vector].search_limit` | 长期记忆召回条数 |
| `[vector].rerank_candidate_multiplier` | rerank 前候选放大倍数 |
| `[vector].relevance_weight` / `[vector].recency_weight` | relevance 与 recency 总权重 |
| `[vector].recency_date_weight` / `[vector].recency_recall_weight` | recency 内部信号权重 |
| `[vector].hybrid_search_enabled` | 是否启用向量 + BM25 混合检索 |
| `[vector].bm25_candidate_limit` | BM25 初筛候选数 |
| `[vector].vector_relevance_weight` / `[vector].bm25_relevance_weight` | hybrid relevance 内部权重 |
| `[agent].run_timeout_seconds` | 单次 Agent 调用超时，角色、narrator、整理、新角色孵化、离场追补共用 |
| `[agent].choices_timeout_seconds` | 选项生成 Agent 调用超时 |
| `[agent].temperature` | 角色与 narrator 对话温度 |
| `[embedding].request_timeout_seconds` | embedding HTTP 请求超时 |
| `[rerank].request_timeout_seconds` | rerank HTTP 请求超时 |
| `[text].max_actions` / `[text].max_ellipsis` | 回复后处理约束 |

> 对话历史使用高低水位截断，由 `config.toml` 中的 `[history].history_high` / `[history].history_low` 控制；超过 high 时会批量截到 low，通过 `.history_window_state.json` 维持窗口，并触发对应角色的后台记忆整理。

## 存档机制

- `/save` 会将当前角色数据、角色记忆、narrator raw 历史、历史窗口 sidecar、角色 recall sidecar 等打包为 zip 存入 `saves/`
- `/load <序号>` 会恢复角色目录，并按需要重建向量索引
- `/reset` 会清空当前运行数据，并从 `data/templates/{story_id}` 重建

## 日志与观测

- 已配置 Logfire（本地 CLI 或 `LOGFIRE_TOKEN`）时，上报 PydanticAI traces、token/cost、路由事件、记忆检索与整理事件；未配置时静默跳过。
- 路由与记忆模块仍使用标准 logger 作为业务事件入口，但默认不再写入本地 `logs/*.log` 轮转文件。

## 测试

运行全部测试：

```bash
uv run pytest
```

当前仓库中可见的测试包括：

- `tests/test_conversation_history.py`
- `tests/test_format_history.py`
- `tests/test_save_load_consistency.py`
- `tests/test_vector_store.py`

> 注意：`test_vector_store.py` 依赖 embedding 相关环境变量，未配置时会跳过。

## 常用脚本

- `scripts/check_status.py`：检查状态文件
- `scripts/consolidate_memories.py`：批量整理记忆
- `scripts/consolidate_one_date.py`：整理单日记忆
- `scripts/consolidate_user_md.py`：整理用户画像
- `scripts/fix_narrator_status.py`：修复 narrator 状态文件
- `scripts/query_vectors.py`：查询向量库
- `scripts/rebuild_vectors.py`：重建向量索引

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `GET /` | — | 游戏主界面 |
| `GET /api/init` | — | 检查存档状态 |
| `GET /api/stories` | — | 列出故事模板 |
| `POST /api/new_game` | — | 开始新游戏 |
| `POST /api/chat` | SSE 流式 | 核心对话 |
| `GET /api/saves` | — | 列出所有存档 |
| `POST /api/save` | — | 创建存档 |
| `POST /api/load` | — | 加载存档 |
| `POST /api/reset` | — | 重置游戏 |
