"""从真实游戏验证中提炼出的两个文字块过滤器 —— 先写测试，再改代码。

## 实测事实（不是推测）

在一个真实 MV 游戏上跑完整 OCR，`www/img/pictures/stand_images/` 下的
**人物立绘**（386x680）被识别出单个字符并**自动"翻译"**：

| 图片 | 识别 | 置信度 | 识别区域占整图 |
| --- | --- | --- | --- |
| `damage_nomal.png` | `'2'` | 0.596 | 48% |
| `victory_near_pinch.png` | `'S'` | 0.598 | 17% |
| `victory_nomal.png` | `'2'` | 0.718 | 41% |
| `wait_pinch.png` | `'0'` | 0.546 | 40% |

对照真文字 `system/Loading.png`（400x100）的 `Now Loading...`：
区域占 44% —— **占比这条对"整张图就是一条文字条"的 UI 图无效**，
所以不能只靠占比判断。

而最糟的一条是 `'0'` 被翻成 `'VS'`：一个**误读**经过翻译层被加固成
一个看起来像样的词，然后被重绘进立绘。用户永远不会知道那里本来没有字。

## 两个独立的判据

单字符会被挡掉：`'2' 'S' '0'` 都是一个字符。
`Now Loading...`（14 字符）不受影响。

占比会被挡掉：立绘的 40%+ 区域会被挡掉。
`Loading.png` 的 44% 也会被挡 —— **这正是"两个判据都要有"的原因**，
所以这两条各自独立测试，并且在集成层面确认 `Now Loading...` 仍能通过
（它靠"字符数足够"这一条活下来，而不是靠占比）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.models import ImageTextBlock  # noqa: E402


def _block(text: str, box: tuple[int, int, int, int]) -> ImageTextBlock:
    return ImageTextBlock(id="t#0", box=box, source=text, confidence=0.9, ocr_engine="ppocrv6")


# ----------------------------------------------------------------------
# 过滤逻辑：先看接口在不在，不在就明确跳过（不假装通过）
# ----------------------------------------------------------------------

def _filter():
    """拿到贴图服务的文字块过滤器。

    它是 `TextureTranslator` 的方法，所以这里造一个**只用到该方法**的实例。
    `__new__` 绕过 `__init__`（构造会去探测 OCR/DirectML，纯逻辑测试不需要）。
    找不到方法就 skip，而不是静默通过。
    """
    from novaloc.images.service import TextureTranslator

    for name in ("_is_plausible_text_block", "is_plausible_text_block"):
        fn = getattr(TextureTranslator, name, None)
        if fn is not None:
            inst = TextureTranslator.__new__(TextureTranslator)
            return lambda blk, w, h: fn(inst, blk, w, h)
    pytest.skip("service 里还没有文字块过滤器（本测试文件先行）")


# 真文字：一个字符都没有的问题吗？不 —— 它够长
REAL_TEXT = [
    ("Now Loading...", (64, 20, 334, 85), (400, 100)),
    ("NEW GAME", (10, 10, 200, 40), (400, 100)),
    ("Continue", (10, 10, 120, 40), (400, 100)),
    ("はい", (10, 10, 60, 40), (200, 100)),      # 日文两个假名也算真文字
    ("开始游戏", (10, 10, 100, 40), (200, 100)),
]

# 幻觉：单个字母/数字/符号，从立绘里"读"出来的
HALLUCINATED = [
    ("2", (100, 100, 286, 480)),
    ("S", (64, 20, 128, 400)),
    ("0", (100, 200, 286, 480)),
    ("8", (10, 10, 50, 50)),
]


@pytest.mark.parametrize("text,box,size", REAL_TEXT)
def test_real_ui_text_passes(text, box, size) -> None:
    fn = _filter()
    blk = _block(text, box)
    assert fn(blk, size[0], size[1]), f"真文字被误杀：{text!r}"


@pytest.mark.parametrize("text,box", HALLUCINATED)
def test_single_glyph_is_rejected(text, box) -> None:
    """单个字母/数字必然是幻觉或噪声 —— 实测四个全是。

    连"数字单独成块"也一并挡掉：游戏 UI 里真有独立数字的情况（分数、
    金币数）几乎都以变量渲染在引擎文本层，而不是烘焙进贴图。
    为极少数例外放过这类块，代价是往人物立绘上重绘 `VS` 这种错误。
    """
    fn = _filter()
    blk = _block(text, (box[0], box[1], box[2], box[3]))
    assert not fn(blk, 386, 680), f"单字符幻觉没被挡住：{text!r}"


def test_huge_region_on_tall_image_is_rejected() -> None:
    """占画面 40% 的"文字块"在立绘上是幻觉。"""
    fn = _filter()
    blk = _block("ABCDEF", (0, 0, 380, 660))  # 386x680 的 96%
    assert not fn(blk, 386, 680)


def test_long_text_in_large_region_is_kept() -> None:
    """**关键边界**：`Loading.png` 是真文字，而且占比也很大（44%）。

    所以过滤器不能只看占比 —— 长文本要放行，否则真实的
    loading 条、标题画面会被一起挡掉。
    """
    fn = _filter()
    blk = _block("Now Loading...", (64, 20, 334, 85))
    assert fn(blk, 400, 100), "Now Loading... 被占比规则误杀了"


def test_empty_text_is_rejected() -> None:
    fn = _filter()
    assert not fn(_block("", (0, 0, 10, 10)), 100, 100)
    assert not fn(_block("   ", (0, 0, 10, 10)), 100, 100)


def test_degenerate_box_is_rejected() -> None:
    fn = _filter()
    assert not fn(_block("Hello", (10, 10, 10, 10)), 100, 100)
