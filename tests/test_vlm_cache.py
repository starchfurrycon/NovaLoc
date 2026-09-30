"""视觉兜底的裁剪图缓存：必须真的减少 `/api/chat` 调用次数。

背景（ROADMAP #9）：VLM 兜底逐块串行、无缓存。一张图里低置信度块多时
会连发多次请求；同一文字在别的图上重复出现还要重问。

重复文字在游戏贴图里极其常见 —— ``OK`` / ``Cancel`` / ``Yes`` / ``No``
这类按钮在一张图里可能出现多次，不同图的 UI 更是反复用同一套图形。

缓存键刻意用**裁剪像素的 sha1**，而不是文字：

* 这里的问题是"我们**还不知道**文字是什么"（知道就不用问 VLM 了），
  所以不能用文字做键；
* 而"像素完全相同 ⇒ 文字相同"是充分条件，且可精确判定。

本测试用假 VLM 计数，验证：

1. 相同的裁剪只问一次；
2. 不同的裁剪各问一次（不能过度缓存把不同文字当成同一个）；
3. 空结果也被缓存（读不出来时重问一次通常还是读不出来）；
4. 缓存命中不改变最终结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.models import ImageTextBlock, TextBlockStyle  # noqa: E402

pytest.importorskip("cv2", reason="需要 OpenCV")


class FakeVLM:
    """按裁剪内容返回固定文字的假 VLM，并记录被问了几次。

    ``calls`` 是**次数**（整数），``seen`` 才保存每次的裁剪字节。
    之前只留了字节列表，测试里写 ``vlm.calls == 1`` 就变成拿列表比整数，
    断言信息也变成一大坨像素，难读到看不出问题。
    """

    def __init__(self, answers: dict[bytes, str] | None = None, default: str = "") -> None:
        self.answers = answers or {}
        self.default = default
        self.calls = 0
        self.seen: list[bytes] = []

    def read_text(self, crop, hint: str = "") -> str:  # noqa: ARG002
        raw = memoryview(np.ascontiguousarray(crop).tobytes()).tobytes()
        self.calls += 1
        self.seen.append(raw)
        return self.answers.get(raw, self.default)


def _service() -> object:
    from novaloc.images.service import TextureTranslator

    return TextureTranslator(Context(config=Config(), events=EventBus()))


def _block(quad, source: str = "", conf: float = 0.1) -> ImageTextBlock:
    xs = [pt[0] for pt in quad]
    ys = [pt[1] for pt in quad]
    box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
    return ImageTextBlock(
        id=f"b{box[0]}_{box[1]}",
        box=box,
        quad=[(float(x), float(y)) for x, y in quad],
        source=source,
        confidence=conf,
        style=TextBlockStyle(),
    )


def _quad(x: int, y: int, w: int = 40, h: int = 16) -> list[list[int]]:
    """轴对齐四边形的四个顶点（左上 → 右上 → 右下 → 左下）。"""
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def test_identical_crops_asked_once() -> None:
    """同一张图里三处像素完全相同的裁剪，VLM 只应被问一次。"""
    svc = _service()
    vlm = FakeVLM(default="OK")
    svc._vlm = vlm  # 直接注入，跳过真实 Ollama
    svc._vlm_tried = True

    # 三处相同内容：同一张纯色小图放在三个位置，裁出来像素一致
    img = np.zeros((64, 200, 3), dtype=np.uint8)
    img[10:26, 10:50] = 200
    img[10:26, 80:120] = 200
    img[10:26, 150:190] = 200

    quads = [_quad(10, 10), _quad(80, 10), _quad(150, 10)]
    for q in quads:
        svc._vlm_reconsider(img, _block(q))

    assert vlm.calls == 1, f"应只问一次，实际 {vlm.calls} 次"
    assert svc.vlm_reads == 1
    assert svc.vlm_cache_hits == 2, f"应命中 2 次，实际 {svc.vlm_cache_hits}"


def test_different_crops_each_asked() -> None:
    """像素不同的裁剪必须分别去问 —— 缓存不能把不同文字混成一个。"""
    svc = _service()
    vlm = FakeVLM(default="X")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.zeros((64, 200, 3), dtype=np.uint8)
    img[10:26, 10:50] = 200
    img[10:26, 80:120] = 100  # 亮度不同 → 像素不同 → 不同缓存键

    svc._vlm_reconsider(img, _block(_quad(10, 10)))
    svc._vlm_reconsider(img, _block(_quad(80, 10)))

    assert vlm.calls == 2, f"应各问一次，实际 {vlm.calls} 次"
    assert svc.vlm_cache_hits == 0


def test_empty_result_is_cached() -> None:
    """VLM 读不出东西时（空串）也要缓存，避免反复重问同样的失败。"""
    svc = _service()
    vlm = FakeVLM(default="")  # 永远返回空
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((32, 32, 3), 128, dtype=np.uint8)
    for _ in range(4):
        out = svc._vlm_reconsider(img, _block(_quad(0, 0, 20, 20)))
        assert out == ""

    assert vlm.calls == 1, f"读不出来也只该问一次，实际 {vlm.calls} 次"
    assert svc.vlm_cache_hits == 3


def test_cache_does_not_change_result() -> None:
    """缓存命中返回的文字必须与首次真实调用一致。"""
    svc = _service()
    vlm = FakeVLM(default="Continue")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((32, 32, 3), 90, dtype=np.uint8)
    b = _block(_quad(0, 0, 20, 20))
    first = svc._vlm_reconsider(img, b)
    second = svc._vlm_reconsider(img, b)
    assert first == second == "Continue"


def test_cache_persists_across_images() -> None:
    """不同图片里出现同一套 UI 图形时，也应复用缓存。

    键存活于 service 实例（不按图片隔离），因为"像素相同 ⇒ 文字相同"
    与图片无关。这是跨图复用的前提。
    """
    svc = _service()
    vlm = FakeVLM(default="Save")
    svc._vlm = vlm
    svc._vlm_tried = True

    patch = np.full((16, 40, 3), 180, dtype=np.uint8)
    img_a = np.zeros((40, 80, 3), dtype=np.uint8)
    img_a[5:21, 5:45] = patch
    img_b = np.zeros((60, 100, 3), dtype=np.uint8)
    img_b[20:36, 30:70] = patch  # 同一块像素，放在不同图的不同位置

    svc._vlm_reconsider(img_a, _block(_quad(5, 5)))
    svc._vlm_reconsider(img_b, _block(_quad(30, 20)))

    assert vlm.calls == 1, f"跨图应复用，实际 {vlm.calls} 次"
    assert svc.vlm_cache_hits == 1


def test_crop_key_is_deterministic_and_content_sensitive() -> None:
    """缓存键本身：同内容同键、异内容异键。"""
    svc = _service()
    a = np.full((16, 16, 3), 10, dtype=np.uint8)
    b = np.full((16, 16, 3), 10, dtype=np.uint8)
    c = np.full((16, 16, 3), 11, dtype=np.uint8)
    ka, kb, kc = svc._vlm_crop_key(a), svc._vlm_crop_key(b), svc._vlm_crop_key(c)
    assert ka and kb and kc
    assert ka == kb
    assert ka != kc


# ----------------------------------------------------------------------
# 兜底整条链路（比上面几个只测缓存更上层）
# ----------------------------------------------------------------------


def _service_with_threshold(threshold: float):
    from novaloc.images.service import TextureTranslator

    cfg = Config()
    cfg.ocr.vlm_threshold = threshold
    cfg.ocr.vlm_fallback = True
    return TextureTranslator(Context(config=cfg, events=EventBus()))


def test_rescue_replaces_text_and_warns() -> None:
    """低置信度块应被 VLM 改写，并留下 ``vlm_reread`` 警告。

    这是"OCR → 判低置信度 → 裁图 → 问 VLM → 改写文字（不动框）"
    的完整路径。之所以要测到这一层：视觉适配器曾经有两个
    AttributeError 让整条路径**从来没通过**，而每个组件的单元测试
    都是绿的 —— 因为没人把两头接起来跑过。
    """
    svc = _service_with_threshold(0.999)
    vlm = FakeVLM(default="CORRECTED")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((40, 120, 3), 255, dtype=np.uint8)
    img[10:26, 20:100] = 0
    block = _block(_quad(20, 10, 80, 16), source="WRONG", conf=0.1)
    out = svc._vlm_rescue(img, [block])

    assert vlm.calls == 1, "兜底没被触发"
    assert out[0].source == "CORRECTED"
    assert "vlm_reread" in out[0].warnings
    # **框必须原样保留**：VLM 的定位精度比专用 OCR 差两个数量级
    assert out[0].quad == block.quad, "兜底改动了文字框 —— 排版会毁"
    assert out[0].box == block.box, "兜底改动了外接框"


def test_rescue_keeps_ocr_result_when_vlm_says_same() -> None:
    """VLM 给出相同文字时不应替换（避免无意义的"已修正"记录）。"""
    svc = _service_with_threshold(0.999)
    vlm = FakeVLM(default="Same")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((40, 120, 3), 255, dtype=np.uint8)
    block = _block(_quad(20, 10, 80, 16), source="Same", conf=0.1)
    svc._vlm_rescue(img, [block])

    assert vlm.calls == 1
    assert svc.vlm_fixes == 0, "文字没变却计入了修正"
    assert "vlm_reread" not in (block.warnings or [])


def test_rescue_keeps_ocr_result_when_vlm_returns_empty() -> None:
    """VLM 读不出来（空串）时必须保留 OCR 原结果。

    这是"兜底把好结果搞坏"的防线：空串绝不能当成"更正"去覆盖。
    实测视觉模型在描述样式时确实会返回空。
    """
    svc = _service_with_threshold(0.999)
    vlm = FakeVLM(default="")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((40, 120, 3), 255, dtype=np.uint8)
    block = _block(_quad(20, 10, 80, 16), source="KEEP ME", conf=0.1)
    svc._vlm_rescue(img, [block])

    assert block.source == "KEEP ME", "空结果覆盖掉了原本正确的 OCR 文字"
    assert svc.vlm_fixes == 0


def test_rescue_skipped_when_confidence_high() -> None:
    """置信度高于阈值时**不应**调用 VLM（省时间，也避免无故改动）。"""
    svc = _service_with_threshold(0.5)
    vlm = FakeVLM(default="X")
    svc._vlm = vlm
    svc._vlm_tried = True

    img = np.full((40, 120, 3), 255, dtype=np.uint8)
    block = _block(_quad(20, 10, 80, 16), source="Good", conf=0.99)
    svc._vlm_rescue(img, [block])

    assert vlm.calls == 0, "高置信度不该触发兜底"
