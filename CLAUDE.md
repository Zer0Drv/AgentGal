# CLAUDE.md

多 Agent 角色扮演游戏。每个角色（包括旁白）拥有独立记忆，通过 Tools 自主管理记忆和目标。

使用 uv 作为项目管理器。

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
├── data/
│   ├── characters/             # 角色数据（运行时）
│   │   ├── lilith/
│   │   │   ├── soul.md         # 性格定义（手写，只读）
│   │   │   ├── memory.md       # 长期记忆（Agent 更新）
│   │   │   ├── status.md       # 当前状态
│   │   │   ├── user.md         # 对玩家的认知
│   │   │   ├── growth.md       # 人格沉淀（整理器生成）
│   │   │   └── raw/            # YYYY-MM-DD.jsonl 对话流水（仅 narrator 有）
│   │   ├── mitsuki/
│   │   │   └── ...             # 结构同 lilith
│   │   └── narrator/           # 旁白角色
│   │       ├── soul.md         # 定义：故事主持人、上帝视角
│   │       ├── memory.md       # 故事事件记录
│   │       └── ...
│   └── templates/              # 角色模板（用于重置游戏）
│       └── ...                 # 结构同 characters
├── engine/                     # 核心引擎
│   ├── agent_manager.py        # Agent 管理
│   ├── config.py               # 配置管理
│   ├── message_router.py       # 消息路由
│   └── response_parser.py      # 响应解析
├── memory/                     # 记忆系统
│   ├── consolidator.py         # 后台记忆整理器
│   ├── file_ops.py             # 文件操作
│   └── vector_store.py         # 向量存储
├── prompts/                    # 系统 Prompt 模板
└── .env
```

## 消息路由

由 **narrator（旁白）** 负责路由决策。

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

## 开发指南

### 代码设计原则

**DRY (Don't Repeat Yourself)**
- 重复逻辑抽为公共函数/模块
- 配置集中管理，禁止硬编码多处
- Agent 共享的 Tools 统一放在 `core/tools/`，禁止各 Agent 自行实现相似功能

**KISS (Keep It Simple, Stupid)**
- 优先使用简单方案，避免过度设计
- 不要为 hypothetical 未来需求添加抽象层
- 三行相似代码优于一个仅用一次的通用封装

**YAGNI (You Aren't Gonna Need It)**
- 只实现当前必需的功能
- 不预置未使用的配置项
- 不添加当前业务不需要的数据库字段

**单一职责 (Single Responsibility)**
- 一个函数只做一件事，控制行数在 50 行以内
- 一个模块只负责一类功能（如 `llm.py` 只处理 OpenRouter 调用）
- Agent 的 soul.md 定义性格，memory.md 存储事实，职责分离

**显式优于隐式 (Explicit over Implicit)**
- 配置参数显式传递，不依赖全局状态
- 函数返回结果明确（成功/失败/异常），不吞掉错误
- 消息路由逻辑在 narrator 中显式声明，不自动推断

### 错误处理

- 网络调用（LLM、embedding）必须加重试机制
- 文件操作先检查路径存在性
- 异常必须携带上下文信息（哪个 Agent、哪一步失败）
- 禁止裸 `except:`，捕获具体异常类型

### 并发与异步

- 所有 IO 操作（LLM 调用、数据库）必须使用 `async/await`
- 多 Agent 并行使用 `asyncio.gather()`，在 `agent_runner.py` 中统一管理
- 共享资源（vector store、文件写入）加锁防止竞争

### 可读性优先

- 变量/函数名要自解释，优先清晰而非简短
- 复杂逻辑添加注释说明「为什么」而非「做什么」
- 类型注解必须标注（Python 3.11+）
- JSON 结构变化必须同步更新文档

### 测试约定

- 纯逻辑抽离为可单元测试的函数（不依赖 Chainlit 上下文）
- Agent 行为通过集成测试验证，模拟 LLM 响应
- 修改记忆系统后必须验证向量搜索准确性
