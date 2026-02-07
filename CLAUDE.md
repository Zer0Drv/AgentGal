# CLAUDE.md

多 Agent 角色扮演游戏。每个角色（包括旁白）拥有独立记忆，通过 Tools 自主管理记忆和目标。

## 核心设计

- **记忆系统**：文件（人类可读）+ 向量数据库（可搜索）双存储
- **信息差**：角色各自维护独立的对话历史
- **自主更新**：Agent 通过 Tools 决定何时搜索/更新记忆

## 技术栈

Chainlit + OpenRouter + sqlite-vec + asyncio

## 项目结构

```
me moBot/
├── app.py                      # Chainlit 入口
├── router.py                   # 路由层：判断哪些角色需要回应
├── agents/                     # 所有角色
│   ├── alice/
│   │   ├── soul.md             # 性格定义（手写，只读）
│   │   ├── user.md             # 对玩家的认知（Agent 更新）
│   │   ├── tasks.md            # 当前目标（Agent 更新）
│   │   └── memory/
│   │       ├── memory.md       # 长期记忆（Agent 更新）
│   │       ├── daily/          # 每日摘要
│   │       └── raw/            # YYYY-MM-DD.jsonl 对话流水
│   ├── bob/
│   │   └── ...
│   └── narrator/               # 旁白角色（结构同其他角色）
│       ├── soul.md             # 定义：故事主持人、上帝视角
│       ├── user.md             # 对玩家的认知
│       ├── tasks.md            # 故事主线任务（如"推进到月圆之夜"）
│       └── memory/
│           ├── memory.md       # 故事事件记录
│           └── ...
├── core/
│   ├── llm.py                  # OpenRouter 异步调用
│   ├── vector_store.py         # embedding + sqlite-vec
│   ├── agent_runner.py         # 并行调用 Agent
│   ├── prompt.py               # system prompt 拼装
│   └── tools/                  # 所有 Agent 共享的 Tools
│       ├── search_memory.py
│       ├── update_memory.py
│       ├── update_player_profile.py
│       ├── update_tasks.py
│       └── summarize_today.py
└── .env
```

## 消息路由

由 **narrator（旁白）** 负责路由决策，不再使用独立的 Router LLM。

```
用户输入 → narrator → {"targets": ["alice", "bob", "narrator"]}
```

`targets` 包含谁，谁就回应。

| 用户输入 | targets | 行为 |
|---------|---------|------|
| "大家好啊" | [alice, bob] | Alice、Bob 回应 |
| "悄悄对 Alice 说..." | [alice] | 仅 Alice（私密） |
| "现在几点了？" | [narrator] | 仅旁白回应 |
| "等到明天" | [narrator, alice, bob] | 旁白+角色都回应 |

**narrator 的路由职责**：
- 分析玩家输入，判断哪些角色需要回应
- 在回应开头输出 `TARGETS: [角色名列表]`
- 仅决定**谁参与**，绝不**替角色说话或决定行为**

## 上下文规则

**消息广播**（系统自动）：
- 玩家消息 → 写入所有 targets 的 jsonl
- 角色回应→ 广播给所有其他 targets，写入所有 targets 的 jsonl
- 每个角色从自己的 jsonl 读取最近 N 条作为上下文

**可见性规则**：
- targets 互相可见
- 非 targets 不可见

## System Prompt 组成

所有角色使用相同的 prompt 模板：

```
1. soul.md（性格定义）
2. memory.md（长期记忆）
3. user.md（对玩家的认知）
4. tasks.md（当前目标）
5. 运行时信息：时间、时区、语言
6. Tools 描述
7. 行为指引
```

**narrator 的差异只在 soul.md 和 tasks.md 内容**：
- 告知它是故事主持人
- 告知拥有上帝视角（可读取所有角色记忆）
- 职责：推进故事、控制时间、描述环境

## Agent Tools（所有角色共享）

| Tool | 功能 |
|------|------|
| `search_memory` | 语义搜索自己的向量库 |
| `update_memory` | 追加/编辑自己的 memory.md |
| `update_player_profile` | 更新自己的 user.md |
| `update_tasks` | 更新自己的 tasks.md |
| `summarize_today` | 生成今日摘要 |

**narrator 使用方式**：
- `tasks.md` = 故事主线任务（"推进到月圆之夜"、"制造冲突"）
- `memory.md` = 已发生的故事事件

## 单轮对话流程

```
用户消息
    ↓
调用 narrator，获取路由决策 + 旁白描述
    ↓
解析 narrator 输出的 TARGETS 列表
    ↓
 narrator 的旁白内容写入所有 targets 的 jsonl
    ↓
并行调用每个 target：
  1. 读取自己的 jsonl 历史（包含 narrator 的旁白）
  2. 拼装 system prompt
  3. 调 LLM（流式 + tools）
  4. 响应完成
    ↓
所有角色回应广播到各自 jsonl
    ↓
合并展示给玩家（旁白 + 角色回应）
```

**注意**：narrator 先执行，其输出作为后续角色的上下文输入，但 narrator **不得**在旁白中替其他角色说话或预设行为。

## 文件更新规则

- **jsonl**：系统维护，自动追加
- **md 文件**：Agent 通过 Tools 自主更新
- **soul.md**：手写，只读
- md 文件更新后自动触发 embedding
- 日期使用 ISO 8601（YYYY-MM-DD）
