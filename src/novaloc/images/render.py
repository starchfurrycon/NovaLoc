"""把中文用**真实字体**画回贴图。

这是整个贴图处理里最不能出错的一环。核心原则：

* **绝不用生成式模型画文字。** 生成模型会缺笔画、串字、糊成一片，
  本质上是"口口口"的另一种形态。中文一律用真实字体光栅化。
* **颜色从原图实测**，不猜。用 2-means 把框内像素分成前景/背景两簇，
  前景即原字色。前景背景差异过小时强制翻转，避免"白字画成白底"。
* **字号自动收缩**，保证中文一定塞得进原框 —— 这是防"文字溢出"的关键。
* **贴回用单应变换**（``findHomography`` + ``warpPerspective``），
  所以斜排、透视变形的招牌也能正确贴上，而不只是轴对齐的矩形。

参考 manga-image-translator 的排版经验：
* 描边宽度 ≈ 字号的 7%（漫画取 10%，游戏 UI 取 7% 更自然）；
* 字号下限 ``(高 + 宽) / 200``，防止极窄区域算出 0 号字；
* 横排行距 1.15，竖排行距更松。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..models import TextBlockStyle
from .ocr_ppocrv6 import order_quad

log = logging.getLogger(__name__)

#: 描边宽度占字号的比例
STROKE_RATIO = 0.07

#: 前景/背景颜色差异低于此值时，强制翻转背景以拉开对比
MIN_FG_BG_DIFF = 30.0

#: 横排行距
LINE_SPACING = 1.15

#: 渲染时的超采样倍率。小字号直接画会有锯齿，放大 3 倍再缩回来。
SUPERSAMPLE = 3


@dataclass
class RenderResult:
    image: np.ndarray
    """贴好文字的 BGR 图像。"""

    rendered_boxes: int = 0
    skipped: list[str] = None  # type: ignore[assignment]
    detail: str = ""

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []


# --------------------------------------------------------------------------
# 颜色分析
# --------------------------------------------------------------------------


def extract_colors(
    image_bgr: np.ndarray, box: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int], tuple[int, int, int], float]:
    """用 2-means 把框内像素分成两簇，得到 ``(字色, 底色, 分离度)``。

    返回的分离度是两簇中心在 RGB 空间的距离；越小说明文字越难分辨。

    为什么用 k-means 而不是取最亮/最暗像素：文字有抗锯齿边缘，
    极值像素往往是过渡色，取极值会把字色和底色都算偏。
    """
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = image_bgr.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    fallback = ((255, 255, 255), (0, 0, 0), 255.0)
    if x2 <= x1 or y2 <= y1:
        return fallback

    patch = image_bgr[y1:y2, x1:x2]
    if patch.size == 0:
        return fallback

    # 统一在 RGB 空间做聚类，最终再转回 BGR 给 OpenCV 用
    rgb = cv2.cvtColor(patch, cv2.COLOR_BGR2RGB).reshape(-1, 3).astype(np.float32)
    if len(rgb) < 4:
        c = tuple(int(v) for v in rgb.mean(axis=0))
        return c, c, 0.0

    # 先下采样，聚类不需要全部像素，能快一个数量级
    step = max(1, len(rgb) // 4000)
    sample = rgb[::step]

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    try:
        _compact, labels, centers = cv2.kmeans(
            sample, 2, None, criteria, 3, cv2.KMEANS_PP_CENTERS
        )
    except cv2.error:
        return fallback

    centers = centers.astype(np.float32)
    counts = np.bincount(labels.flatten(), minlength=2)
    # 字色 = 少数派（文字占的面积通常小于背景）
    fg_i = int(np.argmin(counts))
    bg_i = 1 - fg_i
    fg = tuple(int(round(v)) for v in centers[fg_i])
    bg = tuple(int(round(v)) for v in centers[bg_i])
    sep = float(np.linalg.norm(centers[0] - centers[1]))

    # 分离度不足时强制把底色推成黑或白，保证中文一定看得清
    if sep < MIN_FG_BG_DIFF:
        luminance = 0.299 * fg[0] + 0.587 * fg[1] + 0.114 * fg[2]
        bg = (0, 0, 0) if luminance > 128 else (255, 255, 255)
        sep = float(abs(luminance - (0 if luminance > 128 else 255)))

    return fg, bg, sep


def detect_stroke(
    image_bgr: np.ndarray, box: tuple[int, int, int, int], fg: tuple[int, int, int]
) -> tuple[tuple[int, int, int] | None, int]:
    """估计描边颜色与宽度。

    做法：在文字像素的外围找一圈"既不是字色、也不是背景色"的像素。
    游戏里的艺术字几乎都带描边，不还原会显得很突兀。
    """
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = image_bgr.shape[:2]
    x1, y1 = max(0, x1 - 2), max(0, y1 - 2)
    x2, y2 = min(w, x2 + 2), min(h, y2 + 2)
    if x2 <= x1 or y2 <= y1:
        return None, 0

    patch = cv2.cvtColor(image_bgr[y1:y2, x1:x2], cv2.COLOR_BGR2RGB).reshape(-1, 3)
    if len(patch) < 16:
        return None, 0

    fg_arr = np.asarray(fg, dtype=np.float32)
    dist_to_fg = np.linalg.norm(patch.astype(np.float32) - fg_arr, axis=1)
    # 离字色足够远的像素里，找出现最多的那个颜色就是描边色
    far = patch[dist_to_fg > 60]
    if len(far) < 8:
        return None, 0

    q = (far // 16 * 16).astype(np.uint8)
    uniq, counts = np.unique(q, axis=0, return_counts=True)
    if len(uniq) == 0:
        return None, 0
    stroke = uniq[int(np.argmax(counts))]
    ratio = float(counts.max()) / float(len(patch))
    if ratio < 0.01:  # 占比太低，多半只是抗锯齿噪点
        return None, 0
    # 宽度按字号比例给，实际由调用方按字号换算
    return tuple(int(v) for v in stroke), 1


# --------------------------------------------------------------------------
# 字体与排版
# --------------------------------------------------------------------------


def fit_font_size(
    text: str,
    box_w: float,
    box_h: float,
    font_path: str,
    *,
    max_size: int | None = None,
    min_size: int = 6,
    letter_spacing: float = 0.0,
) -> int:
    """二分找出能把 ``text`` 塞进 ``(box_w, box_h)`` 的最大字号。

    这是防止"中文撑破按钮"的核心。中文方块字比拉丁字母宽，
    同样内容往往更宽，所以必须实测而不是按字符数估算。
    """
    if not text or box_w <= 0 or box_h <= 0:
        return min_size

    lo, hi = min_size, int(max_size or max(min_size, min(box_h * 1.6, 200)))
    best = min_size

    # manga-image-translator 的经验下限，避免极窄区域算出 0 号字
    floor = max(min_size, int((box_h + box_w) / 200))

    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(font_path, mid)
        except Exception:  # noqa: BLE001
            return max(min_size, floor)
        w, h = measure_text(text, font, letter_spacing)
        if w <= box_w and h <= box_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return max(best, floor if best == min_size else best)


def measure_text(text: str, font: ImageFont.FreeTypeFont, letter_spacing: float = 0.0) -> tuple[float, float]:
    """量一段文本的像素宽高。"""
    if not text:
        return 0.0, 0.0
    try:
        box = font.getbbox(text)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0
    w = float(box[2] - box[0])
    h = float(box[3] - box[1])
    if letter_spacing:
        w += letter_spacing * max(0, len(text) - 1)
    return w, h


def wrap_text(
    text: str, font: ImageFont.FreeTypeFont, max_w: float, *, max_lines: int = 12
) -> list[str]:
    """按像素宽度折行。

    中文可以任意位置断行，英文要按词断 —— 两者混排时先按词切，
    单个词过长再按字符切，避免出现一整行只有半个单词。
    """
    if not text:
        return []
    if measure_text(text, font)[0] <= max_w:
        return [text]

    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        # 先按"可断点"切：中文逐字可断，英文按空格
        tokens: list[str] = []
        buf = ""
        for ch in para:
            if _is_cjk(ch):
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
            elif ch == " ":
                if buf:
                    tokens.append(buf)
                tokens.append(" ")
                buf = ""
            else:
                buf += ch
        if buf:
            tokens.append(buf)

        cur = ""
        for tok in tokens:
            cand = cur + tok
            if measure_text(cand.rstrip(), font)[0] <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur.rstrip())
                cur = "" if tok == " " else tok
                if len(lines) >= max_lines:
                    break
        if cur.strip():
            lines.append(cur.rstrip())
        if len(lines) >= max_lines:
            break

    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines or [text]


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF      # 假名
        or 0x3400 <= o <= 0x4DBF   # 扩展 A
        or 0x4E00 <= o <= 0x9FFF   # 基本区
        or 0xF900 <= o <= 0xFAFF   # 兼容
        or 0xFF00 <= o <= 0xFFEF   # 全角
        or 0x20000 <= o <= 0x2FA1F
    )


# --------------------------------------------------------------------------
# 绘制
# --------------------------------------------------------------------------


def render_text_block(
    text: str,
    size: tuple[int, int],
    font_path: str,
    style: TextBlockStyle,
    *,
    bg: tuple[int, int, int] | None = None,
) -> np.ndarray | None:
    """把一段中文渲染成 RGBA 图（透明底），尺寸为 ``size``。

    返回 ``(BGR 图, alpha 图)`` 形式的数组；画不下时返回 ``None``。
    """
    if not text.strip():
        return None
    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        return None

    ss = SUPERSAMPLE
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)

    # 字号：优先用探测值，再按实际空间收缩
    want = style.font_size or 0
    size_px = fit_font_size(
        text, w * ss, h * ss, font_path,
        max_size=int(want * ss) if want else None,
        letter_spacing=style.letter_spacing * ss,
    )
    try:
        font = ImageFont.truetype(font_path, max(6, size_px))
    except Exception as exc:  # noqa: BLE001
        log.warning("字体加载失败 %s：%s", font_path, exc)
        return None

    lines = wrap_text(text, font, w * ss, max_lines=max(1, int(h * ss / max(8, size_px) / LINE_SPACING) + 1))
    if not lines:
        return None

    line_h = measure_text("汉", font)[1] * style.line_spacing
    total_h = line_h * len(lines)
    y = (h * ss - total_h) / 2

    fill = (*style.text_color, 255)
    stroke_fill = (*style.stroke_color, 255) if style.stroke_color else None
    stroke_w = int(round(style.stroke_width * ss)) if style.stroke_width else 0

    for line in lines:
        lw, _lh = measure_text(line, font, style.letter_spacing * ss)
        if style.align == "left":
            x = 0.0
        elif style.align == "right":
            x = w * ss - lw
        else:
            x = (w * ss - lw) / 2
        try:
            draw.text(
                (x, y),
                line,
                font=font,
                fill=fill,
                stroke_width=stroke_w,
                stroke_fill=stroke_fill,
            )
        except Exception:  # noqa: BLE001
            draw.text((x, y), line, font=font, fill=fill)
        y += line_h

    # 缩回原尺寸。用 BOX 而不是 LANCZOS：LANCZOS 的负瓣会在文字边缘
    # 产生振铃（暗边/亮边），小字号下很明显。
    small = big.resize((w, h), Image.BOX)
    arr = np.asarray(small)  # RGBA
    return arr


@dataclass
class BlockRender:
    """**一块**文字的重绘结果。"""

    rgba: np.ndarray | None = None
    """渲染好的 BGRA 图；``None`` 表示渲染失败。"""

    font_size: int = 0
    overflow: bool = False
    """字号已缩到下限仍塞不下。应**标出来交给用户决定**，而不是硬画 ——
    溢出到按钮外的中文比不翻译更糟。"""

    min_readable_size: int = 9
    detail: str = ""

    @property
    def too_small(self) -> bool:
        return 0 < self.font_size < self.min_readable_size

    @property
    def needs_review(self) -> bool:
        return self.overflow or self.too_small


def render_text_block_checked(
    text: str,
    size: tuple[int, int],
    font_path: str,
    style: TextBlockStyle,
    *,
    min_size: int = 8,
    min_readable_size: int = 9,
) -> BlockRender:
    """渲染并**如实报告放不下/太小**，供上层做审校。

    为什么单独有这个函数：中文普遍比英文短（字符数少一半），
    但**渲染宽度并不缩短** —— "OK" 变成"确定"宽度反而翻倍。
    短标签最容易溢出，所以必须检测而不是硬画。
    """
    w, h = int(size[0]), int(size[1])
    if not text.strip() or w <= 0 or h <= 0:
        return BlockRender(detail="空文本或空区域")

    natural = fit_font_size(text, w, h, font_path, min_size=min_size)

    def _measure(size_px: int) -> tuple[float, float]:
        try:
            from PIL import ImageFont

            return measure_text(text, ImageFont.truetype(font_path, size_px))
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    fitted = fit_font_size(
        text,
        w,
        h,
        font_path,
        max_size=int(style.font_size) if style.font_size else None,
        min_size=min_size,
    )
    fw, fh = _measure(fitted)
    overflow = bool(fw > w or fh > h)

    rgba = render_text_block(text, size, font_path, style)
    if rgba is None:
        return BlockRender(font_size=fitted, overflow=True, detail="渲染返回空")

    style.font_size = fitted
    detail = f"字号 {fitted}（自然需求 {natural}，区域 {w}×{h}）"
    if overflow:
        detail += " ⚠️ 溢出"
    return BlockRender(
        rgba=rgba,
        font_size=fitted,
        overflow=overflow,
        min_readable_size=min_readable_size,
        detail=detail,
    )


def paste_quad(
    canvas_bgr: np.ndarray,
    rgba: np.ndarray,
    quad: list[tuple[float, float]],
    *,
    alpha_threshold: int = 8,
) -> bool:
    """用单应变换把渲染好的文字贴到任意四边形上。

    这是支持斜排/透视文字的关键：把"矩形贴图"的四个角映射到目标四边形的
    四个角，再 ``warpPerspective``。直接 ``paste`` 只能贴轴对齐矩形，
    遇到倾斜招牌就会贴歪。
    """
    if rgba is None or rgba.size == 0:
        return False
    pts = order_quad(quad)
    if len(pts) < 4:
        return False

    h, w = rgba.shape[:2]
    src = np.asarray([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    dst = np.asarray(pts, dtype=np.float32)
    if dst.shape != (4, 2):
        return False

    H = cv2.getPerspectiveTransform(src, dst)
    ch, cw = canvas_bgr.shape[:2]

    warped = cv2.warpPerspective(
        rgba[:, :, :3],
        H,
        (cw, ch),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    alpha = cv2.warpPerspective(
        rgba[:, :, 3],
        H,
        (cw, ch),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    # 抗锯齿边缘会留下一圈半透明像素，把它们并进实心区，
    # 否则缩放后文字外缘会出现一圈暗边
    mask = (alpha > alpha_threshold)
    if not mask.any():
        return False
    a = (alpha.astype(np.float32) / 255.0)[:, :, None]
    blended = warped.astype(np.float32) * a + canvas_bgr.astype(np.float32) * (1.0 - a)
    canvas_bgr[mask] = np.clip(blended[mask], 0, 255).astype(np.uint8)
    return True


__all__ = [
    "BlockRender",
    "RenderResult",
    "detect_stroke",
    "extract_colors",
    "fit_font_size",
    "measure_text",
    "paste_quad",
    "render_text_block",
    "render_text_block_checked",
    "wrap_text",
]
