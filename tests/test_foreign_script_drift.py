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

`max_ratio=0.25` 且要求**至少 2 个**外来字符
（这一条现在只作**兜底**，见下）：

* `'伤害 3π'` → 17% → **放行**（希腊字母在数值里是正常写法）
* `'我现在就想让你 دخول我!!!'` → 27% → **挡住**
* `'<right>苏 กี้</right>'` → 75% → **挡住**

单个外来字符**绝不**触发，避免把"长句里一个希腊字母"误杀。

## 主判据换成了"有没有出现一个**外来词**"

比例判据不够用，因为它**落在了边界上**。实测大量真跑偏恰好是 25.0%：

    '而且你竟然饶了它们 ജീവ'          3/12 = 25.0%  → 旧判据放过
    '你…？我 دیگه不用说了，对吧？'     4/16 = 25.0%  → 旧判据放过

**判据落在边界上，就说明判据选错了。**
真正的区分不是"外来字符占多少"，而是**"有没有混进来一个外文词"**：

* 单个外来字符（`π`/`β`/`Ω`）→ 正常符号，玩家认得；
* 连续 ≥3 个外来字符 → 那是**一个词**，中译里绝不该有。

在 **24,882 条真实译文**上实测（BeyondPortal 跑完一轮之后）：

=============================== ====== ====
判据                             命中   误报
=============================== ====== ====
旧：比例 > 25% 且 >= 2 个字符         3    0
新：连续外来段 >= 3 个字符             7    0
新 + 比例兜底（数单字符）              9    0
=============================== ====== ====

后 2 条是比例兜底补上的，都是真跑偏：

* `'¿Reina de las arañas?…'` → `'¿ملكة العنكبوت...؟ …'`
  —— 西语原文被译成了**整句阿拉伯语**（33 个外来字符）；
* `'…no permitiré que mis arañas te moleste'`
  → `'…我不会让你受到我的蜘蛛的 தொல்லை。'` —— 夹了个泰米尔词。

**误报 0 条**（24,882 条里一条正常译文都没被误杀）。
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
    foreign_script_runs,
    has_foreign_word,
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


# ----------------------------------------------------------------------
# 六、主判据："有没有出现一个外来词"
# ----------------------------------------------------------------------

#: 全部 7 条真实跑偏（用"连续外来段"判据就能抓到的那些）
ALL_REAL_DRIFTS = [
    ("而且你竟然饶了它们 ജീവ", "而且你還饒了它們的性命。"),
    ("在你离开之前，我还有 кое-что, 也许能帮到你。", "Antes de que te vayas, tengo algo…"),
    ("我现在就想让你 دخول我!!!", "I want you inside me right now!!!"),
    ("你…？我 دیگه不用说了，对吧？", "你……？我不需要再說什麼……對吧？"),
    ("我 دیگه没时间了。", "我不会再耽误你了。"),
    ("不过你总是 таком不可思议的样子，", "但你總是一副了不起的樣子，"),
    ("这只 الوحش 无懈可击，任何攻击都无法伤害它。", "这只野兽是不可阻挡的，什么都无法杀死它。"),
]

#: 另外 2 条：**只能**靠比例兜底抓到的真跑偏
#: （一条整句阿拉伯语、一条夹了泰米尔词）
FALLBACK_ONLY_DRIFTS = [
    ("¿ملكة العنكبوت...؟ الآن فهمت معنى الخاضعين.", "¿Reina de las arañas?…"),
    ("我可以向你保证，我不会让你受到我的蜘蛛的 தொல்லை。", "Podría prometerte que…"),
]


@pytest.mark.parametrize("target,source", FALLBACK_ONLY_DRIFTS)
def test_fallback_only_drifts_are_also_caught(target: str, source: str) -> None:
    """这两条是**比例兜底**补上的真实跑偏（段判据抓不到或抓不全）。

    留着它们是为了证明"兜底判据不是摆设" ——
    如果哪天有人觉得兜底多余、把它删了，这两条会立刻红。
    """
    assert check_foreign_script(target, source=source), f"漏掉了：{target!r}"


@pytest.mark.parametrize("target,source", ALL_REAL_DRIFTS)
def test_all_seven_real_drifts_are_caught(target: str, source: str) -> None:
    """**核心回归**：全部 7 条真实跑偏都必须在守卫里判坏。

    其中 4 条**旧的纯比例判据抓不到**（它们恰好是 25.0%），
    这条测试就是那次修复的钉子。
    """
    assert check_foreign_script(target, source=source), (
        f"漏掉了真实跑偏：{target!r}"
    )


def test_ratio_alone_would_have_missed_half_of_them() -> None:
    """说明"为什么要换判据"：4 条真跑偏的比例**恰好是 25.0%**。

    旧判据是"比例 > 25%"，所以它们全被放过。
    这里把那个数字钉下来 —— 如果哪天有人把阈值调回 `>=`，
    这条测试会提醒他"边界上还站着 4 条真事故"。
    """
    border = [
        "而且你竟然饶了它们 ജീവ",
        "你…？我 دیگه不用说了，对吧？",
    ]
    for t in border:
        assert foreign_script_ratio(t) == pytest.approx(0.25), (
            f"{t!r} 的比例不再是 25%？请重新校准阈值"
        )
        # 新判据仍然抓得到它
        assert check_foreign_script(t)


@pytest.mark.parametrize(
    "target",
    [
        "伤害 3π",
        "物理攻击 3π, 魔法防御 2Ω",
        "半径 r=5β",
        "穿过传送门",
        "载入中……",
    ],
)
def test_single_math_symbols_are_never_drift(target: str) -> None:
    """**单个**希腊字母是正常写法，绝不能误杀（实测 0 误报）。"""
    assert not check_foreign_script(target)
    assert has_foreign_word(target) == []


def test_source_containing_the_word_is_allowed() -> None:
    """原文本来就有那个外文词（专有名词/引文）→ 译文保留是**对的**。

    实测 7 条真跑偏的外来段**没有一条**出现在源文里（都是模型自己编的），
    所以"源文里没有"是一条干净的判据。
    """
    src = "падеж means grammatical case"
    tgt = "падеж 是格的意思"
    assert foreign_script_runs(tgt) == ["падеж"], "段要能被切出来"
    assert has_foreign_word(tgt, source=src) == [], "源文里有 → 不算跑偏"
    assert not check_foreign_script(tgt, source=src)
    # 但源文里**没有**的时候要判坏
    assert check_foreign_script(tgt, source="grammatical case")


def test_run_minimum_is_three() -> None:
    """`min_run=3`：实测最短的真跑偏段正好是 3（`'ജീവ'`、`'кое'`）。

    所以 3 既能覆盖全部真实案例，又能保住单字符符号。
    """
    # 单字符进不了"段"（段正则本身要求 >= 2），这正是我们要的
    assert foreign_script_runs("伤害 3π") == [], "单字符不该构成段"
    assert has_foreign_word("伤害 3π", min_run=3) == []
    assert has_foreign_word("伤害 3π", min_run=1) == [], "单字符永远进不了段判据"
    # 两字符段能切出来，但 `min_run=3` 会放过它（刻意的：`'3π'` 太容易误伤）
    assert foreign_script_runs("攻击力 ΩΩ") == ["ΩΩ"]
    assert has_foreign_word("攻击力 ΩΩ", min_run=3) == []
    assert has_foreign_word("攻击力 ΩΩ", min_run=2) == ["ΩΩ"], "min_run=2 才抓得到"
    # 三字符段：真实跑偏的最短长度
    assert has_foreign_word("攻击力 ΩΩΩ", min_run=3) == ["ΩΩΩ"]


def test_runs_are_split_by_punctuation_and_spaces() -> None:
    """段是**连续**的：`'кое-что'` 会被连字符切成两段，各自仍 >= 3。"""
    assert foreign_script_runs("我还有 кое-что, 也许") == ["кое", "что"]


def test_foreign_run_regex_shares_the_character_class() -> None:
    """段正则必须和单字符正则**用同一份区段**。

    踩过的坑：早先想省事，用 `_FOREIGN_SCRIPT_RE.pattern.strip("[]")`
    去拼段正则 —— `strip` 会把区段里的 `-` 也切掉，
    于是段正则**静默匹配不到任何东西**，判据等于没写。
    改法是抽一个 `_FOREIGN_SCRIPT_RANGES` 常量给两边共用。
    """
    from novaloc.lang import _FOREIGN_RUN_RE, _FOREIGN_SCRIPT_RE

    for ch in "πജدกЖ":
        assert _FOREIGN_SCRIPT_RE.match(ch), ch
        # 同一字符重复 3 次必须能被段正则认出
        assert _FOREIGN_RUN_RE.search(ch * 3), f"段正则认不出 {ch!r} 的连续段"


def test_fallback_ratio_catches_spaced_out_characters() -> None:
    """兜底：模型若把外文**拆成单个字符加空格**，段判据会失效，比例判据顶上。

    这就是为什么两条判据都要留着（任一命中即判坏）。
    """
    spaced = "a ส ุ ข ี b"  # 段被空格打散
    assert foreign_script_runs(spaced) == [], "段判据确实失效了"
    assert check_foreign_script(spaced), "比例判据应当兜住"
