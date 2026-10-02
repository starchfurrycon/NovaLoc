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


#: 切句前先把缩写里的句点保护起来，否则 ``Mr. Smith`` 会被切成两句，
#: 让"源句数"虚高 —— 就会误报一大批正常译文。
_ABBR_RE = re.compile(
    r"\b(Mr|Mrs|Ms|Dr|St|vs|etc|e\.g|i\.e|No|Fig|approx|Inc|Ltd|Jr|Sr|Prof)\.|"
    r"\b([A-Z])\.(?=\s*[A-Z])",
    re.IGNORECASE,
)
#: 缩写保护用的哨兵。**必须是 \x00**：它不出现在任何真实文本里，
#: 而且能让下面的 `[^\x00]` 精确表达"这一段不是缩写"。
_ABBR_SENTINEL = "\x00"

#: ★ 标点串：连续的句末标点（``.`` ``!`` ``?`` ``;``）加尾随空白。
#:
#: ## 为什么标点必须"整段"识别，而不是逐个判句末（实测踩出来的坑）
#:
#: 最初的写法是把"句末标点 + 后跟空白"当切割点：``[.!?;]+(?=\s|$)``。
#: ``+`` 没有上界，对 ``Sigh... Whatever`` 它只吃**第一个点**就满足了，
#: 于是把一句切成两句：
#:
#:     「Sigh... Whatever, let's go...」
#:       → ["Sigh", " Whatever, let's go"]   ← 2 句，其实只有 1 句
#:       → 中文译文只用「…」→ 判"缺句"      ← **假阳性**
#:
#: 实测这条假阳性占了开火样本的**大多数**，必须修掉。
_LATIN_PUNCT_RUN_RE = re.compile(r"[.!?;]+\s*")
#: 中文译文：全角句末标点（``。！？；``）为主，半角 ``.!?;`` 也接受。
_CJK_PUNCT_RUN_RE = re.compile(r"[。！？；!?;]+\s*")


def _terminates_sentence(run: str) -> bool:
    """这段标点本身能不能收句？

    ## 为什么单靠"标点后跟空白"不够

    ``...`` 是**语气**（对话里到处都是），不是句子边界；但 ``..`` 这种
    残缺省略号同样不该收句。而 ``?!`` / ``!?`` / ``???`` 是**加强语气**，
    确实收句。所以按"标点**种类**"区分，而不是按"标点是不是跟在空白前"：

    * 纯点号（``..`` / ``...``）→ **不收句**（省略号/语气）
    * 单个 ``.`` → 收句
    * 任何含 ``!``/``?``/``;`` 的串（``?!`` / ``!`` / ``?`` / ``;``）→ 收句

    这条规则让 ``Sigh... Whatever`` 保持 1 句，同时 ``What?! No way!``
    仍然是 2 句 —— 两者的区别正是"省略号 vs 加强语气"。
    """
    marks = run.strip()
    if not marks:
        return False
    if set(marks) == {"."}:
        # 纯点号：一个点是句末，两个及以上是省略号（语气），不收句
        return len(marks) == 1
    return True


def _norm_breaks(text: str) -> str:
    """把换行抹成空格，用于**数句子**。

    ## 为什么必须抹平（实测）

    模型经常把一句话在中间**折行**（``消耗 0\\n 点资源``），也可能把源文的
    两行并成一行。换行位置一变，句子数就跟着变 —— 源 2 句、译文 1 句，
    看起来像"丢了一句"，其实内容完整。实测源文含换行的条目占 **72.1%**，
    不抹平的话守卫会说 11.8% 的条目有问题，绝大多数是噪声。

    抹平后重测，开火率降到约 **3%**，剩下的才值得人看。

    换行**是否被保留**不是这条判据的职责 —— 那由 `ollama_provider` 的
    逐行兜底和 QA 的 `line_structure` 单独守着。这里只管"内容有没有少"。
    """
    return re.sub(r"[\r\n]+", " ", text)


def count_sentences(text: str, *, latin: bool) -> int:
    """数一条文本里有几句话（至少 1 句）。

    调用前请先过 :func:`_norm_breaks` —— 换行是**排版**，不是句子边界。

    ## 判据

    逐段扫描标点串。一段标点算"句末"当且仅当**它本身能收句**
    （见 :func:`_terminates_sentence`）**且它前面已经出现过实义字符**
    （否则像 ``... Hello`` 这种开头的省略号会白算一句）。于是：

    * ``One. Two.`` → 2 句 ✅
    * ``Add 4 cards to hand. Then discard 2.`` → 2 句 ✅
    * ``Sigh... Whatever, let's go...`` → 1 句 ✅
    * ``Wait... what?`` → 1 句 ✅
    * ``What?! No way!`` → 2 句 ✅

    ``latin=True`` 时先做**缩写保护**：``Mr.`` / ``vs.`` / ``e.g.`` / ``J.``
    里的句点不是句末。不保护会让源句数虚高，把大量正常译文误判成"缺句"。
    """
    if latin:
        text = _ABBR_RE.sub(
            lambda m: (m.group(0) or "").replace(".", _ABBR_SENTINEL), text
        )
        run_re = _LATIN_PUNCT_RUN_RE
    else:
        run_re = _CJK_PUNCT_RUN_RE

    sentences = 0
    #: 上一段标点之后是否已积累了实义字符（决定下一段标点能否收句）
    has_content = False
    pos = 0
    for m in run_re.finditer(text):
        if text[pos : m.start()].strip():
            has_content = True
        if has_content and _terminates_sentence(m.group(0)):
            sentences += 1
            has_content = False
        pos = m.end()
    # 结尾还有没被标点收掉的残余内容，也算一句
    if text[pos:].strip():
        sentences += 1
    return max(1, sentences)


def check_sentence_drop(source: str, target: str) -> list[str]:
    """源文有几句、译文只剩几句 —— 抓"翻译时丢掉整句"。

    ## 为什么需要这条（实测出来的）

    ``check_length`` 只能查**长度比**，而英→中的正常长度比中位数实测约
    **0.35**，所以阈值必须放得很低（否则误报正常译文），一低就
    **抓不住"丢一个从句"**。真实数据 5,224 条已判成功的译文里，
    按句子数比对有 **480 条（9.2%）**缺句，而旧守卫对它们全部放行：

        Add 4 cards from the deck to hand. Then, randomly discard 2 cards.
        → 加入 4 张牌。                    ← 丢掉的正是"随机弃 2 张"这条玩法规则

        Guaranteed escape from battle. Cannot be used on Area Bosses.
        → 保证在战斗中脱离。               ← 丢掉了使用限制

    丢的是**玩法规则**，玩家照着界面文字操作会出错 —— 属于影响体验的
    严重缺陷，不是措辞瑕疵。

    ## 为什么用句子数而不是长度

    句子数是**结构信号**：源文 3 句、译文 2 句，就值得复核。它不依赖语言对，
    也不会因为"中文天然更短"而误报；长度比则会。

    ## 已知漏报（**必须如实说明**）

    句子数**相等**不等于内容完整。实测在"句数相等且源文较长"的 283 条里
    仍能看到错译：

        Halve Damage → 半身防御          ← 词义错，句数一致，本判据抓不住
        我要填满用我的精液去…             ← 句子破碎，句数一致，抓不住

    所以这条判据只能减少**丢内容**，不能保证**译得对**。

    ## 只对"拉丁字母为主的源文"生效

    中日文源文的句末标点与中文译文的切分习惯差异大，按句数比会大量误报，
    所以只在源文以拉丁字母为主时才检查。
    """
    warnings: list[str] = []
    # 只对拉丁源文检查（中日文源文误报率高，见 docstring）
    if len(re.findall(r"[A-Za-z]", source)) < 12:
        return warnings

    # ★ 换行是**排版**信号，不是句子边界，比对前必须抹平。
    #
    # 实测踩出来的：模型经常把一句话在中间**折行**，于是源 2 句、译文 3 句；
    # 反过来若译文把两行并成一行，就成了"源 2 句、译文 1 句"的**假阳性**。
    # 源文里 72.1% 的条目含换行，所以这条不抹平的话噪声极大。
    #
    # 换行**是否被保留**由 `ollama_provider` 的逐行兜底 + QA 的
    # `line_structure` 单独守着（那边管"排版有没有坏"），职责不重叠：
    # 这里只管"内容有没有少"。
    n_src = count_sentences(_norm_breaks(source), latin=True)
    if n_src < 2:
        return warnings
    n_tgt = count_sentences(_norm_breaks(target), latin=False)
    if n_tgt < n_src:
        warnings.append(f"sentence_drop:{n_src}->{n_tgt}")
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


#: hint 回声判据用的 n-gram 粒度。
#:
#: 取 2/3/4 并**取最大重合率**，理由见 :func:`check_hint_echo` 的
#: "为什么用多个粒度" —— 只用 4-gram 会漏掉"改写型复述"。
_HINT_NGRAM_SIZES = (2, 3, 4)

#: 默认粒度（``_ngrams`` 的缺省参数用）
_HINT_NGRAM = 4

#: hint 至少要有的 n-gram 个数，低于它这个粒度就不参与判断。
#:
#: 理由：比例判据在**短 hint** 上没有统计意义 ——
#: 一个 5 个字的 hint 只要有 2 个 bigram 撞上就是 100%。
#: 现在最短的 KIND_HINT 也有 30 字，所以这条只是防御性的。
_HINT_MIN_HINT_NGRAMS = 12

#: 判定阈值：hint 有多大比例被复现出来。
#:
#: 实测（真实泄漏与真实正常译文各若干条）：
#:   正常译文最高 ≈ 5%，真实泄漏最低 ≈ 44%。
#: 取 35% —— 落在那个数量级的空隙里。
_HINT_ECHO_RATIO = 0.35

#: 参与 n-gram 的字符：汉字 + 假名 + 拉丁字母 + 数字
_HINT_KEEP_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9]")


def _ngrams(text: str, n: int = _HINT_NGRAM) -> set[str]:
    """取"只保留实义字符"的 n-gram（忽略空白与标点）。"""
    clean = "".join(ch for ch in (text or "") if _HINT_KEEP_RE.match(ch))
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def check_hint_echo(
    target: str,
    hints: list[str] | None,
    *,
    source: str = "",
    min_ratio: float = 0.5,
) -> list[str]:
    r"""译文是不是把**我们发给模型的规则**（kind hint）复述了回来。

    ## 为什么单独做这一条判据

    `check_leak` 匹配的是"模型在说它自己"的**通用形状**
    （``要求：``、``抱歉…无法``、``作为 AI``）。它抓不到下面这种：

        源: "Demon:\nStill, we can't count on weapons…"
        译: "角色对白。保持说话者的语气、性格与语域；原文若是粗鲁/亲昵/
             敬语，中文也要对应。不要添加原文没有的称呼。"

    这**不是**通用元话，而是模型在**复述我们提示词里的
    ``KIND_HINT[DIALOGUE]``**。实测（HY-MT1.5-7B，36 条真实英文多行文本）
    有 2 条是这样；改成"``<原文>`` 包裹"的提示词形状后仍有 1 条
    —— 也就是**光改提示词形状治不好**，必须有产品侧的兜底。

    ## 为什么按 n-gram 重合度判，而不是按固定词表

    固定词表（"角色对白"、"物品/技能说明"…）有两个毛病：
    ① 提示词一改就得改词表；② 真正危险的是"整段被复述"，
    而词表只能看到零散关键词。

    改成**拿这条目实际收到的 hint 做比对**：hint 是运行时已知的，
    不需要维护词表，提示词改了判据自动跟着变。

    ## 为什么用**多个粒度**（2/3/4-gram）取最大值

    这是实测逼出来的。模型复述规则时常常**顺手改写几个字**：

        hint: 保持说话人的语气、性格与语域；原文若是粗鲁/亲昵/敬语…
        译文: 请保持说话者的语气、性格和语域；如果原文使用粗鲁、亲昵或敬语…

    "保持→请保持"、"若是→如果"、"与→和" —— 只按 4-gram 比，
    这些"改写型复述"的重合度会掉到 50% 以下而**漏判**（实测 5 条里漏 2 条）。

    改用 2/3/4-gram 的**最大重合率**后：改写只影响很短的片段，
    2-gram 层面依然几乎全中，于是漏判消失；而正常译文因为整体措辞不同，
    任何粒度都到不了阈值。

    ## 为什么不会误杀（实测）

    在 DemonsRoots **已经发出的 3037 条真实译文**（全量 45548 条的等距抽样）
    上跑这条判据：**命中 0 条**。所以留了足够的安全边际来调高灵敏度。

    ## ``source`` 的作用

    如果**原文本身**就含这些字（比如原文是中文且内容就是这句规则），
    译文出现它们是正常的。所以 hint 与 source 共有的 n-gram 不计入。
    """
    hints = [h for h in (hints or []) if h and h.strip()]
    if not hints or not (target or "").strip():
        return []

    # ══════════════════════════════════════════════════════════════════
    # 判据形态：**规则被复现的比例**（hint coverage），不是"译文里有多少像规则"
    # ══════════════════════════════════════════════════════════════════
    #
    # ## 两个方向，选错了就抓不住
    #
    # 一开始量的是"**译文里有多大比例来自 hint**"（hit / |目标 n-gram|）。
    # 这个方向对**改写型复述**不灵敏：模型把规则改写一遍，
    # 每个 n-gram 都差一两个字，命中的就少了；而译文越长，
    # 分母越大，比例被摊薄。
    # 实测那条被漏判的：命中 24/54 = **44.4%**，够不到 50% 的线。
    #
    # 换成量"**hint 有多大比例出现在译文里**"（hit / |hint n-gram|）
    # 就对了 —— 泄漏的定义本来就是"规则被抄了进来"，
    # 主语是**规则**，不是译文。同一条实测数据：
    #
    #     正常译文（演员：不过别担心…）    hint 被复现 2/43 =  4.7%
    #     泄漏（复述了整条 DIALOGUE 规则） hint 被复现 19/43 = 44.2%
    #
    # 4.7% 与 44.2% 之间有**一个数量级**的空隙，阈值取 35% 很安全。
    best = 0.0
    for n in _HINT_NGRAM_SIZES:
        tgt = _ngrams(target, n)
        if not tgt:
            continue
        # 与**原文**共有的 n-gram 不算：原文若本身就是中文规则文本，
        # 译文照抄是正常的（见 docstring 里 source 的说明）。
        src = _ngrams(source, n)
        for hint in hints:
            hg = _ngrams(hint, n) - src
            if len(hg) < _HINT_MIN_HINT_NGRAMS:
                # hint 太短，比例没有统计意义（几个字全中就是 100%）
                continue
            hit = len(tgt & hg)
            if hit:
                best = max(best, hit / len(hg))
    if best >= min_ratio:
        return [
            f"hint_echo: 提示词里那条风格规则有 {best:.0%} 的文字出现在了译文里，"
            "模型把'翻译要求'当成要翻译的内容复述了"
        ]
    return []


#: 复读检测用的 n-gram 长度（字符数）
_REPEAT_N = 4

#: 掩码记号（`⟦0⟧`）—— 复读检测比较前必须去掉，理由见 `_repeat_view`
_MASK_MARK_RE = re.compile(r"⟦\d+⟧")


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


def _repeat_view(text: str) -> str:
    r"""把文本化成"只看真实文字"的形态，供复读检测比较。

    ## 为什么两侧都要过这一道（真实游戏误报）

    复读检测的运行时机很微妙：`guard()` 在 provider 里被调用时，
    **原文是掩码状态**（``⟦0⟧``），而**译文已经还原**（引擎转义码回来了）。
    两边形态根本不一样：

    ```
    掩码原文  '…Weapon Type: ⟦0⟧⟦1⟧   Armor Type:⟦2⟧⟦3⟧⟦4⟧'
    还原译文  '…武器类型：\I[96]\I[97]   护甲类型：\I[129]\I[135]\I[139]'
    ```

    :func:`strip_placeholders` 认引擎转义码（``\I[96]``）和 ``%1``，
    但**不认掩码记号** ``⟦0⟧``。于是早先只对 source 调用它时：
    原文里的重复被减掉了，而**译文里还原出来的同一批图标码**
    （``\I[96]\I[97]`` / ``\I[129]\I[135]\I[139]``）没被减掉 ⇒
    n-gram ``']\I['``、``'\I[1'`` 命中 ≥3 次 ⇒ **误报复读**，
    整条被判致命、译文清空。

    实测这类误报在这个游戏里是 **13 条**，全部是长得完全正确的译文
    （`Actors.json` 的角色 profile，含武器/护甲图标码）。

    修法：两侧都去掉**掩码记号 + 引擎转义码**，只比较真实文字。
    这样"两侧括号不同但文字相同"的误报消失，
    而"译文里真实文字反复出现"的真复读照旧抓得到。
    """
    return strip_placeholders(_MASK_MARK_RE.sub("", text or ""))


def check_repetition(source: str, target: str) -> list[str]:
    r"""检测模型复读。

    本地小模型（尤其没设 ``repeat_penalty`` 时）会把同一短语吐很多遍。
    这是**不可修复**的故障，必须判致命 —— 写回游戏会得到一屏"重复重复重复"。

    关键：**先减掉原文里本来就有的重复**。原文如果自己就写了
    "no no no"，译文重复同样的词是正常翻译，不该误报。

    ## ★ 原文本身是"象声词/叠词"时**整个跳过**（真实数据校准）

    上面那条"减掉"只在**跨语言**时失效：原文是 `*Pant*`、译文是 `喘`，
    字符上一个都对不上，于是译文被当成"原文里没有的新重复"。

    实测这个游戏里被判 `repetition` 的条目，**抽 12 条重译全部是忠实翻译**：

        源 '*Pant* *Pant* *Pant*...'       译 '喘... 喘... 喘...'
        源 '*Shake* *Shake*! *Wobble*...'   译 '摇一摇！摇！摇摇晃晃！'
        源 '*Chomp* *Chomp* *Chomp*...'     译 '咀嚼… 咀嚼… 咕噜…'
        源 '*Smack* *Smack* *Smack*! ...'   译 '啪啪啪！那个男人…'

    这些**必须**跟着原文重复（原文就是靠重复表达动作次数）。
    判别方式看**原文**而不是译文：原文里同一个 4-gram 出现 ≥3 次，
    就说明"重复"是作者的手法，此时对重复下判断只会误杀。

    ## 代价的方向

    漏判复读 ⇒ 一屏重复文字（难看，但内容在）；
    误判忠实翻译 ⇒ **一条完全正确的译文被整条丢弃**（对话框空白）。
    后者更坏 —— 而且误判会把它送进重试，反复浪费模型调用。

    两侧都先过 :func:`_repeat_view` —— 理由见那里的说明
    （掩码记号与引擎转义码的形态差异会造出**假复读**）。
    """
    src_view = _repeat_view(source)
    # 原文自己就在重复 ⇒ 不判（象声词、叠词、"no no no" 都走这条）
    if _repeated_ngrams(src_view, min_hits=3):
        return []
    src_grams = set(_repeated_ngrams(src_view, min_hits=2))
    hits = _repeated_ngrams(_repeat_view(target))
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
    hints: list[str] | None = None,
) -> GuardResult:
    """对单条译文做完整校验。

    ``hints`` 是这条目在提示词里收到的**规则文本**（``KIND_HINT[kind]``）。
    传进来才能检出"模型把规则复述成译文"（见 :func:`check_hint_echo`）。
    不传就跳过这条判据 —— 老调用方行为不变。
    """
    target = clean_translation(raw_target)
    warnings: list[str] = []

    warnings += check_leak(target)
    warnings += check_hint_echo(target, hints, source=source)
    if "prompt_leak" in warnings or any(w.startswith("hint_echo") for w in warnings):
        # 这条译文就是提示词本身，没有任何可用内容 ⇒ 判死、不留文字。
        return GuardResult(text="", warnings=warnings, fatal=True)

    if not target:
        return GuardResult(text="", warnings=["empty_translation"], fatal=True)

    warnings += check_placeholders(source, target)
    warnings += check_percent_vars(source, target)
    warnings += check_length(source, target, max_chars, length_ratio)
    warnings += check_sentence_drop(source, target)
    warnings += check_language_residue(source, target, target_lang)
    warnings += check_foreign_script(target, source=source)
    if check_repeat:
        warnings += check_repetition(source, target)

    if not allow_untranslated and looks_untranslated(source, target):
        warnings.append("looks_untranslated")

    # ★ ``sentence_drop`` **故意只做警告，不判死** —— 这是量出来的结论，不是保守。
    #
    # 实测（5,224 条已判成功的译文）：判据开火 486 条 = 10.7%。人工抽查开火样本
    # 发现**大部分是假阳性**，全是"语气词被合并"这类风格差异，内容其实完整：
    #
    #     「Sigh... Whatever, let's go...」  →  唉… 不管怎样，走吧…      ✅ 完整
    #     「Nhooooo! ♥ Ogh! ♥ Ooooh! ♥」    →  莉菲亚：「唔……哦！噢……♥」  ✅ 完整
    #
    # 也试过用 len_ratio 把真丢与假阳性分开，**分不开**：风格性合并落在
    # 0.28~0.56，真丢内容落在 0.10~0.22，区间**重叠**。
    #
    # 若把它设成 fatal，就会把这几百条**内容完整**的译文退回英文 ——
    # 那比偶尔少译一句更伤体验。所以：**只报，不拦**，交给审校界面。
    # 这与「UI 缩写撞词」同一取舍：拦下正确产物比漏放一个可疑项更糟。

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
