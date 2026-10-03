r"""新增的换行补回：回归测试。

## 钉住的缺陷

真实游戏库（`072 Project`，rpgmaker MV）按**源文行数**分桶的失败率：

| 源文行数 | 条目 | 失败 | 失败率 |
| --- | --- | --- | --- |
| 1 行 | 3614 | 2 | **0.1%** |
| 2 行 | 610 | 215 | 35.2% |
| 3 行 | 381 | 98 | 25.7% |
| 4 行 | 173 | 93 | 53.8% |
| 5 行 | 321 | 310 | **96.6%** |

⇒ 缺陷**只与行数相关**，与内容无关。

## 根因（实测）

批量提示词写"**键是行号**"，模型看到多行条目就按行回答，
把换行记号 `⟦n⟧` 丢掉：

    掩码 'She … combines ⟦0⟧martial arts … ⟦1⟧Healthy …'
    模型 '{"0": "她使用…", "1": "健康美丽的女孩…"}'

译文对且全，只少换行记号 ⇒ 判致命 ⇒ **整条不产出译文**。

## 哪些必须继续被拒（安全边界）

`\V[1]`、`\N[2]`、`\c[0]` 这类**内容类**槽位丢了，位置无法确定，
补回就是猜（实测：颜色码相对位置偏差中位 **0.196**、P90 **0.640**
⇒ 按位置补会把颜色码插进词中间）。所以本函数**只处理换行**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402

#: 真实现场原文（来自工作区 f4b03ca791a9）
REAL_MULTI = (
    "She uses a special combat technique that combines \n"
    "martial arts and magic inherited from her family.\n"
    "Healthy girl with extraordinary athletism."
)
REAL_2LINE = (
    "\\c[2]\u3010Passive\\c[0]\n"
    "Maximum HP increases by 5%. Required LV:1."
)


def _mask(src: str) -> ph.MaskResult:
    return ph.mask(src, newlines=True)


def test_real_multiline_dropped_markers_is_repaired() -> None:
    """★ 真实现场：3 行源文、模型只回一段 ⇒ 必须补回换行。"""
    mr = _mask(REAL_MULTI)
    one = "她使用一种特殊的战斗技巧，该技巧结合了来自她的家族的武术和魔法。"
    _r0, c0 = ph.verify_restored(REAL_MULTI, one, mr.slots, masked_source=mr.text)
    assert c0.fatal, "前提：只回一段时必须判致命（否则这条用例没有意义）"

    fixed = ph.repair_missing_newlines(mr.text, one, mr.slots)
    assert fixed is not None, "3 行只回 1 段 —— 必须能补回"
    _r, c = ph.verify_restored(REAL_MULTI, fixed, mr.slots, masked_source=mr.text)
    assert not c.fatal, f"补回后仍判致命：{c.describe()}"


def test_repair_keeps_all_original_text() -> None:
    """★ 补回**只加记号**，绝不能改动或删除译文里的任何字符。"""
    mr = _mask(REAL_MULTI)
    one = "她使用一种特殊的战斗技巧。"
    fixed = ph.repair_missing_newlines(mr.text, one, mr.slots)
    assert fixed is not None
    stripped = fixed.replace("⟦0⟧", "").replace("⟦1⟧", "")
    assert stripped == one, f"补回改动了译文：{stripped!r} != {one!r}"


def test_inserted_newlines_are_at_sentence_boundaries() -> None:
    """★ 换行要落在**句读之后**，不能把一个词劈成两行。

    实测教训：纯按比例算出来的位置会落在"武|术"中间。
    """
    src = "First sentence here.\nSecond sentence here.\nThird sentence here."
    mr = _mask(src)
    assert len(mr.slots) == 2, "前提：这条应当有 2 个换行记号"
    translated = "第一句话在这里。第二句话在这里。第三句话在这里。"
    fixed = ph.repair_missing_newlines(mr.text, translated, mr.slots)
    assert fixed is not None, "2 个换行记号全丢 —— 必须能补回"
    # 每个换行记号必须紧跟在句读符之后
    for m in ph._MASK_RE.finditer(fixed):  # noqa: SLF001
        before = fixed[: m.start()].rstrip()
        assert before.endswith(("。", "！", "？", "，", "；", "、")), (
            f"换行记号插在了词中间：{fixed!r}"
        )


def test_content_placeholder_loss_is_not_repaired() -> None:
    """★★ 安全边界：**内容类**槽位丢了 ⇒ 绝不补（位置无法确定）。

    这些是 RPG Maker 的颜色/变量码，丢了游戏会显示错东西。
    实测按位置补会把它们插进词中间。
    """
    src = "\\c[2]\u3010Passive\\c[0]\nMaximum HP increases by 5%."
    mr = _mask(src)
    # 模型丢了颜色码，只译了文字
    translated = "【被动】最大生命值增加 5%。"
    out = ph.repair_missing_newlines(mr.text, translated, mr.slots)
    assert out is None, (
        "颜色码缺失时返回了修补结果 —— 位置是猜的，会套错色域"
    )


def test_no_missing_markers_returns_none() -> None:
    """一个记号都没丢 ⇒ 返回 None（不改动，幂等）。"""
    mr = _mask(REAL_MULTI)
    good = "第一行⟦0⟧第二行⟦1⟧第三行"
    assert ph.repair_missing_newlines(mr.text, good, mr.slots) is None


def test_empty_inputs_are_safe() -> None:
    assert ph.repair_missing_newlines("", "x", []) is None
    assert ph.repair_missing_newlines("a⟦0⟧b", "", ["\n"]) is None
    assert ph.repair_missing_newlines("a⟦0⟧b", "x", []) is None


def test_single_line_is_untouched() -> None:
    """单行条目本来没有换行记号 ⇒ 不该被改动。"""
    mr = _mask("Basilisk")
    assert mr.slots == []
    assert ph.repair_missing_newlines(mr.text, "蛇怪", mr.slots) is None


def test_two_newlines_land_in_ascending_order() -> None:
    """★ 多个换行必须**按原顺序**排列（否则第 2 行会跑到第 1 行前面）。"""
    mr = _mask(REAL_MULTI)
    translated = "甲。乙。丙。"
    fixed = ph.repair_missing_newlines(mr.text, translated, mr.slots)
    assert fixed is not None
    i0 = fixed.find("⟦0⟧")
    i1 = fixed.find("⟦1⟧")
    assert -1 < i0 < i1, f"换行顺序反了：{fixed!r}"


def test_real_two_line_color_case_is_not_repaired() -> None:
    """★★ 真实现场：`\\c[2]…\\c[0]` 跨行，模型把两个颜色码都丢了。

    这一条**故意**断言"不修补" —— 它记录的是**当前正确的保守行为**：
    颜色码位置无法确定，宁可保留原文也不套错色域。
    若将来实现了确定性规则，这条用例应当**改为**断言修补成功。
    """
    mr = _mask(REAL_2LINE)
    translated = "被动效果：最大生命值增加 5%。"
    assert ph.repair_missing_newlines(mr.text, translated, mr.slots) is None
