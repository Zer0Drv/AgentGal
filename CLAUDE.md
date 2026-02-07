# CLAUDE.md

此文件为 Claude Code (claude.ai/code) 提供本项目的开发指导。

## 项目概述

谈话游戏是一个基于多 Agent 架构的角色扮演游戏。核心设计理念：

- **解决记忆问题**：每个角色拥有长期记忆系统，记住与玩家的所有过往对话
- **信息差机制**：角色间不沟通，各自维护独立的现实认知
- **自主更新**：记忆读写和配置更新作为 Tool 暴露给 LLM，由 Agent 自主决定何时使用

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 前端 | Chainlit | 专为 LLM 聊天设计，原生支持流式输出和 tool calling |
| LLM 接入 | openai SDK → OpenRouter | 兼容 OpenAI API，改 `base_url` 即可 |
| Embedding | OpenAI API (`text-embedding-3-small`) | 通过 API 调用，轻量无本地模型依赖 |
| 向量存储 | sqlite-vec | 纯 SQLite 扩展，轻量 |
| 异步 | asyncio + aiosqlite | 全异步架构，Chainlit 原生异步 |
| 包管理 | uv | 快速 Python 包管理器，替代 pip/venv |
| 环境变量 | python-dotenv | 管理 API Key 等敏感信息 |

## 项目结构

```
MemoBot/
├── app.py                      # Chainlit 入口
├── router.py                   # 消息路由层（轻量级 LLM 判断消息目标）
├── agents/                     # 所有角色目录
│   ├── alice/
│   │   ├── soul.md             # 角色性格：名字、说话风格、性格特征、价值观（手写）
│   │   ├── user.md             # 角色对玩家的认知（Agent 自主更新）
│   │   ├── tasks.md            # 角色当前目标/待办事项（Agent 自主更新）
│   │   └── memory/
│   │       ├── Memory.md       # 长期记忆（Agent 自主更新）
│   │       ├── daily/          # YYYY-MM-DD.md 每日对话摘要
│   │       ├── raw/            # YYYY-MM-DD.jsonl 原始对话记录
│   │       └── ...             # （同其他角色共享 vector.db）
│   ├── bob/
│   │   └── ...
│   └── cindy/
│       └── ...
├── core/
│   ├── __init__.py
│   ├── llm.py                  # OpenRouter 异步调用，支持 tool calling + 流式输出
│   ├── vector_store.py         # embedding + sqlite-vec 检索 + on_file_updated() 自动入库
│   ├── agent_runner.py         # 并行调用多个 Agent，聚合响应
│   ├── chunker.py              # md/jsonl 文件分块逻辑
│   ├── prompt.py               # system prompt 模板拼装
│   └── tools/
│       ├── __init__.py         # 注册所有 tools，导出 schema 列表 + handler 映射
│       ├── search_memory.py    # 语义搜索向量库
│       ├── update_memory.py    # 追加/编辑 Memory.md
│       ├── update_player_profile.py # 更新对玩家的认知和态度
│       ├── summarize_today.py  # 生成今日对话摘要
│       └── update_tasks.py     # 更新角色目标/待办事项
├── pyproject.toml
├── .env                        # OPENROUTER_API_KEY, OPENAI_API_KEY
└── CLAUDE.md
```

## 消息路由层

```
用户输入
   ↓
Router LLM（轻量级，如 gpt-4o-mini）
   ↓
{"targets": ["alice", "bob", "cindy"]}
```

**路由逻辑**：
- `targets` 长度为 1 → 私密消息，仅该角色回应
- `targets` 包含多个/所有角色 → 公开消息，所有人并行回应

**示例**：

| 用户输入 | 路由结果 | 行为 |
|---------|---------|------|
| "大家好啊" | `{"targets": ["alice", "bob", "cindy"]}` | 三人各自独立回应 |
| "悄悄对 Alice 说..." | `{"targets": ["alice"]}` | 仅 Alice 回应 |
| "Alice，你觉得呢？" | `{"targets": ["alice", "bob", "cindy"]}` | 公开消息，三人都能听到 |

## 伪群聊交互模式

```
玩家："大家好啊"
    ↓
┌───┼───┐
↓   ↓   ↓
Alice Bob Cindy  （并行调用，共享公开上下文）
↓   ↓   ↓
"嘿！" "嗯" "你好～"
    ↓
（所有回应进入共享的群聊历史）
    ↓
玩家："Bob 你刚才说什么？"
    ↓
Alice 知道 Bob 说了"嗯"，可以参与讨论
```

**上下文隔离规则**：
- **私密消息**：仅写入目标角色的个人历史，其他角色不可见
- **公开消息**：写入所有在场角色的共享群聊历史
- **混合场景**：先私密告诉 Alice 一个秘密，再公开问大家问题 → Alice 知道秘密，其他人不知道

## Agent Tools

每个角色的 LLM 通过 function calling 自主决定何时调用这些工具：

| Tool | 功能 | 触发场景 |
|------|------|----------|
| `search_memory` | 语义搜索向量库，返回 Top-K 相关记忆 | 用户提到过去的事、需要回忆上下文 |
| `update_memory` | 追加/编辑 Memory.md 中的特定段落 | 发现新的重要信息值得长期记住 |
| `update_player_profile` | 更新 user.md 的特定字段 | 发现玩家新信息、改变对玩家的态度 |
| `summarize_today` | 生成今日对话摘要，写入 daily | 对话较长或重要时，主动总结今日内容 |
| `update_tasks` | 更新 tasks.md 中的目标状态 | 完成目标、添加新目标、修改优先级 |

## 记忆系统（双存储架构）

每个角色拥有独立的记忆系统：

### 文件存储（人类可读）

- `agents/{name}/user.md` — 角色对玩家的认知和印象（Agent 自主更新）
- `agents/{name}/tasks.md` — 角色当前目标/待办事项，推动故事发展（Agent 自主更新）
- `agents/{name}/memory/Memory.md` — 长期事实和知识（Agent 自主更新）
- `agents/{name}/memory/daily/YYYY-MM-DD.md` — 每日对话摘要，每天一个文件
- `agents/{name}/memory/raw/YYYY-MM-DD.jsonl` — 原始对话记录，键值对格式

### 向量数据库（可搜索）

- `agents/{name}/memory/vector.db` — SQLite + sqlite-vec 扩展
- 对 md 文件内容进行分块和嵌入
- 支持语义搜索相关记忆
- **自动触发**：任何 md 文件被创建或更新时，自动对变更内容做 embedding 入库

## System Prompt 组成

```
1. soul.md 内容（角色性格定义）
2. user.md 内容（该角色对玩家的认知）
3. tasks.md 内容（角色当前目标/待办事项）
4. 运行时信息（关键，影响角色行为）：
   - 当前日期时间（ISO 8601，游戏内时间）
   - 时区
   - 系统语言
   - 时间对角色的意义（如"距离月圆之夜还有3天"）
5. 可用 tools 描述（名称、参数、用途）
6. 行为指引（何时搜索记忆、何时存储、何时更新目标）
```

## 核心流程

### 单轮对话流程

```
用户消息
    ↓
Router 判断 targets
    ↓
并行调用每个目标 Agent：
  1. 自动追加 raw jsonl（系统行为，不需要 LLM 决定）
  2. 检查对话历史 token 数，接近上限时触发压缩
  3. 拼装 system prompt（soul + user + runtime info + tools）
  4. 调 LLM（带 tools，流式输出）
  5. LLM 自主决定是否调用 search_memory / update_memory / update_player_profile / update_tasks / summarize_today 等
  6. 处理 tool calls → 返回结果 → LLM 继续生成
  7. 响应完成
    ↓
合并所有 Agent 响应，展示给玩家
```

### 上下文压缩流程

当某个角色的对话历史接近 context window 上限时：
1. 调 LLM 对当前对话历史生成摘要
2. 摘要写入 `agents/{name}/memory/daily/YYYY-MM-DD.md`
3. 用摘要消息替换旧的对话历史，释放上下文空间
4. 自动触发 embedding 入库

### Embedding 自动同步

- md 文件有任何写入（创建/更新）→ 自动触发 `vector_store.on_file_updated(path)`
- 增量 embedding，只处理变更部分，不全量重建
- 分块策略：按语义边界分割，约 500 tokens，重叠 100 tokens
- 元数据：来源文件、时间戳

## 文件更新规则

- 每日 md/jsonl 文件只追加，不覆盖
- Memory.md 编辑应精准，只更新特定部分，不重写整个文件
- 所有日期使用 ISO 8601 格式（YYYY-MM-DD）
- player_profile (user.md) 在聊天过程中可由 Agent 即时修改
- tasks.md 在聊天过程中可由 Agent 即时修改（完成目标、添加新目标）
- soul.md 由设计者手写，Agent 只读，不可修改