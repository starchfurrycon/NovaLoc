"""去字（inpaint）：把原文字从贴图上抹掉，给中文腾位置。

分三档，按"背景有多复杂"自动选择 —— 这是本模块最重要的设计：

1. **纯色填充**：如果文字周围的背景接近单色（游戏 UI 绝大多数情况），
   直接填背景色，效果比任何生成式修补都干净，而且快到可以忽略。
   生成式修补在这种区域反而会糊出噪点或鬼影。
2. **OpenCV Telea / Navier-Stokes**：背景有渐变或简单纹理时用。
   无额外依赖，速度快。
3. **LaMa**（可选）：背景复杂（照片、手绘纹理）时才需要。
   是 TorchScript 模型，本机未装 torch 所以默认不可用；
   装了 ``simple-lama-inpainting`` 或提供 ``lama.pt`` 就会自动启用。

参考 manga-image-translator 的 ``mask_dilation_offset`` 经验：掩膜要比文字
本身**外扩**一点，否则描边和抗锯齿边缘会残留一圈黑边。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

log = logging.getLogger(__name__)

#: 判定"背景纯色"的阈值：该区域内像素标准差低于此值就认为可以纯色填充
SOLID_STD_THRESHOLD = 12.0

#: 待填充区域的采样框要外扩多少像素（避开文字描边与抗锯齿）
SAMPLING_MARGIN = 4


@dataclass
class InpaintResult:
    image: np.ndarray
    """修补后的 BGR 图像。"""

    method: str = ""
    """实际使用的方法：``solid`` / ``telea`` / ``ns`` / ``lama``。"""

    detail: str = ""


def build_mask(
    shape: tuple[int, int],
    boxes: list[tuple[int, int, int, int]],
    *,
    dilate: int = 3,
    quads: list[list[tuple[float, float]]] | None = None,
) -> np.ndarray:
    """构造 inpaint 用的二值掩膜（255 = 要抹掉）。

    ``dilate`` 是外扩量：文字的抗锯齿边缘和描边通常比 OCR 框大 1~3 像素，
    不外扩就会留下一圈残影。manga-image-translator 的
    ``mask_dilation_offset`` 默认 30 是针对漫画气泡的大场景，
    游戏 UI 用 3 左右就够。
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)

    if quads:
        for quad in quads:
            if len(quad) >= 3:
                pts = np.asarray(quad, dtype=np.int32).reshape(-1, 1, 2)
                cv2.fillPoly(mask, [pts], 255)

    for x1, y1, x2, y2 in boxes:
        # 越界夹紧，避免在边缘的文本框导致 cv2 报错
        ax1, ay1 = max(0, int(x1)), max(0, int(y1))
        ax2, ay2 = min(w, int(x2)), min(h, int(y2))
        if ax2 > ax1 and ay2 > ay1:
            mask[ay1:ay2, ax1:ax2] = 255

    if dilate > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate * 2 + 1, dilate * 2 + 1))
        mask = cv2.dilate(mask, k, iterations=1)
    return mask


def sample_background(
    image: np.ndarray, box: tuple[int, int, int, int], *, margin: int = SAMPLING_MARGIN
) -> tuple[np.ndarray | None, float]:
    """在框**外面**采一圈像素，估计背景色。

    返回 ``(背景色的中位数, 环带的标准差)``。标准差很小说明背景是纯色。
    在框外面采样是有意为之：框内几乎全是文字像素，直接统计会被字色污染。
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    ox1, oy1 = max(0, x1 - margin), max(0, y1 - margin)
    ox2, oy2 = min(w, x2 + margin), min(h, y2 + margin)
    if ox2 <= ox1 or oy2 <= oy1:
        return None, 999.0

    outer = image[oy1:oy2, ox1:ox2]
    inner = image[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
    if inner.size == 0:
        return None, 999.0

    # 用"环带 = 外框 − 内框"来取背景样本
    ring = outer.copy()
    iy1, ix1 = max(0, y1) - oy1, max(0, x1) - ox1
    iy2, ix2 = iy1 + inner.shape[0], ix1 + inner.shape[1]
    if iy2 <= ring.shape[0] and ix2 <= ring.shape[1] and inner.size:
        ring_mask = np.ones(ring.shape[:2], dtype=bool)
        ring_mask[iy1:iy2, ix1:ix2] = False
        ring = ring[ring_mask]

    if ring.size == 0:
        return None, 999.0

    ring_f = ring.reshape(-1, ring.shape[-1]).astype(np.float32)
    median = np.median(ring_f, axis=0)
    std = float(np.mean(np.std(ring_f, axis=0)))
    return median, std


def inpaint_boxes(
    image: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
    *,
    dilate: int = 3,
    quads: list[list[tuple[float, float]]] | None = None,
    prefer_solid: bool = True,
    cv_method: str = "telea",
    lama: Any = None,
) -> InpaintResult:
    """把若干文字框从图里抹掉。

    ``image`` 是 BGR ndarray（OpenCV 约定）。返回修补后的图与所用方法。

    策略：逐框判断背景是否纯色 —— 纯色就单独填，其余区域统一交给
    inpaint 算法。这样一张图里"纯色按钮 + 渐变横幅"能各自用最合适的方法。
    """
    if image is None or image.size == 0:
        return InpaintResult(image=image, method="none", detail="空图像")

    h, w = image.shape[:2]
    out = image.copy()
    solid_boxes: list[tuple[int, int, int, int]] = []
    hard_boxes: list[tuple[int, int, int, int]] = []

    all_boxes = list(boxes)
    if prefer_solid:
        for b in all_boxes:
            color, std = sample_background(out, b)
            if color is not None and std < SOLID_STD_THRESHOLD:
                solid_boxes.append(b)
            else:
                hard_boxes.append(b)
    else:
        hard_boxes = all_boxes

    # --- 第一档：纯色填充 ---
    for b in solid_boxes:
        color, _ = sample_background(out, b)
        if color is None:
            continue
        x1, y1, x2, y2 = (int(v) for v in b)
        ax1, ay1 = max(0, x1 - dilate), max(0, y1 - dilate)
        ax2, ay2 = min(w, x2 + dilate), min(h, y2 + dilate)
        if ax2 > ax1 and ay2 > ay1:
            out[ay1:ay2, ax1:ax2] = color.astype(np.uint8)

    # --- 复杂区域：统一走 inpaint 算法 ---
    method = "solid" if solid_boxes and not hard_boxes else ""
    rest_quads = quads or []
    if hard_boxes or (rest_quads and not prefer_solid):
        mask = build_mask((h, w), hard_boxes, dilate=dilate, quads=rest_quads)
        if mask.any():
            used = ""
            if lama is not None:
                try:
                    out = lama(out, mask)
                    used = "lama"
                except Exception as exc:  # noqa: BLE001
                    log.warning("LaMa 修补失败，回退到 OpenCV：%s", exc)
            if not used:
                flag = cv2.INPAINT_NS if cv_method == "ns" else cv2.INPAINT_TELEA
                radius = max(3, dilate + 2)
                out = cv2.inpaint(out, mask, radius, flag)
                used = "ns" if cv_method == "ns" else "telea"
            method = used if not method else f"{method}+{used}"

    if not method:
        method = "solid" if solid_boxes else "none"

    return InpaintResult(
        image=out,
        method=method,
        detail=f"纯色 {len(solid_boxes)} 框 / 算法修补 {len(hard_boxes)} 框",
    )


# --------------------------------------------------------------------------
# 可选：LaMa
# --------------------------------------------------------------------------


class LamaInpainter:
    """LaMa（TorchScript）封装。装不上就静默降级，不阻塞主流程。

    LaMa 是 Apache-2.0，适合背景复杂的情况。本机没装 torch，
    所以这个类默认处于"不可用"状态，由 :func:`load_lama` 探测。
    """

    def __init__(self, model_path: Path, device: str = "cpu") -> None:
        import torch

        self.device = device
        self.model = torch.jit.load(str(model_path), map_location=device)
        self.model.eval()
        self.pad_to = 8

    def __call__(self, image_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch

        h, w = image_bgr.shape[:2]
        # 输入尺寸对齐到 8 的倍数（LaMa 的 U-Net 要求）
        ph = (self.pad_to - h % self.pad_to) % self.pad_to
        pw = (self.pad_to - w % self.pad_to) % self.pad_to
        img = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = np.pad(img, ((0, ph), (0, pw), (0, 0)), mode="symmetric")
        m = (mask > 127).astype(np.float32)
        m = np.pad(m, ((0, ph), (0, pw)), mode="constant")

        t_img = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).to(self.device)
        t_mask = torch.from_numpy(m).unsqueeze(0).unsqueeze(0).to(self.device)
        with torch.no_grad():
            pred = self.model(t_img, t_mask)
        res = pred.squeeze(0).permute(1, 2, 0).cpu().numpy()
        res = np.clip(res * 255.0, 0, 255).astype(np.uint8)[:h, :w]
        return cv2.cvtColor(res, cv2.COLOR_RGB2BGR)


def load_lama(model_path: Path | None = None) -> Any:
    """尝试加载 LaMa，失败返回 ``None``（调用方据此降级）。"""
    try:
        import torch  # noqa: F401
    except ImportError:
        log.info("未安装 torch，跳过 LaMa（将使用纯色/OpenCV 修补）")
        return None

    if model_path is None or not Path(model_path).exists():
        log.info("未找到 LaMa 模型文件，跳过（将使用纯色/OpenCV 修补）")
        return None

    try:
        inst = LamaInpainter(Path(model_path))
        log.info("LaMa 已加载：%s", model_path)
        return inst
    except Exception as exc:  # noqa: BLE001
        log.warning("LaMa 加载失败：%s", exc)
        return None


__all__ = [
    "InpaintResult",
    "LamaInpainter",
    "build_mask",
    "inpaint_boxes",
    "load_lama",
    "sample_background",
]
