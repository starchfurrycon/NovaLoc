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

#: 永远不会有字形、也永远不需要渲染的字符。
#:
#: * ``\t`` ``\n`` ``\r`` —— 控制字符，换行/缩进由引擎排版处理；
#: * ``\u200b`` ``\u200c`` ``\u200d`` —— 零宽空格/非连接符/连接符；
#: * ``\ufeff`` —— BOM / 零宽不换行空格。
NON_RENDERING = frozenset("\t\n\r\u200b\u200c\u200d\ufeff")


def is_ignorable(ch: str) -> bool:
    """这个字符是不是**不需要字形**的（控制/换行/零宽）。

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
    """
    return not ch or ch in NON_RENDERING


__all__ = ["NON_RENDERING", "is_ignorable"]
