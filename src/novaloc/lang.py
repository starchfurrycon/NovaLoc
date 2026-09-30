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
