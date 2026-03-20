# RAG 优化方案

> 当前进度：已完成 `vector_store.py` 的 jieba 中文分词接入，以及 `pyproject.toml` 的 `jieba` 依赖声明；其余项仍是后续计划。

## 问题诊断

当前 RAG pipeline 存在明显的召回不准确问题：相关记忆在需要时未被检索到。

### 根因分析

**查询侧：**
- query = `user_input + scene_summary`，scene_summary 是噪声：当前场景信息已在 prompt 上下文中，拼入 query 反而把检索方向拉向"已经知道的东西"
- BM25 使用 unicode61 逐字拆分中文，"小巷"变成"小" OR "巷"，区分度极低
- 每条 chunk 都有 `**时间**`、`**地点**` 等模板字段，这些高频低 IDF token 稀释了有意义的关键词

**文档侧：**
- FTS5 只有 `content` 一列，地点等关键属性被长叙事内容淹没
- chunk 缺少语义索引：用户查询语言（"在小巷""第一次主动"）和记忆存储语言（详细叙事）之间有 gap
- consolidation 倾向于合并同场景事件，导致部分 chunk 过长、embedding 被主体内容主导

## 优化方案

### 1. 查询侧

#### Vector query
- **只用 user_input**，不再拼接 scene_summary
- 理由：scene_summary 的场景值会把 embedding 拉向当前场景方向，稀释用户真正的查询意图；当前场景信息已在 prompt 上下文（conversation history + status.md）中

#### BM25 query
- **只用 user_input**，不再拼接 scene_summary
- 使用 **jieba 分词**替代当前逐字拆分
- 效果：`"小巷"` 作为一个完整 token，IDF 远高于单字 `"小"` `"巷"`

#### 分词一致性
- FTS5 入库和 BM25 查询两侧都使用 jieba，保证 token 对齐
- FTS5 继续使用 unicode61 tokenizer（按空格拆预分词结果）

### 2. 文档侧

#### DB schema 变更

`memory_chunks` 表新增：
- `keywords TEXT` — LLM 生成的查询关键字（5 词以内）
- `importance INTEGER` — LLM 生成的重要性评分（1-5），用于检索评分阶段加权

Vector embedding 只 embed `content`，不含 keywords。

#### FTS5 schema

```sql
CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(
    content, keywords,
    tokenize='unicode61'
)
```

- BM25 排序使用列权重：`bm25(memory_chunks_fts, 1.0, 3.0)`
- keywords 列权重 3.0 为起点，待实测调优
- keywords 列短（5 词）→ 高 TF 密度 + 无长度惩罚 + 列权重放大 = 强匹配信号

#### keywords 列设计

**目标：** 弥合查询语言与存储语言的 gap。提供 content 全文检索不容易命中的抽象概念词。

**提取规则（5 词以内，显式分类）：**

| 类别 | 数量 | 说明 | 示例 |
|---|---|---|---|
| 地点 | 1 词 | 从 `**地点**` 字段提取核心地名 | 小巷、咖啡馆、公园 |
| 事件类型 | 1-2 词 | 概括这件事的性质 | 初吻、争吵、告白、约会、道歉 |
| 情绪 | 1-2 词 | 角色的核心感受 | 心动、紧张、愤怒、失望、安心 |

**示例：**
```
content: （500字亲密互动叙事）
keywords: 小巷 初次亲密 突破边界 紧张 心跳
```

#### importance 评分设计

**目标：** 让关系转折点、重大事件等关键记忆在检索时不容易被 recency 压下去。

**评分标准（1-5，显式锚定）：**

| 分数 | 含义 | 示例 |
|---|---|---|
| 5 | 关系里程碑或重大转折 | 初吻、告白、严重争吵、分手、和好 |
| 4 | 显著的情感突破或认知变化 | 第一次主动、说出心里话、建立信任 |
| 3 | 有意义的互动，加深了关系 | 深入聊天、一起经历某事、互相帮助 |
| 2 | 普通日常互动 | 一起吃饭、闲聊、路上偶遇 |
| 1 | 极低信息量的例行事件 | 打招呼、简单寒暄 |

**检索评分公式：**

relevance、recency、importance 三个信号并列加权：

```
normalized_importance = importance / 5
score = relevance_weight × relevance + recency_weight × recency + importance_weight × normalized_importance
```

- 三个 weight 在 `config.toml` 中配置，计算时自动归一化使总和为 1
- 建议起点：`relevance_weight=0.5, recency_weight=0.2, importance_weight=0.3`
- importance=1 → 0.2，importance=5 → 1.0

### 3. Consolidation pipeline 变更

在现有 step1（记忆合并）和 step2（growth 提取）之间新增 **step 1.5**：

```
step 1:   记忆合并整理（现有，不变）
step 1.5: keywords 生成（新增）
step 2:   growth 提取（现有，不变）
step 3:   growth 去重（现有，不变）
```

#### Step 1.5 设计
- **输入：** step 1 输出的结构化记忆 chunks
- **输出：** 每条 chunk 对应的 keywords（5 词以内）+ importance 评分（1-5），放入 memory.md 的 chunk 里
- **模型：** 可使用与 step 1 相同的模型（120B 级别足够）
- **任务性质：** 纯机械抽象，不需要复杂推理
- **prompt 要求：** 显式分类规则 + few-shot 示例，规则越明确越好

**输出格式（每条 chunk 一组）：**
```
<chunk_meta>
时间：10月5日 晚上
keywords：小巷 初次亲密 突破边界 紧张 心跳
importance：5
</chunk_meta>
```

用时间字段作为 chunk 的对齐锚点（与 step 1 输出的 `**时间**` 匹配）。

## 实现清单

- [ ] `retrieval.py`: vector query 和 BM25 query 都改为只用 user_input
- [x] `vector_store.py`: `_tokenize_for_fts` 替换为 jieba 分词；`_build_fts_match_query` 同步改为 jieba
- [ ] `vector_store.py`: `memory_chunks` 表加 `keywords TEXT` 列和 `importance INTEGER` 列
- [ ] `vector_store.py`: FTS5 表重建为 `fts5(content, keywords)`，入库时 keywords 也走 jieba 分词
- [ ] `vector_store.py`: `get_bm25_candidates` 使用 `bm25(memory_chunks_fts, 1.0, 3.0)` 排序
- [ ] `consolidator.py`: 新增 step 1.5 调用，解析 keywords 输出
- [ ] `prompts/`: 新增 step 1.5 的 prompt 文件
- [ ] `vector_store.py`: `add()` / `rebuild()` 路径适配 keywords 字段
- [ ] `retrieval.py`: `apply_recency` 或新函数中加入 importance 加权
- [ ] 现有向量库需要 rebuild（分词方式变更 + schema 变更）
- [ ] `config.toml`: 新增 `importance_weight` 参数
- [x] `pyproject.toml`: 添加 jieba 依赖
