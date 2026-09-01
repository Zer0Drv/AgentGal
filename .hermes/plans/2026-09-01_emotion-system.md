# AgentGal 情绪系统（Emotion System）实现计划

日期：2026-09-01
分支：`feature/emotion-system`

## Goal

给 AgentGal 角色增加三层情绪系统，让角色拥有：
1. **内心层（驱动力状态）**：`mood.json` 记录 孤单/精力/亲近感 三维（0~1），随真实时间演化，受对话内容影响（`emotional_impact`），驱动主动联系门控
2. **表现层（情绪标签）**：角色每轮输出 `emotion` 标签，写入 `emotions.jsonl` 轨迹；标签可映射为 Live2D/模型表情参数（映射表落地为纯数据，前端接入后续做）
3. **注入层**：角色 prompt 注入 `<inner_state>` 文字提示（hint），让 LLM 感知自己内心状态；输出规则增加 emotion / emotional_impact

设计原则：**LLM 只输出语义（标签/符号方向），数值永远由代码算**（映射表 + delta 表）。

## 本次范围（MVP）

- ✅ repository 层：`emotion_state.py`（mood.json 读写 + evolve + impact 合并 + 泊松门控纯函数）、`emotion_store.py`（emotions.jsonl 轨迹读写）
- ✅ app 层：`emotion_mapper.py`（标签→表情参数表 + 强度缩放 + IMPACT_DELTA 表）、`llm_schema.py` 扩展、prompt 注入、回合回调
- ✅ 存档：`save_manager.py` 包含 mood.json / emotions.jsonl
- ✅ 测试：新增单元测试
- ❌ 不做（后续里程碑）：主动联系 cron 集成 / narrator 主动消息流程 / Live2D 前端接入 / 情绪曲线 UI

## 设计要点

### 内心层 mood.json（每角色一份，真实时间演化）

```json
{
  "version": 1,
  "mood": {"loneliness": 0.4, "energy": 0.65, "affection": 0.5},
  "last_evolve_at": "2026-09-01T10:00:00+08:00",
  "last_interaction_at": null
}
```

- `evolve()`：按真实时间漂移——孤单空闲 +0.05/h；精力白天 +0.03/h、夜晚 -0.08/h；亲近感 6h 无接触 -0.01/h
- `note_user()`：玩家互动回调——孤单 -0.3、亲近感 +0.1、精力 +0.05（基准）
- `apply_impact()`：合并 LLM 的 emotional_impact 符号 delta
- `gate_proactive()`：泊松门控纯函数（安静时段 9:00-22:00、每日上限 4 次、λ=base×(1+0.45×孤单+0.2×亲近感)、精力缩放）——只提供判定，不接 cron

### emotional_impact（LLM 输出 → 内心层）

```python
class LLMEmotionalImpact(BaseModel):
    affection: Literal["++", "+", "0", "-", "--"] = "0"
    loneliness: Literal["++", "+", "0", "-", "--"] = "0"
    energy: Literal["++", "+", "0", "-", "--"] = "0"
    reason: str = ""
```

代码映射 IMPACT_DELTA：`+`→±0.12，`++`→±0.28（clamp 0~1）。energy 字段默认 "0"，仅明显消耗/恢复时输出。

### 情绪标签（表现层）

- `LLMCharacterOutput.emotion: str`（如 "开心" / "有点害羞"；空 = 无变化）
- 写入 `emotions.jsonl` 每行：`{"turn": int, "date": str, "time": str, "emotion": str, "reason": str}`
- 映射表：标签→表情参数（mouth_smile / eye_open / brow_anger / blush / action）；强度前缀（有点/有些/很/非常/超级）剥离 → 基础词查表 → 参数缩放

### prompt 注入

- `build_user_message`（character 分支）注入：
  ```
  <inner_state>
  你此刻：{hint 文字}（孤单中/精力充沛/和他很亲近…）
  </inner_state>
  ```
- CHARACTER prompt `<format>` 增加 emotion / emotional_impact 字段说明 + `<rules>` 增加情绪输出规则

### 回合时序（conversation_service.run_turn）

```
build prompt（含 inner_state hint）
  → run agent → LLMCharacterOutput（含 emotion + emotional_impact）
  → _apply_updates 追加：
      emotion 非空 → emotion_store.append()
      emotional_impact 非空 → emotion_state.apply_impact()
      note_user() 基准回调 + evolve()
```

## 文件改动清单

| 文件 | 操作 | 内容 |
|---|---|---|
| `repository/emotion_state.py` | 新增 | mood.json IO + evolve + note_user + apply_impact + gate_proactive + hint_for_mood |
| `repository/emotion_store.py` | 新增 | emotions.jsonl 追加/读取最近/读取全部 |
| `app/emotion_mapper.py` | 新增 | EMOTION_PARAMS 表 + 强度缩放 + IMPACT_DELTA 表 |
| `app/llm_schema.py` | 修改 | + LLMEmotionalImpact + LLMCharacterOutput.emotion/emotional_impact |
| `app/conversation_service.py` | 修改 | run_turn 回合回调 + _apply_updates 追加情绪写回 |
| `app/prompt_builder.py` | 修改 | character 分支注入 `<inner_state>` |
| `prompts/runtime_prompts.py` | 修改 | CHARACTER 增加 emotion/emotional_impact 输出规则 + inner_state 读取说明 |
| `models/__init__.py` | 修改 | 导出 EmotionSnapshot 等（如需） |
| `repository/save_manager.py` | 修改 | `_get_agent_save_files` 增加 emotions.jsonl / mood.json |
| `data/templates/*` | 不动 | 首次读取时自动初始化默认状态 |
| `tests/test_emotion_state.py` | 新增 | evolve/note_user/impact/gate/hint 单测 |
| `tests/test_emotion_store.py` | 新增 | emotions.jsonl 读写单测 |
| `tests/test_emotion_mapper.py` | 新增 | 映射/强度/IMPACT_DELTA 单测 |

## 验证

```bash
unset UV_PROJECT_ENVIRONMENT && uv run pytest tests/test_emotion_*.py -q   # 新测试
unset UV_PROJECT_ENVIRONMENT && uv run pytest -q                          # 全量回归
```

## 风险与决策记录

- **时间源**：内心层用真实时间（datetime.now + timezone），与游戏内时间并行——陪伴场景需求，后续转型时再对齐
- **energy 是否交给 LLM**：LLM 可输出 energy 方向（对话可能"回血"或"耗神"），prompt 强调仅明显时输出
- **情绪词表**：开放词表 + prompt 示例（开心/安心/害羞/紧张/失落/委屈/生气/害怕/平静…）；映射表未命中 → 默认平静表情
- **proactive 门控**：只提供纯函数 + 说明，不接 cron/服务定时器（避免未用代码）
