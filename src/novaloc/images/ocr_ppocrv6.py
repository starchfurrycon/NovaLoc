"""OCR 适配器：PP-OCRv6（RapidOCR + ONNXRuntime/DirectML）。

为什么用 PP-OCRv6 而不是视觉大模型：
在旋转文本检测上 PP-OCRv6 的 Hmean 是 93.7~93.8，而 Qwen3-VL-235B 只有 2.1、
GPT-5.5 只有 10.0 —— 差一个数量级。所以**检测与识别一律走 PP-OCRv6**，
视觉模型只在极端情况（艺术字、严重变形、手写）做兜底。

本机实测（RTX 4060 Laptop + DirectML，1024×1024 贴图）：
    tiny ≈ 170 ms   small ≈ 410 ms   medium ≈ 610 ms
同一张图纯 CPU 要 20~24 秒 —— 差 40~140 倍，所以 DirectML 默认开启。

模型从 ModelScope 下载（本机 hosts 只屏蔽了 GitHub/HuggingFace，
ModelScope 可直连），缓存在数据目录。体积：tiny ≈ 6 MB，medium ≈ 139 MB。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..core.registry import Context, register
from ..models import ImageTextBlock, TextBlockStyle
from .io import imread_bgr

log = logging.getLogger(__name__)

#: 档位名 → RapidOCR 的 ModelType 成员名
_TIERS = {"tiny": "TINY", "small": "SMALL", "medium": "MEDIUM"}


@dataclass
class OcrPage:
    """一张图的识别结果。"""

    blocks: list[ImageTextBlock] = field(default_factory=list)
    width: int = 0
    height: int = 0
    elapsed_ms: float = 0.0
    tier: str = "medium"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def text(self) -> str:
        return "\n".join(b.source for b in self.blocks)


def order_quad(quad: Any) -> list[list[float]]:
    """把任意顺序的四点规整成 ``[左上, 右上, 右下, 左下]``。

    RapidOCR 返回的点序**并不保证**从左上角开始，而贴回阶段要靠
    "上边"推角度、"左边"推高度。所以必须先规整点序，
    否则算出来的角度会差 90°/180°，文字会横竖颠倒。

    做法：先按角度绕质心排序（顺时针），再把最靠近左上角的点转到开头。
    """
    p = np.asarray(quad, dtype=np.float32).reshape(-1, 2)
    if len(p) < 4:
        return [[float(x), float(y)] for x, y in p]
    p = p[:4]
    c = p.mean(axis=0)
    ang = np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0])
    p = p[np.argsort(ang)]  # 角度升序 = 图像坐标系下的顺时针
    # 找左上角：x+y 最小
    start = int(np.argmin(p[:, 0] + p[:, 1]))
    p = np.roll(p, -start, axis=0)
    return [[float(x), float(y)] for x, y in p]


def quad_geometry(quad: Any) -> tuple[float, float, float]:
    """从四点算出 ``(角度, 宽, 高)``。

    角度是**图像坐标系下顺时针为正**（y 轴朝下），与 OpenCV/PIL 的
    ``rotate`` 语义一致，贴回时直接用即可。

    点序先用 :func:`order_quad` 规整，避免 RapidOCR 的点序差异导致
    角度整体偏 90°。
    """
    pts = order_quad(quad)
    if len(pts) < 4:
        return 0.0, 0.0, 0.0
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    angle = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))
    w = float(math.hypot(x1 - x0, y1 - y0))
    h = float(math.hypot(x3 - x0, y3 - y0))
    # 归一到 (-45, 45]：旋转 90° 的等价描述对排版没有意义
    while angle <= -45.0:
        angle += 90.0
    while angle > 45.0:
        angle -= 90.0
    return angle, w, h


@register("ocr", "ppocrv6")
class PPOcrV6Engine:
    """基于 RapidOCR 的 PP-OCRv6 适配器。"""

    name = "ppocrv6"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.config
        self._engine: Any = None
        self._tier: str = ""
        self._ready: tuple[bool, str] | None = None
        self._load_error: str = ""

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------

    @property
    def ocr_cfg(self) -> Any:
        return getattr(self.cfg, "ocr", None) or getattr(self.cfg, "image", None)

    def _opt(self, name: str, default: Any) -> Any:
        cfg = self.ocr_cfg
        return getattr(cfg, name, default) if cfg is not None else default

    @property
    def tier(self) -> str:
        t = str(self._opt("model_tier", "medium") or "medium").lower()
        return t if t in _TIERS else "medium"

    def model_dir(self) -> Path:
        from ..core.paths import models_dir

        d = models_dir() / "rapidocr"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ep_label(self) -> str:
        return "DirectML" if bool(self._opt("use_directml", True)) else "CPU"

    def available(self) -> tuple[bool, str]:
        if self._ready is not None:
            return self._ready
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            self._ready = (False, "未安装 rapidocr")
            return self._ready
        if self._load_error:
            self._ready = (False, self._load_error)
            return self._ready
        try:
            self._ensure()
            self._ready = (True, f"PP-OCRv6 {self.tier}（{self._ep_label()}）")
        except Exception as exc:  # noqa: BLE001
            self._ready = (False, f"PP-OCRv6 初始化失败：{exc}")
        return self._ready

    # ------------------------------------------------------------------
    # 引擎构造
    # ------------------------------------------------------------------

    def _ensure(self) -> Any:
        want = self.tier
        if self._engine is not None and self._tier == want:
            return self._engine

        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        model_type = getattr(ModelType, _TIERS.get(want, "MEDIUM"))
        verbose = bool(self._opt("verbose", False))

        params: dict[str, Any] = {
            "Global.model_root_dir": str(self.model_dir()),
            # 横排游戏 UI 占绝大多数；方向分类会多花一份推理时间。
            "Global.use_cls": bool(self._opt("use_cls", False)),
            "Global.log_level": "info" if verbose else "warning",
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": model_type,
            "Det.lang_type": LangDet.CH,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": model_type,
            "Rec.lang_type": LangRec.CHINESE_CHT,
        }
        if bool(self._opt("use_directml", True)):
            # 关键键名：RapidOCR 从 EngineConfig.onnxruntime 读 EP 配置。
            # 实测 DirectML 比 CPU 快 40~140 倍，是本工具可用性的前提。
            params["EngineConfig.onnxruntime.use_dml"] = True

        threads = int(self._opt("cpu_threads", 0) or 0)
        if threads > 0:
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads

        log.info("加载 PP-OCRv6 %s 模型（%s）", want, self.model_dir())
        t0 = time.time()
        try:
            self._engine = RapidOCR(params=params)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"PP-OCRv6 加载失败：{exc}"
            raise
        self._tier = want
        log.info("PP-OCRv6 %s 就绪，用时 %.1fs", want, time.time() - t0)
        return self._engine

    def unload(self) -> None:
        self._engine = None
        self._tier = ""
        self._ready = None

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------

    def read(
        self,
        image: Any,
        *,
        min_score: float | None = None,
        max_side: int | None = None,
        asset_uid: str = "",
    ) -> OcrPage:
        """识别一张图。``image`` 可为路径 / ndarray / PIL Image。"""
        ok, why = self.available()
        if not ok:
            return OcrPage(error=why)

        arr, w, h = as_rgb_array(image)
        if arr is None:
            return OcrPage(error=f"无法读取图像：{str(image)[:80]}")

        score = float(min_score if min_score is not None else self._opt("min_score", 0.5))
        limit = int(max_side or self._opt("max_side", 4096) or 4096)
        scale = 1.0
        if max(w, h) > limit:
            scale = limit / max(w, h)
            arr = resize_rgb(arr, scale)
            log.debug("图像过大，缩放至 %.0f%% 再识别", scale * 100)

        engine = self._ensure()
        t0 = time.time()
        try:
            # text_score 是 RapidOCR 的置信度下限，低于它的框直接丢掉
            result = engine(arr, text_score=score)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 推理失败：%s", exc)
            return OcrPage(error=f"OCR 推理失败：{exc}", width=w, height=h)

        elapsed = (time.time() - t0) * 1000
        return OcrPage(
            blocks=self._to_blocks(result, score, scale, asset_uid),
            width=w,
            height=h,
            elapsed_ms=elapsed,
            tier=self._tier,
        )

    def detect_and_recognize(
        self, image: Any, *, lang_hint: str | None = None
    ) -> list[ImageTextBlock]:
        """:class:`~novaloc.core.registry.OcrEngine` 协议入口。

        ``lang_hint`` 目前只做日志记录：PP-OCRv6 的中英混排模型
        本身就能处理多语种，不需要按语言换模型。
        """
        if lang_hint:
            log.debug("OCR 语言提示 %s（当前模型已支持中英混排，忽略）", lang_hint)
        return self.read(image).blocks

    def _to_blocks(
        self, result: Any, score: float, scale: float, asset_uid: str
    ) -> list[ImageTextBlock]:
        """把 RapidOCR 的输出转成统一的 :class:`ImageTextBlock`。"""
        txts = getattr(result, "txts", None) or ()
        boxes = getattr(result, "boxes", None)
        scores = getattr(result, "scores", None)
        if boxes is None:
            return []

        inv = 1.0 / scale if scale != 1.0 else 1.0
        out: list[ImageTextBlock] = []
        for i, poly in enumerate(boxes):
            text = str(txts[i]) if i < len(txts) else ""
            if not text.strip():
                continue
            conf = float(scores[i]) if scores is not None and i < len(scores) else 0.0
            if conf < score:
                continue

            pts = np.asarray(poly, dtype=np.float32).reshape(-1, 2) * inv
            xs, ys = pts[:, 0], pts[:, 1]
            x1, y1, x2, y2 = (
                int(round(xs.min())),
                int(round(ys.min())),
                int(round(xs.max())),
                int(round(ys.max())),
            )
            if x2 <= x1 or y2 <= y1:
                continue
            quad = [tuple(pt) for pt in order_quad(pts)]
            angle, bw, bh = quad_geometry(quad)
            vertical = bh > bw * 1.5

            out.append(
                ImageTextBlock(
                    id=f"{asset_uid or 'img'}#{len(out)}",
                    box=(x1, y1, x2, y2),
                    quad=quad,
                    polygon=quad,
                    source=text,
                    confidence=conf,
                    ocr_engine=self.name,
                    style=TextBlockStyle(
                        angle=angle,
                        vertical=vertical,
                        # 字号先按行高估一个初值，精确值由重绘阶段按实际
                        # 墨迹范围重算（OCR 框会带一点padding）
                        font_size=max(8, int(round((y2 - y1) * 0.85))),
                    ),
                )
            )
        return out


# --------------------------------------------------------------------------
# 图像工具（重绘/修补阶段也要用，所以放在模块级）
# --------------------------------------------------------------------------


def as_rgb_array(image: Any) -> tuple[np.ndarray | None, int, int]:
    """统一转成 RGB uint8 ndarray，返回 ``(数组, 宽, 高)``。"""
    if image is None:
        return None, 0, 0
    if isinstance(image, (str, Path)):
        # 不能交给 RapidOCR 自己读路径：它内部用 cv2.imread，
        # 而 OpenCV 不支持非 ASCII 路径，本工具的工作目录常常带中文。
        raw = imread_bgr(image)
        if raw is None:
            log.debug("打开图片失败 %s", image)
            return None, 0, 0
        if raw.ndim == 2:
            raw = np.stack([raw] * 3, axis=-1)
        elif raw.shape[2] == 4:
            raw = raw[:, :, [2, 1, 0]]  # BGRA → RGB
        else:
            raw = raw[:, :, ::-1]  # BGR → RGB
        return np.ascontiguousarray(raw), int(raw.shape[1]), int(raw.shape[0])
    if isinstance(image, np.ndarray):
        arr = image
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        elif arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]
        if arr.ndim != 3 or arr.shape[2] != 3:
            return None, 0, 0
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(arr), int(arr.shape[1]), int(arr.shape[0])
    try:
        rgb = image.convert("RGB")
        return np.asarray(rgb), rgb.width, rgb.height
    except Exception:  # noqa: BLE001
        return None, 0, 0


def resize_rgb(arr: np.ndarray, scale: float) -> np.ndarray:
    import cv2

    h, w = arr.shape[:2]
    return cv2.resize(
        arr,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


__all__ = [
    "PPOcrV6Engine",
    "OcrPage",
    "quad_geometry",
    "order_quad",
    "as_rgb_array",
    "resize_rgb",
]
