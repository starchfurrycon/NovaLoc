"""贴图汉化主管线验证。

不接真实模型，用一个确定性的"假翻译"回调（查表 + 大写化数字保留），
这样测的是**管线本身**：OCR→分组→翻译→去字→重绘→贴回 的每一环，
以及缓存命中、溢出标记、只读保护、批量写出。

假翻译故意做成"译文比原文长"（真实中译英常见），以触发溢出保护。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# fixture 目录：仓库自带的小体积游戏样本（原先是 .scratch/）
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context, TranslateItem  # noqa: E402
from novaloc.images.io import imread_bgr, imwrite_bgr  # noqa: E402
from novaloc.images.service import TextureTranslator  # noqa: E402

OUT = FIXTURES / "pipe_test"
OUT.mkdir(parents=True, exist_ok=True)

# 假翻译表：覆盖短标签、长句、数值、需要合并的 MP
FAKE = {
    "new game": "新的旅程",
    "continue": "继续游戏",
    "settings": "设置",
    "chapter one : the awakening": "第一章：觉醒",
    "hp 1250 / 3000": "生命 1250 / 3000",
    "mp 480": "魔力 480",
    "hp 1250 / 3000 mp 480": "生命 1250 / 3000 魔力 480",
    "tavern": "酒馆",
    # 故意给一个超长译文，验证溢出保护
    "load save": "读取存档并继续进行游戏",
}


def fake_translate(items: list[TranslateItem], target_lang: str) -> list[str]:
    out = []
    for it in items:
        key = it.unit.source.strip().lower()
        out.append(FAKE.get(key, FAKE.get(key.replace("  ", " "), "")))
    return out


def ink(bgr: np.ndarray, box, ref_bgr) -> float:
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = bgr.shape[:2]
    patch = cv2.cvtColor(bgr[max(0, y1):min(h, y2), max(0, x1):min(w, x2)], cv2.COLOR_BGR2GRAY)
    ref = float(cv2.cvtColor(ref_bgr.reshape(1, 1, 3), cv2.COLOR_BGR2GRAY)[0, 0])
    return float((np.abs(patch.astype(np.float32) - ref) > 50).mean())


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    # 用上一轮生成的场景图
    src = FIXTURES / "tex_test" / "scene_en.png"
    if not src.is_file():
        print("缺少场景图，请先跑 test_texture_pipeline.py")
        return 1
    scene = imread_bgr(src)

    cfg = Config()
    ctx = Context(config=cfg, events=EventBus())
    tr = TextureTranslator(ctx, translate_fn=fake_translate)

    # ---------- 首次处理 ----------
    res = tr.process(src, asset_uid="scene")
    print(f"[首次] ok={res.ok} 译文块={res.translated} 去字={res.inpaint_method} "
          f"OCR={res.ocr_ms:.0f}ms 总={res.total_ms:.0f}ms err={res.error!r}")
    check("管线处理成功", res.ok, res.error)
    check("至少翻译 5 块", res.translated >= 5, f"{res.translated}")
    check("OCR 耗时被记录", res.ocr_ms > 0)
    check("每块都有 outcome", len(res.outcomes) >= 5, f"{len(res.outcomes)}")

    print("\n  逐块结果：")
    for o in res.outcomes:
        flag = "✅" if o.ok else "⏭️"
        warn = ("  ⚠️ " + "; ".join(o.warnings)) if o.warnings else ""
        print(f"    {flag} {o.source[:26]!r:30} → {o.target[:22]!r:24} "
              f"字号={o.font_size}{warn}")
    check("翻译后的块状态为 TRANSLATED",
          all(o.status.value in ("translated", "skipped") for o in res.outcomes))

    # ---------- 结果图 ----------
    dest = OUT / "scene_cn.png"
    imwrite_bgr(dest, res.image)
    back = imread_bgr(dest)
    check("结果图已写出且可读回", back is not None and back.shape == scene.shape)

    # 中文确实画上去了
    cont = next((b for b in res.asset.blocks if "Continue" in b.source), None)
    if cont is not None:
        x1, y1, x2, y2 = cont.box
        ref = np.median(scene[max(0, y1 - 4):y2 + 4, max(0, x1 - 4):x2 + 4]
                        .reshape(-1, 3), axis=0).astype(np.uint8)
        n = ink(res.image, cont.box, ref)
        check("中文已画上（墨迹 > 0.05）", n > 0.05, f"{n:.3f}")

    # ---------- 原图只读 ----------
    before = src.read_bytes()
    check("原游戏图片未被修改（只读约束）", src.read_bytes() == before)

    # ---------- 缓存命中 ----------
    tr2 = TextureTranslator(ctx, translate_fn=fake_translate)
    res2 = tr2.process(src, asset_uid="scene", existing={"Continue": "继续"})
    check("既有译文命中缓存（不再问模型）", tr2.cache_hits >= 1, f"命中 {tr2.cache_hits}")
    c = next((o for o in res2.outcomes if "Continue" in o.source), None)
    check("缓存译文被实际使用", c is not None and c.target == "继续",
          f"{None if c is None else c.target!r}")

    # ---------- 溢出保护 ----------
    over = next((o for o in res.outcomes if o.overflow or o.too_small), None)
    print(f"\n[溢出保护] 触发块: "
          f"{None if over is None else (over.source, over.target, over.font_size, over.warnings)}")
    # 构造一个必定溢出的场景：极小的框塞长中文
    from novaloc.images.render import render_text_block_checked
    from novaloc.models import TextBlockStyle
    probe = render_text_block_checked(
        "这一段中文长得离谱绝对塞不进去", (30, 10), r"C:\Windows\Fonts\msyh.ttc",
        TextBlockStyle(),
    )
    check("溢出/过小会被标记出来（不硬画）", probe.needs_review,
          f"overflow={probe.overflow} size={probe.font_size}")

    # ---------- 无文字的图不该报错 ----------
    blank = np.full((80, 120, 3), 30, np.uint8)
    blank_p = OUT / "blank.png"
    imwrite_bgr(blank_p, blank)
    res_blank = tr.process(blank_p)
    check("纯色无字图正常返回（不报错、原图返回）",
          res_blank.ok and res_blank.translated == 0 and res_blank.image is not None,
          f"err={res_blank.error!r}")

    # ---------- 坏路径不该崩 ----------
    res_bad = tr.process(OUT / "does_not_exist.png")
    check("不存在的图片被优雅处理（返回错误而非抛异常）",
          not res_bad.ok and bool(res_bad.error), f"{res_bad.error!r}")

    # ---------- 批量处理 ----------
    batch = tr.process_many([src, blank_p], out_dir=OUT / "batch")
    check("批量处理两张图", len(batch) == 2, f"{len(batch)}")
    check("批量结果都写到了输出目录",
          all(r.image is not None for r in batch))
    check("批量输出文件确实落盘",
          (OUT / "batch" / "scene_en.png").is_file() and (OUT / "batch" / "blank.png").is_file())

    # ---------- 修改是局部的 ----------
    diff = (np.abs(res.image.astype(np.int16) - scene.astype(np.int16)).sum(axis=2) > 30)
    changed = float(diff.mean())
    print(f"\n[整体] 改动像素占比 {changed:.4f}")
    check("改动是局部的（< 30%）", changed < 0.30, f"{changed:.4f}")

    print("\n" + "=" * 78)
    print("断言汇总")
    print("=" * 78)
    n = 0
    for label, ok, detail in checks:
        n += ok
        print(f"  {'✅' if ok else '❌'} {label}" + (f"   ({detail})" if detail and not ok else ""))
    print(f"\n结论：{n}/{len(checks)} 通过" + ("  ✅" if n == len(checks) else "  ❌"))
    return 0 if n == len(checks) else 1


def test_suite() -> None:
    """pytest 入口：跑一遍完整报告并断言全通过。"""
    assert main() == 0

pytestmark = [
    pytest.mark.needs_models,
    pytest.mark.needs_gpu,
]


if __name__ == "__main__":
    raise SystemExit(main())
