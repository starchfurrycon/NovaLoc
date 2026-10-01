r"""RPG Maker 的 `%1` `%2` 消息替换变量：**不能屏蔽，只能出站查**。

## 事故：两个模型都会把 `%1` 当语气词吞掉

`Database → 用语` 里的战斗消息用 `%1` `%2` 这种语法，游戏运行时替换成
**行动者名字**与**目标名字**。实测两个模型：

    '%1 attacks!'   translategemma:4b → '攻击！'        ← %1 没了
    '%1 casts %2!'  translategemma:4b → '%1 施法！'     ← %2 没了
    '¡%1 usa %2!'   HY-MT1.5-7B      → '有人使用了%2！' ← %1 没了

玩家看到 `'攻击！'` —— **谁攻击谁全没了**。

实测规模：真实游戏 357 条含 `%n` 的条目里 **189 条（53%）** 丢了变量。

## 为什么原来的规则抓不到

`placeholders.py` 里的 printf 规则是
`%\d*(?:\.\d+)?[sdfxXeEgGoc]` —— 要求**转换字符**（`%s` `%d`）。
RPG Maker 写的是**裸数字** `%1`，一条都不匹配，
于是 `%1` 被当成可翻译明文，`mask('%1 attacks!')` 返回槽位 `[]`。

## ★ 本文件记录的核心结论：**屏蔽它反而让它更容易被丢**

最直觉的修法是"把 `%1` 屏蔽成 `⟦0⟧` 保护起来"。**实测这是反的。**

拿 14 条**真实丢过变量**的源文做 A/B（`.scratch/_ab_pct_prompt.py`）：

| 做法 | `%n` 保住 |
|---|---|
| 屏蔽成 `⟦0⟧`，基线提示词 | **0 / 14** |
| 屏蔽成 `⟦0⟧` + "必须保留"规则 | **0 / 14** |
| 屏蔽成 `⟦0⟧` + "这是名字"规则 | **1 / 14** |
| 屏蔽成 `⟦0⟧` + 说清语义 + 反面例子 | **1 / 14** |
| `HY-MT1.5` 屏蔽 + "必须保留" | **2 / 14** |
| **不屏蔽**，让模型直接看到 `%1` | **约 7 / 14** |

还试过 6 种记号长相（`⟦0⟧` `{{0}}` `<ph0/>` `${0}` `[[0]]` `<0>`）——
**没有一种**能稳住句首那个，最好的也不到一半。

原因不难理解：`⟦0⟧` 对模型是个**没有语义的装饰符**，
而 `%1` 在训练数据里是**有含义的格式串**（printf 家族）。
**把语义换成装饰，模型就把它当噪音清掉了。**

所以最终做法是"**让模型看得见，出站再查**"：
不屏蔽 → 模型保住一半 → `check_percent_vars` 在写回前比对数量，
数量不对就判 fatal（相关条目会被标成 `failed`，而不是静默写一句坏话）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402
from novaloc.translate.guards import check_percent_vars, guard  # noqa: E402

#: 真实数据里**确认丢过变量**的源文（`.scratch/_verify_pct_var.py` 用的同一批）
REAL_SOURCES = [
    "%1 attacks!",
    "%1 guards.",
    "%1 casts %2!",
    "¡%1 lanza %2!",
    "%1 sucks blood!",
    "%1 meditates!",
    "%1 does bodyslam!",
    "%1 flees.",
    "%1 shoots out slime!",
    "%1 rouses their fighting spirit!",
    "MP: %1",
    "Deals 30% damage to %1",
    "%1 is waiting to see what happens.",
    "%1 recovers %2 HP!",
]


# ---------------------------------------------------------------------------
# 一、掩码器：`%n` **必须不被屏蔽**
# ---------------------------------------------------------------------------


def test_percent_var_is_not_masked() -> None:
    """★ 核心：`%1` 必须留在明文里交给模型看。

    这条是整份修复的重心。如果哪天有人"顺手"把 `%n` 加回掩码表
    （从直觉上看那是在"保护"它），这条会立刻变红 ——
    而实测那样做会让保住率从 ~50% 掉到 ~7%。
    """
    for src in ("%1 attacks!", "%1 casts %2!", "MP: %1"):
        m = ph.mask(src)
        assert m.slots == [], f"%n 不该被屏蔽，但 {src!r} 产生了槽位 {m.slots}"
        assert m.text == src, f"%n 不该被改写：{src!r} → {m.text!r}"


def test_real_sources_all_survive_masking_unchanged() -> None:
    """14 条真实源文全部原样通过掩码器（数字与位置都不动）。"""
    for src in REAL_SOURCES:
        m = ph.mask(src)
        assert m.text == src, f"{src!r} 被掩码器改成了 {m.text!r}"
        assert m.slots == []


def test_percent_var_helpers() -> None:
    """`is_percent_var` / `percent_vars` 的边界。"""
    assert ph.is_percent_var("%1")
    assert ph.is_percent_var("%12")
    assert not ph.is_percent_var("%")
    assert not ph.is_percent_var("%%")
    assert not ph.is_percent_var("%1 attacks!")  # 整串不是变量
    assert not ph.is_percent_var("%s")
    assert ph.percent_vars("%1 casts %2!") == ["%1", "%2"]
    assert ph.percent_vars("no vars here") == []


def test_normal_percentages_are_not_mistaken_for_vars() -> None:
    """**正常百分比不能被当成变量**（否则会把句子切碎）。"""
    for s in ("100% complete", "50% done", "增长 100%", "Deals 30% damage",
              "Heals 25 % of HP", "%", "%%"):
        assert ph.percent_vars(s) == [], f"{s!r} 被误判成含变量"
        assert ph.mask(s).text == s, f"{s!r} 被掩码器改坏"


def test_other_placeholders_still_masked() -> None:
    """放宽 `%n` 不能顺带把**别的**占位符一起放走。"""
    m = ph.mask(r"\C[29]abc\C[0]")
    assert m.slots == [r"\C[29]", r"\C[0]"]
    m2 = ph.mask("<right> X </right>")
    assert m2.slots == ["<right>", "</right>"]
    m3 = ph.mask("MP: %1 \\V[2]")
    assert m3.slots == [r"\V[2]"], "只该放过 %1，\\V[2] 必须仍被屏蔽"


# ---------------------------------------------------------------------------
# 二、出站判据：数量必须对
# ---------------------------------------------------------------------------


def test_missing_variable_is_caught() -> None:
    """丢掉变量必须报出来 —— 这是这条修复的真正价值。"""
    w = check_percent_vars("%1 attacks!", "攻击！")
    assert w and w[0].startswith("percent_var_missing"), w
    assert "%1" in w[0]


def test_partial_loss_is_caught() -> None:
    """丢一半也要报（`2→1` 是真实数据里最多的一类，96 条）。"""
    w = check_percent_vars("%1 casts %2!", "%1 施法！")
    assert w and "percent_var_missing" in w[0]
    assert "%2" in w[0], "应该指出丢的是 %2"


def test_correct_translation_passes() -> None:
    """变量齐全就通过 —— 位置不管（中文语序本来就活）。"""
    for tgt in ("%1 攻击！", "攻击！%1", "%1攻击！"):
        assert check_percent_vars("%1 attacks!", tgt) == [], f"{tgt!r} 不该被拦"


def test_swapped_variable_is_caught() -> None:
    """数量对得上但**内容变了**（`%1`→`%2`）也要拦。

    这会把行动者名字显示成目标名字 —— 数量校验抓不到，
    所以判据里单独比了排序后的内容。
    """
    w = check_percent_vars("%1 attacks!", "%2 攻击！")
    assert w and "percent_var_changed" in w[0]


def test_source_without_vars_never_flagged() -> None:
    """源文没有变量 → 译文中出现的 `%n` 不归这条管（别的判据负责）。"""
    assert check_percent_vars("Hello!", "你好！") == []
    assert check_percent_vars("Hello!", "你好 %1！") == []


def test_guard_marks_missing_variable_fatal() -> None:
    """整条 guard 必须把它判成 **fatal** —— 宁可失败也不写坏话进游戏。"""
    g = guard("%1 attacks!", "攻击！")
    assert g.fatal, f"丢了消息变量却没判 fatal：{g.warnings}"

    ok = guard("%1 attacks!", "%1 攻击！")
    assert not ok.fatal, f"正确的译文被误判 fatal：{ok.warnings}"


def test_guard_still_accepts_normal_percent_text() -> None:
    """反向：正常百分比文本不能被新判据误杀。"""
    g = guard("Deals 100% damage", "造成 100% 伤害")
    assert not g.fatal, f"正常百分比被误判：{g.warnings}"


def test_no_false_positive_on_all_real_sources() -> None:
    """14 条真实源文 + 正确的译文 → **一条都不能误报**。

    误报的代价是把本来正确的译文丢掉；这条守住"该放行的放行"。
    """
    for src in REAL_SOURCES:
        vars_ = ph.percent_vars(src)
        good = "".join(vars_) + "中文译文"
        w = check_percent_vars(src, good)
        assert w == [], f"{src!r} 的正确译文被误报：{w}"
