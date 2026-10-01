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

#: 补回位置时可以"插在哪都行"的占位符。
#:
#: 这类是**样式/控制标记**，插到译文里的任何位置都不会把词切断：
#:
#: * ``\C[n]`` 颜色、``\I[n]`` 图标 —— 只是"从哪开始变色/插图标"；
#: * ``\V[n]`` ``\N[n]`` 这类变量**不在此列**：它们在句子里占一个名词位，
#:   插错位置会让玩家名出现在错误的语法位置；
#: * 单字符控制码 ``\{ \} \| \. \! \> \< \^ \$`` —— 等待、停顿、清屏等，
#:   在哪触发都只是时机略有差别；
#: * 富文本标签 —— 有顺序校验兜底。
#:
#: 落在这一类里的占位符**一定会被补回**（锚点找不到就按比例位置）。
#: 不在这一类里的（``\n`` 换行、``\V[n]``、``%s`` 参数）只有锚点明确时
#: 才补回 —— 插错位置的换行会把一个中文词劈成两半，比不换行更难看。
#: 反斜杠字符（ASCII 92）。用 ``chr(92)`` 而不是字面量 ``"\\"``：
#: 这个仓库路径含中文，写文件必须经工具链，而工具链会对反斜杠**再转义一层**。
#: 实测这层转义让本文件的分类正则连续被写坏四次（两个 ↔ 一个 ↔ 四个反斜杠），
#: 每次的表象都是"颜色类占位符莫名不补回"，而静态检查全部通过 ——
#: 属于最难发现的沉默失效。所以这里彻底不在源码里出现反斜杠字面量。
_BS = chr(92)

#: 补回位置时"插在哪都行"的 RPG Maker 双字符命令的**第二字符**。
#: ``\C[n]`` 颜色、``\I[n]`` 图标、``\V[n]`` 变量、``\N[n]`` 角色名。
#: 它们只在译文里占一个"从这开始生效"的位置，插偏几个字不影响可读性。
_REPAIR_FREE_LETTERS = frozenset("CIVN")

#: 单字符控制码（等待、停顿、清屏、加速等），在哪触发都只是时机差别。
_REPAIR_FREE_CHARS = frozenset("{}|.!><^$")

#: `_REPAIR_FREE_CHARS` 里**其实会断行**的那几个：等待按键 `\|`、停顿 `\.` `\!`、
#: 瞬间显示 `\>` `\<`。它们插在词中间玩家看得见（见 `_needs_word_boundary`）。
_REPAIR_BREAK_CHARS = frozenset("|.!><")

#: 补回时**必须落在词边界**的占位符（换行类）。
#: 找不到边界就干脆不补 —— 见 :func:`_snap_to_boundary`。
_REPAIR_NEEDS_BOUNDARY = (frozenset({"n", "r"}), "<br")


def _is_free_anywhere(slot: str) -> bool:
    r"""该占位符是否"插到译文任何位置都安全"。

    安全 = 只影响样式/时机，不影响语义。**变量类 ``\V[n]`` ``\N[n]``
    也算安全**：它们在句子里占一个名词位，插偏几个字仍然是"从这开始
    显示变量"，比整条不翻译好得多。

    不安全的是换行 ``\n``（插进中文词中间会把词劈开）与格式化参数
    ``%s`` ``%d``（插错位置会让参数出现在错误的语法位置）。
    r"""
    if not slot:
        return False
    if slot.startswith(_BS + _BS):  # 双反斜杠的开头（极少数引擎）
        rest = slot[2:]
    elif slot.startswith(_BS):
        rest = slot[1:]
    else:
        rest = slot
        # 非反斜杠开头的：HTML / Ren'Py 标签
        return bool(slot.startswith("<") and slot.endswith(">")) or (
            slot.startswith("[") and slot.endswith("]")
        )
    return len(rest) >= 1 and rest[0] in (_REPAIR_FREE_LETTERS | _REPAIR_FREE_CHARS)


def _needs_word_boundary(slot: str) -> bool:
    r"""该占位符补回时是否必须落在词边界（换行/停顿类）。

    ## 为什么 `\|` 和 `\.` 也算（一开始把它们归成"插哪都行"是错的）

    `_REPAIR_FREE_CHARS` 里有一批"单字符控制码"，理由是"在哪触发
    都只是时机差别"。这在**颜色/样式**类上成立，但 `\|` 和 `\.` 不是样式：

    * `\|` 是 RPG Maker 的**等待玩家按键** —— 它会在游戏里**断行**；
    * `\.` / `\!` 是停顿 —— 也断行。

    所以它们插在词中间是**玩家看得见的损坏**。实测真实数据：

        译文: '嗯……（亲吻）……嗯……（亲吻）……'
        补回: '嗯……（亲⟦0⟧吻）……嗯⟦1⟧……'    ← `\|` 劈开了"亲吻"

    改动很小（把它们从 FREE 挪到 NEEDS_BOUNDARY），
    但 `_snap_to_boundary` 只在**半径 6 个字符**内找边界，
    找不到就返回 `None` → 调用方放弃补回 → 整条仍被判失败。
    对一个 20 字的句子来说半径 6 太窄，所以这里同时把半径放宽到
    能覆盖整句（停顿的"绝对位置"本来就不需要精确，
    落在正确的**词缝**里比落在精确的**字符位**更重要）。
    """
    if not slot:
        return False
    rest = slot[2:] if slot.startswith(_BS + _BS) else (
        slot[1:] if slot.startswith(_BS) else slot
    )
    low = rest.lower()
    if low in ("n", "r", "n\n", "r\n"):
        return True
    # 断行/停顿类控制码：必须落在词边界
    if rest and rest[0] in _REPAIR_BREAK_CHARS:
        return True
    return low.startswith("<br")

# --------------------------------------------------------------------------
# 占位符识别
# --------------------------------------------------------------------------

#: 按"必须整体保护"的顺序排列（更具体的放前面，避免被通用规则拆碎）
PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # **嵌套形式必须排在基础形式之前**：`\S[\V[101]]` 要整体成一个掩码。
    # 如果让下面的 `\\[VNCPI]\[\d+\]` 先跑，它会先吃掉里面的 `\V[101]`，
    # 外层就留下 `\S[` 和 `]` 变成可被翻译的明文 —— 即使 token **数量**
    # 对得上，结构也已经坏了（实测真实游戏里有 `Obtained \S[\V[101]].`）。
    re.compile(r"\\[Ss]\[\\[VvNnPpCcIiSs]\[\d+\]\]"),
    # RPG Maker MV/MZ 转义：\V[1] \N[2] \C[3] \I[4] \P[5]
    #
    # ⚠️ 必须和 `lang.py:_PLACEHOLDER_PATTERNS`（守卫用的那套）**一致**。
    # 这里原本只有 `[VNCPI]`，漏了 `S`/`s`，而守卫认它们 ——
    # 于是守卫判 `placeholder_count:1->0` fatal，屏蔽器却从没把
    # `\S[88]` 变成掩码，`repair_dropped_masks()` 没有东西可补，
    # **一条完全可救的译文被整条丢弃**。
    # 真实游戏实测：`\S[n]` 33 处、`\v[n]` 32 处，合计 65 处全走这条路。
    # 守卫在 `lang.py` 里把 `S` 当作合法占位符字母（大小写都认），
    # 所以这里也认 —— 宁可误保护一个 `\s`，也不要丢掉一个真的 `\S[n]`
    # （`\s` 在这个语料里根本不是"空格"的意思，它就是 `\S` 的小写写法）。
    re.compile(r"\\[VvNnPpCcIiSs]\[\d+\]"),
    # 单个反斜杠命令：\G \. \| \! \> \< \^ \{ \} \$ \\
    # 注意**不能**写成 \\[G.$|!><^{}] —— 字符类里的 `.` 会匹配任意字符，
    # 结果把 `\n` 也吃掉。`\n` 要留给下面专门的"转义换行"规则处理。
    re.compile(r"\\[G.$|!><^{}$]"),
    # **插件定义的裸字母占位符**：`\D`（伤害表达式）、`\R`（次数表达式）
    #
    # 这两个不在 RPG Maker 原生转义表里，是**这个游戏的插件**自己解析的
    # （BattleCore / CardGame 一类）。以前既不保护也不校验，后果是**静默损坏**：
    # 真实游戏实测 **92 条**含 `\D`/`\R`，其中 **91 条**在译文里
    # **彻底丢失** ——
    #
    #     原文 = 'Deals \D damage. Gain 4 「Defense」 until turn end.'
    #     译文 = '造成 D 点伤害。获得 4 点防御值，持续至回合结束。'
    #                                          ↑ 反斜杠没了
    #
    # 模型把 `\D` 读成"字母 D"，于是要么输出裸 `D`，要么更离谱地
    # 输出 `\textbackslash R`。游戏里这些字符串要交给插件解析，
    # 少了反斜杠就变成**对玩家显示一个字母 D**，或者插件解析失败。
    #
    # 这是本项目最怕的失败模式："阶段报成功，产物已经坏了" ——
    # 它**不会**被任何现有检查抓到，因为双方都不认识这个记号。
    #
    # ⚠️ `lang.py`（守卫）必须同步加同样的规则，否则又回到
    # "两名裁判不一致"的老问题（见 `tests/test_placeholder_consistency.py`）。
    re.compile(r"\\[DR]"),
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
    # ★ RPG Maker MV/MZ 的**消息替换变量**：`%1` `%2` `%3`
    #
    # ## 事故：这两个字符不在掩码表里，于是**两个模型都会丢掉它**
    #
    # `Database → 用语` 里的战斗消息就在用这个语法：
    #
    #     '%1 attacks!'    →  translategemma 出 '攻击！'      ← `%1` 没了
    #     '%1 casts %2!'   →  translategemma 出 '%1 施法！'   ← `%2` 没了
    #     '¡%1 usa %2!'    →  HY-MT1.5 出 '有人使用了%2！'    ← `%1` 没了
    #
    # 游戏运行时会把 `%1` 替换成**行动者名字**、`%2` 替换成**目标名字**。
    # 所以丢掉它不是"少了个符号"，而是玩家看到
    #
    #     '攻击！'            ← 谁攻击谁？名字全没了
    #
    # ## 为什么原来的 printf 规则抓不到
    #
    # 上面那条 `%\d*(?:\.\d+)?[sdfxXeEgGoc]` 要求**转换字符**
    # （`%s` `%d` `%1$s`）。而 RPG Maker 写的是**裸数字** `%1` ——
    # 没有转换字符，于是**一条都不匹配**，`%1` 被当成可翻译明文送给模型。
    #
    # 实测：`mask('%1 attacks!')` 返回槽位 `[]`（**零个**），
    # 所以占位符校验根本不知道它有东西要保 —— 丢了也不报。
    #
    # ## 为什么 `%` 后面只允许数字
    #
    # 不能写成 `%\d+` 之外更宽的形式。`'100% complete'` 这类文本里
    # `%` 后面跟空格，若允许任意字符就会被误屏蔽、把句子切碎
    # （上面那段注释记的就是这个坑）。只认**紧跟数字**的形态。
    re.compile(r"%\d+"),
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

#: 形如 `%1` `%2` 的 **RPG Maker 消息替换变量**。
#:
#: ## 事故：**屏蔽它们反而让模型更容易丢掉它们**
#:
#: 这两个记号的正确性极重要 —— 游戏运行时把 `%1` 换成**行动者名字**、
#: `%2` 换成**目标名字**。丢了它，玩家看到的是
#:
#:     '%1 attacks!'  →  '攻击！'        ← 谁攻击？名字没了
#:
#: 最直觉的做法是"把它屏蔽成 `⟦0⟧` 保护起来"。**实测这是错的**，
#: 而且错得很彻底。拿 14 条**真实丢过变量**的源文做 A/B：
#:
#: | 做法 | `%n` 保住 |
#: |---|---|
#: | 屏蔽成 `⟦0⟧`（+ 各种提示词强调） | **0～2 / 14** |
#: | **不屏蔽**，让模型直接看到 `%1` | **约 7 / 14** |
#:
#: 试过 6 种记号长相（`⟦0⟧` `{{0}}` `<ph0/>` `${0}` `[[0]]` `<0>`），
#: **没有一种**能稳住句首的那个；又试了 3 版提示词
#: （只说"必须保留" / 说清"这是名字" / 加了反面例子），
#: 屏蔽路径最好也只到 2/14。
#:
#: 原因不难理解：`⟦0⟧` 对模型是个**没有语义的装饰符**，
#: 而 `%1` 在训练数据里是**有含义的格式串**（printf 家族），
#: 模型知道它承载数据。把语义换成装饰，模型就把它当噪音清掉了。
#:
#: **结论：对这类变量，"可见"比"被保护"更重要。**
#: 所以从掩码表里排除，改由 :func:`check_percent_vars` 在**出站**做校验 ——
#: 校验不需要把记号藏起来，只需要比对数量。
_PERCENT_VAR_RE = re.compile(r"%\d+")


def is_percent_var(text: str) -> bool:
    """是不是 RPG Maker 的消息替换变量（`%1` `%2` …）。"""
    return bool(_PERCENT_VAR_RE.fullmatch(text or ""))


def percent_vars(text: str) -> list[str]:
    """按出现顺序取出文本里的 `%n` 变量。"""
    return _PERCENT_VAR_RE.findall(text or "")


def _placeholder_spans(text: str) -> list[tuple[int, int, str]]:
    """算出所有需要保护的占位符区间（已去重、去重叠）。

    两个容易踩的坑，都在这里处理：

    1. **必须先把 ``_NEVER_MASK`` 里的候选剔除，再做重叠消解。**
       否则 ``100% complete`` 这类文本里，被排除的候选会挡住真正该保护的匹配。
    2. ``%%`` 是转义后的百分号，不是格式说明符，必须整体忽略；
       否则单个 ``%`` 规则会把它拆开。
    r"""
    # 先把 %% 挖掉，避免单 % 规则把转义百分号拆成两半
    guard = "\x00" * 2
    text = text.replace("%%", guard)

    spans: list[tuple[int, int, str]] = []
    for pat in PLACEHOLDER_PATTERNS:
        for m in pat.finditer(text):
            val = m.group(0)
            if val in _NEVER_MASK or "\x00" in val:
                continue
            # `%n` **故意不屏蔽** —— 见 `_PERCENT_VAR_RE` 上方的实测记录。
            # 屏蔽会把它变成没有语义的装饰符，模型反而更容易丢掉它
            # （实测 0～2/14，而不屏蔽约 7/14）。
            if is_percent_var(val):
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
    r"""按索引把 ``⟦i⟧`` 还原成原始占位符。

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

    runs_split: list[tuple[int, ...]] = field(default_factory=list)
    """被**拆散**的连续记号组（源文里连在一起，译文里被文字隔开）。

    ## 为什么顺序对了还不够（真实事故）

    MV 的 `Actors.json` 角色 profile：

        源：…Weapon Type: \\I[96]\\I[97]   Armor Type:\\I[129]\\I[135]\\I[139]
        译：…武器类型：[武器名称]  防具类型\\I[96]\\I[97]：[防具名称]\\I[129]…

    编号顺序是 **96, 97, 129, 135, 139 —— 递增，顺序判据完全通过**。
    坏掉的是**相邻关系**：图标原本紧跟在 `Weapon Type:` 后面，
    现在跑到了 `防具类型` 后面。玩家看到的是"防具类型"配武器图标。

    这类"顺序对、邻接错"的损坏，`order_changed` 抓不到 ——
    它比的是编号序列，而拆散改变的是**记号与文字的相邻关系**。

    实测：凡是有连续记号组的条目，**100%（14/14）** 都被拆散。

    ## 判据只针对 `\\I[...]` 这类**内容**记号

    `\\C[29]`（改文字颜色）挪位只影响配色；
    `\\I[96]`（画一个图标）挪位就**换了内容** —— 图标本身是有意义的。
    所以只有"内容记号"的组被拆散才判坏。
    """

    @property
    def fatal(self) -> bool:
        return bool(self.missing or self.extra or self.order_changed or self.runs_split)

    def describe(self) -> str:
        bits: list[str] = []
        if self.missing:
            bits.append(f"丢失占位符：{self.missing}")
        if self.extra:
            bits.append(f"多出占位符：{self.extra}")
        if self.order_changed:
            bits.append(f"占位符顺序被打乱（下标 {self.order_changed}）")
        if self.runs_split:
            bits.append(
                f"连续记号组被拆散（{self.runs_split[:3]}）—— "
                "图标滑到了别的词旁边"
            )
        if self.changed:
            bits.append(f"占位符被改写：{self.changed[:5]}")
        return "；".join(bits) or "占位符一致"


def compare_placeholders(source: str, target: str) -> PlaceholderCheck:
    r"""比较原文与译文里的占位符多重集。

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


def strip_unknown_masks(text: str, n_slots: int) -> str:
    """删掉**不存在的**屏蔽记号（下标 >= ``n_slots``）。

    模型偶尔会在**本条根本没有占位符**时凭空写出 ``⟦0⟧``。
    真实记录（BeyondPortal）：

    * ``'啊...乌鲁拉 别那么快，不然我就要射了！'``（无占位符）
      → 模型回 ``'啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧'``
    * ``'啊~... 是-是...拜託 …'`` → ``'哎… 嗯… 是… 请帮帮我…”} ⟦0⟧, {'``

    这些凭空记号会撞进 ``verify_restored`` 的"多出占位符"分支，
    整条判为 fatal、译文清空 —— 玩家看到空白对话框，
    而**真正的问题只是多了一个垃圾记号**。

    凭空记号没有任何对应的原始占位符可还原，删掉它**不会**丢信息，
    所以这是安全的修复（"多出来的"和"丢失的"性质完全不同：
    丢失的必须拒绝，多出来的可以删）。实测 14 条这类失败全部可救。

    注意只删**越界**的：``⟦0⟧``/``⟦1⟧`` 在 ``n_slots=3`` 时是合法的，
    原样保留交给后续校验去判断顺序与重复。
    """
    if n_slots <= 0:
        # 本条没有任何占位符 → 所有记号都是凭空造的
        return _MASK_RE.sub("", text or "")
    if not text:
        return text or ""

    def _sub(m: re.Match[str]) -> str:
        return "" if int(m.group(1)) >= n_slots else m.group(0)

    return _MASK_RE.sub(_sub, text)


def _strip_tokens(text: str, tokens: frozenset[str]) -> str:
    """去掉 ``tokens`` 里的控制码，留下"内容"（用于算比例）。"""
    for tok in sorted(tokens, key=len, reverse=True):
        text = text.replace(tok, "")
    return text


def verify_restored(
    original: str,
    translated_raw: str,
    slots: list[str],
    *,
    masked_source: str | None = None,
) -> tuple[str, PlaceholderCheck]:
    r"""还原 + 校验的组合入口，返回 ``(还原后的文本, 校验结果)``。

    ``masked_source`` 是屏蔽后的原文，**顺序校验必须靠它** ——
    ``original`` 里全是原始占位符、没有 ``⟦i⟧``，看不出索引顺序。

    三层校验，缺一不可：

    1. **多重集**：数量与内容必须一致（``\\V[1]`` 出现两次就还原两次）。
    2. **剩余记号**：还原不掉的自造记号（``⟦9⟧``）也算失败。
    3. **出现顺序**：``⟦0⟧ ⟦1⟧`` 不能被写成 ``⟦1⟧ ⟦0⟧``。
       成对标签的顺序一旦交换，还原出的标记法是坏的，但文本看起来完全正常，
       属于最容易漏过的"沉默损坏"。
    r"""
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

        # ---- 连续「内容记号」组不许被拆散 ----
        #
        # 只在多重集完整时判（缺/多记号另有分支报错，且拆散判定需要编号可比）。
        if not check.missing and not check.extra:
            check.runs_split = find_split_content_runs(
                masked_source, translated_raw, slots
            )
            if check.runs_split:
                check.ok = False
    return restored, check


#: 连续记号组的最小长度 —— 单个记号谈不上"被拆散"
_MIN_RUN = 2

#: **内容**类记号的形状（挪位会换内容，不只是换样式）。
#: `\I[96]` 画一个图标、`\N[1]` 是角色名 —— 都有实义。
#: `\C[29]`（文字颜色）与 `<color=…>`（样式标签）**不在**这里：
#: 它们挪位只影响配色，而中文语序本来就和原文不同，
#: 强行要求邻接会误杀大量正常译文。
_CONTENT_MARK_RE = re.compile(r"\\(?:I|N|V|P)\[")


def _content_runs(masked: str, slots: list[str]) -> list[tuple[int, ...]]:
    """取出掩码文本里**连续的、内容是实义符号**的记号组。"""
    out: list[tuple[int, ...]] = []
    for m in re.finditer(r"(?:⟦(\d+)⟧)+", masked):
        idxs = tuple(int(x) for x in re.findall(r"⟦(\d+)⟧", m.group(0)))
        if len(idxs) < _MIN_RUN:
            continue
        # 整组都必须是内容记号（混了样式记号就跳过，避免误杀）
        texts = [slots[i] for i in idxs if 0 <= i < len(slots)]
        if len(texts) != len(idxs):
            continue
        if all(_CONTENT_MARK_RE.match(t) for t in texts):
            out.append(idxs)
    return out


def find_split_content_runs(
    masked_source: str, translated_raw: str, slots: list[str]
) -> list[tuple[int, ...]]:
    r"""找出邻居被换掉的「内容记号」连续组。

    ## 判据：记号紧贴的**字符类**不能变

    源文 ``Weapon Type: \\I[96]\\I[97]   防具类型…`` —— 组紧跟在**冒号**后面。
    译文 ``…[武器名称]  防具类型\\I[96]\\I[97]：`` —— 组现在紧跟在**汉字**后面。

    记号本身没丢、编号顺序也对，但**它贴着的东西从标点变成了汉字**，
    说明它滑到了另一个词旁边。

    所以判据是：组前/后紧邻的第一个**非空白**字符，
    从"标点"变成了"汉字/字母"，或反过来 —— 就算拆散。

    ## 为什么不能只数"组之间有没有文字"

    实测那 14 条里，组与组之间**本来就有** `   Armor Type: ` 这类标签，
    所以"组之间出现文字"在原文和译文里都成立，区分不出来。
    真正变的是**组自己贴着什么**。

    ## 为什么只对内容记号判

    `\\C[29]`（改颜色）挪位只影响配色，而中文语序本来就和原文不同 ——
    对它判邻接会误杀大量正常译文。
    `\\I[96]`（画图标）挪位就**换了内容**。
    """
    raw = translated_raw or ""
    runs = _content_runs(masked_source, slots)
    if not runs:
        return []

    # 译文里每个编号的位置
    pos: dict[int, tuple[int, int]] = {}
    for m in re.finditer(r"⟦(\d+)⟧", raw):
        pos.setdefault(int(m.group(1)), (m.start(), m.end()))

    split: list[tuple[int, ...]] = []
    for run in runs:
        if any(i not in pos for i in run):
            continue  # 缺记号 → 交给 missing 分支
        src_span = _mask_span(masked_source, run)
        tgt_span = (pos[run[0]][0], pos[run[-1]][1])
        if src_span is None:
            continue
        for side in ("before", "after"):
            a = _neighbour_class(masked_source, src_span, side)
            b = _neighbour_class(raw, tgt_span, side)
            if not a or not b:
                continue
            # 只拦"标点 ↔ 词"的跨越。
            #
            # * `punct → punct`（`:` → `：`）是正常的中文标点转换；
            # * `latin → cjk`（`Armor Type:` → `防具类型：`）是标签被翻译，
            #   上面两种情况都**正常**；
            # * 但 `punct → cjk/latin` 表示记号本来贴着标点
            #   （即"标签之后"），现在却贴着一个词 —— 它滑到别的词旁边了。
            #
            # ## 试过但**放弃**的两条更宽的判据
            #
            # 1. **`latin → cjk` 一律判坏** —— 误报：标签被翻译本来就会
            #    出现这个跨越（`⟦0⟧⟦1⟧   Armor Type:` → `⟦0⟧⟦1⟧   防具类型：`）。
            # 2. **源文有空白而译文没空白就判坏** —— 误报：一组记号正好
            #    收在句尾时，源文后面有空白、译文后面没有，完全正常。
            #
            # 实测只有"标点↔词"这一条能做到：真实事故 **13/13 全抓、
            # 正常译文 0 误报**。
            if a == "punct" and b in ("cjk", "latin", "digit"):
                split.append(run)
                break
            if b == "punct" and a in ("cjk", "latin", "digit"):
                split.append(run)
                break
    return split


def content_runs_split_in_restored(
    source: str, target: str, *, slots: list[str] | None = None
) -> list[tuple[int, ...]]:
    r"""对**已还原**的译文判断"连续内容记号组有没有被拆散"。

    用于**重查已有数据**（工作区里躺着的旧译文）——
    :func:`find_split_content_runs` 需要"带 ``⟦n⟧`` 的模型原始输出"，
    而落盘的是还原后的文本，所以这里把槽位内容**反推**回记号。

    ## 只在槽位内容互不相同时才敢反推

    反推是把第一个出现的 ``\I[96]`` 换成 ``⟦0⟧``、第二个换成 ``⟦1⟧``……
    如果**多个槽位的内容一模一样**（例如同一个图标码用了两次），
    第一处到底对应哪个编号就**无法确定**，反推可能张冠李戴。
    那种情况直接返回空（宁可漏报，不可误报 ——
    误报会把一条好译文作废并重译）。
    """
    if not source or not target:
        return []
    m = mask(source)
    if len(m.slots) < _MIN_RUN:
        return []
    # 槽位内容必须唯一，否则反推不可靠
    if len(set(m.slots)) != len(m.slots):
        return []
    used = slots if slots is not None else m.slots
    raw = target
    for i, s in enumerate(used):
        if s and s in raw:
            raw = raw.replace(s, f"⟦{i}⟧", 1)
    return find_split_content_runs(m.text, raw, used)


def _mask_span(masked: str, run: tuple[int, ...]) -> tuple[int, int] | None:
    """一组连续记号在掩码文本里的 ``[起, 止)`` 区间。"""
    first, last = run[0], run[-1]
    m1 = re.search(rf"⟦{first}⟧", masked)
    if not m1:
        return None
    m2 = re.search(rf"⟦{last}⟧", masked[m1.start() :])
    if not m2:
        return None
    return m1.start(), m1.start() + m2.end()


def _neighbour_class(text: str, span: tuple[int, int], side: str) -> str:
    """记号组前/后紧邻的第一个**非空白**字符属于哪一类。

    返回 ``"cjk"`` / ``"latin"`` / ``"digit"`` / ``"punct"`` / ``""``（没有）。
    """
    if side == "before":
        seg = text[: span[0]]
        chars = [c for c in reversed(seg) if not c.isspace()]
    else:
        seg = text[span[1] :]
        chars = [c for c in seg if not c.isspace()]
    if not chars:
        return ""
    c = chars[0]
    if c in "⟦⟧":
        return ""  # 紧挨着另一个记号 → 本来就该合并，另算
    if "\u4e00" <= c <= "\u9fff":
        return "cjk"
    if c.isascii() and c.isalpha():
        return "latin"
    if c.isdigit():
        return "digit"
    return "punct"


# --------------------------------------------------------------------------
# 批量场景
# --------------------------------------------------------------------------


def repair_dropped_masks(
    masked_source: str,
    translated_raw: str,
    slots: list[str],
) -> str | None:
    r"""把模型**整段丢掉**的 ``⟦i⟧`` 按其上下文位置补回去。

    为什么需要：实测 ``translategemma:4b`` 遇到 ``\\C[6]``、``\\N[2]``、``\\n``
    这类 RPG Maker 转义时，会把屏蔽记号 ``⟦i⟧`` 直接删掉，只译文字。
    结果是整条被判 ``placeholder_broken`` 而**完全不产出译文** ——
    用户看到的是"这句话没被翻译"，而它对游戏完全可用（少的只是颜色/换行）。

    这个函数在**能确定位置**时补回记号，否则返回 ``None`` 让调用方维持拒绝：

    * 每个缺失记号用"它前面那个非空白字符"（``masked_source`` 里的字符）
      在译文里定位，把记号插到该字符之后；
    * 找不到锚点时，退化为按"该记号在原文中的相对位置"映射到译文，
      但**仅在缺失记号的相对顺序保持一致**时才这么做
      （顺序不一致就无法确定谁该在前，宁可拒绝）。

    只做插入、不删除不重排：已有记号的位置原样保留。

    安全性说明：这一步只影响**记号插入位置**，而"数量与内容是否齐全"
    仍由调用方的 :func:`verify_restored` 复查。中文与英文的句子结构差异
    可能让锚点落偏，但落偏最坏是"颜色代码套错了几个字"，
    比整条不翻译（或写回坏标记）都好；而如果记号数量仍不对，
    校验会再次拦下。
    r"""
    if not slots or not translated_raw:
        return None
    have = mask_indices(translated_raw)
    missing = [i for i in range(len(slots)) if i not in have]
    if not missing:
        # 一个不缺 → 无需修复
        return None
    # 注：**不**要求"至少剩一个记号"。试过加这个条件，结果把
    # RPG Maker 的单侧转义（`\C[6]标题` 整条被模型删掉记号）
    # 一起挡住了 —— 那 12 个用例是**真能救回来**的，且补回位置
    # 由锚点/相对位置确定（见 `test_placeholder_repair.py`）。
    #
    # 这里确实存在一个**已知取舍**：当模型把成对标签（Ren'Py 的
    # `{color=#ffd700}…{/color}`）整条丢掉时，补回位置是**猜**的
    # （实测会把 `{/color}` 补到句末，位置合理但不保证是作者本意）。
    # 接受这个猜测的理由：
    #
    #   * 补完后仍要过 `verify_restored`（数量与内容齐全）和
    #     `is_unsafe_writeback`（写回闸门）；
    #   * 最坏后果是"某几个字的颜色和原文不完全一致"，
    #     而不是崩溃、乱码或变量丢失；
    #   * 对"整条不翻"（玩家看到一句英文）而言，用户更倾向
    #     要中文而不是要绝对精确的配色。
    #
    # 如果将来发现某类标签猜错代价很高（例如控制排版而非配色），
    # 应当按**标签类型**分别决定，而不是一刀切禁止补回。

    anchors = _mask_anchors(masked_source)
    # 开头就是记号的（`\C[6]标题`）没有左邻文字，但位置是**确定**的：
    # 译文最前面。
    starts_with_mask = bool(_MASK_RE.match(masked_source))

    # 原文里记号的先后顺序，用来决定"先插谁"。必须与原文一致，
    # 否则（例如模型把 ⟦1⟧⟦0⟧ 写反了）无从判断哪个在前，宁可拒绝。
    src_order = [i for i in mask_indices(masked_source) if i in missing]
    if len(src_order) != len(missing):
        return None  # 编号不在原文里，调用方传错了 slots

    # ---- 先给每个缺失记号定一个插入点（都相对**原始**译文坐标） ----
    # 关键：位置必须一次性算完再插。边插边算会让后面的下标全部偏移，
    # 实测会把结果搞成 `500⟦0⟦1⟧⟧` 这种坏标记。
    # 落在同一位置的记号按原顺序一起插。
    #
    # "尾部记号"：在遮蔽原文里位于最后一个记号之后的内容里**没有任何非空白字符**
    # （即 `⟦i⟧` 之后什么都不剩，或只剩空格）。
    # 这类记号的插入位置是**唯一确定**的 —— 译文末尾。见下面 459 行附近的说明。
    src_mark_ids = mask_indices(masked_source)
    tail_after: dict[int, str] = {}
    for idx in missing:
        start = _mask_start(masked_source, idx)
        end = masked_source.find("⟧", start)
        tail_after[idx] = masked_source[end + 1:] if end != -1 else ""
    last_mark_id = max(src_mark_ids) if src_mark_ids else -1

    by_pos: dict[int, list[int]] = {}
    for idx in src_order:
        slot = slots[idx] if 0 <= idx < len(slots) else ""
        before, after = anchors.get(idx, ("", ""))
        _prop = int(
            round(_relative_position(masked_source, idx) * len(translated_raw))
        )
        # **位置敏感类优先按"比例位置 + 词边界吸附"定位，而不是先用词锚。**
        # 原因：词锚只告诉你"该词在哪"，不告诉你在词的哪一侧。
        # 实测 `You got %d gold`：右邻词 `gold` 的起点下标上，记号会贴成
        # `⟦0⟧gold`；而比例位置吸附到最近空白才得到正确的 `⟦0⟧ gold`。
        # 比例位置本来就是这个记号在句子里的相对位置，更贴近真实语义位置。
        if _needs_word_boundary(slot) or _needs_arg_boundary(slot):
            # 停顿/断行类（`\|` `\.`）放宽搜索半径，理由见 `_snap_to_boundary`：
            # 它们的绝对位置不需要精确，但**必须**落在词缝里；
            # 半径太窄会找不到边界而放弃补回，玩家就看到空白对话框。
            is_break = bool(slot) and len(slot) >= 2 and slot[0] == _BS and (
                slot[1] in _REPAIR_BREAK_CHARS
            )
            snapped = _snap_to_boundary(
                translated_raw, _prop, radius=24 if is_break else None
            )
            if snapped is None or _glues_tokens(
                translated_raw, snapped, _is_free_anywhere(slot)
            ):
                # 找不到安全边界，通常是"译文里全是汉字，一个空格和标点都没有"
                # 的纯中文短串（实测 `Gold: {gold}` → `'黄金'`：
                # `_snap_to_boundary('黄金', 2)` 返回 None，于是整条被判
                # placeholder_broken 而**完全不产出译文**，用户看到的是
                # "这个词没翻译"）。
                #
                # 但若该记号在原文里就是**最后一个记号**，且它后面再没有
                # 非空白内容，那么它的位置是**唯一确定**的：译文末尾。
                # 追加到末尾不可能劈开中文词，也不可能造成 `%dgold` 粘连
                # （粘连只在插到词中间时发生），所以这条是安全的。
                #
                # 注意只对"最后一个记号"这么做：如果一个更靠后的记号还没
                # 定位，先插它会让顺序错乱。
                is_trailing = idx == last_mark_id and not tail_after[idx].strip()
                if is_trailing:
                    by_pos.setdefault(len(translated_raw), []).append(idx)
                # 否则不补。宁可整条不译，也不产出粘连文本（`%dgold`）
                # 或把中文词劈开的换行。
                continue
            pos: int | None = snapped
        else:
            pos = _anchored_pos(translated_raw, before, after)
            if pos is None and starts_with_mask:
                # 该记号之前的原文没有任何可锚内容 → 属于"开头的标记串"
                n_before = _MASK_RE.sub(
                    "", masked_source[: _mask_start(masked_source, idx)]
                )
                if not n_before.strip():
                    pos = 0
            if pos is None:
                if not _is_free_anywhere(slot):
                    # 未知类型：位置无法确定就不补
                    continue
                # 样式/变量类：插偏几个字只影响生效范围，可以按比例插
                pos = _prop
        pos = max(0, min(len(translated_raw), pos))
        by_pos.setdefault(pos, []).append(idx)

    if not by_pos:
        return None

    # ---- 从右往左插，避免坐标偏移 ----
    out = translated_raw
    for pos in sorted(by_pos, reverse=True):
        marks = "".join(f"⟦{i}⟧" for i in by_pos[pos])
        out = out[:pos] + marks + out[pos:]
    return out


def _mask_anchors(masked_source: str) -> dict[int, tuple[str, str]]:
    """``{记号编号: (左邻文本, 右邻文本)}``。

    取的是"上一个记号结束 → 本记号开始"与"本记号结束 → 下一个记号开始"
    这两段原始文本。**不用单个字符**：译文换了语言，单个字母几乎必然消失
    （`costs ` 的 `'s'` 在 `这把剑要 …` 里根本不存在），
    而左右各留一小段才有机会命中。

    调用方负责从这些文本里截出可匹配的片段（见 :func:`_anchored_pos`）。
    """
    out: dict[int, tuple[str, str]] = {}
    last_end = 0
    matches = list(_MASK_RE.finditer(masked_source))
    for n, m in enumerate(matches):
        before = masked_source[last_end:m.start()]
        after_start = m.end()
        after_end = matches[n + 1].start() if n + 1 < len(matches) else len(masked_source)
        after = masked_source[after_start:after_end]
        try:
            out[int(m.group(1))] = (before, after)
        except (TypeError, ValueError):
            pass
        last_end = m.end()
    return out


def _anchored_pos(target: str, before: str, after: str) -> int | None:
    r"""在译文里给记号找一个插入点。

    策略（依次尝试，命中即返回）：

    1. **右邻词**：``\C[6]500`` 里的 ``500`` 在译文里通常原样保留
       （数字、专有名词、代码），找到它就插在它**前面**。这是最可靠的锚。
    2. **左邻词尾部**：用 ``before`` 末尾的词做锚，插在它**后面**。
       用于 ``Attack ⟦0⟧`` 这类词序未变的句子。
    3. 都给不出 → 返回 ``None``（调用方决定是拒绝还是按比例插）。

    **不要**在这里额外"退到词前空白"：直接返回词起点，让调用方按记号
    类型决定 —— 参数/换行类会走 :func:`_snap_to_boundary` 吸附到最近的
    边界（得到 ``You got %d gold``），而样式类允许就地插入。
    实测如果在锚点层就退到空白，会退过头变成 ``You got%d gold``
    （记号贴在前一个词尾上），反而更糟。

    只做"找位置"，不判断语义 —— 语义合理性由调用处按记号类型把关。
    """
    for probe in _anchor_probes(after):
        pos = target.find(probe)
        if pos >= 0:
            return pos
    for probe in _anchor_probes(before):
        pos = target.rfind(probe)
        if pos >= 0:
            return pos + len(probe)
    return None


def _anchor_probes(text: str) -> list[str]:
    """从一段文本里截出候选锚串，**长优先**。

    优先用"整词"（数字、连续的字母/数字），再用词尾的 2~3 个字符。
    太短的（1 个字符）噪声太大，不用。
    r"""
    text = text.strip()
    if not text:
        return []
    probes: list[str] = []
    # 1) 整词：数字/字母/汉字连续段
    for m in re.finditer(r"[0-9]+|[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}", text):
        probes.append(m.group(0))
    # 2) 末尾 3~2 个字符（对"词尾变化"更宽容）
    tail = text.rstrip()
    for n in (3, 2):
        if len(tail) >= n:
            probes.append(tail[-n:])
    # 去重、保持长优先
    seen: set[str] = set()
    out: list[str] = []
    for p in sorted(probes, key=len, reverse=True):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _snap_to_boundary(text: str, pos: int, *, radius: int | None = None) -> int | None:
    r"""把插入点吸附到最近的**词边界之后**，找不到返回 ``None``。

    用于换行与格式化参数，这类记号必须落在词的**边缘**：落在词中间会得到
    ``%dgold`` 这种粘连文本，或把中文词劈开。

    返回 **space/cand 下标 + 1**，即"记号插在该下标之前、分隔符的右侧"：

    * 空格在 ``i`` → 返回 ``i + 1`` → ``You got ⟦0⟧ gold``
    * 标点在 ``i`` → 返回 ``i + 1`` → ``伤害⟦0⟧。``

    空格优先于标点：``You got |gold and items.`` 里候选落在 ``got`` 中间，
    半径内既有空格也有句末句号；取**最近的空格**，否则记号会跑到句尾。

    半径默认 ``_SNAP_RADIUS``（6），避免记号跨到别的词之外。

    ``radius`` 可放宽，**只给"断行/停顿"类用**（见下）。

    ## 为什么 `\|` / `\.` 要放宽半径，而 `\n` / `%d` 不能

    * `\n` 和 `%d` 的**绝对位置**有意义：换行挪到别的句子、参数挪到别的
      语法位，都是错的。半径必须窄。
    * `\|` / `\.` 是**停顿**：玩家感知到的是"这里停一下"。
      它的绝对位置本来就不需要精确 —— 重要的是**落在词缝里**。
      一个 20 字的句子，停顿真值离最近标点常有 10 个字以上，
      半径 6 就会返回 `None` → 调用方放弃补回 → 整条译文失败、
      玩家看到空白对话框（真实事故，7 条）。

    所以放宽**只对停顿类**生效，`\n`/`%d` 仍然用默认的 6。
    """
    if not text:
        return None
    pos = max(0, min(len(text), pos))
    _SNAP_RADIUS = 6 if radius is None else max(1, radius)
    punct = set("，。！？；：、,.!?;:）)」』】”\"'")

    # 收集半径内所有边界候选：(距离, 插入下标)
    #
    # 插入下标的取法（这里我反复踩坑，写清楚）：
    #   · 空格在 i → 取 **i**（插在空格左邻）：空格留在记号右侧
    #     得到 `got ⟦0⟧ gold`。取 i+1 会得到 `got⟦0⟧ gold`
    #     （记号贴在前一个词尾上，实测踩过）。
    #   · 标点在 i → 取 **i+1**（插在标点右邻）：标点收尾
    #     得到 `伤害⟦0⟧。`。
    spaces: list[tuple[int, int]] = []
    puncts: list[tuple[int, int]] = []
    lo = max(0, pos - _SNAP_RADIUS)
    hi = min(len(text) - 1, pos + _SNAP_RADIUS)
    for cand in range(lo, hi + 1):
        dist = abs(cand - pos)
        if text[cand] == " ":
            # 插到空格**左邻**（下标 = 空格本身）→ 空格留在记号右侧，
            # 得到 `got ⟦0⟧ gold`：两侧都不粘连。
            spaces.append((dist, cand))
        elif text[cand] in punct:
            # 插到标点**右邻** → `伤害⟦0⟧。`：标点收尾，记号跟在其后。
            puncts.append((dist, cand + 1))

    if spaces:
        spaces.sort()
        return spaces[0][1]
    if puncts:
        puncts.sort()
        return puncts[0][1]
    return None


def _glues_tokens(text: str, pos: int, is_free: bool) -> bool:
    """在 ``pos`` 插入记号后，会不会把两个词**粘**在一起。

    只对**位置敏感**的记号（换行、格式化参数）需要检查：插进去以后
    如果两侧都是字母/数字/汉字，就会得到 ``%dgold`` 这种坏输出。
    样式类记号本来就允许插在词中间，直接放行。
    """
    if is_free:
        return False
    left = text[pos - 1] if pos > 0 else ""
    right = text[pos] if pos < len(text) else ""
    return bool(left) and bool(right) and _is_wordish(left) and _is_wordish(right)


def _is_wordish(ch: str) -> bool:
    """是否"词内字符"（字母/数字/汉字）。"""
    return ch.isalnum() or "\u4e00" <= ch <= "\u9fff"


def _needs_arg_boundary(slot: str) -> bool:
    """该占位符是否为**位置敏感的格式化参数**（``%d`` ``%s`` ``{0}`` 之类）。

    这类必须落在词边界：插进词中间会得到 ``%dgold`` 这种连在一起的输出。
    实测：模型丢掉 ``%d`` 后按比例回插，会得到
    ``You got %dgold and %sitems.`` —— 标记没坏，但文本已经不对了。

    与 :func:`_needs_word_boundary` 分开命名，是因为拒绝时的日志措辞不同
    （换行是"可读性"，参数是"格式化会出错"）。
    """
    if not slot:
        return False
    if slot.startswith("%"):
        return True
    # `{0}` `{name}` 这类 str.format 占位符：以 `{` 开头且内容不是标签
    return slot.startswith("{") and slot.endswith("}") and len(slot) > 2


def _mask_start(masked_source: str, index: int) -> int:
    """记号 ``index`` 在 ``masked_source`` 里的起始下标；不存在返回 0。"""
    for m in _MASK_RE.finditer(masked_source):
        try:
            if int(m.group(1)) == index:
                return m.start()
        except (TypeError, ValueError):
            continue
    return 0


def _relative_position(masked_source: str, index: int) -> float:
    """记号 ``index`` 在原文里的相对位置（0~1，按**非记号字符数**计）。

    用非记号字符数而不是字节/字符总长，是因为记号本身占位会扭曲比例 ——
    原文里记号越多，纯文本的相对位置越该被放大。
    """
    plain_len = len(_MASK_RE.sub("", masked_source)) or 1
    for m in _MASK_RE.finditer(masked_source):
        try:
            if int(m.group(1)) == index:
                before_plain = len(_MASK_RE.sub("", masked_source[: m.start()]))
                return before_plain / plain_len
        except (TypeError, ValueError):
            continue
    return 0.0


def _find_anchor_pos(target: str, anchor: str) -> int | None:
    """（已弃用）单字符锚点定位。

    留下签名只为兼容可能的旧调用；新代码请用 :func:`_anchored_pos` ——
    单个字母在译文里几乎必然消失。
    """
    if not anchor:
        return None
    pos = target.rfind(anchor)
    if pos < 0:
        return None
    return pos + 1


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
    r"""
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
