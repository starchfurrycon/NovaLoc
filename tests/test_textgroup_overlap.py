"""文本分组：**重叠块必须合并**，且合并后的文本不能有幻影字符。

这一组是纯几何/逻辑测试，**不需要字体、GPU 或模型**，CI 里会跑。

## 背景：这是实测抓到的真 bug

对一张写着 ``NEW GAME`` 的贴图，PP-OCRv6 (medium) 有时会把它切成
**互相重叠**的两块（实测数据，不是构造的）：

    'NEW'     box=(13, 29, 123, 76)   conf=0.99998
    'V GAME'  box=(99, 27, 253, 78)   conf=0.95670
                     ↑ 99 < 123 → 横向重叠 24 px

重绘流程是"先擦掉本块的框、再写本块的译文"，所以：

1. 擦 ``'NEW'`` 的框会连带擦掉 ``'V GAME'`` 的左半边；
2. 两块各写各的译文，后画的盖住先画的；
3. 产物 OCR 回读得到 ``'新V GAME'`` —— 中文与残留英文叠在一起。

**而流水线当时报告 `ok=True / translated=2 / changed=True`**，
从统计上看完全成功。这类"看着成功、实际做坏了"正是本项目最怕的失败模式。

## 修法分两层

* 几何层：重叠超过较小块宽度 15% 的两块**强制合并成一组**
  （越过 `MIN_MERGE_CHARS` 这类保守判据），绘制时用整组包围盒画一次；
* 文本层：合并后的组**不能**用拼接文本翻译 —— 重叠区的文字会被数两遍
  （``'NEW'`` + ``'V GAME'`` → ``'NEW V GAME'``，凭空多一个 ``V``），
  必须对整组包围盒**重读**。重读与绘制这两件事在 `test_texture_overlap.py` 里验证，
  本文件只验证几何与分配。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.textgroup import (  # noqa: E402
    MIN_MERGE_CHARS,
    OVERLAP_FORCES_MERGE,
    TextGroup,
    allocate_translation,
    group_blocks,
)
from novaloc.models import ImageTextBlock, TextBlockStyle  # noqa: E402


def blk(uid: str, box: tuple[int, int, int, int], text: str, conf: float = 0.99) -> ImageTextBlock:
    x1, y1, x2, y2 = box
    return ImageTextBlock(
        id=uid,
        box=list(box),
        quad=[(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
        source=text,
        confidence=conf,
        style=TextBlockStyle(),
    )


# 实测的两个块（见模块文档）
NEW = blk("p#0", (13, 29, 123, 76), "NEW", 0.99998)
VGAME = blk("p#1", (99, 27, 253, 78), "V GAME", 0.95670)


# ----------------------------------------------------------------------
# 1. 重叠必须合并
# ----------------------------------------------------------------------


def test_measured_overlapping_blocks_merge_into_one_group() -> None:
    """实测那两块必须落到同一组（否则产物会花）。"""
    groups = group_blocks([NEW, VGAME])
    assert len(groups) == 1, f"重叠块被分成了 {len(groups)} 组，重绘会互相覆盖"
    assert len(groups[0].blocks) == 2
    assert groups[0].has_overlap is True


def test_merge_happens_even_though_label_is_too_short() -> None:
    """``'NEW'`` 只有 3 字符（< MIN_MERGE_CHARS），但仍必须合并。

    `MIN_MERGE_CHARS` 的用意是"短标签合并后切不回去"。重叠时这条不适用：
    不合并的后果不是排版难看，而是**文字互相覆盖**。所以强制合并要越过它。
    """
    assert len("NEW") < MIN_MERGE_CHARS, "本测试的前提：'NEW' 短于合并门槛"
    assert len(group_blocks([NEW, VGAME])) == 1


def test_merge_happens_even_though_gap_is_negative_and_huge_span() -> None:
    """重叠块的"间距"是负数，跨度也可能超限，但都不该阻止强制合并。"""
    a = blk("a", (0, 0, 100, 40), "SOMETHING LONG")
    b = blk("b", (50, 1, 400, 39), "ELSE ALSO LONG")  # 重叠 50/100 = 50%
    assert len(group_blocks([a, b])) == 1


def test_tiny_jitter_overlap_does_not_force_merge() -> None:
    """1~2 px 的框抖动是正常的，不该把两个独立 UI 元素硬并成一句。

    原图的 ``same-line`` 聚类本来就会把它们放一行；这里要确认**没有**
    触发强制合并 —— 也就是仍然受 `MIN_MERGE_CHARS` / 间距判据约束。
    """
    a = blk("a", (0, 0, 100, 40), "Ab")
    b = blk("b", (99, 1, 200, 41), "Cd")  # 只重叠 1 px
    assert len(group_blocks([a, b])) == 2


def test_threshold_boundary_is_where_documented() -> None:
    """阈值语义：重叠 / 较小块宽度 达到 ``OVERLAP_FORCES_MERGE`` 才触发。"""
    assert OVERLAP_FORCES_MERGE == 0.15
    width = 100
    # 刚好 15% → 触发
    ov = int(width * OVERLAP_FORCES_MERGE)
    a = blk("a", (0, 0, width, 40), "AAAAAA")
    b = blk("b", (width - ov, 1, width * 2, 41), "BBBBBB")
    assert len(group_blocks([a, b])) == 1
    # 略低于 15% → 不触发（两个长标签本来也可能因间距判据合并，
    # 所以这里用短标签确保只可能由"强制"路径合并）
    a2 = blk("a", (0, 0, width, 40), "Ab")
    b2 = blk("b", (width - ov + 3, 1, width * 2, 41), "Cd")
    assert len(group_blocks([a2, b2])) == 2


def test_contained_block_merges_with_container() -> None:
    """小块完全被大块包住时必须合并 —— 否则擦大块会抹掉小块的全部笔画。"""
    big = blk("big", (0, 0, 200, 50), "OUTER TEXT HERE")
    small = blk("small", (60, 10, 120, 40), "IN", 0.9)
    groups = group_blocks([big, small])
    assert len(groups) == 1
    assert groups[0].has_overlap is True


def test_non_overlapping_blocks_keep_previous_behaviour() -> None:
    """没有重叠时行为不变：两个短标签仍然各自成组（保守合并的原有取舍）。"""
    a = blk("a", (0, 0, 60, 40), "Ok")
    b = blk("b", (200, 0, 300, 40), "No")
    groups = group_blocks([a, b])
    assert len(groups) == 2
    assert all(g.has_overlap is False for g in groups)


def test_long_adjacent_blocks_still_merge_as_before() -> None:
    """原有能力不能退化：相邻的长句仍要合并成一组送翻译。"""
    a = blk("a", (0, 0, 160, 40), "HP 1250 / 3000")
    b = blk("b", (170, 0, 260, 40), "MP 480")
    groups = group_blocks([a, b])
    assert len(groups) == 1
    assert groups[0].has_overlap is False  # 不是靠强制路径


# ----------------------------------------------------------------------
# 2. 重叠组的译文分配：整句给首块，绝不切分
# ----------------------------------------------------------------------


def test_overlapping_group_gives_whole_translation_to_first_block() -> None:
    """重叠组不做比例切分，整句交给首块，其余块为空串。

    切分在这里毫无意义：各块的框互相交叠，绘制时用的是**整组包围盒**
    （见 `TextureTranslator._draw_block(draw_box=...)`），切出来的碎片
    既画不到自己的框里，还会把完整译文劈成两半。
    """
    g = TextGroup(blocks=[NEW, VGAME])
    assert g.has_overlap is True
    alloc = allocate_translation(g, "全新游戏")
    assert [t for _b, t in alloc] == ["全新游戏", ""]


def test_overlapping_group_never_leaves_gap_or_duplicates() -> None:
    """整句译文必须**一字不丢、一字不重**地落在第一个块上。"""
    g = TextGroup(blocks=[NEW, VGAME])
    alloc = allocate_translation(g, "全新游戏")
    joined = "".join(t for _b, t in alloc)
    assert joined == "全新游戏"


def test_non_overlapping_group_still_splits_into_pieces() -> None:
    """未重叠的合并组仍然按比例切回各块 —— 这条能力不能被顺手改坏。"""
    a = blk("a", (0, 0, 160, 40), "HP 1250 / 3000")
    b = blk("b", (170, 0, 260, 40), "MP 480")
    g = TextGroup(blocks=[a, b])
    alloc = allocate_translation(g, "生命 1250 / 3000 魔力 480")
    pieces = [t for _b, t in alloc]
    assert all(pieces), f"应当切分成功，实际 {pieces}"
    assert "".join(pieces).replace(" ", "") == "生命1250/3000魔力480"


def test_group_with_three_overlapping_blocks_is_one_unit() -> None:
    """三块两两重叠时也应视为一个整体（只画一次）。"""
    a = blk("a", (0, 0, 100, 40), "AA")
    b = blk("b", (60, 0, 160, 40), "BB")
    c = blk("c", (120, 0, 220, 40), "CC")
    groups = group_blocks([a, b, c])
    assert len(groups) == 1, f"应合成一组，实际 {len(groups)} 组"
    assert groups[0].has_overlap is True
    alloc = allocate_translation(groups[0], "一二三")
    assert [t for _b, t in alloc] == ["一二三", "", ""]
