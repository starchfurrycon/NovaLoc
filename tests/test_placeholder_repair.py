"""占位符**丢失补回**的回归测试。

背景（实测 `translategemma:4b`）：遇到 RPG Maker 转义（颜色 `\\C[n]`、
变量 `\\N[n]`、换行 `\\n`）时，模型会把屏蔽记号整段删掉只译文字。
旧行为是整条判 `placeholder_broken`、**不产出任何译文** —— 用户看到
"这句没翻译"，而它其实完全可用（少的只是颜色或换行）。

补回逻辑必须区分两类占位符，这是本测试的核心：

* **样式/变量类**（颜色、图标、变量、富文本标签）—— 插偏几个字只影响
  样式生效范围，可以按比例位置补回；
* **换行与格式化参数类** —— 位置敏感：换行落进词中间会把中文词劈开，
  参数落错位置会得到 `%dgold` 这种粘连文本。这类只允许吸附到**词边界**，
  找不到边界就不补（宁可整条不译）。

方向性要求：记号顺序被交换时，补回逻辑**不得**把它"修"成通过的 ——
那属于静默损坏（占位符多重集仍然正确，但游戏读到的是错的值）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402

BS = chr(92)

#: (说明, 原文, 模拟模型输出, 是否应补回, 补回后是否应通过校验)
CASES: list[tuple[str, str, str, bool, bool]] = [
    # ---- 样式类：应补回 ----
    (
        "相邻的两个颜色标记",
        "The sword costs " + BS + "C[6]500" + BS + "C[0] gold.",
        "这把剑要 500 金币。",
        True,
        True,
    ),
    (
        "开头就是颜色标记",
        BS + "C[6]Chapter One",
        "第一章",
        True,
        True,
    ),
    (
        "单色标记在中间",
        "Deal " + BS + "C[2]fire" + BS + "C[0] damage.",
        "造成 火焰 伤害。",
        True,
        True,
    ),
    # ---- 变量类：占名词位，插偏可接受，应补回 ----
    (
        "变量 \\N[n]",
        "Beware the " + BS + "N[2] lurking within.",
        "小心潜伏在里面的东西。",
        True,
        True,
    ),
    (
        "变量 \\V[n] 在句首",
        BS + "V[1] joined the party!",
        "加入了队伍！",
        True,
        True,
    ),
    # ---- 换行类：有词边界才补 ----
    (
        "换行且译文有空格可断",
        "Restores 500 HP." + BS + "nTastes faintly of mint.",
        "恢复 500 生命值 尝起来有点薄荷味",
        True,
        True,
    ),
    (
        "换行但译文无任何边界",
        "Restores 500 HP." + BS + "nTastes faintly of mint.",
        "恢复生命值尝起来有点薄荷味",
        False,
        False,
    ),
    (
        "换行且译文只有汉字无标点",
        "Line one." + BS + "nLine two.",
        "第一行第二行",
        False,
        False,
    ),
    # ---- 格式化参数：位置敏感，只能吸附到词边界 ----
    (
        "printf 参数且译文有句号可断",
        "Dealt %d damage to %s.",
        "造成了伤害。",
        True,   # 句号是合法边界 → 补到句尾，得到「造成了伤害。%d%s」
        True,
    ),
    (
        "printf 参数且译文纯汉字无边界",
        "Dealt %d damage to %s.",
        "造成了伤害",
        False,  # 没有任何可断处 → 补进去会粘连，拒绝
        False,
    ),
    # ---- 不应改动 ----
    (
        "记号本来就齐全",
        "勇者" + BS + "V[1]获得圣剑" + BS + "C[3]",
        "勇者⟦0⟧获得圣剑⟦1⟧",
        False,
        True,
    ),
    (
        "没有占位符",
        "Hello world",
        "你好世界",
        False,
        True,
    ),
]


def _case(label: str, original: str, model_out: str, want_repair: bool, want_pass: bool) -> None:
    m = ph.mask(original)
    rep = ph.repair_dropped_masks(m.text, model_out, m.slots)
    if not want_repair:
        assert rep is None, f"{label}：不应补回，却得到 {rep!r}"
        return
    assert rep is not None, f"{label}：应补回，却返回 None"
    restored, chk = ph.verify_restored(original, rep, m.slots, masked_source=m.text)
    if want_pass:
        assert not chk.fatal, f"{label}：补回后仍致命 {chk.describe()}"
        assert "⟦" not in restored, f"{label}：还原后残留记号 {restored!r}"
    else:
        assert chk.fatal, f"{label}：应仍判致命"


@pytest.mark.parametrize(
    ("label", "original", "model_out", "want_repair", "want_pass"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_repair_dropped_masks(
    label: str, original: str, model_out: str, want_repair: bool, want_pass: bool
) -> None:
    _case(label, original, model_out, want_repair, want_pass)


def test_swapped_tag_order_is_not_repaired() -> None:
    """成对标签顺序被交换属于静默损坏，补回逻辑不得放过。"""
    original = "<color=#f00>Warning!</color>"
    m = ph.mask(original)
    swapped = "⟦1⟧警告！⟦0⟧"
    rep = ph.repair_dropped_masks(m.text, swapped, m.slots)
    target = rep if rep is not None else swapped
    _, chk = ph.verify_restored(original, target, m.slots, masked_source=m.text)
    assert chk.fatal, "顺序交换被放过了 —— 这是静默损坏"


def test_out_of_range_marker_is_not_repaired() -> None:
    """凭空多造的记号（越界）不得被接受。"""
    original = "勇者" + BS + "V[1]获得圣剑"
    m = ph.mask(original)
    bogus = "勇者⟦0⟧获得圣剑⟦9⟧"
    rep = ph.repair_dropped_masks(m.text, bogus, m.slots)
    target = rep if rep is not None else bogus
    _, chk = ph.verify_restored(original, target, m.slots, masked_source=m.text)
    assert chk.fatal, "越界记号被放过了"


def test_snap_to_boundary_never_glues_words() -> None:
    """不变量：位置敏感记号的插入点，两侧不得同时是词内字符。

    这条不变量是"补回不会产出 `%dgold` 之类粘连文本"的充要条件，
    比逐个样例断言更强 —— 它覆盖任意文本与任意候选位置。
    """
    texts = [
        "[译]You got gold and items.",
        "恢复 500 生命值 尝起来有点薄荷味",
        "造成了伤害。",
        "伤害%d。",
        "a",
        "  ",
        "",
        "x y z",
    ]
    for text in texts:
        for pos in range(len(text) + 1):
            p = ph._snap_to_boundary(text, pos)
            if p is None:
                continue
            assert 0 <= p <= len(text), f"越界：{text!r} pos={pos} → {p}"
            left = text[p - 1] if p > 0 else ""
            right = text[p] if p < len(text) else ""
            glued = bool(left) and bool(right) and ph._is_wordish(left) and ph._is_wordish(right)
            assert not glued, (
                f"插入点把词粘在一起：text={text!r} pos={pos} → {p}，"
                f"上下文 {text[max(0, p - 3):p]!r}|{text[p:p + 3]!r}"
            )


def test_slot_classification() -> None:
    """分类必须正确 —— 分类错了会导致"该补的不补"或"乱插位置"。"""
    free = [BS + "C[2]", BS + "I[4]", BS + "V[1]", BS + "N[2]", "<color=#f00>", "[b]"]
    sensitive = [BS + "n", BS + "r", "%d", "%s", "{0}"]
    for slot in free:
        assert ph._is_free_anywhere(slot), f"{slot!r} 应可随意插"
    for slot in sensitive:
        assert not ph._is_free_anywhere(slot), f"{slot!r} 不应可随意插"
    for slot in (BS + "n", BS + "r", "<br>", "<br/>"):
        assert ph._needs_word_boundary(slot), f"{slot!r} 应需词边界"
    for slot in ("%d", "%s", "{name}"):
        assert ph._needs_arg_boundary(slot), f"{slot!r} 应需参数边界"
