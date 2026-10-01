"""字符集相关的**叶子**工具：不依赖 fonts 包内任何其它模块。

## 为什么单独一个文件

`charset.py` 依赖 `coverage.py`（要读字体 cmap 才能规划覆盖），
所以 `coverage.py` **不能**反过来 `from .charset import ...` ——
那会形成循环导入，包一导入就炸：

    ImportError: cannot import name 'is_ignorable' from
    partially initialized module 'novaloc.fonts.charset'

而"这个字符需要字形吗"这个判定，`charset.py` 和 `coverage.py`
两边都要用，且必须用**同一套规则**（不然覆盖率的分子分母会对不上）。
把判定放到这个谁都不依赖的小模块里，两边都从这里取。
"""

from __future__ import annotations

import unicodedata

#: 永远不会有字形、也永远不需要渲染的字符。
#:
#: * ``\t`` ``\n`` ``\r`` —— 控制字符，换行/缩进由引擎排版处理；
#: * ``\u200b`` ``\u200c`` ``\u200d`` —— 零宽空格/非连接符/连接符；
#: * ``\ufeff`` —— BOM / 零宽不换行空格。
NON_RENDERING = frozenset("\t\n\r\u200b\u200c\u200d\ufeff")


def _is_variation_selector(ch: str) -> bool:
    """变体选择符（`U+FE00–FE0F`、`U+E0100–E01EF`）。

    这类字符把**前一个**字符选成某种呈现形式（例如 `U+FE0F`
    把 `✌` 选成 emoji 呈现）。它们天生没有自己的字形，也不该有。
    """
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def is_ignorable(ch: str) -> bool:
    """这个字符是不是**不需要字形**的（控制/换行/零宽/组合记号）。

    ## 为什么这个判定很关键

    字体里没有 ``\\n``、``\\t``、零宽空格这种东西的**字形** ——
    它们不是"缺字"，是根本不该被当成要渲染的字符。

    但字符集是从**译文和原文**里逐字符汇总出来的，而译文天然带换行。
    于是一个 ``\\n`` 会混进"需要覆盖"的集合，任何字体都不可能覆盖它：

    * 实测一个 549 字符的字符集，``\\n`` 让覆盖率停在
      **99.934%**（``0.999344...``）；
    * 合并器判定 ``coverage_after >= 0.999 and not missing_chars``，
      于是 ``ok=False``、``missing_chars='\\n'``；
    * 流水线按"缺字就硬失败"的契约**中止**，报"字体合并未成功产出" ——
      看起来像字体不够，实际是**一个换行符**。

    真实游戏上表现就是"字体适配永远失败、流程走不下去"，
    而报错信息完全指不到真正的原因。所以这个判定必须在**每一个**
    把字符当成"要渲染"的地方都用上（汇总、算覆盖率、算仍缺哪些字）。

    ## ★ 变体选择符与组合记号（真实事故，比换行更隐蔽）

    `U+FE0F` 出现在译文里（`✌️` / `❤️` 这类），而**没有任何**字体提供它：

        字符集里有 1 个字符没有任何候选字体能提供，无法生成完整字体：️

    （冒号后面**看着是空的** —— 那个字符本来就不可见。）

    后果有三层：

    1. **每一个**游戏字体都白跑一遍"剔除 → 重试"的弯路；
    2. `ship.otf` 的**真实**失败原因（缺 `glyf`/`loca`，CFF 字体
       无法合并）被这条噪音盖住，差点没查出来；
    3. 判据再严一点就会让**整轮字体适配失败** ——
       也就是"因为一个玩家看不见的字符，让全部中文变口口口"。

    同类的还有组合记号（`Mn`/`Me`，如 `U+0301`）：它们是**接在别的字上**的，
    同样没有独立字形。

    ## 这里和 `qa._is_blank_by_design` 的关系

    两者是**两个不同的问题**，早先被混成了一个：

    | 问题 | 谁答 | 答"是"的含义 |
    |---|---|---|
    | 这个字符**需要字形**吗？ | :func:`is_ignorable` | 不需要 ⇒ 不该进字符集 |
    | 渲染成空白**可以接受**吗？ | `qa._is_blank_by_design` | 可以 ⇒ 不算缺字 |

    大部分字符两者一致，但有**两处必须不同**，混用就会出事（都实测过）：

    * **全角空格 `U+3000` / NBSP `U+00A0`** —— 它们确实占宽度，
      字符集里**该有**（`UI_SAFE_CHARS` 特意列了它们），
      所以 `is_ignorable` 必须说"不忽略"；但渲染出来是空白，
      QA 必须说"这是正常的空白"。
      （早先 `is_ignorable` 认 `Zs` ⇒ 这两个空格被踢出字符集，
      实测 `test_ui_safe_chars_contain_no_ignorables` 与
      `test_incident_chars_would_be_source_only` 两条同时变红。）
    * **泰文的声调符号 / 阿拉伯文的元音符号**（`U+0E48` `U+0E35` `U+064B` 这类）
      —— 它们是**要渲染的**（有宽度的记号），不能因为"类别是 `Mn`"
      就从字符集里剔掉，否则那几条坏译文会变成"缺字"。
      但 QA 那一侧仍要把它们当"空白"放过，免得再制造噪音。

    结论：`is_ignorable` 只认**真正没有字形**的类别
    （控制 `Cc`、格式 `Cf`、零宽、变体选择符）；
    更宽的"渲染成空可以接受"判定放在 `qa` 一侧。
    """
    if not ch:
        return True
    if ch in NON_RENDERING:
        return True
    if _is_variation_selector(ch):
        return True
    # 只认真正没有字形的类别。
    # **不能**把 Zs/Zl/Zp（空格类）算进来：全角空格是有宽度的，字符集里该有它。
    return unicodedata.category(ch) in ("Cc", "Cf")


def is_blank_by_design(ch: str) -> bool:
    """渲染成空白是**预期行为**（不是"缺字"）。

    比 :func:`is_ignorable` **更宽**：空格类字符（`Zs`/`Zl`/`Zp`）
    与组合记号（`Mn`/`Me`）渲染出来是空的，但那不是"字体缺字"。

    两处不同的理由见 :func:`is_ignorable` 的说明 —— 混用会让
    全角空格被踢出字符集、或让 Thai/阿拉伯文的记号被当成缺字。
    """
    if is_ignorable(ch):
        return True
    cp = ord(ch)
    cat = unicodedata.category(ch)
    # 空格类：占宽度但没有墨迹，渲染成空白是正确的（不是缺字）
    if cat in ("Zs", "Zl", "Zp"):
        return True
    # 组合记号：接在别的字上，没有独立字形。
    # 只放行非 ASCII，避免把 ASCII 里的怪字符也放过。
    return cp > 0x7F and cat in ("Mn", "Me")


__all__ = ["NON_RENDERING", "is_blank_by_design", "is_ignorable"]
