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
    extract_placeholders,
    has_hangul,
    has_kana,
    has_latin,
    normalize_for_compare,
    strip_placeholders,
)

# 模型偶尔会把提示词要求也照抄进译文
_LEAK_PATTERNS = [
    r"^要求[:：]",
    r"^译文[:：]",
    r"^翻译[:：]",
    r"^输出[:：]",
    r"以下是",
    r"抱歉",
    r"作为(一名|一个)",
    r"JSON\s*数组",
    r"^\s*```",
]

_LEAK_RE = re.compile("|".join(_LEAK_PATTERNS), re.MULTILINE)

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
        if len(q) == 1 and len(t) >= 2 and t[0] == q and t[-1] == q:
            t = t[1:-1].strip()
        elif len(q) == 2 and len(t) >= 2 and t[0] == q[0] and t[-1] == q[1]:
            t = t[1:-1].strip()
    return t


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

    if count_cjk(target) == 0 and has_latin(target):
        return True
    return False


def check_leak(target: str) -> list[str]:
    if _LEAK_RE.search(target):
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
    warnings += check_length(source, target, max_chars, length_ratio)
    warnings += check_language_residue(source, target, target_lang)
    if check_repeat:
        warnings += check_repetition(source, target)

    if not allow_untranslated and looks_untranslated(source, target):
        warnings.append("looks_untranslated")

    # 致命错误分三类：
    #  * 占位符崩掉 —— 写回去游戏会崩或丢变量；
    #  * 复读 —— 不可修复，写回去是一屏重复文字；
    #  * 空译文。
    # 其余（过长、像是没翻）只警告，交给用户在审校界面判断。
    fatal = (
        any(
            w.startswith(
                (
                    "placeholder_count",
                    "placeholder_missing",
                    "placeholder_extra",
                    "repetition",
                )
            )
            for w in warnings
        )
        or not target
    )
    return GuardResult(text=target, warnings=warnings, fatal=fatal)


def summarize_warnings(entries_warnings: list[list[str]]) -> dict[str, int]:
    """统计告警分布，用于质检报告。"""
    out: dict[str, int] = {}
    for ws in entries_warnings:
        for w in ws:
            key = w.split(":", 1)[0]
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
