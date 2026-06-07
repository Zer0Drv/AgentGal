"""status.md 的已知语义字段名常量。

status.md 字段仍是文件驱动 / 作者可自定义（白名单现读 ## 标题）；这里只收拢
代码里频繁引用的语义字段名，避免散落魔法字符串。不构成 schema，不做结构化建模。
"""

# 角色
IDENTITY = "身份"
MOOD = "心境"
CONCERN = "在意的事"
PLANS = "打算"  # 事件段（逐条维护，禁止整段覆写）

# 角色 + narrator 共有
PLAYER_RELATION = "和玩家的关系"

# narrator
CURRENT_TIME = "当前时间"
SCENE = "场景"
CHARACTER_LOCATIONS = "角色位置"
NARRATIVE_FOCUS = "叙事焦点"
RECENT_WORLD_EVENT = "最近世界事件"
PENDING_EVENTS = "待触发事件"  # 事件段

# 事件段：只能经 add_event / mark_triggered 逐条维护，禁止从字段写回整段覆写
EVENT_SECTIONS = frozenset({PLANS, PENDING_EVENTS})

# state_updater 评估剧情时读取的角色字段顺序
CHARACTER_STATUS_FIELDS = (IDENTITY, MOOD, CONCERN, PLANS)
