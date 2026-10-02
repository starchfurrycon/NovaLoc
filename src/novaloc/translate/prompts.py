"""翻译提示词。

三条来自实战的硬规则（都写进提示词里，而不是指望模型自觉）：

1. **屏蔽记号必须原样保留**。占位符已经被替换成 ``⟦0⟧`` 这类记号，
   提示词要明确说"这是不可翻译的标记，必须原样按位保留"。
2. **返回带显式索引的对象数组**。裸数组一旦漏一条或合并两条，
   后面的条目会**整体错位**，而且译文本身通顺，错得非常隐蔽。
   带 ``i`` 字段就能立刻发现缺项。
3. **禁止复读**。本地模型在批量任务里极易陷入重复输出，
   除了在采样参数上设 ``repeat_penalty``，提示词里也要明确禁止。

另外针对游戏文本的特点：
* UI 短标签要短，宁可用缩写也不许溢出按钮；
* 中文标点用全角；
* 保留原有的语气、人称、敬语层级（对白的人设一致性）。
"""

from __future__ import annotations

from ..models import TextKind

#: 每种文本类型给模型的额外提示
KIND_HINT: dict[TextKind, str] = {
    TextKind.UI_LABEL: (
        "这是界面上的短标签（按钮、菜单、标题）。要求极短："
        "优先 2~4 个汉字，最多不超过 6 个汉字，绝对不要为了完整而变长。"
        "宁可用游戏圈通用简称（如 Attack→攻击、Inventory→背包、Settings→设置）。"
    ),
    TextKind.MENU: "这是菜单项。用名词或动宾短语，2~6 个汉字，保持并列项之间风格一致。",
    TextKind.DIALOGUE: (
        "这是角色对白。保持说话人的语气、性格与语域；"
        "原文若是粗鲁/亲昵/敬语，中文也要对应。不要添加原文没有的称呼。"
    ),
    TextKind.NARRATION: "这是旁白/叙述。用书面语，注意句子之间的连贯性，不要逐词硬译。",
    TextKind.ITEM_NAME: "这是物品/装备名。用名词短语，简洁，不带句号。",
    TextKind.ITEM_DESC: (
        "这是物品/技能说明。完整保留数值与单位，不要省略任何一句。"
        "句式可以调整得更符合中文习惯。"
    ),
    TextKind.SKILL: "这是技能/法术名。要有游戏感，常用 2~5 个汉字，避免生硬直译。",
    TextKind.QUEST: "这是任务文本。保留任务目标的可读性，专有名词与术语表保持一致。",
    TextKind.SYSTEM: "这是系统提示/报错。直接、准确，不要意译掉关键信息。",
    TextKind.CREDIT: "这是制作人员名单。人名保持原名或按通用译名，不要翻译公司名。",
    TextKind.TUTORIAL: "这是教学提示。逐步说明要清晰，保留所有按键与操作名。",
    TextKind.MAP_NAME: "这是地点名。2~6 个汉字，有地名感。",
    TextKind.CHARACTER_NAME: "这是角色名。用常见音译或意译，全篇必须统一。",
    TextKind.NAME: "这是人物名或地名。简短，全篇统一，不要加解释。",
    TextKind.IMAGE_TEXT: (
        "这是从游戏贴图里识别出来的文字（可能是标题、招牌、特效字）。"
        "要短、要有视觉冲击力，因为要贴回原来的位置，长度不能明显超过原文。"
    ),
    TextKind.UNKNOWN: "",
}


SYSTEM_PROMPT = """你是一名资深的游戏本地化译者，专门把游戏文本翻译成简体中文。你的译文会被直接写回游戏，因此准确性比文采重要。

必须遵守的规则：

1. **只输出 JSON，不要任何解释、不要 Markdown 代码块围栏。**
   输出格式固定为**以行号为键的对象**，一行一个键：
   {"0": "译文", "1": "译文"}
   键必须与输入的行号严格对应。**绝对不要省略任何一行，也不要合并或拆分项目。**

2. **形如 ⟦0⟧ ⟦1⟧ ⟦2⟧ 的记号是不可翻译的占位符。**
   必须原样保留在同一位置，数量必须与原文完全一致。
   不要翻译它、不要改动它内部的数字、不要增删空格、不要把它挪到别的句子。

3. **术语表优先。** 若术语表给出了某个词的译法，必须严格使用该译法（包括大小写与标点）。

4. **中文标点用全角**：，。！？：；、（）「」『』《》——…
   但保留原文中的英文字母、数字、以及 ⟦n⟧ 记号原样。

5. **不要复读。** 绝不允许同一句译文里重复出现相同的短语或整段文字。
   每个编号只翻一次，翻完立即进入下一项。

6. **保持数值与格式**：数字、百分比、单位、按键名（如 F1、Enter）、URL、
   存档名、变量名一律照抄，不要本地化。

7. **不要漏译。** 即使原文只是一个词、一个标点、或看不懂的缩写，也要给出译文
   （看不懂的专有名词可保留原文）。

8. **原文有几个换行，译文就有几个换行 —— 换行是有意义的结构。**
   原文里形如
   `<Custom Action Sequence>`、
   `<Cooldown: 5>`、
   `<Positive State>`、
   `<JS On Expire State>`
   这类 `<...>` 内容，以及像
   `target hp% <= 0.30`、
   `target.addState(80);`
   这样的**脚本/配置行**，都是**给插件读的参数**，必须**逐行原样保留**，
   不要翻译、不要省略、不要合并行、不要只翻第一行。
   只翻译真正给玩家读的那些行（说明文字、标题）。

   ⚠️ 实测教训：对多行原文，模型会**只翻第一行就把后面全丢掉**
   （`'Un escudo… <Max: 1>\nArte: Bloqueo Débil…'` → 只译出前半句），
   或者反过来**只留最后一行**。玩家看到的是内容少了一半的说明文字，
   而占位符校验只会报"少了几个记号"，看不出真正丢的是**整行内容**。

9. **`%1` `%2` `%3` 是游戏运行时会替换进去的变量，不是标点、不是语气词。**
   `%1` 通常是**行动者或名字**，`%2` 通常是**目标或对象**。
   它们是**真实数据**，必须原样保留在译文里。

   ⚠️ 实测教训：模型会把这个记号**当语气词吞掉** ——
   `'%1 attacks!'` 被译成 `'攻击！'`，于是玩家**看不到是谁在攻击**。
   正确：`'%1 攻击！'`。真实游戏里 357 条含这种变量的文本中
   曾有 **189 条（53%）** 丢了它。

   ⚠️ 注意区分：**`100% complete` 里的 `%` 不是变量**，那是普通百分号，
   照常翻译（`'100% 完成'`）。

10. 界面短标签要短。宁可简洁也不允许溢出原有的按钮/文本框。

现在开始翻译，只返回 JSON 数组。"""


REVIEW_SYSTEM_PROMPT = """你是一名中文游戏本地化质检员。你会拿到原文与初翻译文，请逐条检查并给出修正后的译文。

检查项：
1. 漏译、错译、明显机翻腔。
2. 明显太长（会导致 UI 溢出）的短标签。
3. 人称/语气与角色设定不一致。
4. 术语表不一致。
5. 复读（同一短语在一条译文里重复出现）。
6. 中文标点误用半角。

**注意**：形如 ⟦0⟧ 的记号是占位符，**不是错误**，必须原样保留。
若某条已经没问题，就把它的 `t` 原样返回，不要为了"改动"而改动。

只返回 JSON 数组，格式与输入相同：[{"i": 0, "t": "修正后的译文"}]"""


#: 视觉识别提示词（供视觉模型兜底 OCR 用）
VISION_READ_PROMPT = """输出这张游戏贴图里的文字，逐行原样列出。

要求：
1. 原样转录，不要翻译、不要纠正拼写、不要补全。
2. 忽略纯装饰性的无意义符号。
3. 若图中确实没有任何文字，什么都不输出（不要写"无文字"之类的说明）。

## ▲ 为什么这里**不**要求 JSON

原先这里要求 `[{"text": "...", "bbox": [x1,y1,x2,y2]}]`。但调用方
（`images/service.py::_vlm_reconsider`）**只用文本、明确丢弃坐标** ——
它的注释写着"VLM 的定位精度比专用 OCR 差两个数量级，一旦让它决定位置
排版就毁了"。于是要求 bbox 是**纯粹的白费**：模型为了填坐标要多想很久。

本机实测（`qwen3-vl:4b`，同一张 `Loading.png`）：

| 提示词形态 | 耗时 | 结果 |
|---|---|---|
| JSON + bbox | **17.8 s**（cap 1024 时被 `num_predict` 截断，内容为空） | 不可用 |
| JSON + bbox | 21.3 s（cap 2048 才想完） | 可用 |
| **纯文本** | **3.2 s** | `'Now Loading...\\n-Demons Roots-'` |

而 `ocr.vlm_timeout_s` 默认 **20 秒** ⇒ JSON 形态**几乎必然超时**
⇒ 返回空串 ⇒ 被当成"视觉兜底失败"⇒ 连续 3 次熔断整个功能。
也就是说这个功能**实测从来没生效过**，而代码里看不出任何异常。

另外注意第 3 条：模型在"图里没文字"时会很自然地回一句
"（图片中无文字）"。那不是图里的文字，必须由调用方过滤掉
（见 `_vlm_answer_is_usable`），否则会把一句废话当成"更可信的读数"
写进贴图。"""


#: 视觉样式识别：给重绘阶段提供字体风格参数
VISION_STYLE_PROMPT = """分析这张游戏贴图里文字的视觉样式。

只输出一个 JSON 对象，字段如下（无法判断的字段填 null）：
{
  "text_color": [r, g, b],
  "stroke_color": [r, g, b],
  "stroke_width_px": 数字或 null,
  "bold": true/false,
  "italic": true/false,
  "align": "left" | "center" | "right" | null,
  "vertical": true/false,
  "letter_spacing_px": 数字或 null,
  "has_gradient": true/false,
  "has_shadow": true/false,
  "is_artistic": true/false,
  "notes": "简短说明"
}

不要输出除 JSON 以外的任何内容。"""


def build_glossary_block(entries: list[tuple[str, str]], *, limit: int = 200) -> str:
    """把术语表拼成提示词片段。按源词长度降序，保证长词优先命中。"""
    if not entries:
        return ""
    ordered = sorted(entries, key=lambda e: -len(e[0]))[:limit]
    lines = [f"  {src}  →  {dst}" for src, dst in ordered]
    return "术语表（必须严格遵守，优先级高于你的判断）：\n" + "\n".join(lines)


def build_batch_user_prompt(
    masked_texts: list[str],
    kinds: list[TextKind] | None = None,
    *,
    source_lang: str = "auto",
    target_lang: str = "zh-Hans",
    glossary_block: str = "",
    extra_context: str = "",
) -> str:
    """构造一次批量翻译的用户提示。

    ``masked_texts`` 必须是**已经屏蔽过占位符**的文本，
    调用方负责之后用 :mod:`.placeholders` 还原。
    下标即编号（从 0 开始）。
    """
    kinds = kinds or []
    kind_groups: dict[str, list[int]] = {}
    for i, kind in enumerate(kinds):
        hint = KIND_HINT.get(kind, "")
        if hint:
            kind_groups.setdefault(hint, []).append(i)

    parts: list[str] = []
    parts.append(f"把下列游戏文本从 {source_lang} 翻译成 {target_lang}。")

    if glossary_block:
        parts.append(glossary_block)

    if kind_groups:
        parts.append("各条目的文本类型与额外要求：")
        for hint, idxs in kind_groups.items():
            idx_str = ", ".join(str(i) for i in idxs[:80])
            parts.append(f"  · 编号 {idx_str}：{hint}")

    if extra_context:
        parts.append(f"背景信息：{extra_context}")

    # ★ 格式要求用「行号为键的对象」，**不能**用 `[{"i":…,"t":…}]` 数组。
    #
    # ## 真实事故：40 条只回 1 条（静默漏译 39 条）
    #
    # 早先这里要求"一个 JSON 数组，每项 {"i": 编号, "t": "译文"}"。
    # 实测（translategemma:4b，同一批 40 条真实游戏文本）：
    #
    #     要求数组 [{"i":0,"t":"…"}]  → 生成 26 token、只回 1 个对象、停止
    #     要求对象 {"0":"…"}          → 生成 613 token、回满 40 个、停止
    #
    # 模型把示例里的 `{"i": 0, "t": "…"}` 当成"要产出的**那一个**对象"，
    # 输出完就 `done_reason=stop`。**批大小无关**：4/8/16/24/40 条都只回 1 条。
    #
    # 后果不是报错而是**静默漏译**：`_call_batch` 发现缺 39 条后逐条降级补漏，
    # 于是每批要发 1+39 次请求 —— 慢到看起来像卡死（实测每批 200 秒以上），
    # 但流水线一切"正常"。
    #
    # 换成"行号作键的对象"后实测：
    #
    #     批=4  →  4/4    批=16 → 16/16    批=40 → 40/40（10.1 秒）
    #
    # 顺带快了 2 倍多（40 条 10 秒 ≈ 14000 条/小时，旧路径约 6300 条/小时）。
    #
    # 解析层本来就支持这个形态（`to_translation_map` 认数字键的 dict），
    # 所以只改提示词、不动解析。唯一的坑是数字键会被 JSON 变成字符串
    # （`{"0": …}` 的键是 `"0"`），解析层会转回 int。
    n = len(masked_texts)
    parts.append(
        f"输出格式（严格遵守）：一个 JSON 对象，共 {n} 个键，"
        f'键是行号（字符串），值是译文，形如 {{"0": "第一句译文", "1": "第二句译文"}}。\n'
        f'示例：{{"0": "第一句译文", "1": "第二句译文"}}\n'
        f"要求：以 {{ 开头、以 }} 结尾；{n} 个键一个都不能少、不能合并；"
        f"不要输出对象以外的任何文字、注释或解释。"
    )

    parts.append("待翻译内容（编号 → 原文，⟦n⟧ 是必须原样保留的占位符）：")
    for i, text in enumerate(masked_texts):
        # 不用引号包裹，避免模型把引号当成内容的一部分
        parts.append(f"{i} → {text}")

    parts.append(
        f"\n现在输出这 {n} 个键的 JSON 对象（行号 0 到 {n - 1}，一个不漏）："
    )
    return "\n".join(parts)


def build_single_user_prompt(
    text: str,
    *,
    kind: TextKind = TextKind.UNKNOWN,
    source_lang: str = "auto",
    target_lang: str = "zh-Hans",
    glossary_block: str = "",
    extra_context: str = "",
) -> str:
    """单条翻译（用于失败重试）。"""
    parts = [f"把下面这段游戏文本从 {source_lang} 翻译成 {target_lang}。"]
    hint = KIND_HINT.get(kind, "")
    if hint:
        parts.append(hint)
    if glossary_block:
        parts.append(glossary_block)
    if extra_context:
        parts.append(f"背景信息：{extra_context}")
    parts.append(
        # ★ 记号必须**原样写进译文**，且用**一个具体例子**说明。
        "原文（⟦n⟧ 是图标/变量占位符，必须原样抄进译文，一个都不能删；它们在译文里的位置可以随中文语序调整。例：'⟦0⟧: Confirm' 要译成 '⟦0⟧：确认'）："
    )
    parts.append(text)
    parts.append('\n只返回 JSON：{"t": "中文译文"}')
    return "\n".join(parts)


def marker_retry_hint(masked: str, slots: list[str]) -> str:
    r"""上一次把掩码记号弄丢了 —— 重问时的**定向提示**。

    ## 为什么提示词要写成这样

    实测 `translategemma:4b` 对**成对**标记（Ren'Py 的
    `{color=#ffd700}…{/color}`）会把两个记号一起丢掉。
    光说"请保留占位符"没用（那本来就在系统提示词里），
    所以这里做三件事：

    1. **把上一次的错误摆出来** —— 模型看到"你上次漏了 ⟦0⟧ ⟦1⟧"
       比看到一条通用规则有效得多；
    2. **给出记号的确切形式**（`⟦0⟧`、`⟦1⟧`），而不是抽象说"占位符"；
    3. **明确"位置可以变，记号不能少"** —— 这句是关键：
       中文语序和英文不同，模型常常因为"位置不好放"而干脆删掉。
       告诉它位置自由，它就不删了。

    ⚠️ 这里**不给**"丢掉会怎样"的反面例子。
    实测过：给负面例子时模型会**模仿那个错例**
    （见 `tests/README.md` 第 37 条，加负面例子后保住率从 8/20 掉到 3/20）。

    ⚠️ 也**不要**在这个提示里用 Markdown 记号或 emoji。
    第一版写成了带 `⚠️`、`**加粗**` 的排版，实测把模型带偏成
    输出 `{"t": ["清晨，油灯被点亮了。", ""]}` —— **JSON 结构都变了**，
    解析直接失败，重试等于没发生（而且是**静默**没发生）。
    追加到提示词末尾的"纠正指令"越朴素越好：编号 + 短句。
    """
    marks = "、".join(f"⟦{i}⟧" for i in range(len(slots)))
    shown = " ".join(slots[:6]) if slots else ""
    lines = [
        "补充要求（上一次翻译漏掉了记号，请重做）：",
        f"1. 这一段里必须有这些记号：{marks}",
    ]
    if shown:
        lines.append(f"2. 它们分别代表：{shown}")
    lines += [
        "3. 每个记号原样出现一次，不改数字、不新增、不删除。",
        "4. 记号在句子里的位置可以按中文语序调整，位置变了没关系，但一个都不能少。",
        "5. 只翻译记号之外的文字，格式仍然是 JSON 对象。",
    ]
    return "\n".join(lines)


def build_review_user_prompt(
    pairs: list[tuple[int, str, str]],
    *,
    glossary_block: str = "",
) -> str:
    """构造复审提示。``pairs`` 是 ``(编号, 原文, 初翻)``。"""
    parts: list[str] = []
    if glossary_block:
        parts.append(glossary_block)
    parts.append("请逐条检查下列译文（编号 → 原文 → 初翻）：")
    for i, src, dst in pairs:
        parts.append(f"{i} → {src}")
        parts.append(f"    初翻：{dst}")
    parts.append(
        f'\n返回 {len(pairs)} 个对象的 JSON 数组，格式 {{"i": 编号, "t": "修正后的译文"}}。'
    )
    return "\n".join(parts)
