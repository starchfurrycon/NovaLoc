"""模型跑到**别的文字系统**去了 —— 这是本轮最"看不出来"的一类坏译文。

## 事故：字体阶段报"21 个字符没有字体覆盖"

跑 `fonts` 阶段时它硬失败了，报这些字符没有任何候选字体能提供：

    خ د ل ه و گ ی ജ വ ീ ก ค ด ย อ ี ไ ๆ ็ ่ ้

（阿拉伯／波斯、马拉雅拉姆、泰）

字体阶段的处理是**正确**的（拒绝写一个缺字形的字体，
否则游戏里就是口口口）。但**这些字符根本不该出现在中译里** ——
所以正确反应不是去找一个能覆盖阿拉伯文的字体，
而是去查它们从哪来。查出来的结果是：

============================ ==========================================
原文                          译文
============================ ==========================================
`'Suky'`（人名）              `'<right>苏 กี้</right>'` ← **泰文**
台词                          `'我现在就想让你 دخول我!!!'` ← **阿拉伯文**
台词                          `'...ജവീ...'` ← **马拉雅拉姆文**
============================ ==========================================

模型有时不翻译，而是**按发音给源文凑一个别的文字系统的拼写**。

## 为什么原先所有检查都放过去了

这些译文：非空 ✓、长度合理 ✓、占位符完好 ✓、还含汉字 ✓、
不含拉丁字母 ✓（所以 `looks_untranslated` 也不触发）。
玩家看到的是一句夹着阿拉伯/泰文的乱码话。

## 两个"分母口径"的坑（都实测踩过）

1. **不能只数 `isalpha()` 的字母**：泰文/天城文/阿拉伯文的
   元音与声调是**组合记号**，Python 不算 letter。
   实测 `'กี้'` 只数出 **1 个**（`ก`），
   于是 `'<right>苏 กี้</right>'` 的外来比例只有 8.3%，判据放过。
2. **分母必须排除富文本标签**：`<right>`/`</right>` 贡献了 12 个字母，
   把外来比例的分母撑大 2.4 倍。而这些标签在游戏里一个字符都不显示。
   用 `lang.visible_text()` 剥掉之后，同一条的比值从 8.3% 升到 **75%**。

## 阈值是拿真实数据校准的

`max_ratio=0.25` 且要求**至少 2 个**外来字符：

* `'伤害 3π'` → 17% → **放行**（希腊字母在数值里是正常写法）
* `'我现在就想让你 دخول我!!!'` → 27% → **挡住**
* `'<right>苏 กี้</right>'` → 75% → **挡住**

单个外来字符**绝不**触发，避免把"长句里一个希腊字母"误杀。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.lang import (  # noqa: E402
    count_foreign_script,
    foreign_script_ratio,
    longest_foreign_run,
    visible_text,
)
from novaloc.translate.guards import check_foreign_script, guard  # noqa: E402

# ----------------------------------------------------------------------
# 一、真实坏译文必须挡住
# ----------------------------------------------------------------------

REAL_MODEL_DRIFT = [
    "<right>苏 กี้</right>",       # 泰文（人名 Suky）
    "我现在就想让你 دخول我!!!",      # 阿拉伯文
    "<right>苏 ജവീ</right>",       # 马拉雅拉姆文
    "مرحبا بالعالم",                # 纯阿拉伯
    "<right>د ه گ ی</right>",      # 波斯/阿拉伯
    "你好 กี้ 世界",                # 中泰混排
]


@pytest.mark.parametrize("target", REAL_MODEL_DRIFT)
def test_model_drift_is_caught(target: str) -> None:
    """**核心回归**：这些译文原先**通过了所有检查**。"""
    assert check_foreign_script(target), f"模型跑偏没抓到：{target!r}"


@pytest.mark.parametrize("target", REAL_MODEL_DRIFT)
def test_model_drift_is_fatal(target: str) -> None:
    """必须是 **fatal** —— 写回游戏就是一句乱码。"""
    res = guard("Suky", target, target_lang="zh-Hans")
    assert res.fatal, f"应当 fatal：{target!r} {res.warnings}"
    assert any(w.startswith("foreign_script") for w in res.warnings)


def test_warning_says_what_got_in() -> None:
    """告警要**指出混进了什么**，否则排查时只知道"有外来字符"。"""
    res = check_foreign_script("<right>苏 กี้</right>")
    assert res, "应当命中"
    assert "กี้" in res[0], f"告警里应带上那段文字：{res[0]}"


# ----------------------------------------------------------------------
# 二、正常中译**必须放行**（误杀会丢真译文）
# ----------------------------------------------------------------------

NORMAL_TRANSLATIONS = [
    "游戏结束",
    "攻击力",
    "HP 恢复 30 点",
    "伤害 3π",              # 希腊字母在数值里是正常写法
    "角度 90°",
    "Use β version",
    "「你好」",
    "……",
    "Café Latte",
    "Boss 战",
    "π",                     # 单个字符绝不触发
    "苏基",
    "<right>苏基</right>",
    "造成 3.14 倍伤害",
    "α 粒子",
    "第 2 章 · 觉醒",
    "攻击 x2 (CT 100%)",
]


@pytest.mark.parametrize("target", NORMAL_TRANSLATIONS)
def test_normal_translation_passes(target: str) -> None:
    assert not check_foreign_script(target), f"正常译文被误杀：{target!r}"


def test_single_greek_letter_never_triggers() -> None:
    """**关键边界**：单个外来字符绝不触发，无论它在多短的文本里。

    否则 `'π'`、`'β'` 这种数值/术语会被误杀。
    """
    for t in ["π", "β", "α", "2π", "π 值", "用 β 版"]:
        assert not check_foreign_script(t), f"单个希腊字母被误杀：{t!r}"


def test_threshold_boundary_is_where_we_calibrated_it() -> None:
    """把校准点钉住：`'伤害 3π'` 卡在阈值上、`'…دخول…'` 明确越线。

    注意 `'伤害 3π'` 的比值**正好等于** 0.25（`π` 除以 4 个可见字符），
    所以真正让它放行的是 **`min_count=2`**（只有一个外来字符），
    不是比例条件。两个条件**都必须满足**才判坏 ——
    这条测试就是把这个分工写清楚，免得日后有人以为
    "调大 max_ratio 就能救希腊字母"。
    """
    low = "伤害 3π"                      # 1 个外来字符，比值恰好 0.25
    high = "我现在就想让你 دخول我!!!"      # 4 个外来字符，比值 0.267

    assert count_foreign_script(low) == 1, "π 只有一个字符"
    assert foreign_script_ratio(low) <= 0.25, "比值不应超过阈值"
    assert foreign_script_ratio(high) > 0.25, "阿拉伯那条必须越线"

    assert not check_foreign_script(low), "单个希腊字母必须放行（靠 min_count）"
    assert check_foreign_script(high), "成串阿拉伯文必须挡住"


# ----------------------------------------------------------------------
# 三、"分母口径"这两个坑本身
# ----------------------------------------------------------------------

def test_combining_marks_are_counted() -> None:
    """**坑 1**：泰文的 `ี`/`้` 是组合记号，`isalpha()` 不认。

    只数字母的话 `'กี้'` 只得到 1 个，判据会放过。
    """
    assert count_foreign_script("กี้") == 3, "三个字符（含两个组合记号）都要数"
    assert sum(1 for c in "กี้" if c.isalpha()) == 1, "证明 isalpha 口径确实只给 1"


def test_richtext_tags_are_not_part_of_the_denominator() -> None:
    """**坑 2**：`<right>`/`</right>` 撑着分母，把比值稀释掉。"""
    tagged = "<right>苏 กี้</right>"
    assert visible_text(tagged) == "苏 กี้", f"标签应被剥掉：{visible_text(tagged)!r}"
    # 剥掉标签后比值大幅上升 —— 这就是修复的关键
    assert foreign_script_ratio(tagged) > 0.5
    assert count_foreign_script(tagged) == 3


def test_escape_codes_are_also_stripped() -> None:
    """RPG Maker 转义也要剥掉：`\\C[29]` 在游戏里不显示。"""
    assert visible_text(r"\C[29]恢复\C[0]") == "恢复"


@pytest.mark.parametrize(
    "text,expect",
    [
        ("<right>苏 กี้</right>", "กี้"),
        ("我现在就想让你 دخول我!!!", "دخول"),
        ("ജവീ", "ജവീ"),
        ("游戏结束", ""),
        ("伤害 3π", "π"),
    ],
)
def test_longest_foreign_run(text: str, expect: str) -> None:
    assert longest_foreign_run(text) == expect
