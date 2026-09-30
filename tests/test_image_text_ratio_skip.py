"""`ocr.skip_if_no_text_ratio` 必须**真的起作用**。

## 为什么专门测这个

这个配置项以前只写在 `config.py` 里，**代码里从来没人读它** ——
等于没有。这类"僵尸开关"很危险：用户看配置以为能控制行为，
实际一点效果都没有，还以为是自己没配置对。

## 为什么这个开关现在变得重要

修好 RPG Maker 加密资源支持之后，真实游戏的候选贴图从十几张涨到几百张：

* BeyondPortal（MZ）：**0 → 750** 张
* ElfLifia（MV）：**11 → 391** 张

多出来的绝大多数是背景图和地图图块。OCR 偶尔会在噪点上读出一两个短词，
那种"文字只占画面万分之几"的结果几乎必然是误检，却会让整张图走完
inpaint + 重绘（比单纯 OCR 贵一个数量级），还可能把乱字画到背景上。

## 阈值必须定得保守

实测 `system/Loading.png`（400x100）的 `Now Loading...` 一个块就占
整图 **44%** —— 真文字可以占很大比例。所以阈值只能用来卡
"小到不可能是真文字"的情况，默认 0.02% 是非常低的下限。
这个测试同时守住"真文字绝不能被跳掉"。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.service import TextureTranslator  # noqa: E402


def _blk(text: str, box: tuple[int, int, int, int] | None) -> SimpleNamespace:
    return SimpleNamespace(source=text, box=box)


# ----------------------------------------------------------------------
# 一、面积计算
# ----------------------------------------------------------------------

def test_text_area_ratio_basic() -> None:
    # 100x100 的图，一个 10x10 的框 → 1%
    assert TextureTranslator._text_area_ratio([_blk("hi", (0, 0, 10, 10))], 100, 100) == pytest.approx(0.01)


def test_text_area_ratio_sums_multiple_blocks() -> None:
    blocks = [_blk("a", (0, 0, 10, 10)), _blk("b", (20, 20, 30, 30))]
    assert TextureTranslator._text_area_ratio(blocks, 100, 100) == pytest.approx(0.02)


def test_text_area_ratio_handles_no_blocks_and_bad_size() -> None:
    assert TextureTranslator._text_area_ratio([], 100, 100) == 0.0
    assert TextureTranslator._text_area_ratio([_blk("a", (0, 0, 5, 5))], 0, 100) == 0.0
    assert TextureTranslator._text_area_ratio([_blk("a", (0, 0, 5, 5))], 100, 0) == 0.0


def test_text_area_ratio_ignores_blocks_without_box() -> None:
    """没有框的块不参与计算（不能用它把比例压低或抬高）。"""
    assert TextureTranslator._text_area_ratio([_blk("abc", None)], 100, 100) == 0.0


def test_text_area_ratio_is_capped_at_one() -> None:
    """重叠框可能相加超过整图 —— 必须夹到 1.0，不能让比例变成 3.0。"""
    blocks = [_blk("x", (0, 0, 100, 100)) for _ in range(5)]
    assert TextureTranslator._text_area_ratio(blocks, 100, 100) == 1.0


def test_text_area_ratio_handles_degenerate_box() -> None:
    """宽度/高度为 0 的框算 0 面积，不能算成负数。"""
    assert TextureTranslator._text_area_ratio([_blk("x", (10, 10, 10, 10))], 100, 100) == 0.0
    assert TextureTranslator._text_area_ratio([_blk("x", (20, 20, 10, 10))], 100, 100) == 0.0


# ----------------------------------------------------------------------
# 二、开关真的被读了吗（这是重点）
# ----------------------------------------------------------------------

def test_skip_ratio_is_actually_read_by_process() -> None:
    """**回归**：`process()` 必须读这个配置项。

    以前它是个僵尸开关 —— 只声明不读取。这条测试直接查源码里
    有没有引用，因为"配置存在"和"配置生效"是两件事，
    而僵尸开关最容易在重构里悄悄回来。
    """
    import inspect

    src = inspect.getsource(TextureTranslator.process)
    assert "skip_if_no_text_ratio" in src, (
        "`process()` 没有读 `skip_if_no_text_ratio` —— 这个开关又变回僵尸了"
    )


def test_skip_ratio_threshold_is_conservative() -> None:
    """阈值必须很低。真文字可以占很大面积（实测 Loading 图占 44%）。"""
    from novaloc.core.config import Config

    v = float(Config().ocr.skip_if_no_text_ratio)
    assert 0.0 < v <= 0.005, (
        f"阈值 {v} 太大 —— 真文字会被误跳。"
        "实测 `system/Loading.png` 的 `Now Loading...` 一个块就占整图 44%，"
        "所以只能卡'小到不可能是真文字'的情况。"
    )


def test_loading_screen_text_would_not_be_skipped() -> None:
    """**关键反例**：真实 loading 图上的文字绝不能被跳过。

    400x100 的 loading 图，`Now Loading...` 的框几乎占满 →
    比例约 44%，远高于默认阈值。
    """
    from novaloc.core.config import Config

    thr = float(Config().ocr.skip_if_no_text_ratio)
    ratio = TextureTranslator._text_area_ratio([_blk("Now Loading...", (60, 35, 340, 65))], 400, 100)
    assert ratio > thr, (
        f"loading 图的真文字占比 {ratio:.4%} 竟然低于阈值 {thr:.4%} —— 会被误跳"
    )


def test_noise_speck_would_be_skipped() -> None:
    """反过来：噪点上的一个小误检必须被跳过。"""
    from novaloc.core.config import Config

    thr = float(Config().ocr.skip_if_no_text_ratio)
    # 1920x1080 背景图上一个 12x8 的"文字"
    ratio = TextureTranslator._text_area_ratio([_blk("S", (900, 500, 912, 508))], 1920, 1080)
    assert ratio < thr, (
        f"1920x1080 背景图上的 12x8 噪点占比 {ratio:.6%} 高于阈值 {thr:.6%}，"
        "会被走完整套 inpaint + 重绘"
    )
