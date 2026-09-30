"""重叠文字块的整条链路：**重读 + 整体重绘**，产物不得出现"中文+残留外文"。

这是 `test_textgroup_overlap.py` 的端到端对应物：那边只验证几何与分配，
这里验证"真的画到图上之后，回读产物是干净的"。

## 数据来源（重要，别当成凭空构造的）

本文件注入的两块坐标是**在一张真实游戏贴图上实测到的**，不是编的：

    img/system/Window.png （某 RPG Maker MV 项目的窗口皮肤）
    PP-OCRv6 medium + DirectML，对同一张图的同一区域

    'NEW'     box=(13, 29, 123, 76)   conf=0.99998
    'V GAME'  box=(99, 27, 253, 78)   conf=0.95670
                     ↑ 99 < 123 → 横向重叠 24 px

OCR 会把 `NEW GAME` 里的 `W` 读成 `V`，于是切出这两块。
重叠 24 px / 较小块宽 110 px = 22% > `OVERLAP_FORCES_MERGE`(15%)。

**注意**：这个切法在合成图上复现不出来。我试过 Arial Bold 24/32/40/48
在浅色与深色底上各 5 次，全部稳定读成单块 `'NEW GAME'`
（见 `.scratch/_find_overlap_params.py`）。触发条件与那张贴图的具体
像素（描边、渐变背景、JPEG 似的噪声）有关，所以这里**固定注入实测坐标**，
而不是赌"某组参数能复现" —— 赌不中的测试等于没写。

## 为什么必须用假 OCR

真实 OCR 在这里是不确定的（同一张图有时 1 块、有时 2 块），
拿它当断言就是**随机测试**：有时通过有时不通过。所以：

* 第一次 `read`（整图）返回受控的**重叠两块**；
* 第二次 `read`（重叠组重读的裁剪区）返回正确的整串；
* 顺带断言"确实发生了第 2 次调用" —— 否则修法可能悄悄退化成
  "用拼接文本翻译"，而那正是幻影字符 `'NEW V GAME'` 的来源。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.io import imwrite_bgr  # noqa: E402
from novaloc.images.ocr_ppocrv6 import OcrPage, as_rgb_array  # noqa: E402
from novaloc.images.render import render_text_block_checked  # noqa: E402
from novaloc.images.service import (  # noqa: E402
    PAD_X_RATIO,
    PAD_Y_RATIO,
    TextureTranslator,
)
from novaloc.models import (  # noqa: E402
    EntryStatus,
    ImageTextBlock,
    TextBlockStyle,
    TranslationEntry,
)

OUT = Path(__file__).resolve().parent / "fixtures" / "overlap_test"

#: 实测坐标（见模块文档），原样固定在这里
MEASURED_BLOCKS = [
    ("p#0", (13, 29, 123, 76), "NEW", 0.99998),
    ("p#1", (99, 27, 253, 78), "V GAME", 0.95670),
]


def blk(uid: str, box: tuple[int, int, int, int], text: str, conf: float) -> ImageTextBlock:
    x1, y1, x2, y2 = box
    return ImageTextBlock(
        id=uid,
        box=list(box),
        quad=[(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
        source=text,
        confidence=conf,
        style=TextBlockStyle(),
    )


class FakeOcr:
    """受控 OCR：**第一次**返回切碎的重叠块，之后返回整串。

    只实现 `read`（`TextureTranslator` 只用这一个方法）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []  # 每次收到的图/裁剪尺寸

    def read(self, image, *, min_score=None, max_side=None, asset_uid=""):  # noqa: ANN001, ANN201
        # 与真实引擎一致：第一次收到的是**路径**，重读时收到的是裁剪后的数组
        arr, w, h = as_rgb_array(image)
        assert arr is not None, f"FakeOcr 无法读取 {image}"
        self.calls.append((w, h))
        first = len(self.calls) == 1
        blocks = (
            [blk(*spec) for spec in MEASURED_BLOCKS]
            if first
            else _whole_string(arr)
        )
        return OcrPage(blocks=blocks, width=w, height=h)


def _whole_string(arr: np.ndarray) -> list[ImageTextBlock]:
    """重读整组包围盒时应得到的**正确**文字（'NEW GAME' 而不是 'NEW V GAME'）。"""
    return [blk("r#0", (2, 2, arr.shape[1] - 2, arr.shape[0] - 2), "NEW GAME", 0.9999)]


class NonOverlappingOcr(FakeOcr):
    """两块相距很远、不重叠 —— 用来确认正常路径**不会**多花一次 OCR。"""

    def read(self, image, *, min_score=None, max_side=None, asset_uid=""):  # noqa: ANN001, ANN201
        arr, w, h = as_rgb_array(image)
        assert arr is not None
        self.calls.append((w, h))
        return OcrPage(
            blocks=[
                blk("q#0", (10, 30, 100, 70), "HELLO THERE", 0.99),
                blk("q#1", (200, 30, 300, 70), "WORLD AGAIN", 0.99),
            ],
            width=w,
            height=h,
        )


def make_translator() -> tuple[TextureTranslator, FakeOcr]:
    """构造服务：假翻译回调（'NEW GAME' → '全新游戏'）+ 受控 OCR。"""
    cfg = Config()
    ctx = Context(config=cfg, events=EventBus())

    def translate_fn(items, target_lang):  # noqa: ANN001, ANN202
        return [
            TranslationEntry(
                uid=it.unit.uid,
                source=it.unit.source,
                target=("全新游戏" if it.unit.source.strip().upper() == "NEW GAME"
                        else it.unit.source),
            )
            for it in items
        ]

    tr = TextureTranslator(ctx, translate_fn=translate_fn)
    fake = FakeOcr()
    tr._ocr = fake  # noqa: SLF001  # 绕开惰性加载，注入受控 OCR
    # 关掉视觉兜底：本测试验证的是重绘几何与重叠重读
    tr._vlm_rescue = lambda image, blocks: blocks  # noqa: SLF001
    return tr, fake


def make_image(path: Path) -> None:
    """一张带文字的图。文字内容不重要（OCR 是受控的），
    但有笔画才能验证"原文字被擦掉了"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((120, 400, 3), 30, dtype=np.uint8)
    arr[27:78, 12:253] = 200  # 原文字所在的亮块
    imwrite_bgr(path, arr)


def test_overlapping_blocks_are_reread_and_drawn_as_one_unit(tmp_path: Path) -> None:
    """核心回归：重叠块 → 重读 + 整组重绘 → 产物只有中文。"""
    img = tmp_path / "Window.png"
    make_image(img)
    tr, fake = make_translator()

    res = tr.process(img, asset_uid="probe")
    assert res.ok, res.error
    assert res.image is not None

    # ① 必须发生重读（第二次 OCR 调用）
    assert len(fake.calls) == 2, (
        f"应恰好调用 OCR 两次（整图 + 重叠组重读），实际 {len(fake.calls)} 次：{fake.calls}"
    )
    # ② 重读的是**裁剪区**，不是整图（否则等于没重读）
    assert fake.calls[1][0] < fake.calls[0][0], (
        f"第二次调用应是裁剪区，实际尺寸 {fake.calls[1]} vs 整图 {fake.calls[0]}"
    )

    # ③ 译文落在首块，其余块为空且标为 SKIPPED —— 不留原始外文
    by_id = {o.block_id: o for o in res.outcomes}
    assert by_id["p#0"].target == "全新游戏"
    assert by_id["p#0"].status is EntryStatus.TRANSLATED
    assert by_id["p#1"].target == ""
    assert by_id["p#1"].status is EntryStatus.SKIPPED

    # ④ 每块的 outcome.box 仍是**自己的**框（审校页要在原图上标位置）
    assert by_id["p#0"].box == (13, 29, 123, 76)
    assert by_id["p#1"].box == (99, 27, 253, 78)


def test_output_has_no_leftover_latin_from_the_overlapped_region(tmp_path: Path) -> None:
    """产物上不能留下原始外文 —— 这正是修复前的症状（`新V GAME`）。

    ## 怎么在**不看图**的前提下判断"残留"

    用"墨迹可解释度"：把译文用**产品同一个渲染器、同一套内边距**渲染出来，
    取其 alpha 掩码对齐到原文字的包围盒，再看产物里的亮像素有多少能落在
    这个掩码内。

    * 只剩译文 → 几乎全部落在掩码内；
    * 译文上还压着原始外文 → 多出来的笔画落在掩码**之外**。

    实测标定（`.scratch/_calib_leftover.py`，同一组注入块）：

        修复后        extra = 0.031
        修复前(模拟)  extra = 0.710     ← 20 倍差距

    所以阈值取 0.15：修复后 0.03 稳稳通过，修复前 0.71 稳稳失败。
    **这个数字是量出来的，不是拍的** —— 第一版我写的是"亮像素占比 < 5%"，
    结果正确产物也过不了（译文本身的笔画就占 11%），那种阈值等于没测。

    另外这里还用**另一个方法**独立验证：把"合并前的拼接文本"
    （即幻影字符 `'NEW V GAME'`）渲染出来对比。若产物的墨迹和"拼接文本"
    的墨迹高度吻合，说明画的还是那个错误文本 —— 与掩码指标互为交叉验证。
    """
    img = tmp_path / "Window.png"
    make_image(img)

    tr, _fake = make_translator()
    res = tr.process(img, asset_uid="probe")
    assert res.image is not None

    group_box = (13, 27, 253, 78)
    text, extra = _explain_ink(tr, res.image, group_box, "全新游戏")

    assert extra < 0.15, (
        f"原文字区域有 {extra:.1%} 的墨迹无法由译文 {text!r} 解释 —— "
        f"说明原始外文没被完全覆盖（标定值：修复后 3.1%，修复前 71%）"
    )
    assert extra > -0.01, "explain 返回值不该为负"

    # 交叉验证：产物不能更像"幻影拼接文本"而不是真正的译文
    _t2, extra_phantom = _explain_ink(tr, res.image, group_box, "NEW V GAME")
    assert extra < extra_phantom, (
        f"产物墨迹与幻影文本 'NEW V GAME' 的吻合度（extra={extra_phantom:.3f}）"
        f"不低于与真译文 '全新游戏' 的吻合度（extra={extra:.3f}）—— "
        f"说明画上去的还是拼接出来的错误文本"
    )


def _explain_ink(
    tr: TextureTranslator,
    out_bgr: np.ndarray,
    box: tuple[int, int, int, int],
    text: str,
) -> tuple[str, float]:
    """产物中"无法由 ``text`` 的笔画解释"的亮像素占比。

    返回 ``(text, extra)``；``extra`` 越低说明产物越像只画了 ``text``。
    """
    x1, y1, x2, y2 = box
    lit = out_bgr[y1:y2, x1:x2].astype(np.int16).max(axis=2) >= 180
    total = int(lit.sum())

    # 复刻产品内边距（`_draw_block` 的算法），否则掩码对不齐
    bw, bh = x2 - x1, y2 - y1
    pad_x = max(2, int(bw * PAD_X_RATIO))
    pad_y = max(1, int(bh * PAD_Y_RATIO))
    rw, rh = max(8, bw - pad_x * 2), max(8, bh - pad_y * 2)
    br = render_text_block_checked(
        text, (rw, rh), tr._pick_font(),  # noqa: SLF001
        TextBlockStyle(text_color=(255, 255, 255)),
    )
    if br.rgba is None or total == 0:
        return text, 1.0

    rgba = br.rgba
    h, w = rgba.shape[:2]
    ox = x1 + pad_x + max(0, (rw - w) // 2)
    oy = y1 + pad_y + max(0, (rh - h) // 2)

    mask = np.zeros_like(lit)
    ys, xs = max(0, oy - y1), max(0, ox - x1)
    ye = min(mask.shape[0], oy - y1 + h)
    xe = min(mask.shape[1], ox - x1 + w)
    if ye > ys and xe > xs:
        mask[ys:ye, xs:xe] = rgba[ys - (oy - y1):ye - (oy - y1),
                                 xs - (ox - x1):xe - (ox - x1), 3] > 40

    inside = int((lit & mask).sum())
    return text, 1.0 - inside / total


def test_non_overlapping_blocks_do_not_trigger_reread(tmp_path: Path) -> None:
    """不重叠时**不该**多花一次 OCR —— 这个修复不能拖慢正常路径。"""
    img = tmp_path / "Window.png"
    make_image(img)
    cfg = Config()
    ctx = Context(config=cfg, events=EventBus())
    tr = TextureTranslator(ctx, translate_fn=lambda items, lang: [
        TranslationEntry(uid=it.unit.uid, source=it.unit.source, target=it.unit.source)
        for it in items
    ])
    # 两个**不重叠**的块（间距 100 px，远大于字高）
    long_fake = NonOverlappingOcr()
    tr._ocr = long_fake  # noqa: SLF001
    tr._vlm_rescue = lambda image, blocks: blocks  # noqa: SLF001

    res = tr.process(img, asset_uid="probe")
    assert res.ok
    assert len(long_fake.calls) == 1, (
        f"不重叠时不该重读，实际调用 OCR {len(long_fake.calls)} 次"
    )
    # 两块各自拿到译文（没有被并成一组）
    assert [o.target for o in res.outcomes] == ["HELLO THERE", "WORLD AGAIN"]


def test_reread_is_bounded_to_the_group_box(tmp_path: Path) -> None:
    """重读只裁剪整组包围盒，不是整张图 —— 否则等于白读一遍。"""
    img = tmp_path / "Window.png"
    make_image(img)
    tr, fake = make_translator()
    res = tr.process(img, asset_uid="probe")
    assert res.ok
    assert len(fake.calls) == 2
    full_w, full_h = fake.calls[0]
    crop_w, crop_h = fake.calls[1]
    # 整组包围盒 = (13, 27, 253, 78)，加 8% 边距
    assert crop_w < full_w and crop_h < full_h
    assert crop_w <= 253 - 13 + 20, f"裁剪宽度 {crop_w} 明显超出组框"
    assert crop_h <= 78 - 27 + 20, f"裁剪高度 {crop_h} 明显超出组框"


pytestmark = [pytest.mark.needs_fonts]
