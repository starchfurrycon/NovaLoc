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
   输出格式固定为对象数组，每一项都必须带索引：
   [{"i": 0, "t": "译文"}, {"i": 1, "t": "译文"}]
   `i` 必须与输入的编号严格对应。**绝对不要省略任何一项，也不要合并或拆分项目。**

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

8. 界面短标签要短。宁可简洁也不允许溢出原有的按钮/文本框。

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
VISION_READ_PROMPT = """请识别这张游戏贴图里的所有文字，逐条输出。

要求：
1. 只输出 JSON 数组，每项形如 {"text": "...", "bbox": [x1, y1, x2, y2]}，
   坐标用像素，相对图片左上角。
2. 按阅读顺序（从上到下、从左到右）排列。
3. 原样转录，不要翻译、不要纠正拼写、不要补全。
4. 若某处文字模糊到无法确认，用 "?" 代替不确定的字符，不要凭空猜测。
5. 忽略纯装饰性的无意义符号。

若图中确实没有任何文字，返回 []。"""


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

    parts.append("待翻译内容（编号 → 原文，⟦n⟧ 是必须原样保留的占位符）：")
    for i, text in enumerate(masked_texts):
        # 不用引号包裹，避免模型把引号当成内容的一部分
        parts.append(f"{i} → {text}")

    parts.append(
        f"\n请返回 {len(masked_texts)} 个对象的 JSON 数组，"
        f'每项形如 {{"i": 编号, "t": "中文译文"}}，编号必须一项不漏。'
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
    parts.append("原文（⟦n⟧ 是必须原样保留的占位符）：")
    parts.append(text)
    parts.append('\n只返回 JSON：{"t": "中文译文"}')
    return "\n".join(parts)


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
