"""验证视觉兜底（低置信度时让 VLM 重读文字，但不改文字框）。

这个机制的风险在于"兜底把好结果搞坏"，所以测试重点不是
"VLM 能不能读字"，而是**边界行为**：

1. 置信度够高时**完全不该调用** VLM（不能白烧 GPU）；
2. 调用时**只改文字、绝不改四边形**（VLM 定位精度比专用 OCR
   差两个数量级，让它决定位置会毁掉排版）；
3. VLM 读不出东西时**保留 OCR 原结果**（兜底失败不能倒退）；
4. VLM 返回的内容与 OCR 相同（仅空白/全半角差异）时**不替换**；
5. VLM 抛异常时整张图仍然处理成功；
6. ``vlm_fallback=False`` 时彻底不调用。

用假 OCR + 假 VLM，不依赖模型和 GPU。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images import service as svc  # noqa: E402
from novaloc.images.io import imwrite_bgr  # noqa: E402
from novaloc.models import ImageTextBlock, TextBlockStyle  # noqa: E402

SB = FIXTURES
checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))


def make_block(source: str, box, conf: float, quad=None) -> ImageTextBlock:
    """用**真实的** ImageTextBlock，不要自己造一个假类。

    假类会和真模型漂移（第一版就因为缺 ``style`` 字段直接崩了），
    而且容易漏掉真模型的必填字段。
    """
    return ImageTextBlock(
        id=f"b{abs(hash(source)) % 10000}",
        box=box,
        source=source,
        confidence=conf,
        quad=quad or [
            (float(box[0]), float(box[1])),
            (float(box[2]), float(box[1])),
            (float(box[2]), float(box[3])),
            (float(box[0]), float(box[3])),
        ],
        style=TextBlockStyle(),
    )


class FakePage:
    def __init__(self, blocks) -> None:
        self.blocks = blocks
        self.error = ""


class FakeOcr:
    def __init__(self, blocks) -> None:
        self._blocks = blocks
        self.calls = 0

    def read(self, path, *, asset_uid=""):
        self.calls += 1
        # 每次返回**新的**块对象，避免测试之间互相污染
        return FakePage([
            make_block(b.source, b.box, b.confidence, [tuple(p) for p in b.quad])
            for b in self._blocks
        ])


class FakeVlm:
    """假视觉模型：按预置脚本回答，并可选择抛异常。"""

    def __init__(self, answers: dict[str, str] | None = None, *, boom: bool = False) -> None:
        self.answers = answers or {}
        self.boom = boom
        self.calls: list[str] = []

    def read_text(self, image, *, hint: str = "") -> str:
        self.calls.append(hint)
        if self.boom:
            raise RuntimeError("模拟 VLM 崩溃")
        # 按裁剪区域的像素特征选答案：这里用行数当"哪一块"的近似
        h = image.shape[0]
        for key, val in self.answers.items():
            if key in hint:
                return val
        del h
        return self.answers.get("default", "")


def make_ctx(*, vlm_fallback=True, threshold=0.6) -> Context:
    cfg = Config()
    cfg.ocr.vlm_fallback = vlm_fallback
    cfg.ocr.vlm_threshold = threshold
    return Context(config=cfg, events=EventBus())


def make_image(path: Path) -> None:
    arr = np.full((80, 240, 3), 255, np.uint8)
    arr[20:40, 20:100] = 0
    arr[50:70, 120:220] = 0
    imwrite_bgr(path, arr)


def translator_with(ctx: Context, blocks, vlm) -> svc.TextureTranslator:
    """``translate_fn`` 收到的是 TranslateItem 列表（不是字符串），
    所以取 ``.unit.source``。这个签名在早期版本里踩过坑：
    把结果当字符串用会抛 AttributeError，而异常被上层吞掉，
    表现为"一张贴图都没汉化"。"""
    t = svc.TextureTranslator(ctx, translate_fn=_fake_translate)
    t._ocr = FakeOcr(blocks)
    t._vlm = vlm
    t._vlm_tried = True
    return t


def _fake_translate(items, target_lang, *a, **k):
    return ["【中】" + it.unit.source for it in items]


def main() -> int:
    img = SB / "_vlmtest" / "ui.png"
    if img.parent.exists():
        shutil.rmtree(img.parent)
    img.parent.mkdir(parents=True)
    make_image(img)

    LOW = make_block("Rest0re", (20, 20, 100, 40), 0.31)
    HIGH = make_block("Options", (120, 50, 220, 70), 0.97)

    # ---------- 1. 高置信度时不该调用 VLM ----------
    print("[1] 置信度足够高时不应调用 VLM（不能白烧 GPU）")
    vlm = FakeVlm({"default": "不该被调用"})
    t = translator_with(make_ctx(), [HIGH], vlm)
    res = t.process(img, asset_uid="ui")
    print(f"    块数={len(res.asset.blocks)} VLM 调用={len(vlm.calls)} 修正={t.vlm_fixes}")
    check("高置信度：VLM 一次都没被调用", len(vlm.calls) == 0, str(len(vlm.calls)))
    check("高置信度：文字保持原样",
          res.asset.blocks[0].source == "Options", res.asset.blocks[0].source)
    check("高置信度：不该有 vlm_reread 标记",
          "vlm_reread" not in res.asset.blocks[0].warnings,
          str(res.asset.blocks[0].warnings))
    check("处理成功", res.error == "", res.error)

    # ---------- 2. 低置信度时应调用，且只改文字不改框 ----------
    print("\n[2] 低置信度时应重读，且**只改文字不改文字框**")
    vlm = FakeVlm({"default": "Restore"})
    t = translator_with(make_ctx(), [LOW, HIGH], vlm)
    orig_quad = list(LOW.quad)
    orig_box = tuple(LOW.box)
    res = t.process(img, asset_uid="ui")
    got = {b.source: b for b in res.asset.blocks}
    print(f"    结果：{[(b.source, round(b.confidence,2), b.warnings) for b in res.asset.blocks]}")
    print(f"    VLM 调用={len(vlm.calls)} 修正={t.vlm_fixes}")
    check("低置信度：调用了 VLM", len(vlm.calls) == 1, str(len(vlm.calls)))
    check("低置信度：文字被修正为 Restore", "Restore" in got, str(list(got)))
    fixed = got.get("Restore")
    check("修正后带 vlm_reread 标记（可追溯）",
          fixed is not None and "vlm_reread" in fixed.warnings,
          str(fixed.warnings if fixed else None))
    # 最关键的一条：框不能被 VLM 改掉
    check("修正后文字框（box）完全没变",
          fixed is not None and tuple(fixed.box) == orig_box,
          f"{fixed.box if fixed else None} vs {orig_box}")
    check("修正后四边形（quad）完全没变",
          fixed is not None and [tuple(p) for p in fixed.quad] == [tuple(p) for p in orig_quad],
          f"{fixed.quad if fixed else None}")
    # 这一条以前断言"置信度被抬到阈值以上"，那是在**保护一个 bug**：
    # 旧代码写 `b.confidence = max(原值, thr)`，把 VLM 的答案（很可能是
    # 垃圾，实测它真回过 `'? ? ? ? ? ? ?'`）凭空提升到"可信"档，
    # 下游质检和审校页就再也看不出这块可疑了。
    # VLM 给不出置信度，所以正确的做法是**保持 OCR 的原值**。
    check("修正后置信度保持 OCR 原值（不凭空抬高）",
          fixed is not None and fixed.confidence == 0.31,
          str(fixed.confidence if fixed else None))
    check("高置信度的块未被牵连",
          got.get("Options") is not None and "vlm_reread" not in got["Options"].warnings,
          str(got.get("Options").warnings if got.get("Options") else None))

    # ---------- 3. VLM 读不出来时保留 OCR 原结果 ----------
    print("\n[3] VLM 读不出东西时必须保留 OCR 原结果（兜底不能倒退）")
    vlm = FakeVlm({"default": ""})
    t = translator_with(make_ctx(), [LOW], vlm)
    res = t.process(img, asset_uid="ui")
    b = res.asset.blocks[0]
    print(f"    文字={b.source!r} 置信度={b.confidence:.2f} 修正={t.vlm_fixes}")
    check("VLM 空结果：保留原文字", b.source == "Rest0re", b.source)
    check("VLM 空结果：置信度不被抬高", b.confidence == 0.31, str(b.confidence))
    check("VLM 空结果：不计入修正数", t.vlm_fixes == 0, str(t.vlm_fixes))
    check("VLM 空结果：不加标记", "vlm_reread" not in b.warnings, str(b.warnings))

    # ---------- 4. 内容相同（仅空白/全半角差异）不替换 ----------
    print("\n[4] 内容与 OCR 实质相同（仅空白/全半角差异）时不该替换")
    vlm = FakeVlm({"default": " Rest0re "})
    t = translator_with(make_ctx(), [LOW], vlm)
    res = t.process(img, asset_uid="ui")
    b = res.asset.blocks[0]
    print(f"    VLM 返回=' Rest0re ' 结果文字={b.source!r} 修正={t.vlm_fixes}")
    check("仅空白差异：不替换", b.source == "Rest0re", b.source)
    check("仅空白差异：不加标记", "vlm_reread" not in b.warnings, str(b.warnings))

    # ---------- 5. VLM 崩溃时整图仍成功 ----------
    print("\n[5] VLM 崩溃时整张图仍须处理成功")
    vlm = FakeVlm(boom=True)
    t = translator_with(make_ctx(), [LOW, HIGH], vlm)
    res = t.process(img, asset_uid="ui")
    print(f"    error={res.error!r} 块数={len(res.asset.blocks)} 修正={t.vlm_fixes}")
    check("VLM 崩溃：处理仍然成功", res.error == "", res.error)
    check("VLM 崩溃：块数量不变", len(res.asset.blocks) == 2, str(len(res.asset.blocks)))
    check("VLM 崩溃：原文保留", all(b.source for b in res.asset.blocks),
          str([b.source for b in res.asset.blocks]))
    check("VLM 崩溃：修正数为 0", t.vlm_fixes == 0, str(t.vlm_fixes))

    # ---------- 6. vlm_fallback=False 时彻底不调用 ----------
    print("\n[6] vlm_fallback=False 时应彻底跳过")
    vlm = FakeVlm({"default": "不该被调用"})
    t = svc.TextureTranslator(make_ctx(vlm_fallback=False), translate_fn=_fake_translate)
    t._ocr = FakeOcr([LOW])
    t._vlm = None
    t._vlm_tried = False
    res = t.process(img, asset_uid="ui")
    b = res.asset.blocks[0]
    print(f"    文字={b.source!r}（VLM 未被使用）")
    check("关闭兜底：文字保持 OCR 结果", b.source == "Rest0re", b.source)
    check("关闭兜底：vlm 属性为 None", t.vlm is None)

    # ---------- 7. 缩放到 0 尺寸的坏框不能崩 ----------
    print("\n[7] 退化文字框（零宽度）不能导致崩溃")
    tiny = make_block("x", (20, 20, 20, 40), 0.2)
    vlm = FakeVlm({"default": "应保留原值"})
    t = translator_with(make_ctx(), [tiny], vlm)
    res = t.process(img, asset_uid="ui")
    print(f"    error={res.error!r}")
    check("退化框：处理不崩", res.error == "", res.error)

    # ---------- 8. 透视校正的几何正确性 ----------
    print("\n[8] 透视校正：斜排文字应被摆正")
    arr = np.full((100, 200, 3), 255, np.uint8)
    quad = [(60, 20), (140, 40), (135, 70), (55, 50)]  # 倾斜的四边形
    crop = svc._crop_quad(arr, quad)
    print(f"    裁剪尺寸={crop.shape if crop is not None else None}（应接近水平矩形）")
    check("透视校正返回图像", crop is not None and crop.size > 0)
    if crop is not None:
        # 顶点顺序必须是"左上打头、顺时针"，否则裁出来是旋转/镜像的。
        # 与项目里 OCR 用的 ocr_ppocrv6.order_quad 对比 —— 两处必须一致，
        # 否则同一块文字在两条路径上会被摆成不同朝向。
        from novaloc.images.ocr_ppocrv6 import order_quad as ref_order

        pts = np.asarray(quad, dtype="float32")
        ordered = svc._order_quad(pts)
        ref = ref_order(quad)
        check("顶点排序：与 OCR 的 order_quad 结果一致",
              [[float(a), float(b)] for a, b in ordered] == ref,
              f"{[[float(a),float(b)] for a,b in ordered]} vs {ref}")
        check("顶点排序：左上打头（x+y 最小）",
              int(np.argmin(ordered.sum(axis=1))) == 0, str(ordered.tolist()))
        check("顶点排序：顺时针（图像坐标约定）",
              _is_clockwise(ordered), str(ordered.tolist()))

    print("\n" + "=" * 78)
    n = 0
    for label, ok, detail in checks:
        n += ok
        print(f"  {'✅' if ok else '❌'} {label}" + (f"   ({detail})" if detail and not ok else ""))
    print(f"\n结论：{n}/{len(checks)} 通过" + ("  ✅" if n == len(checks) else "  ❌"))
    return 0 if n == len(checks) else 1


def _is_clockwise(pts: np.ndarray) -> bool:
    """图像坐标（y 向下）下的有符号面积判据。

    这里用 **y 向上的数学系** shoelace 公式 Σ(x2-x1)(y2+y1)：
    在图像坐标（y 向下）里，图像视觉上的"顺时针"在这个公式下得**负值**。
    （第一版把符号写反了，误判了自己正确的输出。）
    """
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += (x2 - x1) * (y2 + y1)
    return s < 0


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0


pytestmark = [
    # 这一套是真的用 Pillow 把中文画到贴图上（比对"重绘中文"的像素结果），
    # 所以**需要本机有中文字体**。Linux CI runner 没有，于是抛
    # `RuntimeError: 找不到可用的中文字体，拒绝在贴图上绘制中文` ——
    # 这不是被测代码坏了，只是这台机器没装字体。
    pytest.mark.needs_fonts,
]


if __name__ == "__main__":
    raise SystemExit(main())
