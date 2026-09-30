"""语言识别与"是否需要翻译"的判定。

纯粹基于 Unicode 区段的启发式规则 —— 本工具只需区分
"已经是中文" / "是拉丁或日文" / "没有可翻译内容"，不需要通用语言识别模型。
"""

from __future__ import annotations

import re
import unicodedata

# 常用 CJK 统一表意文字
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 中日韩标点
_CJK_PUNCT = set("、。！？；：（）《》「」『』【】…—～·")
# 假名
_HIRAGANA_RE = re.compile(r"[\u3040-\u309f]")
_KATAKANA_RE = re.compile(r"[\u30a0-\u30ff]")
# 韩文
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
# 拉丁字母（含重音）
_LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
# 西里尔
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
# 任何字母
_ANY_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

#: 「绝不该出现在中译里」的字母。
#:
#: 目标语言是中文，合法字母只有 **汉字 / 拉丁 / 数字**。
#: 外来文字系统的**码点区段**（字符类内部文本，不含外层方括号）。
#:
#: 抽成单独常量是为了让"单个字符"和"连续段"两个正则**共用同一份区段** ——
#: 各写一份迟早会改漏一处，而这两处判据必须完全一致
#: （早先版本用 `pattern.strip("[]")` 去复用，结果
#: 把区段里的 `-` 一起切坏了，段正则静默匹配不到任何东西）。
_FOREIGN_SCRIPT_RANGES = (
    "\u0370-\u03ff"  # 希腊
    "\u0400-\u052f"  # 西里尔
    "\u0530-\u058f"  # 亚美尼亚
    "\u0590-\u05ff"  # 希伯来
    "\u0600-\u06ff"  # 阿拉伯
    "\u0700-\u074f"  # 叙利亚
    "\u0750-\u077f"  # 阿拉伯补充
    "\u0780-\u07bf"  # 塔纳
    "\u0900-\u097f"  # 天城
    "\u0980-\u09ff"  # 孟加拉
    "\u0a00-\u0a7f"  # 古木基
    "\u0a80-\u0aff"  # 古吉拉特
    "\u0b00-\u0b7f"  # 奥里亚
    "\u0b80-\u0bff"  # 泰米尔
    "\u0c00-\u0c7f"  # 泰卢固
    "\u0c80-\u0cff"  # 卡纳达
    "\u0d00-\u0d7f"  # 马拉雅拉姆
    "\u0d80-\u0dff"  # 僧伽罗
    "\u0e00-\u0e7f"  # 泰
    "\u0e80-\u0eff"  # 老挝
    "\u0f00-\u0fff"  # 藏
    "\u1000-\u109f"  # 缅甸
    "\u10a0-\u10ff"  # 格鲁吉亚
    "\u1200-\u137f"  # 埃塞俄比亚
)

#: 下面的区段全部是"模型跑偏跑到别的文字系统去了"。
#:
#: 注意**故意不含** CJK 与拉丁区段 —— 那两类是合法的；
#: 也**不含** 韩文/假名：那属于"源语言是日韩"的情况，
#: 另有 `looks_untranslated` / `guess_language` 去管。
_FOREIGN_SCRIPT_RE = re.compile("[" + _FOREIGN_SCRIPT_RANGES + "]")

# 常见控制码/转义序列（RPG Maker、Ren'Py、Unity 富文本……）
_CONTROL_PATTERNS = [
    r"\\[VvNnPpCcIiSs]\[\d+\]",         # \V[1] \N[1] 等 RPG Maker
    r"\\[|><^$.!]?",                     # RPG Maker 转义
    r"\{[^{}]*\}",                       # Ren'Py {b} {color=#fff} {w}
    r"\[[a-zA-Z]+(?:=[^\]]*)?\]",        # [b] [color=red]
    r"</?[a-zA-Z][^>]*>",                # <color=#fff> <size=20> <br>
    r"%[sdifgxXo0-9.+\-]*[sdifgxXo%]",   # printf
    r"\$\{[^}]*\}",                      # 模板
    r"\{\d+(?::[^}]*)?\}",               # {0} {1:format}
    r"&[a-zA-Z]+;|&#\d+;",               # HTML 实体
]

_PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    # **嵌套形式必须排在基础形式之前**：`\S[\V[101]]` 要整体算**一个**占位符。
    # 排在后面的话，基础规则会先只匹配到里面的 `\V[101]`，
    # 于是守卫认为"原文有 1 个占位符、译文有 1 个"——数量对得上，
    # 但结构已经坏了（外层 `\S[` 和 `]` 被当成可翻译的明文）。
    # 必须和 `translate/placeholders.py` 的排列顺序保持一致。
    ("rpgmaker_escape_nested", r"\\[VvNnPpCcIiSs]\[\\[VvNnPpCcIiSs]\[\d+\]\]"),
    ("rpgmaker_escape", r"\\[VvNnPpCcIiSs]\[\d+\]"),
    ("renpy_tag", r"\{/?[a-zA-Z]+(?:=[^{}]*)?\}"),
    ("rich_text_tag", r"</?[a-zA-Z][^>]*>"),
    ("bracket_tag", r"\[/?[a-zA-Z]+(?:=[^\]]*)?\]"),
    ("printf", r"%(?:\d+\$)?[-+#0]*[\d*]*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrs%]"),
    ("brace_index", r"\{\d+(?::[^{}]*)?\}"),
    ("template_var", r"\$\{[^}]*\}"),
    ("html_entity", r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);"),
    ("newline_escape", r"\\n"),
    ("fmt_escape", r"\\{2,}"),
    # 插件定义的裸字母占位符：`\D`（伤害表达式）、`\R`（次数表达式）。
    # 这两个**不是** RPG Maker 原生转义，是游戏插件自己解析的。
    # 必须和 `translate/placeholders.py` 同步 —— 只在一侧加会重现
    # "守卫认、屏蔽器不认"（或反之）的老问题，实测代价是 91 条静默损坏。
    ("plugin_letter_escape", r"\\[DR]"),
]

# 明确不该送进翻译引擎的东西
_BLOCKLIST_EXACT = {
    "ok", "no", "yes",  # 这类单词在游戏里常是按键提示，翻译反而有害——但仍交给用户决定
}

_NUMERIC_RE = re.compile(r"^[\s\d\W_]*$", re.UNICODE)

#: 连续的**外来文字段**（长度 ≥ 2 才会有意义）。
#:
#: 和 `_FOREIGN_SCRIPT_RE` **共用同一份区段常量**：
#: 各写一份迟早改漏一处，而这两处判据必须完全一致。
#: 之所以要 >=2，是因为单个外来字符（`π`）是正常符号，
#: 调用方还会再按 `min_run` 过滤一次，这里只负责"切段"。
_FOREIGN_RUN_RE = re.compile("(?:" + "[" + _FOREIGN_SCRIPT_RANGES + "]" + "){2,}")


def count_cjk(text: str) -> int:
    return len(_CJK_RE.findall(text))


def cjk_ratio(text: str) -> float:
    letters = _ANY_LETTER.findall(text)
    if not letters:
        return 0.0
    return count_cjk(text) / len(letters)


def has_kana(text: str) -> bool:
    return bool(_HIRAGANA_RE.search(text) or _KATAKANA_RE.search(text))


def has_hangul(text: str) -> bool:
    return bool(_HANGUL_RE.search(text))


def has_latin(text: str) -> bool:
    return bool(_LATIN_RE.search(text))


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


#: 富文本/HTML 风格的标签：`<right>`、`</right>`、`<color=#fff>`、`<br>`
_VISIBLE_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>|\[/?[a-zA-Z]+(?:=[^\]]*)?\]")


def visible_text(text: str) -> str:
    """去掉**不显示给玩家看**的部分：富文本标签、RPG Maker 转义。

    比例类判据必须拿"玩家真正看到的字"当分母。
    实测教训：`'<right>苏 กี้</right>'` 里的 `<right>`/`</right>`
    一共 12 个字母，把外来比例的**分母撑大了 2.4 倍**，
    于是比值从"明显异常"掉到 8.3%，判据放过去了。
    而这些标签在游戏里**一个字符都不显示**。
    """
    t = _VISIBLE_TAG_RE.sub("", text or "")
    t = re.sub(r"\\[VvNnPpCcIiSs]\[[^\]]*\]", "", t)
    return t


def count_foreign_script(text: str) -> int:
    """数出"绝不该出现在中译里"的外来字符个数（**含组合记号**）。

    中译目标语言是中文，所以合法字母只有三类：
    **汉字**、**拉丁**（专有名词/缩写保留）、**数字**。
    其余任何字母（阿拉伯、泰、天城、希伯来、希腊、亚美尼亚、
    马拉雅拉姆、埃塞俄比亚、格鲁吉亚……）都是**模型跑偏**。

    真实记录（BeyondPortal）：

    * 说话人名 `'Suky'` → `'<right>苏 กี้</right>'`
    * 台词 → `'我现在就想让你 دخول我!!!'`（**阿拉伯文**）
    * 台词 → `'...ജവീ...'`（**马拉雅拉姆文**）

    ## 为什么不能只数"字母"

    泰文/天城文/阿拉伯文的**元音与声调是组合记号**，
    Python 不把它们算作 letter。实测 `'กี้'` 用 `str.isalpha()`
    逐字符数只得到 **1 个**（`ก`），`ี` 和 `้` 都被漏掉 ——
    于是 `'<right>苏 กี้</right>'` 的外来比例只有 8.3%，判据放过去了。

    所以这里数的是**原始字符**（用区段匹配），不是 letter。
    """
    return len(_FOREIGN_SCRIPT_RE.findall(text or ""))


def longest_foreign_run(text: str) -> str:
    """最长的一段连续外来文字（用来报告"混进了什么"）。"""
    best = ""
    cur = ""
    for ch in visible_text(text):
        if _FOREIGN_SCRIPT_RE.match(ch):
            cur += ch
            if len(cur) > len(best):
                best = cur
        else:
            cur = ""
    return best


def foreign_script_runs(text: str) -> list[str]:
    """切出所有**连续外来文字段**（用来判断"是不是混进来一个词"）。

    ## 为什么需要"段"，而不能只看个数或比例

    真实译文里，最长外来段的长度分布是**清清楚楚的两堆**：

        长度 3：2 条     长度 4：3 条     长度 5：2 条

    而且这 **7 条全部是跑偏**，一条都不是正常译文：

        '而且你竟然饶了它们 ജീവ'          ← 马拉雅拉姆文
        '在你离开之前，我还有 кое-что, …'  ← 西里尔
        '我现在就想让你 دخول我!!!'          ← 阿拉伯文
        '你…？我 دیگه不用说了，对吧？'      ← 波斯/阿拉伯
        '我 دیگه没时间了。'
        '不过你总是 таком不可思议的样子，'  ← 西里尔
        '这只 الوحش 无懈可击，…'            ← 阿拉伯文

    正常译文里**一个"成串的外来词"都没有** ——
    只有单个数学/单位符号（`π`/`β`）。

    ## 为什么"比例"这个判据不够（它只是碰巧挡住了一部分）

    比例判据把边界设在 25%，而实测大量真跑偏**恰好落在 25.0%**：

        '而且你竟然饶了它们 ജീവ'          3/12 = 25.0%  → 放过
        '你…？我 دیگه不用说了，对吧？'     4/16 = 25.0%  → 放过

    **判据落在边界上，就说明这个判据本身选错了。**
    真正的区分不是"外来字符占多少"，而是
    **"有没有出现一个外来词"**：
    一个 4 字的马拉雅拉姆词混在中文句子里，
    哪怕整句只有它 3 个字符，也绝不可能是正常译文。

    所以这里返回的是**段**，由调用方按"段长度 ≥ 3"来判定 ——
    单个字符（`π`）永远不构成"词"。
    """
    return _FOREIGN_RUN_RE.findall(visible_text(text))


def has_foreign_word(text: str, *, min_run: int = 3, source: str = "") -> list[str]:
    """译文里出现了**成串的外来文字**（即"混进来一个词"）吗？

    返回需要报告的段（空列表 = 干净）。

    ## 为什么要排除"源文里本来就有"的那些

    如果原文里就有那个西里尔/希腊词（比如专有名词、引文），
    译文保留它是**正确的**，不能判坏。

    实测 7 条真跑偏**没有一条**的外来段出现在源文里：
    模型是**自己编**出来的。所以"源文里没有"是一条干净的判据。

    ## `min_run` 为什么是 3

    单个外来字符是正常写法（`π`/`β`/`Ω` 在数值和术语里很常见，
    玩家也认得）。实测**最短的真跑偏段正好是 3**
    （`'ജീവ'`、`'кое'`），所以 3 既能覆盖全部真实案例，
    又能保住单字符符号。
    """
    runs = foreign_script_runs(text)
    if not runs:
        return []
    src_visible = visible_text(source) if source else ""
    bad: list[str] = []
    for r in runs:
        if len(r) < min_run:
            continue
        if src_visible and r in src_visible:
            continue  # 原文本来就有 → 保留是对的
        bad.append(r)
    return bad


def foreign_script_ratio(text: str) -> float:
    """外来字符占**玩家可见字符**的比例（``0.0`` 表示干净）。

    分母是 `visible_text()` 之后的非空白字符：
    既排除了不显示的富文本标签，也把组合记号算进来
    （细节见 `count_foreign_script` 的说明）。
    """
    stripped = visible_text(text)
    visible = [ch for ch in stripped if not ch.isspace()]
    if not visible:
        return 0.0
    return count_foreign_script(stripped) / len(visible)


def guess_language(text: str) -> str:
    """返回粗粒度语言标签：``zh`` / ``ja`` / ``ko`` / ``en`` / ``ru`` / ``unknown``。"""
    if not text.strip():
        return "unknown"
    if has_kana(text):
        return "ja"
    if has_hangul(text):
        return "ko"
    ratio = cjk_ratio(text)
    if ratio >= 0.25:
        return "zh"
    if has_latin(text):
        return "en"
    if has_cyrillic(text):
        return "ru"
    return "unknown"


def is_already_chinese(text: str, threshold: float = 0.5) -> bool:
    """判断是否已经是中文（避免二次翻译把中文搅乱）。"""
    stripped = strip_placeholders(text).strip()
    if not stripped:
        return False
    return cjk_ratio(stripped) >= threshold


def is_numeric_or_symbols(text: str) -> bool:
    return bool(_NUMERIC_RE.match(text))


def strip_placeholders(text: str) -> str:
    """去掉占位符与控制码，得到"真正的文字"。"""
    out = text
    for _, pat in _PLACEHOLDER_PATTERNS:
        out = re.sub(pat, " ", out)
    return out


def extract_placeholders(text: str) -> list[str]:
    """按出现顺序抽出所有占位符（用于完整性校验）。"""
    found: list[tuple[int, str]] = []
    for _, pat in _PLACEHOLDER_PATTERNS:
        for m in re.finditer(pat, text):
            found.append((m.start(), m.group(0)))
    found.sort(key=lambda x: x[0])
    return [f[1] for f in found]


def has_control_codes(text: str) -> bool:
    return any(re.search(p, text) for p in _CONTROL_PATTERNS)


def needs_translation(
    text: str,
    *,
    source_lang: str = "auto",
    blocklist: set[str] | None = None,
) -> tuple[bool, str]:
    """判定一条文本是否需要翻译。

    返回 ``(需要翻译, 跳过原因)``；原因为空串表示需要翻译。
    """
    if not text or not text.strip():
        return False, "empty"

    stripped = strip_placeholders(text).strip()
    if not stripped:
        return False, "placeholder_only"

    if blocklist and text.strip().lower() in blocklist:
        return False, "blocklist"

    # 纯数字/符号/空白
    if not _ANY_LETTER.search(stripped):
        return False, "no_letters"

    if is_numeric_or_symbols(stripped):
        return False, "no_letters"

    if source_lang in ("zh", "zh-Hans", "zh-Hant", "zh-CN", "zh-TW") or source_lang == "auto":
        if is_already_chinese(stripped):
            return False, "already_chinese"

    # 只有单个 ASCII 字符的（按键提示）通常不翻
    if len(stripped) == 1 and ord(stripped) < 128:
        return False, "control_code"

    return True, ""


def normalize_for_compare(text: str) -> str:
    """归一化用于翻译记忆比对：NFKC、去多余空白、统一大小写。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


# 车轮（Unity 富文本）里必须保留的属性名
RICH_TEXT_ATTRS = ("color", "size", "b", "i", "u", "s", "material", "quad", "sprite", "align", "alpha", "cspace", "indent", "line-height", "margin", "mark", "mspace", "nobr", "page", "pos", "rotate", "space", "style", "sub", "sup", "voffset", "width", "gradient", "font", "font-weight")
