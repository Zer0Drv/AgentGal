# AgentGal

多 Agent 角色扮演 / 叙事游戏系统。项目围绕 **旁白路由 + 角色独立记忆 + 结构化写回 + 向量检索** 构建，当前主要以 **Chainlit** 作为交互入口。

## 项目特点

- **独立记忆**：每个角色维护自己的 `memory.md / status.md / user.md`
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

> 说明：如果你想使用 Anthropic 模型，请通过 `openrouter` 路由，而不是把 `LLM_PROVIDER` 直接写成 `anthropic`。

### 3. 启动 Chainlit

```bash
uv run chainlit run app.py
```

默认可在浏览器打开 `http://localhost:8000`。

## 使用方式

启动后，系统会让你选择故事。当前内置两套模板：校园故事和现代都市故事。

然后直接在聊天界面输入消息即可。

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

- `<memory>` → `memory.md`
- `<status>` → `status.md`
- `<player>` → `user.md`
- `<triggered>` / `<add_event>` → `status.md` 中的事件区块

其中：

- `narrator` 使用 `待触发事件`
- 角色使用 `打算`

### 4. 记忆整理

`memory/consolidator.py` 会定期整理：

- `memory.md`
- `growth.md`（仅角色）
- `user.md`（仅角色）

并同步更新向量索引。整理频率由 `CONSOLIDATION_INTERVAL` 控制。

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
- `saves/`：导出的 zip 存档
- `logs/`：路由、记忆、调用日志

### 角色文件职责

- `soul.md`：角色定义，只读
- `memory.md`：长期记忆
- `status.md`：当前状态 / 打算 / 待触发事件
- `user.md`：角色对玩家的认知
- `growth.md`：整理器维护的人格沉淀（仅角色有，`narrator` 无）

## 环境变量速览

更完整说明请查看 `.env.example`。常用变量如下：

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
| `RERANK_MODEL` | 否 | rerank 模型，不配置则跳过 rerank |
| `RERANK_API_URL` | 否 | rerank 端点 URL |
| `RERANK_API_KEY` | 否 | rerank API Key |
| `CONSOLIDATION_INTERVAL` | 否 | 记忆整理轮次间隔 |
| `HISTORY_LIMIT_NARRATOR` | 否 | narrator 历史条数 |
| `HISTORY_LIMIT_DEFAULT` | 否 | 角色历史条数 |
| `MAX_CONCURRENT_AGENTS` | 否 | 角色并发上限 |
| `VECTOR_SEARCH_LIMIT` | 否 | 向量检索条数 |
| `AGENT_RUN_TIMEOUT_SECONDS` | 否 | Agent 超时 |
| `MAX_ACTIONS` | 否 | 回复后处理配置 |
| `MAX_ELLIPSIS` | 否 | 回复后处理配置 |

## 存档机制

- `/save` 会将当前角色数据、记忆、历史等打包为 zip 存入 `saves/`
- `/load <序号>` 会恢复角色目录，并按需要重建向量索引
- `/reset` 会清空当前运行数据，并从 `data/templates/{story_id}` 重建

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

- [ ] **新增引导Agent**：在角色回复完成后，引导用户进行下一步的行为