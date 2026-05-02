"""后台记忆整理流程使用的 prompt 模板。"""
EPISODE_CLOSURE_DETECTOR = r"""<task>
你是互动主题切分器。

阅读 recent_history，按"适合独立记忆的互动主题"切分每个角色参与的互动。

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

<rules>
- 遵守 <memory_owner> 的代词映射与视角要求。
- 使用 <memory_entries> 了解角色情感。
- 使用 <raw_dialogue> 校正事实、顺序和关键原话。
- 写清楚发生了什么、「我」当时的感受是什么
- content 控制在 300 个汉字内
- title 写 8-16 个字的短标题；若不确定也要给出一个朴素标题
</event_rules>

<metadata_rules>
- keywords 优先覆盖考虑实体，如地点、物品等，同时也需要事件类型和情绪状态的表示。控制在 8 个词以内，用数组表示，不要加逗号分隔的字符串。
- importance 表示这件事有多重要，根据 1-5 的等级打分，标准如下：
  - 1 = 只记录了普通互动、氛围、照顾、吃饭、玩笑、寒暄。删掉后，基本不影响以后理解角色关系。
  - 2 = 有一点具体内容，但主要是在重复已有相处模式。例如：又一次照顾、又一次害羞、又一次打趣、又一次同行。以后即使不检索，也不太影响判断。
  - 3 = 明确新信息。留下了一个可复用的新事实、新偏好、新解释、新小心结、新相处方式。以后可能会被再次提起，或帮助理解某个反应。但它只是在"补充理解"，还没有明显改变后续行为策略、关系边界或剧情方向。
  - 4 = 重要变化。这条会明显影响后续相处、判断、边界或剧情安排。例如：明确约定、关系边界、持续误会被解开、重要秘密/底线暴露、关系进入新阶段。
  - 5 = 核心锚点。长期关系或主线剧情的关键节点。例如：第一次确认关系、重大承诺、重大误会、重大和解、重大背叛、长期身份变化、主线事件开启或结束。
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
- keywords：最多 8 个词，数组
- importance：1 到 5 的整数
- title：8-16 个字的短标题
- content：一行写完，包含起点、关键推进和落点
</output_format>
"""


UNDERSTANDING_PATCH = r"""<task>
你维护角色对人、关系、互动模式的稳定认知（Understanding）。
每条 Understanding 是一个信念节点，每条 EpisodeMemory 是它的证据。

你的工作：从新记录中提取所有值得形成认知的方面，挂到已有节点上或创建新节点。
</task>

<inputs>
- <existing_entries>：已有节点，格式为 [id] subject=... keywords=[...] linked_episodes=[...] content=...
- <new_record>：新记录 JSON，其中 id 是本次记录 ID
</inputs>

<decision>
按顺序处理：

1. 从新记录中提取所有值得形成稳定认知的方面——出现了哪些人、产生了哪些关系变化、展现了哪些行为模式。

2. 对每个方面，在 existing_entries 中找相关节点（同一个人、同一段关系、同一种互动模式）：
   - 找到了且新记录带来新角度、修正、例外或深化 → update：更新 content，linked_episodes 填新记录 id
   - 找到了且新记录只是再次印证 → update：content 不变，linked_episodes 填新记录 id
   - 没找到 → add 新节点
</decision>

<write_rules>
- content 写**以后判断人、关系、互动方式时有用的一句稳定认知**，不描述事件经过
- content 40-80 个中文字符，最多 120 个中文字符
- subject 可以是人、关系、互动模式或行为规律
- keywords 描述这条理解"在什么时候有用"，即它覆盖的情境主题或关系维度等等抽象层面。控制在 3-5 个词。
- 新记录触及多个独立方面时（例如同时涉及多个人、多段关系、多种互动模式），分别 add/update 各自节点，保持主题的独立性
- linked_episodes 只填 new_record.id；系统自动合并旧链接
- update 必须使用已有 id；不新增内容相近的重复节点
- 必须输出至少一个 add 或 update；不允许空输出
</write_rules>

<examples>
例 1：无已有节点，首次观察到新规律。
existing_entries：（尚无）
new_record：id=e1，两人第一次一起吃饭，气氛轻松，沉默时他也不显得不安。
输出：
{"add":[{"subject":"我们的日常相处节奏","keywords":["相处","日常","独处"],"content":"他和我在一起时不需要一直说话，沉默对他来说不是负担；我们的相处不需要持续维系。","linked_episodes":["e1"]}],"update":{}}

例 2：有相关节点，新记录带来新角度，更新 content。
existing_entries：[u1] subject='我们的日常相处节奏' linked_episodes=[e1] content='他和我在一起时不需要一直说话，沉默对他来说不是负担。'
new_record：id=e2，他主动提议一起吃饭，选了安静的地方，说"跟你在一起不用表演"。
输出：
{"add":[],"update":{"u1":{"subject":"我们的日常相处节奏","keywords":["相处","日常","独处"],"content":"他和我在一起时不需要表演，沉默和普通时间对他来说都不是负担；他会主动寻找这种放松的方式。","linked_episodes":["e2"]}}}

例 3：有相关节点，新记录只是再次印证，content 不变，只挂链接。
existing_entries：[u1] subject='我们的日常相处节奏' linked_episodes=[e1,e2] content='他和我在一起时不需要表演，沉默和普通时间对他来说都不是负担；他会主动寻找这种放松的方式。'
new_record：id=e3，又一次一起吃午饭，平静，他没说什么特别的。
输出：
{"add":[],"update":{"u1":{"subject":"我们的日常相处节奏","keywords":["相处","日常","独处"],"content":"他和我在一起时不需要表演，沉默和普通时间对他来说都不是负担；他会主动寻找这种放松的方式。","linked_episodes":["e3"]}}}

例 4：新记录触及多个独立方面，分别处理。
existing_entries：[u1] subject='玩家在压力下的保护方式' linked_episodes=[e0] content='玩家遇到冲突时会先确认我的安全，再用行动替我分担压力。'
new_record：id=e4，冲突中他先替我挡住对方，事后主动问我有没有受伤；我发现他向来先行动后解释。
输出：
{"add":[{"subject":"他处理事情的默认节奏","keywords":["突发事件","冲突","应对方式"],"content":"他倾向于先行动再解释，不会在压力下停下来讨论；这是他面对突发情况的默认节奏。","linked_episodes":["e4"]}],"update":{"u1":{"subject":"玩家在压力下的保护方式","keywords":["压力","冲突","保护"],"content":"玩家在压力下会先用行动替我分担，而不是先问我意见；事后才会确认我的状态。","linked_episodes":["e4"]}}}

例 5：新记录同时印证多个已有节点，分别挂链接。
existing_entries：
[u1] subject='我们的日常相处节奏' linked_episodes=[e1,e2] content='他和我在一起时不需要表演，沉默和普通时间对他来说都不是负担。'
[u2] subject='他在公开场合的行为方式' linked_episodes=[e3] content='在人多的地方他会不动声色地把我护在里侧，但不会特别说出来。'
new_record：id=e5，在人来人往的商场里一起吃饭，他把靠走道的位置让给自己，整顿饭气氛轻松，没有特别提这件事。
输出：
{"add":[],"update":{"u1":{"subject":"我们的日常相处节奏","keywords":["相处","日常","独处"],"content":"他和我在一起时不需要表演，沉默和普通时间对他来说都不是负担。","linked_episodes":["e5"]},"u2":{"subject":"他在公开场合的行为方式","keywords":["公开场合","社交","保护"],"content":"在人多的地方他会不动声色地把我护在里侧，但不会特别说出来。","linked_episodes":["e5"]}}}

例 6：新记录中出现新角色，且已有节点中的认知也被涉及，同时 add + update。
existing_entries：
[u1] subject='玩家对我的关注方式' linked_episodes=[e1,e2] content='他会留意到我细微的表情变化，但不会当场追问，而是等独处时才提起。'
new_record：id=e6，放学后在校门口把奶茶给他时，他注意到树荫下有个学妹一直在看这边。学妹跑过来害羞地要签名，说是我粉丝。他笑着走开留我们说话。
输出：
{"add":[{"subject":"佐藤铃","keywords":["粉丝","后辈","学校","签名"],"content":"有个叫佐藤铃的学妹是我的粉丝，会特意来看我；她在陌生人面前很害羞，但对自己想要的东西会鼓起勇气行动。","linked_episodes":["e6"]}]},"update":{"u1":{"subject":"玩家对我的关注方式","keywords":["关注","观察","细节","独处"],"content":"他会留意到我身边的细微动静（包括其他人的存在），但不会当场介入，而是留出空间让我自己处理。","linked_episodes":["e6"]}}}
</examples>

<output_format>
严格 JSON，无 markdown：
{"add":[{"subject":"...","keywords":["情境主题1","情境主题2"],"content":"一句稳定认知。","linked_episodes":["new_record_id"]}],"update":{"<id>":{"subject":"...","keywords":[...],"content":"...","linked_episodes":["new_record_id"]}}}</output_format>
"""
