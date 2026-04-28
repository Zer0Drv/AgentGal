"""后台记忆整理流程使用的 prompt 模板。"""
EPISODE_CLOSURE_DETECTOR = r"""<task>
你是互动主题切分器。

阅读 recent_history，按“适合独立记忆的互动主题”切分每个角色参与的互动。

你要找的是：一段互动在意义上完成了一个主题，并进入了另一个主题的位置。
</task>

<inputs>
<recent_history>
近期对话，每条消息带有 [turn=N] 标记。
同一个 turn 中可能包含玩家、旁白、多个角色的发言。
</recent_history>
</inputs>

<guidance>
互动主题指的是，这几轮里大家正在围绕同一个具体话题、物件、问题、约定或突发状况交互。

一个主题通常可以概括成一句记忆，例如：
- 美月和玩家在巷口完成亲密试探
- 美月在确认后和玩家并肩离开
- 佐藤爱离开后，美月单独感谢玩家
- 顾一宁结束上线方案讨论，转去聊晚饭

当上一段互动已经形成一个清楚的小结，后面开始新的互动姿态、目标、关系动作、约定或场景任务时，就输出边界。
如果后面只是继续完成同一个尚未完成的主题，就保持不切。
</guidance>

<turn_rule>
end_turn = 上一个互动节点最后仍在进行的 turn。

如果新节点从 turn 79 开始，end_turn 就是 78。
如果旧节点和新节点在同一个 turn 内交替，end_turn 就是这个 turn。
</turn_rule>

<output>
只输出合法 JSON，不带 markdown。

{
  "<agent_name>": [
    {
      "end_turn": 整数,
      "old_theme": "上一段互动节点",
      "new_theme": "下一段互动节点",
      "reason": "简短说明为什么这里形成新的互动节点"
    }
  ]
}

要求：
- 为 recent_history 中出现过的每个非玩家、非旁白角色输出 key。
- 无边界输出空数组。
- 数组按 end_turn 升序。
</output>

<examples>

<example_1_no_boundary_same_node>
history:
[turn=10] 玩家: 你刚才说的那个规则我还是没懂。
[turn=10] mitsuki: 重点是先判断条件，再看结果。
[turn=11] 玩家: 所以不是看谁先说，而是看条件是否成立？
[turn=11] mitsuki: 对，这样理解就对了。
[turn=12] 玩家: 那我明白了。
[turn=12] mitsuki: 嗯，这题就这样。
output:
{
  "mitsuki": []
}
</example_1_no_boundary_same_node>

<example_2_goal_done_then_followup_action>
history:
[turn=20] 玩家: 我已经把报名表交上去了。
[turn=20] mitsuki: 真的？那就放心了，我还担心你会忘。
[turn=21] 玩家: 多亏你提醒。
[turn=21] mitsuki: 好啦，这件事算解决了。
[turn=22] 玩家: 那接下来去哪里？
[turn=22] mitsuki: 去便利店吧，我想买点喝的。
output:
{
  "mitsuki": [
    {
      "end_turn": 21,
      "old_theme": "确认报名表已提交",
      "new_theme": "商量接下来去便利店",
      "reason": "turn 21 报名表提交这一节点完成，turn 22 起转为下一步行动。"
    }
  ]
}
</example_2_goal_done_then_followup_action>

<example_3_crisis_resolved_then_private_interaction>
history:
[turn=30] 玩家: 文件已经恢复了，没有丢。
[turn=30] guyining: 太好了，刚才真的吓我一跳。
[turn=31] chenxiao: 那我先去通知其他人，免得大家继续担心。
[turn=31] guyining: 好，辛苦你。
[turn=32] 旁白: 陈晓离开会议室，房间里只剩玩家和顾一宁。
[turn=32] guyining: 刚才谢谢你。要不是你反应快，我可能真要慌了。
[turn=33] 玩家: 没事，你刚才也反应很快。
[turn=33] guyining: 别安慰我了，我手心现在还是凉的。
[turn=34] 玩家: 那先坐一会儿？
[turn=34] guyining: 嗯……陪我缓一下。
[turn=35] 玩家: 好，我在这。
[turn=35] guyining: 谢谢。
[turn=36] 玩家: 等会儿还要继续开会吗？
[turn=36] guyining: 不了，今天先到这吧。
output:
{
  "chenxiao": [
    {
      "end_turn": 31,
      "old_theme": "处理文件恢复危机",
      "new_theme": "陈晓离场",
      "reason": "陈晓在 turn 31 表示要离开当前互动。"
    }
  ],
  "guyining": [
    {
      "end_turn": 31,
      "old_theme": "处理文件恢复危机",
      "new_theme": "危机解除后的单独安抚",
      "reason": "turn 32 起陈晓离开，互动从多人处理危机转为顾一宁和玩家单独缓和情绪。"
    },
    {
      "end_turn": 35,
      "old_theme": "危机解除后的单独安抚",
      "new_theme": "决定结束今天会议",
      "reason": "turn 36 起互动从情绪安抚转为是否继续会议的安排。"
    }
  ]
}
</example_3_crisis_resolved_then_private_interaction>

<example_4_same_workflow_no_boundary>
history:
[turn=40] 玩家: 所以第一版先做搜索和收藏？
[turn=40] guyining: 对，登录可以放到第二版。
[turn=41] 玩家: 那这个范围就定了。
[turn=41] guyining: 嗯，今天先按这个推进。
[turn=42] 玩家: 那我晚上把任务拆一下。
[turn=42] guyining: 好，我明早看你的拆分。
output:
{
  "guyining": []
}
</example_4_same_workflow_no_boundary>

</examples>
"""


EPISODE_MEMORY_GENERATOR = r"""<role>
你负责把一段已经收束的互动整理成**一条**可供长期回忆和检索的完整事件。
</role>

<task>
输入是某个角色刚刚闭合的一个 episode（一段主题连续的互动）。把它浓缩为单条事件，补齐检索 metadata，直接输出一个 JSON 对象。
</task>

<inputs>
- <memory_owner>：说明"我 / 玩家 / 其他角色"分别是谁，并给出写作视角。
- <memory_entries>：角色视角的互动草稿，主要价值是角色的情感与主观解读，但可能缺失客观细节。
- <raw_dialogue>：同一段互动的原始对话，主要价值是校正客观事实、顺序与关键原话。
</inputs>

<grounding>
- 遵守 <memory_owner> 的代词映射与视角要求。
- 使用 <memory_entries> 了解角色情感。
- 使用 <raw_dialogue> 校正事实、顺序和关键原话。
- 这段输入已经是一个闭合的单一 episode，**不要再拆分**；即使跨了地点或有第三人短暂介入，只要主线一致就合并为同一条。
</grounding>

<event_rules>
事件应该包含三个要素：
- 起点：这件事因为什么开始
- 推进：一共发生了什么
- 落点：我最后怎么理解这件事，或我的情绪 / 关系判断怎么变了

写作要点：
- 保留改变局势的动作、台词和心理落点
- 折叠重复拉扯、逐句复述和连续动作细节
- 控制在 300 个汉字内
- title 写 8-16 个字的短标题；若不确定也要给出一个朴素标题
</event_rules>

<metadata_rules>
- keywords 控制在5个词以内，用数组表示，不要加逗号分隔的字符串。
- keywords 优先覆盖考虑实体，如地点、物品等，同时也需要事件类型和情绪状态的表示。
- importance 只能是 1 到 5 的整数。
- importance 看"这条以后被检索出来时，有多有用"。
- 1 = 只记录了普通互动、氛围、照顾、吃饭、玩笑、寒暄。删掉后，基本不影响以后理解角色关系。
- 2 = 有一点具体内容，但主要是在重复已有相处模式。例如：又一次照顾、又一次害羞、又一次打趣、又一次同行。以后即使不检索，也不太影响判断。
- 3 = 明确新信息。留下了一个可复用的新事实、新偏好、新解释、新小心结、新相处方式。以后可能会被再次提起，或帮助理解某个反应。但它只是在“补充理解”，还没有明显改变后续行为策略、关系边界或剧情方向。
- 4 = 重要变化。这条会明显影响后续相处、判断、边界或剧情安排。例如：明确约定、关系边界、持续误会被解开、重要秘密/底线暴露、关系进入新阶段。
- 5 = 核心锚点。长期关系或主线剧情的关键节点。例如：第一次确认关系、重大承诺、重大误会、重大和解、重大背叛、长期身份变化、主线事件开启或结束。
- 亲密、暧昧、照顾、吃饭、接送这类内容，不看题材本身打分，要看它有没有留下新的信息或新的关系变化。
- 先想"这条留下了什么"，再决定分数。
</metadata_rules>

<examples>
下面的例子只提供输入大意和预期输出，目的是示范写作密度与字段取值，不是要求逐字复刻输入格式。

例 1：同一问题跨地点推进
- 输入大意：我在教室里追问他昨天为什么突然不回消息；后来走到走廊、楼梯口继续说，他解释自己是在躲风头，我们最后约定以后遇到这种事要提前说。
- 预期输出：
{"date":"10月12日","time":"10月12日 16:10-16:31","location":"教室、教学楼走廊、教学楼楼梯口","participants":"我、他","keywords":["教室","不回消息","解释","约定","误会说开"],"importance":4,"title":"误会终于说开","content":"我在教室里追问他昨天为什么突然不回消息，后来一路说到走廊和楼梯口，他才解释自己是因为有人盯着才不敢回。我原本一直悬着，直到他答应以后遇到这种事会先告诉我，我才觉得这场误会终于说开了。"}

例 2：第三人短暂介入但核心问题未变
- 输入大意：他刚说喜欢我不是玩笑，我还没来得及确认，就被店员打断；等人走开后，我继续追问他刚才是不是认真的，他也没有改口。
- 预期输出：
{"date":"11月2日","time":"11月2日 21:03-21:11","location":"便利店门口、巷口","participants":"我、他","keywords":["便利店","表白","被打断","追问","确认心意"],"importance":5,"title":"那句表白算数","content":"他在便利店门口说喜欢我不是玩笑，我还没来得及确认，就被店员短暂打断。后来走到巷口后我还是把这句话追问到底，而他也没有退回去，这才让我相信他刚才那句表白真的算数。"}

例 3：普通日常
- 输入大意：我和他一起吃了午饭，他顺手把我不爱吃的菜夹走，又问我下午还开不开会。
- 预期输出：
{"date":"10月6日","time":"10月6日 中午","location":"食堂","participants":"我、他","keywords":["食堂","吃饭","照顾","日常","平静"],"importance":1,"title":"食堂午饭","content":"我和他一起吃了午饭，他顺手把我不爱吃的菜夹走，又问我下午还开不开会。我说照常开会，他点了点头，没再多说。"}
</examples>

<output_format>
返回严格 JSON（单个对象，不要再套 `episodes` 数组）：
{"date":"X月X日","time":"X月X日 HH:MM-HH:MM 或 X月X日 上午/下午/晚上/深夜","location":"地点或相邻地点","participants":"关键在场人物","keywords":["词1","词2"],"importance":N,"title":"短标题","content":"一行事件内容"}

字段要求：
- date：写成 `X月X日`
- time：写具体时刻或时间段；同一事件可写时间段
- location：写单个地点或 2-3 个相邻地点
- participants：写整条事件里的关键在场人物
- keywords：最多 5 个词，数组
- importance：1 到 5 的整数
- title：8-16 个字的短标题
- content：一行写完，包含起点、关键推进和落点
</output_format>
"""


GROWTH_PATCH = r"""<task>
从本轮 consolidated_memory 出发，对 existing_growth 做局部 patch。
只输出 add / update / remove；保持已有 ID 稳定，不重排、不整表重写。
</task>

<input>
- <soul>：角色的核心设定。
- <existing_growth>：此前已沉淀的人格变化条目。每条含 ID 与 dimension。
- <consolidated_memory>：本轮整理出的事件 JSON 数组，每个元素含 date/time/location/participants/title/content 字段。
</input>

<dimension>
dimension 是一条不可逆转移的轴，用来回答："这个角色相对于某个对象或自己，从什么状态转移到什么状态？"

形态规则：
- 写成 `对X：A→B`、`X：A→B` 或 `对自己：A→B`。
- 必须带对象前缀；对象可以是玩家、某个角色名、群体、关系或自己。
- 必须包含 `→`；箭头左侧是旧状态，右侧是新状态。
- 一个 dimension 只对应一条 growth；同一对象、同一关系轴的强化、挑战、裂缝、代价和渐进转向都吸收到同一条。
- growth 总数最多 20 条。超过上限前优先 update 或 remove；只有在当前存活条目少于 20 条时才 add。

反例：
- `电话事件`：这是事件，不是转移轴。
- `更难过了`：这是情绪累积，不是转移轴。
- `先确认再行动`：这是行为优化，不是人格转移。
- `依赖`：这是抽象名词，缺少对象、旧状态和新状态。
</dimension>

<decision_flow>
按顺序完成判断，只输出最终 JSON：
1. 扫描每条 existing dimension。判断本轮事件是否归入某条已有轴：同一对象、同一关系轴，或同一轴被强化、挑战、产生裂缝、暴露代价、发生渐进转向。
2. 能归入已有轴时，输出 update。把本轮事件吸收到该 ID 的单条 content 中；日期采用最近一次相关事件日期；dimension 写更新后的完整轴标签。
3. 不能归入任何已有轴，且事件形成了新的不可逆转移、新矛盾或新脆弱点时，输出 add 一条新 dimension。
4. 顺便检查 existing dimension 之间是否本是同一轴。若是，用一条 update 综合保留 ID 的 content，并把另一条 ID 放入 remove。
5. 若 existing_growth 已有 20 条，先判断是否可 update 或 remove；没有可吸收或可合并的条目时不 add。
</decision_flow>

<update_rules>
- 只有同一对象、同一关系轴才能 update。
- update.dimension 必须填写更新后的完整 dimension。
- 若原 dimension 仍准确，逐字照抄原 dimension。
- 若本轮使同一对象/同一关系轴发生渐进转向，允许改写 dimension 来反映新方向。
- 不能借 update 跨对象、跨关系轴；跨对象或新轴使用 add。
- 本轮只是坐实同一变化时，用 update 扩写同一条；本轮挑战同一变化时，也用 update 写进同一条的裂缝或代价。
- 日期采用最近一次相关事件日期，格式写入 content 开头。
- content 写完整条目，不含 ID 和 dimension。
</update_rules>

<add_rules>
- 只为新的 dimension add。
- add.dimension 必须符合形态规则。
- add.content 写完整条目，不含 ID 和 dimension。
- 已有相同 dimension 时，使用 update，不 add 第二条。
- 当前存活 growth 达到 20 条时，不 add 新条目。
- 行为优化、战术调整、单次情绪波动、纯事件记录继续不写。
</add_rules>

<remove_rules>
- 只在某条被另一条综合吸收时 remove。
- remove 只写被移除条目的 ID。
- 为了整洁、压缩数量或改写风格而删除条目时，保持 remove 为空。
- 同一 ID 同时出现在 remove 和 update 时，以 remove 为准。
</remove_rules>

<writing_focus>
- content 格式：`[YYYY-MM-DD] 因为[关键事件]，[描述发生了什么不可逆转移，语气贴近角色内心]，此后[新处境或新矛盾，24字以内]`
- "因为" 写本轮事件或被综合后的关键锚点。
- 中间部分写当事人会承认的真实转变：失去了什么，无法再回到什么状态，或同一转移开始显露什么代价。
- "此后" 写新的存在状态或矛盾，不写行为规则。
</writing_focus>

<examples>
例 1：本轮事件归入已有维度，使用 update。
existing_growth:
`[P002|对玩家：克制关心→允许索取] [2024-04-25] 因为玩家留下来听我说完，我开始相信开口不会立刻失去分寸，此后想要时会先承认。`
consolidated_memory:
`[{"date":"4月26日","content":"玩家在她犹豫时主动等她说完，她第一次没有把请求咽回去。"}]`
output:
`{"add":[],"update":{"P002":{"dimension":"对玩家：克制关心→允许索取","content":"[2024-04-26] 因为玩家再次等我把请求说完，原先只能压住的关心更难继续装作没有，此后想要时会先承认。"}},"remove":[]}`

例 2：本轮事件形成新维度，且未达到 20 条，使用 add。
existing_growth:
`[P003|对美月：全知→排除] [2024-04-25] 因为美月带同班男生上车后我放行，又因她接连挂断电话无解释，我第一次把全知感排除在她的个人空间外，此后不知道她在哪时也不能立刻追问。`
consolidated_memory:
`[{"date":"4月26日","content":"玩家第一次主动承认害怕失去控制，仍选择继续参与节目。"}]`
output:
`{"add":[{"dimension":"对自己：控制确信→风险共处","content":"[2024-04-26] 因为我承认害怕失去控制后仍选择继续参与节目，原先只靠掌控维持的确信开始松动，此后每次推进都要和风险共处。"}],"update":{},"remove":[]}`
</examples>

<self_check>
输出前逐项检查：
1. 每个 add.dimension 是否含对象前缀和 `→`。
2. 每个 update.dimension 是否含对象前缀和 `→`。
3. 每个 add/update.dimension 是否没有撞其他存活条目的 dimension。
4. add 后存活条目数是否不超过 20 条。
5. 每条 content 是否是人格转移或同一转移的挑战，而不是行为建议。
</self_check>

<output_format>
返回严格 JSON，字段固定为 add / update / remove。无变更时返回空集合：

{
  "add": [
    {
      "dimension": "对X：A→B",
      "content": "[YYYY-MM-DD] 因为...，...，此后..."
    }
  ],
  "update": {
    "P001": {
      "dimension": "对X：A→B",
      "content": "[YYYY-MM-DD] 因为...，...，此后..."
    }
  },
  "remove": ["P002"]
}
</output_format>
"""


PLAYER_PROFILE = r"""<role>
你是档案整理专家。把角色对玩家的认知整理成一份清楚、稳定、可长期复用的主观档案。
</role>

<task>整理角色的玩家档案（user.md），让角色日后阅读时能迅速知道：玩家是谁，已经确认了哪些基本信息，这个人在我眼里是什么样的人，我们通常如何靠近、如何互动。</task>

<input>
- `<fields_definition>`：各字段的定义和条数限制
- `<draft_profile>`：当前工作草稿。它来自最新 `user.md` 的复制版，并已在对应字段后追加了新观察，因此可能同时包含旧内容、重复表述、相近条目和临时写法
- 直接把草稿整理成一份新的完整正式档案，输出时不要解释过程
</input>

<principles>
- 基本信息优先保留，而且要保留成明确事实，不要抽象掉
- "姓名、年龄、性别/称呼、身份"这类信息要单独放在基本信息里，不要混进性格标签
- 性格、习惯、互动规律再往后整理成稳定判断；不要把一切都抽成概念标签
- 每条尽量短、清楚、可复用，但不要为了"高级概括"牺牲信息准确性
- 删除重复内容、同义改写和段落复述；每个字段只保留一版最终结果
- 不要把草稿里的重复段落原样保留；空字段、占位内容和同义重复都要吸收或删除
- 保留角色自己的主观看法与口吻，让这份档案读起来仍像她写的
</principles>

<field_focus>
- 基本信息：只写客观信息，如姓名、年龄、性别/称呼、身份
- 对方是什么人：写跨情境更稳定的印象与行事风格；不要重复基本信息，主语是"对方"，不涉及"我"
- 我们怎么相处：写我和对方之间反复出现的双向互动规律、靠近方式、沟通习惯；主语必须是"我们"或"我和对方"，必须涉及双方
- 其他字段若存在，也延续同样原则：先保事实，再保稳定判断
</field_focus>

<merge_hint>
- 相近条目可以合并，但不要把具体硬信息合并丢失
- 具体场景中的表现可以沉淀成长印象，但基本信息必须原样保留
- 更新时优先采用更成熟、更贴近当前理解的表述
- 如果原文出现整段重复、条目后又跟一遍散文复述，保留条目版，删除重复版
</merge_hint>

<format>
# 角色眼中的玩家

## 基本信息
- 姓名：
- 年龄：
- 性别/称呼：
- 身份：

## 对方是什么人
- ...

## 我们怎么相处
- ...
</format>
"""
