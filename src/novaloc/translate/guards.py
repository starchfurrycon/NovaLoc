"""译文质量守卫。

在中文写回游戏之前，这一层负责回答："这条译文安全吗？"

这里所有的检查都是**确定性**的，不依赖模型，因此可以放心地作为最后一道闸门。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..lang import (
    count_cjk,
    count_foreign_script,
    extract_placeholders,
    foreign_script_ratio,
    foreign_script_runs,
    has_foreign_word,
    has_hangul,
    has_kana,
    has_latin,
    longest_foreign_run,
    normalize_for_compare,
    strip_placeholders,
    visible_text,
)
from .placeholders import percent_vars

# 模型偶尔会把提示词要求也照抄进译文。
#
# ## 这些模式必须**收得足够紧** —— 踩过一个很贵的坑
#
# 原先这里有一条裸的 ``r"抱歉"``（任何位置匹配）。真实数据里
# **34 条译文被它判成 prompt_leak 并整条丢弃**，而它们**全是误杀**：
#
#     原文 "I'm sorry..."            → 译文 "抱歉……"      ❌ 被丢
#     原文 '對不起……但是你的陰道很棒……！' → 译文 "对不起……"   ❌ 被丢
#     原文 '... Lo lamento...'        → 译文 "对不起……"     ❌ 被丢
#
# **"对不起"是剧情里最常见的台词之一**，"抱歉"就是它的标准译文。
# 判据把"正常的道歉"当成了"模型在道歉说我不能翻" ——
# 结果是玩家在游戏里看到 34 处空白对话框，而所有检查都是绿的。
#
# 共同教训：**判据要匹配"形状"，不只是"词"**。
# "模型在道歉"的形状是"抱歉 + 我无法/不能"，
# 光有"抱歉"两个字说明不了任何事。
_LEAK_PATTERNS = [
    # 提示词结构泄漏（行首才可能是）
    r"^\s*要求[:：]",
    r"^\s*译文[:：]",
    r"^\s*翻译[:：]",
    r"^\s*输出[:：]",
    r"^\s*```",
    r"JSON\s*数组",
    # 「以下是…」单独出现**不算泄漏** —— `'以下是你的奖励。'` 是正常台词。
    # 只有后面跟着"翻译/译文/结果/输出"这类**在说翻译这件事**的词才算。
    r"以下是[^。！？\n]{0,8}(翻译|译文|结果|输出|答案)",
    # 自我指认（模型在描述自己的身份/能力）
    r"作为(一名|一个)?\s*(AI|人工智能|语言模型|助手)",
    r"(AI|人工智能|语言模型)\s*(助手)?\s*(无法|不能|不便)",
    # 拒答：**道歉必须和"做不到"同时出现**才算拒答。
    # 只有"抱歉"两个字是剧情，不是泄漏（见上面那 34 条误杀）。
    r"抱歉[，,、\s]*[^。！？\n]{0,12}?(无法|不能|没法|不便|做不到|没办法)",
    r"(无法|不能|没法|不便)(确定|识别|翻译|处理|回答)[^。！？\n]{0,12}(具体|这段|这个|该)",
    # 英文侧的同类拒答（同样要求"apologize + cannot"共现）。
    # 这里用**局部标志** `(?i:...)` 而不是全局 `(?i)` ——
    # 这些模式是被 `"|".join()` 拼成一条大正则的，
    # 全局标志只要不在最开头就会报
    # `global flags not at the start of the expression`。
    r"(?i:\b(sorry|apolog)\w*[^.\n]{0,30}\b(can(no|')?t|cannot|unable)\b)",
    r"(?i:\b(unable|not able)\s+to\s+(translate|read|determine|identify))",
    r"(?i:\bno\s+text\s+(was\s+)?(found|detected|visible))",
]

_LEAK_RE = re.compile("|".join(_LEAK_PATTERNS), re.MULTILINE)

#: 拒答判据里的"间隔"窗口，只允许被这些**不显示给玩家**的记号填充。
#:
#: ## 为什么需要这一层（一个真实的回归）
#:
#: 拒答判据的形状是"道歉 + 做不到同时出现且隔得不远"，
#: 中间用 `[^。！？\n]{0,12}?` 限制窗口。真实数据里
#: `请稍等，我现在无法确定` 这类写法证明"中间可以有内容"是必要的。
#:
#: 但**补回占位符之后**，记号会插进这个窗口里：
#:
#:     模型回复: '抱歉，我无法完成这个请求。'      → check_leak 命中 ✅
#:     补回标签: '抱歉，<color=#ff0000></color>我无法…'  → check_leak **漏掉** ❌
#:
#: `<color=#ff0000></color>` 是 25 个字符，把窗口撑爆了。
#: 而玩家在游戏里**一个字都看不到它** —— 它是颜色标签。
#:
#: 所以判据必须看"玩家读到的文本"：把**样式/时机**类记号从窗口里剔掉。
#:
#: ⚠️ **绝不能直接把所有占位符都剔掉**：`\V[1]`（变量）、`%s`（参数）
#: 在句子里占一个**实义位置**，剔掉它们会让
#: `'抱歉，\V[1]做不到'` 变成 `'抱歉，做不到'` —— 窗口里凭空少了一段，
#: 反而更容易误判。这里只剔"纯样式"的那批（颜色/图标/停顿/标签）。
_LEAK_GAP_MARKUP_RE = re.compile(
    # `\C[6]` `\I[4]` —— 颜色/图标（带 [n] 参数的基本形式）
    r"\\[CImgpP]\[\d+\]"
    # `\C` `\p` —— 不带参数的写法（`\p` 是"等待并行事件"）
    r"|\\[CImgpP](?!\[)"
    # 停顿/等待/瞬间显示： `\|` `\.` `\!` `\>` `\<` `\^` `\$`
    r"|\\[|.!><^$]"
    r"|</?[A-Za-z][^<>]{0,60}?/?>"   # HTML / 富文本标签（含属性）
    r"|\{[^}]{0,30}\}"               # Ren'Py 标签
    r"|\[[A-Za-z_][^\]]{0,30}\]"     # [b] [color=red]
)

_MD_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


@dataclass
class GuardResult:
    text: str
    warnings: list[str] = field(default_factory=list)
    fatal: bool = False
    """fatal=True 表示绝不能写回游戏。"""

    def ok(self) -> bool:
        return not self.fatal


def clean_translation(text: str) -> str:
    """剥掉模型爱加的外壳：代码块、前后引号、多余空白。"""
    t = text.strip()
    t = _MD_FENCE_RE.sub("", t).strip()
    # 去掉整体包裹的成对引号
    for q in ('"', "'", "“”", "「」", "『』"):
        if len(q) == 1 and len(t) >= 2 and t[0] == q and t[-1] == q or len(q) == 2 and len(t) >= 2 and t[0] == q[0] and t[-1] == q[1]:
            t = t[1:-1].strip()
    return t


def check_foreign_script(
    target: str,
    *,
    max_ratio: float = 0.25,
    source: str = "",
) -> list[str]:
    """译文里混进了"绝不该出现"的文字系统吗？

    目标语言是中文，合法字母只有 **汉字 / 拉丁 / 数字**。
    其余任何文字都说明**模型跑偏了**。真实记录（BeyondPortal）：
    共 7 条，模型把整句改成别的语言，或者按发音硬凑了一个外文词：

    * `'Suky'` → `'<right>苏 กี้</right>'`（**泰文**）
    * → `'我现在就想让你 دخول我!!!'`（**阿拉伯文**）
    * → `'在你离开之前，我还有 кое-что, …'`（**西里尔**）
    * → `'而且你竟然饶了它们 ജീവ'`（**马拉雅拉姆文**）

    这些译文**通过了原先所有检查** —— 非空、长度合理、
    占位符完好、也含汉字。玩家看到的是夹杂外文的乱码句。

    ## 主判据：出现了**成串的外来文字**（即"混进来一个词"）

    见 `lang.has_foreign_word`。实测 24,234 条真实译文里：

        旧判据（比例 > 25% 且外来字符 >= 2）  命中 3 条
        新判据（连续外来段 >= 3 个字符）        命中 7 条
        新判据多抓 4 条、**漏掉 0 条**、误报 0 条

    为什么比例判据不够：真跑偏**大量恰好落在 25.0%** 这个边界上
    （`'而且你竟然饶了它们 ജീവ'` 3/12、`'你…？我 دیگه…'` 4/16）。
    **判据落在边界上，就说明判据选错了。**
    真正的区分不是"外来字符占多少"，而是"有没有出现一个外来词"——
    一个 4 字的马拉雅拉姆词混在中文句子里，
    哪怕整句只有它 3 个字符，也绝不可能是正常译文。

    `source` 传进来时会排除"原文本来就有"的外来词
    （专有名词、引文 → 保留是对的）。

    ## 保留比例判据作为兜底

    如果模型改用**单个外来字符乱凑**（把 `'Suky'` 写成 `'ส ุ ข ี'`），
    段判据会被空格/组合记号打散。这时比例判据仍然有效。
    两个条件**任一命中**就判坏。
    """
    bad = has_foreign_word(target, source=source)
    if bad:
        joined = "".join(bad)
        n = count_foreign_script(target)
        ratio = foreign_script_ratio(target)
        return [f"foreign_script:{joined}({len(bad)}段/{n}字符/{ratio:.0%})"]

    # ---- 兜底：比例判据 ----
    #
    # 用于"段判据被空格/拆字打散"的情况（见
    # `test_fallback_ratio_catches_spaced_out_characters`）。
    #
    # 阈值都拿真实数据校准过：
    # * **至少 2 个**：单个外来字符多半是**正常写法** ——
    #   希腊字母 `π`/`β` 在数值和术语里很常见，玩家也认得。
    # * **比例 > 25%**：分母是"玩家可见字符"（见 `lang.visible_text`）。
    #
    # ⚠️ 这里**必须也排除"原文里本来就有"的外来字符**，
    # 否则会把 `'падеж means case'` → `'падеж 是格的意思'`
    # 这种**正确**的译文判坏（实测比例 50%，远超阈值）。
    # 段判据排除了源文，兜底判据却不排除 —— 两条判据口径不一致，
    # 就会在"段判据没命中、比例判据命中"的缝隙里误杀。
    # 数**所有**外来字符，再扣掉"已经由段判据处理过"的部分
    # （长度 >= 3 的段）—— 它们要么已判坏、要么已在源文里被放行，
    # 两种情况都不该在这里重复计数。
    #
    # 剩下的就是被空格/拆字打散的单个字符。
    # 真实样本：`'<right>د ه گ ی</right>'` —— 模型把阿拉伯词拆成单字符加空格，
    # 段判据完全失效（每段只有 1 个字符），只有比例判据救得回来。
    #
    # ⚠️ 不能用"把单字符段加起来"来算：`_FOREIGN_RUN_RE` 要求连续 >= 2 个，
    # 所以被空格隔开的单字符**根本不会形成段**，加起来永远是 0。
    # 必须从 `count_foreign_script()`（按字符类直接数）里扣。
    long_runs = [r for r in foreign_script_runs(target) if len(r) >= 3]
    stray = count_foreign_script(target) - sum(len(r) for r in long_runs)
    if stray < 2:
        return []
    visible = [ch for ch in visible_text(target) if not ch.isspace()]
    if not visible:
        return []
    ratio = stray / len(visible)
    if ratio > max_ratio:
        run = longest_foreign_run(target)
        return [f"foreign_script:{run}({stray}字符/{ratio:.0%})"]
    return []


def check_placeholders(source: str, target: str) -> list[str]:
    """校验占位符完整性。"""
    warnings: list[str] = []
    src = extract_placeholders(source)
    tgt = extract_placeholders(target)
    if len(src) != len(tgt):
        warnings.append(f"placeholder_count:{len(src)}->{len(tgt)}")
        return warnings
    cs, ct = Counter(src), Counter(tgt)
    if cs != ct:
        missing = list((cs - ct).elements())
        extra = list((ct - cs).elements())
        if missing:
            warnings.append("placeholder_missing:" + "|".join(missing[:5]))
        if extra:
            warnings.append("placeholder_extra:" + "|".join(extra[:5]))
    return warnings


def check_length(source: str, target: str, max_chars: int | None, ratio: float) -> list[str]:
    warnings: list[str] = []
    stripped_t = strip_placeholders(target).strip()
    if max_chars is not None and len(stripped_t) > max_chars:
        warnings.append(f"too_long:{len(stripped_t)}>{max_chars}")
    src_len = max(1, len(strip_placeholders(source).strip()))
    if len(stripped_t) > src_len * ratio + 6:
        warnings.append(f"too_long_ratio:{len(stripped_t)}/{src_len}")
    if not stripped_t:
        warnings.append("empty_translation")
    return warnings


def looks_untranslated(source: str, target: str) -> bool:
    """译文看起来和原文是同一语言 / 根本没翻。

    判定顺序（宁可漏报，不可误报 —— 误报会把好译文丢掉）：
    1. 译文为空，或与原文归一化后完全相同；
    2. 译文一个汉字都没有，但仍含拉丁字母（说明模型原样吐回来了）。
    """
    s = normalize_for_compare(strip_placeholders(source))
    t = normalize_for_compare(strip_placeholders(target))
    if not t:
        return True
    if s == t:
        return True

    # 译文一个汉字都没有、却还有拉丁字母 → 模型原样吐回来了
    return count_cjk(target) == 0 and has_latin(target)


def check_leak(target: str) -> list[str]:
    r"""译文是不是模型在"说它自己"（提示词泄漏 / 拒答 / 元话）。

    先在两种形态上各查一遍：

    1. **原样**；
    2. **剔掉纯样式记号之后**。

    第 2 种是必需的：补回占位符会把 `<color=#ff0000></color>` 这类标签
    插进"道歉 … 做不到"的窗口里，把窗口撑爆，于是**拒答被漏掉**
    （真实回归，见 `_LEAK_GAP_MARKUP_RE` 的说明）。
    而玩家在游戏里一个字符都看不到这些标签。
    r"""
    if _LEAK_RE.search(target):
        return ["prompt_leak"]
    stripped = _LEAK_GAP_MARKUP_RE.sub("", target or "")
    if stripped != target and _LEAK_RE.search(stripped):
        return ["prompt_leak"]
    return []


#: 复读检测用的 n-gram 长度（字符数）
_REPEAT_N = 4


def _repeated_ngrams(text: str, n: int = _REPEAT_N, *, min_hits: int = 3) -> list[str]:
    """找出在文本里重复出现达到 ``min_hits`` 次的 n-gram（按出现次数降序）。"""
    if len(text) < n * min_hits:
        return []
    counts: Counter[str] = Counter()
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        if gram.strip():  # 忽略纯空白
            counts[gram] += 1
    hits = [(g, c) for g, c in counts.items() if c >= min_hits]
    hits.sort(key=lambda gc: -gc[1])
    # 去掉被更长重复串包含的短串，避免一次复读报出一堆碎片
    out: list[str] = []
    for gram, _c in hits:
        if not any(gram in kept for kept in out):
            out.append(gram)
    return out[:5]


def check_repetition(source: str, target: str) -> list[str]:
    """检测模型复读。

    本地小模型（尤其没设 ``repeat_penalty`` 时）会把同一短语吐很多遍。
    这是**不可修复**的故障，必须判致命 —— 写回游戏会得到一屏"重复重复重复"。

    关键：**先减掉原文里本来就有的重复**。原文如果自己就写了
    "no no no"，译文重复同样的词是正常翻译，不该误报。
    """
    src_grams = set(_repeated_ngrams(strip_placeholders(source), min_hits=2))
    hits = _repeated_ngrams(target)
    novel = [g for g in hits if g not in src_grams]
    if not novel:
        return []
    return ["repetition:" + "|".join(novel[:3])]


def check_language_residue(source: str, target: str, target_lang: str) -> list[str]:
    """检测译文里残留的**源语言字符**。

    最常见的是日语游戏文本翻成中文后，译文里还夹着平假名/片假名 ——
    说明模型把没翻完的片段直接抄了回来。这类残留在游戏里非常显眼。
    """
    warnings: list[str] = []
    if not target:
        return warnings
    tl = (target_lang or "").lower()
    is_chinese_target = tl.startswith("zh")
    if is_chinese_target and has_kana(target):
        # 原文本身是日文时，专有名词可能有意保留假名；但只要整句还有假名
        # 且汉字很少，就基本可以确定是漏译
        if count_cjk(target) < len(target) * 0.5:
            warnings.append("kana_residue")
    if is_chinese_target and has_hangul(target):
        warnings.append("hangul_residue")
    return warnings


def check_percent_vars(source: str, target: str) -> list[str]:
    r"""RPG Maker 的 `%1` `%2` 消息替换变量必须**一个不少**。

    ## 为什么单独查这个（而不是靠掩码）

    直觉做法是把 `%1` 屏蔽成 `⟦0⟧`"保护"起来。**实测这是反的**：
    屏蔽之后模型更容易把它当噪音清掉。

    拿 14 条**真实丢过变量**的源文做 A/B：

    | 做法 | `%n` 保住 |
    |---|---|
    | 屏蔽成 `⟦0⟧`（+ 三版提示词强调） | **0～2 / 14** |
    | **不屏蔽**，让模型看到 `%1` | **约 7 / 14** |

    试过 6 种记号长相、3 版提示词，屏蔽路径最好只到 2/14。
    原因：`⟦0⟧` 是没有语义的装饰符，而 `%1` 在训练数据里是
    有含义的格式串 —— **把语义换成装饰，模型就把它当噪音清掉了**。

    所以改成"**让模型看得见，出站再查**"：
    不屏蔽，翻完之后在这里比对数量。查数量不需要把记号藏起来。

    ## 判据为什么是"数量"而不是"位置"

    位置的正确性无法自动判定：`'%1 attacks!'` 译成 `'%1 攻击！'`
    与 `'攻击！%1'` 都不算错，中文语序本来就活。
    而**数量少一个**是确定无疑的损坏 —— 玩家会看到一句
    "谁干了什么"都说不清的话。宁可报失败，也不写一句坏话进游戏。
    """
    sv, tv = percent_vars(source), percent_vars(target)
    if not sv:
        return []
    if len(tv) == len(sv):
        # 数量对得上就不再苛求位置；但**完全不出现**也不行（数量能对上
        # 却内容不同，例如 `%1` 变成 `%2`，那会把行动者显示成目标）。
        if sorted(sv) != sorted(tv):
            return [
                f"percent_var_changed:变量内容变了 {sorted(sv)} → {sorted(tv)}"
            ]
        return []
    return [
        f"percent_var_missing:丢失消息变量 {sorted(set(sv) - set(tv))}"
        f"（源 {len(sv)} 个 → 译 {len(tv)} 个）"
    ]


def guard(
    source: str,
    raw_target: str,
    *,
    max_chars: int | None = None,
    length_ratio: float = 2.2,
    allow_untranslated: bool = False,
    target_lang: str = "zh-Hans",
    check_repeat: bool = True,
) -> GuardResult:
    """对单条译文做完整校验。"""
    target = clean_translation(raw_target)
    warnings: list[str] = []

    warnings += check_leak(target)
    if "prompt_leak" in warnings:
        return GuardResult(text="", warnings=warnings, fatal=True)

    if not target:
        return GuardResult(text="", warnings=["empty_translation"], fatal=True)

    warnings += check_placeholders(source, target)
    warnings += check_percent_vars(source, target)
    warnings += check_length(source, target, max_chars, length_ratio)
    warnings += check_language_residue(source, target, target_lang)
    warnings += check_foreign_script(target, source=source)
    if check_repeat:
        warnings += check_repetition(source, target)

    if not allow_untranslated and looks_untranslated(source, target):
        warnings.append("looks_untranslated")

    # 致命错误分四类：
    #  * 占位符崩掉 —— 写回去游戏会崩或丢变量；
    #  * 复读 —— 不可修复，写回去是一屏重复文字；
    #  * 混进别的文字系统 —— 玩家看到的是一句夹杂阿拉伯/泰文的乱码；
    #  * 空译文。
    # 其余（过长、像是没翻）只警告，交给用户在审校界面判断。
    fatal = (
        any(
            w.startswith(
                (
                    "placeholder_count",
                    "placeholder_missing",
                    "placeholder_extra",
                    "percent_var_missing",
                    "percent_var_changed",
                    "repetition",
                    "foreign_script",
                )
            )
            for w in warnings
        )
        or not target
    )
    return GuardResult(text=target, warnings=warnings, fatal=fatal)


#: **写回前的硬门禁** —— 一套判据，多处执行。
#:
#: ## 为什么必须是一个具名函数，而不是把两个调用抄三遍
#:
#: 这两条判据目前在**三个地方**执行：
#:
#: 1. `translate` 的强制重译判定（"已完成"也要能被推翻）；
#: 2. `translate` 开头的重查入口（作废旧译文）；
#: 3. `apply` 的回写闸门（最后一道）。
#:
#: 曾经它们是**各写各的**，代价很具体：`%n` 判据上线时只加进了
#: 第 2 处，于是 **120 条**丢了消息变量的译文在 1 和 3 处被放行 ——
#: 重查作废它们，下一轮又按同样方式写回来（还标成"已翻译"），
#: 每轮空跑一次，坏数据一条没少。
#:
#: **判据有多个执行点时，它们必须指向同一份定义。**
#: 加新判据只需要改这里，三处同时生效。
def is_unsafe_writeback(source: str, target: str) -> bool:
    """这条译文**绝对不能写回游戏**吗？

    与 :func:`guard` 的 `fatal` 不同：`guard` 是在翻译**过程中**做全量校验
    （还管占位符、复读、长度），这里只抽查那两条**"产物已经写下去了才发现"**
    的判据 —— 它们要么让玩家看到乱码，要么让玩家看不到是谁做了什么。
    """
    return bool(
        check_foreign_script(target, source=source) or check_percent_vars(source, target)
    )


def summarize_warnings(entries_warnings: list[list[str]]) -> dict[str, int]:
    """统计告警分布，用于质检报告。"""
    out: dict[str, int] = {}
    for ws in entries_warnings:
        for w in ws:
            key = w.split(":", 1)[0]
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
