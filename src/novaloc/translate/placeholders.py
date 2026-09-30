"""占位符屏蔽（masking）—— 保护游戏文本里的语法记号。

为什么需要它
------------
游戏文本里混着大量**不是自然语言**的记号：

* RPG Maker：``\\V[1]`` ``\\N[2]`` ``\\C[3]`` ``\\I[5]`` ``\\{`` ``\\}``
* Ren'Py：``{color=#fff}{/color}`` ``[variable]`` ``{w}`` ``{nw}``
* 富文本：``<color=#FF0000>`` ``</size>`` ``<b>``
* 格式化：``%s`` ``%1$s`` ``{0}`` ``${var}`` ``{name}``
* 其它：``&nbsp;`` ``\\n`` ``\\t``

把这些原样发给模型，它**一定会**偶尔改坏：
`%s` 变成 `% s`、`{0}` 变成 `{ 0 }`、`\\V[1]` 少个反斜杠。
后果是游戏运行时崩溃或显示错字，而且这种 bug 极难定位。

解决办法
--------
翻译前把每个占位符换成一个**稀有 Unicode 记号**（形如 ``⟦0⟧``，
U+27E6/U+27E7 数学白方括号，自然文本里几乎不会出现），
翻译后再按索引还原。模型看到的就是一串"普通词"，无处可改。

同时提供 :func:`verify_restored` 做还原后的多重集比对，
任何数量不符都视为致命错误。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

#: 屏蔽用的括号字符。选数学白方括号是因为：
#: 1) 出现在正常游戏文本里的概率极低；
#: 2) 大多数 BPE 词表会把它切成独立 token，不容易被模型拆开或吞掉。
MASK_OPEN = "\u27e6"  # ⟦
MASK_CLOSE = "\u27e7"  # ⟧

_MASK_RE = re.compile(rf"{MASK_OPEN}(\d+){MASK_CLOSE}")

# --------------------------------------------------------------------------
# 占位符识别
# --------------------------------------------------------------------------

#: 按"必须整体保护"的顺序排列（更具体的放前面，避免被通用规则拆碎）
PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # RPG Maker MV/MZ 转义：\V[1] \N[2] \C[3] \I[4] \P[5]
    re.compile(r"\\[VNCPI]\[\d+\]"),
    # 单个反斜杠命令：\G \. \| \! \> \< \^ \{ \} \$ \\
    # 注意**不能**写成 \\[G.$|!><^{}] —— 字符类里的 `.` 会匹配任意字符，
    # 结果把 `\n` 也吃掉。`\n` 要留给下面专门的"转义换行"规则处理。
    re.compile(r"\\[G.$|!><^{}$]"),
    re.compile(r"\\\\"),
    # Ren'Py 文本标签：{color=#fff} {/color} {w} {nw} {size=+2} [variable]
    re.compile(r"\{/?[A-Za-z_][A-Za-z0-9_]*(?:=[^}]*)?\}"),
    re.compile(r"\[[A-Za-z_][A-Za-z0-9_.]*\]"),
    # 富文本 / HTML 风格标签
    # `(?:\s+[^<>]*)?` 里的 `\s+` 必须是可选的 —— 写成 `\s+` 会漏掉
    # `<color=#ff0000>` 这种"属性紧跟标签名、中间没有空格"的写法，
    # 那正是 Unity/Ren'Py/RPG Maker 里最常见的富文本标签形态。
    re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:[^<>]*)?/?>"),
    # RPG Maker 插件元数据标签：`<CustomEffect:heal:500>` `<PassiveSkill:5>`
    # 这类标签的**内部**是插件读取的参数，翻译了插件就认不出来，
    # 游戏行为直接出问题。上面的 HTML 规则匹配不到它（标签名后面
    # 跟的是 `:` 而不是 `>`），所以必须单独一条。
    re.compile(r"<[A-Za-z_][A-Za-z0-9_]*(?::[^<>\n]*)?>"),
    re.compile(r"&[A-Za-z][A-Za-z0-9]{1,10};"),
    re.compile(r"&#\d+;"),
    re.compile(r"&#x[0-9A-Fa-f]+;"),
    # printf / .NET 格式化
    # 关键：百分号与转换字符之间**不允许有空格**，否则 "100% complete" 会被
    # 误判成 `% c` 而被屏蔽，把一句正常文本切碎。
    # 这里拆成多条而不是一条大字符类，就是为了避免空格被当成合法 flag。
    re.compile(r"%\d+\$[sdfxXeEgGoc]"),
    re.compile(r"%\d*(?:\.\d+)?[sdfxXeEgGoc]"),
    re.compile(r"%[-+0#]+[^A-Za-z0-9\s]?\d*(?:\.\d+)?[sdfxXeEgGoc]"),
    re.compile(r"%l{1,2}[sdfxXeEgGoc]"),
    # Python / C# 命名与位置格式化
    re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}"),
    re.compile(r"\{\d+(?::[^}]*)?\}"),
    re.compile(r"\{\}"),
    # shell / 模板
    re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"),
    re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*"),
    # 反引号代码
    re.compile(r"`[^`\n]{1,40}`"),
    # 具名尖括号占位（排除已经被上面 HTML 规则吃掉的）
    re.compile(r"<[A-Z_][A-Z0-9_]{1,30}>"),
    # 转义换行/制表 —— 放在最后，保证它只处理没被前面规则吃掉的情况
    re.compile(r"\\[nrt]"),
)

#: 这些"占位符"其实太常见、屏蔽它们反而会破坏翻译，所以排除
_NEVER_MASK = {
    "%", "%%", "{}", "[]", "<>", "\\",
}


def _placeholder_spans(text: str) -> list[tuple[int, int, str]]:
    """算出所有需要保护的占位符区间（已去重、去重叠）。

    两个容易踩的坑，都在这里处理：

    1. **必须先把 ``_NEVER_MASK`` 里的候选剔除，再做重叠消解。**
       否则 ``100% complete`` 这类文本里，被排除的候选会挡住真正该保护的匹配。
    2. ``%%`` 是转义后的百分号，不是格式说明符，必须整体忽略；
       否则单个 ``%`` 规则会把它拆开。
    """
    # 先把 %% 挖掉，避免单 % 规则把转义百分号拆成两半
    guard = "\x00" * 2
    text = text.replace("%%", guard)

    spans: list[tuple[int, int, str]] = []
    for pat in PLACEHOLDER_PATTERNS:
        for m in pat.finditer(text):
            val = m.group(0)
            if val in _NEVER_MASK or "\x00" in val:
                continue
            spans.append((m.start(), m.end(), val))

    # 重叠消解：保留"最靠前、且最长"的匹配
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    chosen: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, v in spans:
        if s >= last_end:
            chosen.append((s, e, v))
            last_end = e
    return chosen


def find_placeholders(text: str) -> list[str]:
    """按出现顺序列出文本里所有需要保护的占位符（含重复）。"""
    if not text:
        return []
    return [v for _, _, v in _placeholder_spans(text)]


@dataclass
class MaskResult:
    """屏蔽结果。"""

    text: str
    """屏蔽后的文本（发给模型的就是这个）。"""

    slots: list[str] = field(default_factory=list)
    """按索引排列的原始占位符，``slots[0]`` 对应 ``⟦0⟧``。"""

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "slots": list(self.slots)}


@dataclass
class PlaceholderSet:
    """一段文本里出现的占位符（保留重复次数）。"""

    items: list[str] = field(default_factory=list)

    @property
    def counter(self) -> Counter[str]:
        return Counter(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


def mask(text: str, *, start_index: int = 0) -> MaskResult:
    """把文本里的占位符替换成 ``⟦i⟧`` 记号。

    ``start_index`` 允许多段文本共用一个屏蔽命名空间（批量翻译时有用）。
    """
    if not text:
        return MaskResult(text=text, slots=[])

    # 注意：_placeholder_spans 内部会把 %% 临时替换掉，所以这里的偏移量
    # 是基于"%% 被换成等长哨兵"后的文本，长度不变，索引依然正确。
    chosen = _placeholder_spans(text)
    if not chosen:
        return MaskResult(text=text, slots=[])

    out: list[str] = []
    slots: list[str] = []
    cursor = 0
    for i, (s, e, v) in enumerate(chosen):
        out.append(text[cursor:s])
        out.append(f"{MASK_OPEN}{start_index + i}{MASK_CLOSE}")
        slots.append(v)
        cursor = e
    out.append(text[cursor:])
    return MaskResult(text="".join(out), slots=slots)


def unmask(text: str, slots: list[str]) -> str:
    """按索引把 ``⟦i⟧`` 还原成原始占位符。

    找不到索引时**保留记号原样**而不是删掉 —— 让后续的多重集校验能发现问题，
    比悄悄丢掉一个 `\\V[1]` 安全得多。
    """
    if not text or not slots:
        return text

    def _sub(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(slots):
            return slots[idx]
        return m.group(0)

    return _MASK_RE.sub(_sub, text)


def remaining_masks(text: str) -> list[str]:
    """找出文本里还没被还原的屏蔽记号（模型可能自己编了几个出来）。"""
    return _MASK_RE.findall(text or "")


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


@dataclass
class PlaceholderCheck:
    ok: bool
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)
    order_changed: list[int] = field(default_factory=list)
    """记号出现顺序与原文不一致的下标。

    这是**必须拦掉**的情况：成对标签（``<color>`` / ``</color>``）一旦被
    模型交换，还原后会得到 ``</color>警告！<color=#f00>`` —— 多重集完全正确、
    文本看着也通顺，但游戏渲染必然出错。这类"沉默的损坏"比丢字更危险。
    """

    @property
    def fatal(self) -> bool:
        return bool(self.missing or self.extra or self.order_changed)

    def describe(self) -> str:
        bits: list[str] = []
        if self.missing:
            bits.append(f"丢失占位符：{self.missing}")
        if self.extra:
            bits.append(f"多出占位符：{self.extra}")
        if self.order_changed:
            bits.append(f"占位符顺序被打乱（下标 {self.order_changed}）")
        if self.changed:
            bits.append(f"占位符被改写：{self.changed[:5]}")
        return "；".join(bits) or "占位符一致"


def compare_placeholders(source: str, target: str) -> PlaceholderCheck:
    """比较原文与译文里的占位符多重集。

    用 ``Counter`` 而不是 ``set``：`\\V[1]` 出现两次就必须还原出两次，
    少一次同样是 bug。
    """
    src = Counter(find_placeholders(source))
    tgt = Counter(find_placeholders(target))
    missing = sorted((src - tgt).elements())
    extra = sorted((tgt - src).elements())
    return PlaceholderCheck(ok=not (missing or extra), missing=missing, extra=extra)


def compare_restored(original: str, restored: str) -> PlaceholderCheck:
    """还原**之后**再比一次。

    这一层能抓到"模型把 ``⟦0⟧`` 改成了别的东西、导致还原失败"的情况。
    """
    return compare_placeholders(original, restored)


def mask_indices(text: str) -> list[int]:
    """按出现顺序列出文本里的屏蔽记号下标。"""
    return [int(m.group(1)) for m in _MASK_RE.finditer(text or "")]


def verify_restored(
    original: str,
    translated_raw: str,
    slots: list[str],
    *,
    masked_source: str | None = None,
) -> tuple[str, PlaceholderCheck]:
    """还原 + 校验的组合入口，返回 ``(还原后的文本, 校验结果)``。

    ``masked_source`` 是屏蔽后的原文，**顺序校验必须靠它** ——
    ``original`` 里全是原始占位符、没有 ``⟦i⟧``，看不出索引顺序。

    三层校验，缺一不可：

    1. **多重集**：数量与内容必须一致（``\\V[1]`` 出现两次就还原两次）。
    2. **剩余记号**：还原不掉的自造记号（``⟦9⟧``）也算失败。
    3. **出现顺序**：``⟦0⟧ ⟦1⟧`` 不能被写成 ``⟦1⟧ ⟦0⟧``。
       成对标签的顺序一旦交换，还原出的标记法是坏的，但文本看起来完全正常，
       属于最容易漏过的"沉默损坏"。
    """
    restored = unmask(translated_raw, slots)
    left = remaining_masks(restored)
    check = compare_restored(original, restored)
    if left:
        check.extra.extend(f"⟦{i}⟧" for i in left)
        check.ok = False

    if masked_source:
        src_order = mask_indices(masked_source)
        tgt_order = mask_indices(translated_raw)
        # 只在"数量与内容都对得上"时才判顺序，否则交给上面的缺失/多余分支报错
        if src_order and tgt_order and not check.missing and not check.extra:
            if tgt_order != src_order:
                check.order_changed = [
                    i
                    for i, (a, b) in enumerate(zip(src_order, tgt_order, strict=False))
                    if a != b
                ]
                check.ok = False
    return restored, check


# --------------------------------------------------------------------------
# 批量场景
# --------------------------------------------------------------------------


def mask_batch(texts: list[str]) -> tuple[list[str], list[list[str]]]:
    """一次屏蔽一批文本，**每条各自从 ⟦0⟧ 开始编号**。

    为什么不用"全批共用一个索引空间"（看起来更严格）：
    模型是**逐条**返回的，每条译文的记号只能对着**那一条自己的** slots 还原。
    若全批共用索引，第 2 条的记号是 ``⟦2⟧`` ``⟦3⟧``，
    而还原时拿到的 slots 列表只有 2 个元素，索引直接越界 —— 好译文会被误判成
    "占位符被破坏"。所以必须逐条局部编号。

    跨条目抄写记号的风险交给 :func:`_detect_cross_item_leak` 在批次层兜底。
    """
    masked: list[str] = []
    all_slots: list[list[str]] = []
    for t in texts:
        r = mask(t, start_index=0)
        masked.append(r.text)
        all_slots.append(r.slots)
    return masked, all_slots


def _detect_cross_item_leak(
    masked: list[str], translations: list[str]
) -> list[tuple[int, int]]:
    """检测"把 A 条的记号抄进 B 条"。

    局部编号后，越界检查抓不到这种情况了（每条都是 ``⟦0⟧`` 起步），
    所以单独比一次：某条译文里出现了**超出该条自身槽位数**的记号下标，
    就是抄错了。返回 ``[(条目下标, 越界记号), ...]``。
    """
    bad: list[tuple[int, int]] = []
    for i, (src, tgt) in enumerate(zip(masked, translations, strict=False)):
        n_slots = len(mask_indices(src))
        for idx in mask_indices(tgt):
            if idx >= n_slots:
                bad.append((i, idx))
    return bad


# --------------------------------------------------------------------------
# 全角化检测（中文译文里不该出现的半角/全角混用错误）
# --------------------------------------------------------------------------

#: 这些字符在中文句子里应该是全角
_SHOULD_BE_FULLWIDTH = {
    ",": "，", ".": "。", "!": "！", "?": "？",
    ":": "：", ";": "；", "(": "（", ")": "）",
}

#: 全角标点的范围
_FULLWIDTH_PUNCT = set("，。！？：；、（）「」『』【】《》〈〉“”‘’…—～·")


def check_punctuation_style(target: str, *, strict_fullwidth: bool = False) -> list[str]:
    """检查中文译文里的标点风格问题。

    ``strict_fullwidth=True`` 时，句末的 ASCII 标点会被报出来。
    注意不要对**含占位符/URL/代码**的文本启用严格模式。
    """
    issues: list[str] = []
    if not target:
        return issues
    for half, full in _SHOULD_BE_FULLWIDTH.items():
        if half in target and full not in target:
            issues.append(f"使用了半角 “{half}”，中文里通常应为 “{full}”")
    # 连续多个半角句点 / 感叹号
    if re.search(r"[!?]{2,}", target):
        issues.append("出现连续半角感叹号/问号")
    return issues


def has_source_leak(source: str, target: str, *, min_run: int = 4) -> list[str]:
    """检测"原文泄漏"：译文里残留了源语言的长片段（通常是漏译）。

    只在**目标语言不是源语言**时有意义。
    """
    if not source or not target:
        return []
    leaks: list[str] = []
    # 按空格切成词，找长度达标的、原样出现在译文里的连续片段
    words = source.split()
    run: list[str] = []
    for w in words:
        if len(w) < 3 or re.fullmatch(r"[\W\d_]+", w):
            if run:
                leaks.append(" ".join(run))
                run = []
            continue
        run.append(w)
    if run:
        leaks.append(" ".join(run))

    found = [seg for seg in leaks if len(seg) >= min_run and seg in target]
    return found
