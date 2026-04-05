# AgentGal

多 Agent 角色扮演 / 叙事游戏系统。项目围绕 **旁白路由 + 角色独立记忆 + 结构化写回 + 向量检索** 构建，当前主要以 **Chainlit** 作为交互入口。

## 项目特点

- **独立记忆**：角色维护自己的 `memory.md / status.md / user.md`，`narrator` 只维护 `status.md` 和单一 raw 历史
- **真实信息差**：消息通过 `visible_to` 控制可见性，不在场的角色不会自动知情
- **旁白驱动路由**：`narrator` 决定谁参与当前回合，并推进场景与时间
- **结构化更新**：Agent 通过 `<update_notes>` 输出记忆/状态更新，由系统统一写回
- **双层记忆**：Markdown 文件负责可读存储，`sqlite-vec` 负责检索

## 当前技术栈

- Python 3.11+
- Chainlit
- OpenAI-compatible LLM client
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

- `LLM_PROVIDER`：当前代码支持 `openai` / `deepseek` / `openrouter`
- `LLM_API_KEY`
- `LLM_MODEL_ID`

如果需要向量记忆检索，还应配置：

- `EMBEDDING_API_URL`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIM`
- `EMBEDDING_API_KEY`
- `RERANK_ENABLED`（可选，开启后才会调用 rerank）
- `RERANK_MODEL` / `RERANK_API_URL` / `RERANK_API_KEY`（可选）

> 说明：如果你想使用 Anthropic 模型，请通过 `openrouter` 路由，而不是把 `LLM_PROVIDER` 直接写成 `anthropic`。

### 3. 启动 Chainlit

```bash
uv run chainlit run app.py
```

默认可在浏览器打开 `http://localhost:8100`。

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

每轮先调用 `narrator`，由它输出：

- `TARGETS: [角色列表]`
- 当前时间、地点、在场信息
- 必要的环境描述或纯 NPC 行为

`narrator` 只能决定谁参与、环境如何变化，**不能替角色说话或行动**。

### 2. 单一历史源 + 可见性过滤

- 对话统一写入 `data/characters/narrator/raw/YYYY-MM-DD.jsonl`
- 每条消息记录 `visible_to`
- 各角色在读取上下文时，按 `visible_to` 过滤出自己能看到的内容

### 3. 结构化写回

Agent 回复由两部分组成：

1. 面向玩家展示的正文
2. 末尾的 `<update_notes>`

系统会解析并写回：

- 角色的 `<memory>` → `memory.md`
- `<status>` → `status.md`
- `<player>` → 追加到 `tmp_user.md`；首次写入时先复制 `user.md` 作为工作草稿，整理后再回写 `user.md`
- `<triggered>` / `<add_event>` → `status.md` 中的事件区块

其中：

- `narrator` 使用 `待触发事件`
- 角色使用 `打算`

### 4. 记忆整理

`memory/consolidator.py` 会定期整理角色记忆：

- 通过 `memory/consolidation_inputs.py` 组装 step1 输入，并把 raw 对话对齐到当前角色视角
- `memory.md`
- `growth.md`（仅角色）
- `user.md`（仅角色）

`narrator` 不维护 `memory.md`，也不参与整理。整理频率由 `config.toml` 中的 `[memory].consolidation_interval` 控制。

### 5. 长期记忆检索

- 只有角色会做向量召回，`narrator` 依赖 `status.md` 中的场景状态和待触发事件推进剧情
- 向量库只索引 `memory.md` 中的长期记忆事件，不再混入其他来源
- `memory/retrieval.py` 会先按规则生成 `vector_query` 与 `bm25_query`，再分别送入向量检索与 BM25 检索
- 检索默认走 hybrid search：向量相关性 + BM25 关键字相关性，再叠加游戏内时间 recency 排序
- `logs/memory/memory.log` 会记录每轮的 query 与 top 命中摘要，便于调试召回效果
- 每个角色会维护 `.memory_recall_state.json`，记录记忆最近一次被想起的游戏日期，供重建和存档恢复

## 关键目录说明

```text
.
├── app.py
├── data/
│   ├── characters/
│   └── templates/
├── engine/
├── game/
├── llm/
├── log_config/
├── memory/
├── prompts/
├── scripts/
└── tests/
```

### 运行时数据

- `data/characters/`：当前游戏状态
- `data/templates/`：故事模板
- `data/vectors.sqlite`：长期记忆向量库
- `saves/`：导出的 zip 存档
- `logs/`：路由、记忆、调用日志

### 角色文件职责

- `soul.md`：角色定义，只读
- `memory.md`：角色长期记忆（仅角色有）
- `status.md`：当前状态 / 打算 / 待触发事件
- `user.md`：角色对玩家的认知（仅角色有）
- `tmp_user.md`：`user.md` 的工作草稿；由 `<player>` 增量写入，整理后删除
- `growth.md`：整理器维护的人格沉淀（仅角色有）
- `.history_window_state.json`：对话历史高低水位窗口 sidecar
- `.consolidation_state.json`：角色整理进度 sidecar
- `.memory_recall_state.json`：角色记忆召回状态 sidecar

## 配置速览

### `.env`

密钥、模型和外部服务地址放在 `.env`。常用变量如下：

| 变量 | 必需 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | 是 | `openai` / `deepseek` / `openrouter` |
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
| `EMBEDDING_API_URL` | 否 | embedding 接口地址 |
| `EMBEDDING_MODEL` | 否 | embedding 模型（默认 `BAAI/bge-m3`） |
| `EMBEDDING_DIM` | 否 | 向量维度（默认 1024） |
| `EMBEDDING_API_KEY` | 否 | embedding API Key |
| `RERANK_ENABLED` | 否 | `true/false`，开启后才会调用 rerank |
| `RERANK_MODEL` | 否 | rerank 模型，未开启时忽略 |
| `RERANK_API_URL` | 否 | rerank 端点 URL |
| `RERANK_API_KEY` | 否 | rerank API Key |

### `config.toml`

运行策略和调参项放在 `config.toml`：

| 键 | 说明 |
|---|---|
| `[memory].consolidation_interval` | 记忆整理触发频率 |
| `[consolidation].temperature` | 整理模型温度 |
| `[consolidation].max_tokens` | 整理输出上限 |
| `[consolidation].growth_dedup_threshold` | `growth.md` 去重阈值 |
| `[vector].search_limit` | 长期记忆召回条数 |
| `[vector].rerank_candidate_multiplier` | rerank 前候选放大倍数 |
| `[vector].relevance_weight` / `[vector].recency_weight` | relevance 与 recency 总权重 |
| `[vector].recency_date_weight` / `[vector].recency_recall_weight` | recency 内部信号权重 |
| `[vector].hybrid_search_enabled` | 是否启用向量 + BM25 混合检索 |
| `[vector].bm25_candidate_limit` | BM25 初筛候选数 |
| `[vector].vector_relevance_weight` / `[vector].bm25_relevance_weight` | hybrid relevance 内部权重 |
| `[agent].run_timeout_seconds` | 单次 Agent 调用超时 |
| `[agent].temperature` | 角色与 narrator 对话温度 |
| `[text].max_actions` / `[text].max_ellipsis` | 回复后处理约束 |

> 对话历史使用高低水位截断，由 `config.toml` 中的 `[history].history_high` / `[history].history_low` 控制；超过 high 时会批量截到 low，并通过 `.history_window_state.json` 维持窗口。

## 存档机制

- `/save` 会将当前角色数据、角色记忆、narrator raw 历史、历史窗口 sidecar、角色整理 sidecar、角色 recall sidecar 等打包为 zip 存入 `saves/`
- `/load <序号>` 会恢复角色目录，并按需要重建向量索引
- `/reset` 会清空当前运行数据，并从 `data/templates/{story_id}` 重建

## 日志与观测

- `logs/agent/agent_calls_readable.log`：可读的请求/响应日志
- `logs/agent/agent_calls.jsonl`：结构化调用日志
- Agent 调用日志会记录 token 使用量，以及 prompt cache 的 hit / miss / ratio（provider 支持时）

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

## 说明

- 当前主入口是 `app.py`
- 如需了解具体 prompt 约束，请查看 `prompts/character_prompt.txt` 和 `prompts/narrator_prompt.txt`

## TODO

- [x] **玩家选项生成**：在角色回复完成后，生成 2-3 个可选行动引导玩家

## FastAPI 前端（fastapi-frontend 分支）

替代 Chainlit 的轻量前端，提供可视化存档管理面板。

### 启动

```bash
uv run uvicorn server:app --reload
```

然后访问 http://localhost:8100

### 新增端点

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
