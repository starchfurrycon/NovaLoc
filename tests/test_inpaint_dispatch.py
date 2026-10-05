r"""★ 背景采样环带宽度：**渐变背景必须走 inpaint**，不能被当成纯色。

## 实测缺陷（LaMa 因此**永远不被调用**）

`sample_background` 采的是"文字框外面 margin 像素的环带"。
原来 `SAMPLING_MARGIN = 4`，环带只有约 2~3 像素宽。

**垂直渐变在这么窄的带里几乎是常数** ⇒ 标准差 < `SOLID_STD_THRESHOLD(12)`
⇒ 被判成"纯色" ⇒ 直接填一块纯色 ⇒ **永远走不到 LaMa 那条路**。

实测（240×400 垂直渐变，框 `(55,100,300,155)`，阈值 12）：

| margin | std | 判定 |
|---|---|---|
| 3 | 11.28 | 纯色 ← **错** |
| **8** | **12.28** | 复杂 ← 对 |

于是 `inpaint_boxes(lama=...)` 对那些渐变图**根本没调用 LaMa**，
`method` 一直是 `solid`，而 `localize.json` 里看不到任何 `lama` ——
**加载了权重却一次都没用上**，属于最难发现的一类静默失效。

## 本文件测什么（钉住分派行为，不测具体像素）

1. **纯色**背景 ⇒ 走 `solid`（快，且不该退化）；
2. **渐变**背景 ⇒ **不能**是 `solid`；给了 lama 就必须是 `lama`。

这两条合起来正好覆盖"margin 太小"这个回归：
margin 一缩回去，第 2 条立刻红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.inpaint import (  # noqa: E402
    SOLID_STD_THRESHOLD,
    inpaint_boxes,
    sample_background,
)

H, W = 240, 400
BOX = (55, 100, 300, 155)


def _solid() -> np.ndarray:
    img = np.full((H, W, 3), (40, 40, 40), np.uint8)
    cv2.putText(img, "HELLO", (60, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3)
    return img


def _gradient() -> np.ndarray:
    """垂直渐变（真实游戏里很常见：横幅、按钮底、立绘边）。"""
    img = np.zeros((H, W, 3), np.uint8)
    for y in range(H):
        img[y, :] = (int(30 + 120 * y / H), int(80 + 80 * y / H), int(200 - 100 * y / H))
    cv2.putText(img, "HELLO", (60, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3)
    return img


class _FakeLama:
    """假 LaMa：只记录"被调用过"，并原样返回（不跑 6 秒真推理）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        self.calls += 1
        return image


# ----------------------------------------------------------------------
# 判据 1：std 阈值本身能区分纯色与渐变
# ----------------------------------------------------------------------
def test_gradient_std_exceeds_solid_threshold() -> None:
    r"""★ 核心判据：渐变的环带标准差必须**高于**阈值。

    这条是在"环带宽度"这一层钉住问题 —— 比测 `method` 字符串更靠近根因。
    """
    _col_g, std_g = sample_background(_gradient(), BOX)
    _col_s, std_s = sample_background(_solid(), BOX)
    assert std_s < SOLID_STD_THRESHOLD, f"纯色背景的 std 应低于阈值，实为 {std_s:.2f}"
    assert std_g >= SOLID_STD_THRESHOLD, (
        f"渐变背景的 std 应不低于阈值，实为 {std_g:.2f} —— "
        "环带太窄会把渐变误判成纯色，LaMa 就永远不会被调用"
    )


# ----------------------------------------------------------------------
# 判据 2：分派行为
# ----------------------------------------------------------------------
def test_solid_background_uses_solid_fill() -> None:
    """纯色背景 ⇒ 直接填色（最快，也不该退化）。"""
    res = inpaint_boxes(_solid(), [BOX], dilate=3, lama=None)
    assert res.method == "solid", res.method


def test_gradient_background_does_not_use_solid_fill() -> None:
    r"""★★ 渐变背景**绝不能**用纯色填充。

    用纯色块盖住渐变上的文字，在视觉上是明显的方框瑕疵。
    这条就是"margin 太小"那个回归的守卫。
    """
    res = inpaint_boxes(_gradient(), [BOX], dilate=3, lama=None)
    assert res.method != "solid", (
        f"渐变背景被判成纯色（method={res.method}）—— "
        "环带太窄，见 SAMPLING_MARGIN 的 docstring"
    )
    assert res.method in {"telea", "ns"}, res.method


def test_lama_is_actually_called_for_gradient() -> None:
    r"""★★ 给了 LaMa 时，渐变图**必须真的调用它**。

    这是"权重加载了却一次没用上"的直接守卫 —— 那个缺陷的表现是
    `method` 恒为 `solid`，日志里却一切正常。
    """
    fake = _FakeLama()
    res = inpaint_boxes(_gradient(), [BOX], dilate=3, lama=fake)
    assert fake.calls == 1, f"LaMa 应被调用 1 次，实际 {fake.calls}"
    assert res.method == "lama", res.method


def test_lama_is_not_called_for_pure_solid() -> None:
    """反向：纯色背景**不该**浪费一次 LaMa（6 秒/图）。"""
    fake = _FakeLama()
    res = inpaint_boxes(_solid(), [BOX], dilate=3, lama=fake)
    assert fake.calls == 0, "纯色背景不该调用 LaMa"
    assert res.method == "solid", res.method
