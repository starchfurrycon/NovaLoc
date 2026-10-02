"""贴图重绘**不能丢掉 alpha 通道**。

## 为什么这是必须守住的

RPG Maker（以及绝大多数 2D 游戏的 UI）大量依赖贴图的透明通道：
`img/system/Window.png`（对话框九宫格）、`ButtonSet.png`、各种图标、
`img/pictures/` 下的叠加层。这些图的特点是**中间有内容、四周透明**。

如果重绘后写出的是不透明图，游戏里会变成：

* 对话框四周出现实心黑边／白边（九宫格拉伸后尤其明显）；
* 图标变成一个个带底色的方块，糊在场景上。

这是那种"一眼就能看出汉化坏了"的画面损坏，而且它只影响**带透明的图** ——
如果测试只用不带 alpha 的图（RGB），这个 bug 会完全测不出来。

## 这个 bug 的成因（写清楚，避免再犯）

```python
elif raw.shape[2] == 4:
    raw = raw[:, :, :3]     # ← 这里拿到的是**视图**，不是拷贝
```

NumPy 的切片给的是视图。`raw` 之后一路流进 `inpaint_boxes()`，
而 inpaint 内部会做 `canvas[y1:y2, x1:x2] = ...` 这种按 3 通道的赋值 ——
对这个视图赋值会把结果变成 3 通道数组（视图与原始 buffer 共享内存，
没法再 dstack 回去）。所以旧代码的输出必然是 3 通道，alpha 就这样没了。

修法：读进来就 `alpha = raw[:, :, 3].copy()`，RGB 部分用
`np.ascontiguousarray` 拷成独立数组；`process()` 的所有出口
通过闭包 `_restore_alpha()` 把 alpha 贴回。

## 测试图必须画**真能认出来的字**

纯噪声／纯色图 OCR 读不出东西，`process()` 会走"没文字 → 原图返回"
的早退路径，重绘分支压根不执行 —— 测试会**假绿**
（断言 alpha 保住了，但其实没测到重绘路径）。
所以这里用真实字体画清晰文字，并显式断言"OCR 确实读到了块"。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.io import imread_bgr, imwrite_bgr  # noqa: E402
from novaloc.images.service import TextureTranslator  # noqa: E402

W, H = 400, 100

#: 真实游戏里的明文贴图（含 OCR 稳定读得出的 `Now Loading...`）。
#: 用它当文字来源是刻意的：我先用 `cv2.putText` 画字想让 OCR 读，
#: 结果读不出来（"text detection result is empty"）—— RapidOCR 的检测头
#: 认不出那种合成字形，测试会假绿。真实资产才读得出来。
_REAL_TEXT_ASSET = (
    Path(r"D:\NovaLocData\real\ElfLifia\www\img\system\Loading.png"),
    "Now Loading...",
)


def _has_real_text_asset() -> bool:
    return _REAL_TEXT_ASSET[0].is_file()


def _rgba_with_text(w: int = W, h: int = H) -> np.ndarray:
    """造一张"中间有清晰文字、四周全透明"的 RGBA UI 贴图。

    文字来自真实游戏资产（OCR 读得出），布局用 alpha 合成摆到透明画布上，
    所以既覆盖**重绘路径**，又覆盖**透明区域**。
    """
    src_path, _ = _REAL_TEXT_ASSET
    text_img = imread_bgr(src_path)
    if text_img is None:  # pragma: no cover - 依赖真实游戏
        raise FileNotFoundError(src_path)
    if text_img.ndim == 2:
        text_img = np.stack([text_img] * 3, axis=-1)
    text_img = text_img[:, :, :3]

    th, tw = text_img.shape[:2]
    img = np.zeros((max(h, th), max(w, tw), 4), dtype=np.uint8)
    img[:, :, 3] = 0  # 全透明底
    # 把文字图当成不透明内容贴到中间：亮的地方当"字"，
    # 用它的灰度做 alpha，这样透明底上就有清晰的深色文字
    y0, x0 = 8, 8
    patch = text_img[: min(th, img.shape[0] - y0), : min(tw, img.shape[1] - x0)]
    gray = patch.mean(axis=2)
    alpha_patch = np.where(gray > 128, 255, 0).astype(np.uint8)
    img[y0 : y0 + patch.shape[0], x0 : x0 + patch.shape[1], :3] = patch
    img[y0 : y0 + patch.shape[0], x0 : x0 + patch.shape[1], 3] = alpha_patch
    return img


def _translator() -> TextureTranslator:
    return TextureTranslator(Context(config=Config(), events=EventBus()))


def _assert_alpha_intact(out: np.ndarray, original: np.ndarray) -> None:
    """公共断言：通道数、尺寸、透明区、不透明区都还对。"""
    assert out.ndim == 3 and out.shape[2] == 4, (
        f"输出是 {out.shape} —— alpha 通道丢了，"
        "游戏里透明区域会变成实心方块"
    )
    assert out.shape[:2] == original.shape[:2], "尺寸被改了"
    assert out[0, 0, 3] == 0, "左上角原本透明，现在不透明了"
    assert out[-1, -1, 3] == 0, "右下角原本透明，现在不透明了"


requires_real = pytest.mark.skipif(
    not _has_real_text_asset(),
    reason="需要真实游戏的明文贴图当文字来源（本机没有就跳过）",
)

#: 任何**真的调用 `TextureTranslator.process()`** 的用例都需要 OCR 引擎。
#:
#: 没有 rapidocr 时 `process()` 不会抛异常，而是返回
#: `image=None, error='未安装 rapidocr'` —— 于是断言表现为
#: `assert None is not None`，**完全看不出是缺依赖**（实测 CI 就是这么红的）。
#: 所以这里显式 skip，让原因写在报告里。
requires_ocr = pytest.mark.skipif(
    importlib.util.find_spec("rapidocr") is None,
    reason="需要 rapidocr（否则 process() 返回 image=None、报错信息看不出来）",
)


# ----------------------------------------------------------------------
# 一、主路径：真的走到重绘
# ----------------------------------------------------------------------

@requires_real
def test_alpha_survives_redraw_path(tmp_path: Path) -> None:
    """**核心断言**：走过重绘路径后 alpha 仍在。

    这条在旧代码下必然失败（输出 3 通道）。
    同时断言 OCR 真读到了字 —— 否则就是假绿（早退路径没覆盖重绘）。
    """
    src = tmp_path / "loading.png"
    img = _rgba_with_text()
    assert imwrite_bgr(src, img)

    res = _translator().process(src, asset_uid="t")
    assert res.image is not None, f"处理失败：{res.error}"

    assert res.asset.blocks, (
        "OCR 没读出任何文字 —— 这张测试图没起作用，重绘路径没被覆盖，"
        "alpha 断言会变成假绿"
    )
    _assert_alpha_intact(res.image, img)
    assert not np.array_equal(res.image[:, :, :3], img[:, :, :3]), (
        "画面完全没变，说明重绘没生效"
    )


@requires_real
def test_alpha_survives_disk_round_trip(tmp_path: Path) -> None:
    """写盘再读回 alpha 必须还在（回写游戏时真正发生的事）。"""
    src = tmp_path / "rt.png"
    img = _rgba_with_text()
    assert imwrite_bgr(src, img)

    res = _translator().process(src, asset_uid="rt")
    assert res.image is not None

    out = tmp_path / "out.png"
    assert imwrite_bgr(out, res.image)
    back = imread_bgr(out)
    assert back is not None
    _assert_alpha_intact(back, img)


# ----------------------------------------------------------------------
# 二、早退路径也要保住 alpha
# ----------------------------------------------------------------------

@requires_ocr
def test_alpha_survives_no_text_early_return(tmp_path: Path) -> None:
    """**没有文字**的图会走"原图返回"的早退路径，alpha 同样不能丢。"""
    src = tmp_path / "blank.png"
    img = np.zeros((80, 120, 4), dtype=np.uint8)
    img[:, :, :3] = 40
    img[10:70, 10:110, 3] = 255
    assert imwrite_bgr(src, img)

    res = _translator().process(src, asset_uid="blank")
    assert res.image is not None
    assert res.image.shape[2] == 4, f"早退路径丢了 alpha：{res.image.shape}"
    assert res.image[0, 0, 3] == 0
    assert res.image[40, 60, 3] == 255


# ----------------------------------------------------------------------
# 三、边界：不能凭空造出 / 抹掉通道
# ----------------------------------------------------------------------

@requires_ocr
def test_rgb_image_stays_rgb(tmp_path: Path) -> None:
    """本来没有 alpha 的图不能凭空多出一个通道。

    多出 alpha=全不透明视觉上看不出来，但会改变产物通道数，
    进而在回写时改变文件大小/格式 —— 没必要动就别动。
    """
    src = tmp_path / "rgb.png"
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    assert imwrite_bgr(src, img)

    res = _translator().process(src, asset_uid="rgb")
    assert res.image is not None
    assert res.image.shape[2] == 3, (
        f"原本 3 通道的图变成了 {res.image.shape} —— 凭空加了 alpha"
    )


@requires_ocr
@pytest.mark.parametrize("channels", [3, 4])
def test_channel_count_is_preserved(tmp_path: Path, channels: int) -> None:
    src = tmp_path / f"ch{channels}.png"
    rng = np.random.default_rng(11)
    img = rng.integers(0, 256, size=(64, 96, channels), dtype=np.uint8)
    assert imwrite_bgr(src, img)
    res = _translator().process(src, asset_uid="ch")
    assert res.image is not None
    assert res.image.shape[2] == channels, (
        f"输入 {channels} 通道，输出 {res.image.shape[2]} 通道"
    )


@requires_ocr
def test_grayscale_input_becomes_three_channels(tmp_path: Path) -> None:
    """灰度图会被升成 3 通道（处理链只吃 3/4 通道），这是预期行为。

    记下来是为了防止"通道数守恒"被误解成"灰度也必须输出 1 通道"。
    """
    import cv2

    src = tmp_path / "gray.png"
    gray = np.full((48, 48), 128, dtype=np.uint8)
    assert cv2.imwrite(str(src), gray)
    res = _translator().process(src, asset_uid="gray")
    assert res.image is not None
    assert res.image.shape[2] == 3
