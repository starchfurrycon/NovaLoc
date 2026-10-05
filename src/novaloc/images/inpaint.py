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


class LamaOnnxInpainter:
    r"""LaMa 的 **ONNX** 封装（本机首选路径）。

    ## ★ 为什么走 ONNX 而不是 TorchScript

    代码原本只支持 `.pt`（`torch.jit.load`），但那要求 **torch**
    —— 本机**没装**，而装它约 2 GB。同时本机**已经有**
    `onnxruntime 1.24.4 + DirectML`（GPU 加速）在跑 OCR。

    ⇒ 用 ONNX 版 LaMa 是**零新依赖 + 能复用 GPU**的路子。

    实测（`D:\NovaLoc\models\lama\inpainting_lama_2025jan.onnx`，92.6 MB）：

    * 来源：**OpenCV 官方** `opencv/inpainting_lama`（配 `cv2 5.0`，
      兼容性有保障，且比社区版小一半）；
    * 输入 `image [b,3,512,512] float` + `mask [b,1,512,512] float`；
    * 输出 `output [b,3,512,512] float`；
    * `providers = ['DmlExecutionProvider', 'CPUExecutionProvider']`
      ⇒ **DML 可用，走 GPU**。

    ## 尺寸处理

    模型固定吃 512×512，所以：
    把原图缩放到 512、推理、再把**结果**放大回原尺寸。

    ⚠️ 放大回原尺寸会**损失原图细节** —— 所以**不能整张图替换**，
    只把修补结果**贴回 mask 覆盖的区域**（见 `__call__`），
    其余像素保持原样。这是"只抹文字、不动背景"的关键。
    """

    def __init__(self, model_path: Path, *, providers: list[str] | None = None) -> None:
        r"""加载 ONNX 会话。

        ## ★ 默认 **不用 DirectML**（实测该模型跑不了）

        `DirectML` 在 LaMa 的 FFC 模块上执行 `MatMul` 节点会失败：

            Non-zero status code returned while running MatMul node.
            Name:'/generator/model.5/conv1/ffc/conv2g2/fu/rtn/MatMul_5'

        ⇒ 实测 `providers=['DmlExecutionProvider']` **每次都抛异常**，
        而 CPU 跑得通（512×512 约 9.4 秒）。

        更糟的是那个异常的信息是**本地化的 GBK 字节**，
        在 UTF-8 环境里解码又抛 `UnicodeDecodeError`，
        把真正的错误盖掉了。

        ⇒ 所以默认只用 CPU：**慢，但确定能用**。
        调用方若确认自己环境支持，可以显式传 `providers=['DmlExecutionProvider', ...]`。
        """
        import onnxruntime as ort

        so = ort.SessionOptions()
        # 关掉啰嗦日志（否则每次推理刷一堆 native warning）
        so.log_severity_level = 3
        want = providers or ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(model_path), so, providers=want)
        self.size = 512

    def __call__(self, image_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
        r"""抹掉 mask 区域的文字，返回新图（BGR uint8）。

        ## ★ 预处理/后处理**必须照 OpenCV 参考实现**（我第一版两处都错了）

        官方 `lama.py` 的关键三行：

        ```python
        image_blob = cv.dnn.blobFromImage(image, 0.00392, (512,512), (0,0,0), False, False)
        mask_blob  = (cv.dnn.blobFromImage(mask, 1.0, (512,512), (0,), False, False) > 0).astype(np.float32)
        result     = np.transpose(output[0], (1,2,0)).astype(np.uint8)
        ```

        我第一版的两个错误：

        1. **画蛇添足转了 RGB**（`cvtColor(BGR2RGB)`）。参考实现第 5 个参数
           `swapRB=False` ⇒ **直接用 BGR**。颜色通道搞反会让输出色彩错乱。
        2. **输出又乘了 255**。实测模型输出范围**已经是 `[0, 255]`**
           （不是 `[0,1]`），参考实现也是直接 `astype(np.uint8)`。
           再乘 255 会**全部溢出成白色**。

        ## 只贴回 mask 区域（这一条是我加的，参考实现没做）

        参考实现整张图替换。但模型固定吃 512，放大回原尺寸会**糊掉整图**。
        我们只需要"抹掉文字"，所以**只把 mask 覆盖的像素贴回去**，
        其余保持原图 —— 背景细节一点不损失。
        """
        h, w = image_bgr.shape[:2]
        side = self.size
        # 1) 原图 → BGR float [0,1]（**不转 RGB**，照参考实现），缩放到 512
        img = image_bgr.astype(np.float32) * 0.00392
        img_s = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
        # 2) mask → 0/1，同样缩放（最近邻，避免半透明边缘）
        m = (mask > 127).astype(np.float32)
        m_s = cv2.resize(m, (side, side), interpolation=cv2.INTER_NEAREST)

        inp = {
            "image": img_s.transpose(2, 0, 1)[None],      # [1,3,H,W]
            "mask": m_s[None, None],                       # [1,1,H,W]
        }
        out = self.session.run(None, inp)[0][0]            # [3,H,W]，范围 [0,255]
        # 3) 结果 → BGR uint8（**不乘 255**），放回原尺寸
        res = np.clip(out.transpose(1, 2, 0), 0, 255).astype(np.uint8)
        res = cv2.resize(res, (w, h), interpolation=cv2.INTER_LINEAR)

        # 4) 只把 mask 覆盖的区域贴回去，其余保持原图
        sel = (mask > 127)[:, :, None]
        out_img = image_bgr.copy()
        np.copyto(out_img, res, where=np.broadcast_to(sel, out_img.shape))
        return out_img


class LamaInpainter:
    """LaMa（TorchScript）封装。装不上就静默降级，不阻塞主流程。

    LaMa 是 Apache-2.0，适合背景复杂的情况。本机没装 torch，
    所以这个类默认处于"不可用"状态，由 :func:`load_lama` 探测。

    ⚠️ 本机**优先走** :class:`LamaOnnxInpainter`（ONNX + DirectML，
    零新依赖）；这个 TorchScript 版本留着兼容已有 `.pt` 权重。
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
    r"""加载 LaMa 修补器；失败返回 ``None``（调用方据此降级）。

    ## 支持两种权重，**优先 ONNX**

    1. **`.onnx`** ⇒ :class:`LamaOnnxInpainter`（需要 `onnxruntime`，
       本机已装且带 DirectML ⇒ 走 GPU）。**零 torch 依赖**。
    2. **`.pt`** ⇒ :class:`LamaInpainter`（需要 `torch`，本机未装）。

    ``model_path`` 给的是**首选**路径；若它不存在，会去同一目录找
    其它已知文件名（实测本机放的是 OpenCV 官方的
    `inpainting_lama_2025jan.onnx`，而不是代码原来期望的 `lama.pt`）。

    ## 为什么要"自动找同目录的其它候选"

    原代码写死 `models/lama/lama.pt`。实测该文件**不存在**，
    于是永远静默降级到纯色/OpenCV 修补 —— 而用户以为贴图汉化在工作。
    多认几个候选名可以让"权重放对了但名字不同"不再变成静默失效。
    """
    # 解析实际要用的文件：先试给定路径，再在它的目录里找候选
    chosen: Path | None = None
    if model_path is not None:
        p = Path(model_path)
        if p.is_file():
            chosen = p
        elif p.parent.is_dir():
            for pat in ("*.onnx", "*.pt"):
                cands = sorted(p.parent.glob(pat))
                if cands:
                    chosen = cands[0]
                    log.info("LaMa：%s 不存在，改用同目录的 %s", p.name, chosen.name)
                    break
    if chosen is None:
        log.info("未找到 LaMa 模型文件，跳过（将使用纯色/OpenCV 修补）")
        return None

    suffix = chosen.suffix.lower()
    if suffix == ".onnx":
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            log.info("未安装 onnxruntime，跳过 ONNX LaMa（将降级）")
            return None
        try:
            inst = LamaOnnxInpainter(chosen)
            prov = inst.session.get_providers()
            log.info("LaMa(ONNX) 已加载：%s  providers=%s", chosen.name, prov)
            return inst
        except Exception as exc:  # noqa: BLE001
            log.warning("LaMa(ONNX) 加载失败：%s", exc)
            return None

    # --- TorchScript 路径（需要 torch） ---
    try:
        import torch  # noqa: F401
    except ImportError:
        log.info("未安装 torch，跳过 TorchScript LaMa（将降级）")
        return None
    try:
        inst = LamaInpainter(chosen)
        log.info("LaMa(TorchScript) 已加载：%s", chosen.name)
        return inst
    except Exception as exc:  # noqa: BLE001
        log.warning("LaMa(TorchScript) 加载失败：%s", exc)
        return None


__all__ = [
    "InpaintResult",
    "LamaInpainter",
    "LamaOnnxInpainter",
    "build_mask",
    "inpaint_boxes",
    "load_lama",
    "sample_background",
]
