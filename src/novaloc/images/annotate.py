"""给贴图文字块画标注框，供人工复核。

为什么需要它：用户要判断"这块文字翻得对不对"，就必须知道
"这块文字在原图的哪个位置"。光给一个文本列表是没法审校的。

用**颜色区分状态**，让人一眼看出哪块需要关注：

* 绿框 —— 已成功重绘
* 橙框 —— 有警告（过长/过小）
* 红框 —— 失败
* 灰框 —— 跳过（没译文）

框角画短折线而不是整框，这样不会把底下的原文和译文挡住。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from .io import imread_rgba, imwrite_bgr

log = logging.getLogger(__name__)

# BGR
COLOR_OK = (80, 200, 80)
COLOR_WARN = (60, 190, 250)
COLOR_FAIL = (70, 70, 235)
COLOR_SKIP = (150, 150, 150)
COLOR_TEXT_BG = (0, 0, 0)


def _color_for(block: Any) -> tuple[int, int, int]:
    status = getattr(block, "status", None)
    status_v = getattr(status, "value", status)
    warnings = list(getattr(block, "warnings", []) or [])
    if status_v == "failed":
        return COLOR_FAIL
    if warnings:
        return COLOR_WARN
    if status_v in ("translated", "reviewed", "locked"):
        return COLOR_OK
    return COLOR_SKIP


def _draw_corners(
    arr: np.ndarray, quad: list[tuple[float, float]], color: tuple[int, int, int], thick: int = 2
) -> None:
    """在四个角画折线，而不是整框 —— 不遮挡内容。"""
    if len(quad) != 4:
        return
    h, w = arr.shape[:2]
    pts = [(int(round(x)), int(round(y))) for x, y in quad]
    for i in range(4):
        # 从每个顶点出发沿两条边各画一段角标
        for (ax, ay), (bx, by) in ((pts[(i - 1) % 4], pts[i]), (pts[i], pts[(i + 1) % 4])):
            dx, dy = bx - ax, by - ay
            length = (dx * dx + dy * dy) ** 0.5
            # 角标长度 = 22% 边长，但**至少 3 像素**。
            # 早先这里算出了 length 却没用（ruff F841），一律硬编码 22%；
            # 于是小框（OCR 出的小字，边长只有 1~3 像素）画出来的角标
            # 不足 1 像素，取整后退化成同一个点 —— 框"画了但看不见"，
            # 审校页面一片空白，用户以为这张图没检出任何文字。
            seg_len = min(length, max(length * 0.22, 3.0))
            if length <= 0:
                continue
            k = seg_len / length
            ex, ey = int(round(ax + dx * k)), int(round(ay + dy * k))
            _line(arr, ax, ay, ex, ey, color, thick, h, w)


def _line(
    arr: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
    thick: int,
    h: int,
    w: int,
) -> None:
    n = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(n + 1):
        t = i / n
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        for dy in range(-thick // 2, thick // 2 + 1):
            for dx in range(-thick // 2, thick // 2 + 1):
                xx, yy = x + dx, y + dy
                if 0 <= yy < h and 0 <= xx < w:
                    arr[yy, xx] = color


def annotate_blocks(
    image_path: str | Path,
    blocks: Iterable[Any],
    out_path: str | Path,
    *,
    draw_text: bool = True,
) -> Path:
    """在原图上画出所有文字块，输出到 ``out_path``。"""
    src = Path(image_path)
    dst = Path(out_path)

    rgba = imread_rgba(src)
    if rgba is None:
        raise FileNotFoundError(f"无法读取图片：{src}")

    # 有 alpha 就合成到深色底上，否则透明区域看不出框
    if rgba.ndim == 3 and rgba.shape[2] == 4:
        alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
        rgb = rgba[:, :, :3].astype(np.float32)
        bg = np.full_like(rgb, 24.0)
        arr = (rgb * alpha + bg * (1 - alpha)).astype(np.uint8)
    else:
        arr = rgba[:, :, :3].copy() if rgba.ndim == 3 else np.stack([rgba] * 3, -1)

    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]

    for b in blocks:
        color = _color_for(b)
        quad = getattr(b, "quad", None) or []
        if not quad or len(quad) != 4:
            x1, y1, x2, y2 = getattr(b, "box", (0, 0, 0, 0))
            quad = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        quad = [(float(x), float(y)) for x, y in quad]
        _draw_corners(arr, quad, color)
        if draw_text:
            _mark_index(arr, quad, color, h, w)

    dst.parent.mkdir(parents=True, exist_ok=True)
    imwrite_bgr(dst, arr)
    return dst


def _mark_index(
    arr: np.ndarray, quad: list[tuple[float, float]], color: tuple[int, int, int], h: int, w: int
) -> None:
    """在框的左上角外画一个小色块，作为状态提示。"""
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    x = int(min(xs)) - 1
    y = int(min(ys)) - 1
    size = 6
    for dy in range(size):
        for dx in range(size):
            xx, yy = x + dx, y + dy
            if 0 <= yy < h and 0 <= xx < w:
                arr[yy, xx] = color


__all__ = ["annotate_blocks"]
