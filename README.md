# MemoBot

多 Agent 角色扮演游戏系统。每个角色拥有独立记忆，通过 Tools 自主管理记忆和目标，形成真实的信息差。

## 项目意图

传统角色扮演游戏中，NPC 通常共享同一个世界观数据库，缺乏真实的信息不对等。MemoBot 尝试模拟更真实的社交情境：

- **独立记忆**：每个角色维护自己的对话历史和认知，不会自动知道其他角色的秘密
- **自主管理**：Agent 自己决定何时记录重要信息、更新对他人的印象
- **信息差设计**：消息按可见性广播，未在场的角色不会收到那段对话
- **故事推进**：旁白（narrator）作为上帝视角的主持人，控制时间、环境、路由决策

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

### 3. 启动

```bash
uv run chainlit run app.py
```

打开 http://localhost:8000

## 使用方法

在聊天界面输入消息。

## 配置说明

| 环境变量 | 必需 | 说明 |
|---------|------|------|
| `LLM_API_KEY` | 是 | DeepSeek API Key |
| `MODEL_ID` | 否 | 模型 ID，默认 `deepseek-chat` |
| `AGENT_LOG_ENABLED` | 否 | 是否记录 Agent 调用日志，默认 `true` |
| `CONSOLIDATION_INTERVAL` | 否 | 记忆整理间隔（轮数），默认 `5` |

## 核心机制

### 记忆系统

- **对话历史**（`raw/YYYY-MM-DD.jsonl`）：自动追加，包含该角色可见的所有消息
- **长期记忆**（`memory.md`）：Agent 通过 `update_memory` Tool 自主更新，记录重要事件和情感
- **记忆整理**：每 N 轮自动触发，压缩冗长历史

## 技术栈

- **Chainlit**: Web UI 框架
- **Agno**: Agent 框架
- **DeepSeek**: LLM 模型