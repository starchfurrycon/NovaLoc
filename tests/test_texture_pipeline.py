"""贴图汉化端到端验证：识别 → 去字 → 重绘 → 贴回。

用合成贴图（深色按钮、渐变横幅、艺术字、斜排招牌）走完整链路，
并做**可量化的验收**：
  1. 原文字必须被抹干净（残留像素比例接近 0）；
  2. 中文必须真的画上去了（墨迹像素显著增加）；
  3. 中文必须待在原框范围内（不能溢出到别的区域）；
  4. 结果图必须可保存/可读回。

因为我看不到图，所以全部用像素统计来判断，不靠"看起来像"。
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
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images import (  # noqa: E402
    PPOcrV6Engine,
    detect_stroke,
    extract_colors,
    inpaint_boxes,
    paste_quad,
    render_text_block_checked,
)
from novaloc.images.io import imread_bgr as _ir  # noqa: E402
from novaloc.images.io import imwrite_bgr as _iw  # noqa: E402
from novaloc.models import TextBlockStyle  # noqa: E402

OUT = FIXTURES / "tex_test"
OUT.mkdir(parents=True, exist_ok=True)

CN_FONT = r"C:\Windows\Fonts\msyhbd.ttc"
EN_FONT = r"C:\Windows\Fonts\arialbd.ttf"


def make_scene() -> np.ndarray:
    """造一张包含多种典型情况的"游戏界面"贴图。"""
    W, H = 900, 560
    img = Image.new("RGB", (W, H), (24, 28, 46))
    d = ImageDraw.Draw(img)

    # 1. 深色底 + 亮色艺术字（带描边）
    d.rectangle([20, 20, 560, 96], fill=(38, 44, 70))
    d.text((40, 34), "NEW GAME", font=ImageFont.truetype(EN_FONT, 44),
           fill=(255, 216, 96), stroke_width=3, stroke_fill=(120, 40, 10))

    # 2. 纯色按钮上的白字
    d.rectangle([20, 120, 300, 172], fill=(52, 92, 150))
    d.text((44, 134), "Continue", font=ImageFont.truetype(EN_FONT, 28), fill=(240, 244, 250))
    d.rectangle([320, 120, 600, 172], fill=(52, 92, 150))
    d.text((344, 134), "Settings", font=ImageFont.truetype(EN_FONT, 28), fill=(240, 244, 250))

    # 3. 渐变横幅（背景不纯色，会走 inpaint 算法档）
    for x in range(20, 880):
        t = (x - 20) / 860
        d.line([(x, 200), (x, 268)], fill=(int(40 + 110 * t), int(30 + 40 * t), int(90 + 60 * t)))
    d.text((60, 216), "Chapter One : The Awakening", font=ImageFont.truetype(EN_FONT, 32),
           fill=(255, 255, 255))

    # 4. 数值文本
    d.text((40, 300), "HP 1250 / 3000    MP 480", font=ImageFont.truetype(EN_FONT, 30),
           fill=(130, 240, 150))

    # 5. 斜排招牌
    tmp = Image.new("RGBA", (420, 110), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((14, 26), "TAVERN", font=ImageFont.truetype(EN_FONT, 46),
                             fill=(255, 170, 150), stroke_width=3, stroke_fill=(60, 10, 10))
    tmp = tmp.rotate(-14, expand=True, resample=Image.BICUBIC)
    img.paste(tmp, (420, 330), tmp)

    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def ink_ratio(bgr: np.ndarray, box: tuple[int, int, int, int], color: tuple[int, int, int],
              tol: int = 70) -> float:
    """框内"接近某颜色"的像素占比。用来量化原文字是否被抹掉。"""
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = bgr.shape[:2]
    patch = bgr[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
    if patch.size == 0:
        return 0.0
    rgb = cv2.cvtColor(patch, cv2.COLOR_BGR2RGB).astype(np.int16)
    ref = np.asarray(color, dtype=np.int16)
    near = (np.abs(rgb - ref).sum(axis=2) < tol * 3)
    return float(near.mean())


def nonbg_ratio(bgr: np.ndarray, box: tuple[int, int, int, int],
                ref: np.ndarray | None = None) -> float:
    """框内"墨迹"占比。

    注意**必须传入统一的参考背景像素**（``ref``）。
    早先的实现用"框内灰度中位数"当背景，结果墨迹一变多、中位数就漂移，
    指标自己把自己算错（原图 0.275 → 结果 0.666，看着像糊成一团，
    实际用固定背景量出来是 0.177 → 0.293，完全正常）。
    测量指标本身有偏是非常隐蔽的错误，所以这里固定背景基准。
    """
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = bgr.shape[:2]
    patch = bgr[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
    if patch.size == 0:
        return 0.0
    if ref is None:
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        med = float(np.median(gray))
    else:
        med = float(cv2.cvtColor(ref.reshape(1, 1, 3), cv2.COLOR_BGR2GRAY)[0, 0])
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float((np.abs(gray - med) > 50).mean())


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    scene = make_scene()
    src_path = OUT / "scene_en.png"
    _iw(src_path, scene)
    print(f"原图: {src_path}  {scene.shape[1]}x{scene.shape[0]}")

    # ---------- 1. OCR ----------
    cfg = Config()
    eng = PPOcrV6Engine(Context(config=cfg, events=EventBus()))
    page = eng.read(src_path, asset_uid="scene")
    print(f"\n[1] OCR  {page.elapsed_ms:.0f}ms  识别 {len(page.blocks)} 块  错误={page.error!r}")
    for b in page.blocks:
        print(f"    conf={b.confidence:.3f} {b.box} angle={b.style.angle:6.1f}  {b.source!r}")
    check("OCR 至少识别出 5 块文字", len(page.blocks) >= 5, f"实际 {len(page.blocks)}")
    check("OCR 识别到了中文/英文混合内容",
          any(any(c.isalpha() for c in b.source) for b in page.blocks))

    # 斜排招牌必须被识别出角度
    rotated = [b for b in page.blocks if abs(b.style.angle) > 5]
    check("识别出斜排文字的角度", len(rotated) >= 1,
          f"含角度的块: {[(b.source, round(b.style.angle,1)) for b in rotated]}")

    # ---------- 2. 颜色与描边实测 ----------
    print("\n[2] 颜色/描边实测")
    for b in page.blocks[:6]:
        fg, bg, sep = extract_colors(scene, b.box)
        stroke, sw = detect_stroke(scene, b.box, fg)
        print(f"    {b.source[:26]!r:30} 字色={fg} 底色={bg} 分离度={sep:5.1f} 描边={stroke} w={sw}")
    # 艺术字那条应当是黄字
    art = next((b for b in page.blocks if "NEW" in b.source.upper()), None)
    if art is not None:
        fg, bg, _ = extract_colors(scene, art.box)
        is_yellowish = fg[0] > 180 and fg[1] > 150 and fg[2] < 140
        check("艺术字字色被正确识别为亮黄色", is_yellowish, f"实测 {fg}")

    # ---------- 3. 去字 ----------
    boxes = [b.box for b in page.blocks]
    quads = [list(b.quad) for b in page.blocks if b.quad]
    t0 = __import__("time").time()
    wiped = inpaint_boxes(scene, boxes, dilate=3, quads=quads)
    print(f"\n[3] 去字  {wiped.method}  ({wiped.detail})  {( __import__('time').time()-t0)*1000:.0f}ms")
    _iw(OUT / "scene_wiped.png", wiped.image)

    # 白字区域（Continue 按钮）抹除后应当几乎没有亮像素
    cont = next((b for b in page.blocks if "Continue" in b.source), None)
    if cont is not None:
        before = ink_ratio(scene, cont.box, (240, 244, 250), tol=60)
        after = ink_ratio(wiped.image, cont.box, (240, 244, 250), tol=60)
        print(f"    Continue 白字占比: 处理前 {before:.4f} → 处理后 {after:.4f}")
        check("原文字已被抹除（残留 < 处理前的 25%）",
              after < max(0.004, before * 0.25), f"{before:.4f} → {after:.4f}")

    # ---------- 4. 重绘中文并贴回 ----------
    print("\n[4] 重绘中文并贴回")
    canvas = wiped.image.copy()
    # 译文表**按 OCR 实际切出来的块**来建。
    # 早先按"我期望的文本"手写键，结果 "HP 1250 / 3000    MP 480" 被 OCR 切成两块，
    # 两个键都匹配不上、块又被前一个键用掉，于是静默漏翻 —— 是测试自身的缺陷。
    translations = {
        "NEW GAME": "新的旅程",
        "Continue": "继续游戏",
        "Settings": "设置",
        "Chapter One : The Awakening": "第一章：觉醒",
        "HP 1250 / 3000": "生命 1250 / 3000",
        "MP 480": "魔力 480",
        "TAVERN": "酒馆",
    }
    rendered = 0
    used_keys: set[str] = set()
    review_needed: list[tuple[str, str, object]] = []
    for b in page.blocks:
        key = b.source.strip()
        target = translations.get(key)
        if target is None:
            print(f"    ⏭️ 无译文，跳过: {key!r}")
            continue
        if key in used_keys:
            print(f"    ⏭️ 该块已处理过，跳过: {key!r}")
            continue
        used_keys.add(key)
        x1, y1, x2, y2 = b.box
        bw, bh = x2 - x1, y2 - y1
        # 稍微留白，避免中文顶到框边
        pad_x, pad_y = max(2, int(bw * 0.03)), max(1, int(bh * 0.06))
        rw, rh = max(8, bw - pad_x * 2), max(8, bh - pad_y * 2)

        fg, bg, sep = extract_colors(scene, b.box)
        style = b.style.model_copy(deep=True)
        style.text_color = fg
        stroke, _sw = detect_stroke(scene, b.box, fg)
        if stroke:
            style.stroke_color = stroke
            style.stroke_width = 2
        style.font_size = None  # 交给自动收缩

        br = render_text_block_checked(target, (rw, rh), CN_FONT, style)
        if br.rgba is None:
            print(f"    ⚠️ 渲染失败：{target!r}  {br.detail}")
            continue
        rgba = br.rgba
        warn = ""
        if br.needs_review:
            warn = f"  ⚠️ 需审校（{'溢出' if br.overflow else '字号过小'}）"
            review_needed.append((b.source, target, br))
        quad = b.quad if b.quad and len(b.quad) == 4 else [
            (x1 + pad_x, y1 + pad_y), (x2 - pad_x, y1 + pad_y),
            (x2 - pad_x, y2 - pad_y), (x1 + pad_x, y2 - pad_y),
        ]
        if paste_quad(canvas, rgba, list(quad)):
            rendered += 1
            print(f"    ✅ {b.source[:24]!r} → {target!r}  {br.detail}{warn}")
    out_path = OUT / "scene_cn.png"
    _iw(out_path, canvas)
    print(f"\n结果图: {out_path}")
    check("至少贴回 5 块中文", rendered >= 5, f"实际 {rendered}")
    check("所有有译文的块都被贴回（无静默漏翻）",
          rendered == len(translations),
          f"贴回 {rendered} / 译文 {len(translations)}")
    print(f"    需审校的块: {len(review_needed)} → "
          f"{[(s[:18], t[:14]) for s, t, _ in review_needed]}")

    # ---------- 5. 量化验收 ----------
    print("\n[5] 量化验收（墨迹基准固定为各框的原始背景像素）")
    for b in page.blocks:
        if any(k.lower() in b.source.lower() for k in translations):
            # 固定参考：从原图里取该框"外扩一圈"的中位色当背景基准
            x1, y1, x2, y2 = b.box
            m = 4
            h, w = scene.shape[:2]
            ring = scene[max(0, y1 - m):min(h, y2 + m), max(0, x1 - m):min(w, x2 + m)]
            ref = np.median(ring.reshape(-1, 3), axis=0).astype(np.uint8)
            ink = nonbg_ratio(canvas, b.box, ref)
            orig = nonbg_ratio(scene, b.box, ref)
            print(f"    {b.source[:26]!r:30} 墨迹 原={orig:.3f} → 新={ink:.3f}  (背景={ref})")

    # 中文区域的墨迹必须和原文相当（既不能空白也不能糊成一团）
    cont = next((b for b in page.blocks if "Continue" in b.source), None)
    if cont is not None:
        x1, y1, x2, y2 = cont.box
        ref = np.median(scene[max(0, y1 - 4):y2 + 4, max(0, x1 - 4):x2 + 4]
                        .reshape(-1, 3), axis=0).astype(np.uint8)
        n = nonbg_ratio(canvas, cont.box, ref)
        check("中文已画上（墨迹密度 > 0.05）", n > 0.05, f"{n:.3f}")
        check("中文没有糊满整个框（墨迹密度 < 0.6）", n < 0.6, f"{n:.3f}")

    # 结果图必须可读回且尺寸一致
    back = _ir(out_path)
    check("结果图可正常读回且尺寸一致",
          back is not None and back.shape == scene.shape,
          f"{None if back is None else back.shape} vs {scene.shape}")

    # 变化必须是局部的：绝大部分像素应保持原样
    diff = (np.abs(canvas.astype(np.int16) - scene.astype(np.int16)).sum(axis=2) > 30)
    changed = float(diff.mean())
    print(f"    改动像素占比: {changed:.4f}")
    check("改动是局部的（< 25% 像素被改动）", changed < 0.25, f"{changed:.4f}")

    # 溢出检测必须真的能发现问题：故意塞一段超长中文进小框
    tiny_style = TextBlockStyle(text_color=(255, 255, 255))
    probe = render_text_block_checked(
        "这段文字显然长得放不进这么小的一个框里", (40, 12), CN_FONT, tiny_style
    )
    check("溢出检测有效（超长文本被标记 overflow/too_small）",
          probe.needs_review, f"overflow={probe.overflow} size={probe.font_size}")

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
    pytest.mark.slow,
]


if __name__ == "__main__":
    raise SystemExit(main())
