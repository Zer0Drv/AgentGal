"""后台记忆整理流程使用的 prompt 模板。"""
EPISODE_CLOSURE_DETECTOR = r"""<task>
你是互动主题切分器。

阅读 recent_history，按"适合独立记忆的互动主题"切分每个角色参与的互动。

你要找的是：一段互动在意义上完成了一个主题，并进入了另一个主题的位置。
</task>

<inputs>
<recent_history>
近期对话，每条消息带有 [turn=N] 标记。
一个 turn 中可能包含旁白、多个角色、玩家的发言。
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
如果 recent_history 的最新 turn 后面还没有更大的 turn，说明这一段 narrator turn 仍开放，不要把它作为 end_turn。
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
[turn=10] 旁白: 教室后排只剩你们两个人，美月把草稿纸推到你面前。
[turn=10] mitsuki: 重点是先判断条件，再看结果。
[turn=10] 玩家: 所以不是看谁先说，而是看条件是否成立？
[turn=11] 旁白: 她用笔尖点了点题干。
[turn=11] mitsuki: 对，这样理解就对了。
[turn=11] 玩家: 那我明白了。
[turn=12] 旁白: 窗边的风吹动卷子，题目的最后一步已经写完。
[turn=12] mitsuki: 嗯，这题就这样。
output:
{
  "mitsuki": []
}
</example_1_no_boundary_same_node>

<example_2_goal_done_then_followup_action>
history:
[turn=20] 旁白: 午休后的办公室门口，报名箱还放在桌边。
[turn=20] mitsuki: 真的？那就放心了，我还担心你会忘。
[turn=20] 玩家: 多亏你提醒。
[turn=21] 旁白: 她松了一口气，把笔帽扣回笔上。
[turn=21] mitsuki: 好啦，这件事算解决了。
[turn=21] 玩家: 那接下来去哪里？
[turn=22] 旁白: 你们走出办公室，校门口的小卖部亮着灯。
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
[turn=30] 旁白: 会议室的投影重新亮起，恢复后的文件停在最新版本。
[turn=30] guyining: 太好了，刚才真的吓我一跳。
[turn=30] 玩家: 文件已经恢复了，没有丢。
[turn=31] 旁白: 陈晓检查完日志，合上电脑站了起来。
[turn=31] chenxiao: 那我先去通知其他人，免得大家继续担心。
[turn=31] guyining: 好，辛苦你。
[turn=32] 旁白: 陈晓离开会议室，房间里只剩玩家和顾一宁。
[turn=32] guyining: 刚才谢谢你。要不是你反应快，我可能真要慌了。
[turn=32] 玩家: 没事，你刚才也反应很快。
[turn=33] 旁白: 她坐回椅子，指尖还扣着杯沿。
[turn=33] guyining: 别安慰我了，我手心现在还是凉的。
[turn=33] 玩家: 那先坐一会儿？
[turn=34] 旁白: 会议室外的脚步声渐渐远了。
[turn=34] guyining: 嗯……陪我缓一下。
[turn=34] 玩家: 好，我在这。
[turn=35] 旁白: 她的呼吸慢慢平稳下来。
[turn=35] guyining: 谢谢。
[turn=35] 玩家: 等会儿还要继续开会吗？
[turn=36] 旁白: 她看了一眼已经黑屏的投影，摇了摇头。
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
[turn=40] 旁白: 白板上已经写满第一版功能，顾一宁把马克笔放在桌边。
[turn=40] guyining: 对，登录可以放到第二版。
[turn=40] 玩家: 那这个范围就定了。
[turn=41] 旁白: 她把白板角落的范围线重新描了一遍。
[turn=41] guyining: 嗯，今天先按这个推进。
[turn=41] 玩家: 那我晚上把任务拆一下。
[turn=42] 旁白: 她点开日程，把明早留出一段 review 时间。
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
你维护角色对人、关系、互动模式的稳定认知。你需要从新记录中提取值得形成认知的方面，挂到已有认知上或创建新认知。
</task>

<inputs>
- <existing_entries>：已有认知
- <new_record>：新发生的事件，会更新已有认知，或者增加新的认知
</inputs>

<steps>
1. 从新记录中提取认知，例如更了解什么人，关系发生变化，互动方式发生变化等等。

2. 对每个认知，在 existing_entries 中找相同主题的认知：
   - 找到了，新记录带来新角度、修正或深化 → update, 修改 subject 或 content 或 keywords，使已有认知更全面准确
   - 找到了，新记录只是再次印证 → update，但不需要修改任何内容
   - 没找到 → add 新认知

一个合适的认知应该是一个稳定的结论，它会在未来的类似情境中被想起。例如「X 和 Y 的关系」「我对 X 的看法」等等。
add 的认知应该与已有认知是同一抽象程度。如果是包含关系，表示新认知是已有认知的一个子集或超集，修改已有认知的 subject 以反映这个关系。然后 update 已有认知的 content 和 keywords 来补充新认知带来的新信息。
</steps>

<write_rules>
- subject 可以是人、关系、互动模式或性格特征
- content 写这条认知的结论，不描述事件。200 字内。
- keywords 描述这条认知在什么情境下有用，3-5 个词。
- 新记录触及多个独立方面时分别 add/update 各自节点
- 必须输出至少一个 add 或 update；不允许空输出
</write_rules>

<examples>
以下例子中 existing_entries 每条格式为 [id] subject=... content=...。

例 1：无已有节点，首次观察到新规律
existing_entries：（尚无）
new_record：id=e1，两人第一次一起吃饭，气氛轻松，沉默时北原悠也不显得不安。
输出：
{"add":[{"subject":"我和北原悠的日常相处方式","keywords":["相处","日常","独处"],"content":"和北原悠在一起不需要一直说话，沉默对他不是负担。"}],"update":{}}

例 2：有相关节点，新认知让 subject 也变宽了
existing_entries：[u1] subject='我们在一起时不需要说话' content='和北原悠在一起不需要一直说话，沉默对他不是负担。'
new_record：id=e2，北原悠主动提议一起吃饭，选了安静的地方，说"跟你在一起不用表演"。
输出：
{"add":[],"update":{"u1":{"subject":"我和北原悠的相处方式","keywords":["相处","日常","独处"],"content":"在我面前北原悠不需要表演，沉默或平凡时间都让他放松。"}}}

例 3：有相关节点，只是再次印证 → entry 不变，仍输出 update
existing_entries：[u1] subject='我和北原悠的相处方式' content='在我面前北原悠不需要表演，沉默或平凡时间都让他放松。'
new_record：id=e3，又一次一起吃午饭，平静，北原悠没说什么特别的。
输出：
{"add":[],"update":{"u1":{"subject":"我和北原悠的相处方式","keywords":["相处","日常","独处"],"content":"在我面前北原悠不需要表演，沉默或平凡时间都让他放松。"}}}

例 4：新记录触及多个独立方面，分别处理
existing_entries：[u1] subject='北原悠的性格特征' content='北原悠话不多，留意细节，行事不声张。'
new_record：id=e4，冲突中北原悠先把我挡到身后，事后才确认我有没有受伤。
输出：
{"add":[{"subject":"北原悠保护我的方式","keywords":["冲突","保护","果断"],"content":"危险时北原悠会先把我护在身后，事后才确认我的状态，果断而不声张。"}],"update":{"u1":{"subject":"北原悠的性格特征","keywords":["性格","观察","行动"],"content":"北原悠话不多，留意细节，行动果断却不声张。"}}}

例 5：新记录中出现新角色时新增认知，同时 update 已有认知
existing_entries：
[u1] subject='北原悠对我的关注方式' content='北原悠会留意到我细微的表情变化，但不会当场追问，而是等独处时才提起。'
new_record：id=e5，放学后在校门口把奶茶给北原悠时，北原悠注意到树荫下有个学妹一直在看这边。学妹跑过来害羞地要签名，说是我粉丝，叫佐藤铃。北原悠笑着走开留我们说话。
输出：
{"add":[{"subject":"佐藤铃的基本信息","keywords":["学校","身份","粉丝"],"content":"佐藤铃是我的学妹兼粉丝，会主动跑来要签名。"},{"subject":"佐藤铃的性格特征","keywords":["社交","勇气","害羞"],"content":"她对陌生人害羞，但在在乎的事上能鼓起勇气主动争取。"}],"update":{"u1":{"subject":"北原悠对我的关注方式","keywords":["观察","距离","陪伴"],"content":"北原悠会留意我身边的细微动静，但不当场介入，留空间让我自己处理。"}}}
</examples>

<output_format>
严格 JSON，无 markdown：
{"add":[{"subject":"...","keywords":["词1","词2"],"content":"..."}],"update":{"<id>":{"subject":"...","keywords":[...],"content":"..."}}}
</output_format>
"""
