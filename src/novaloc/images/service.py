"""贴图汉化主管线。

一条贴图的完整处理流程::

    读图 → OCR 识别 → 文本分组合并
         → 交给翻译层（真实模型）
         → 实测原文的字色/底色/描边
         → inpaint 抹掉原文字
         → 用真实中文字体按原样式重绘
         → 单应变换贴回原位置（支持斜排/透视）
         → 产出结果图 + 审校清单

设计约束（都是硬性的）：

1. **贴图上的中文绝不出自生成式模型。** 只用真实字体光栅化。
   生成模型画文字会缺笔画、串字，是"口口口"的另一种形态。
2. **溢出和过小的块必须标出来交给用户。** 宁可让用户看一眼，
   也不能把中文硬画出按钮外面。
3. **原游戏目录只读。** 结果一律写到工作区，不碰原文件。
4. 任何一块失败都不影响其它块 —— 逐块降级，整图不失败。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..core.registry import Context, TranslateItem
from ..models import (
    EntryStatus,
    ImageAsset,
    ImageTextBlock,
    TextBlockStyle,
    TextKind,
    TextLocation,
    TextUnit,
)
from .inpaint import InpaintResult, inpaint_boxes, load_lama
from .io import imread_bgr, imwrite_bgr
from .ocr_ppocrv6 import PPOcrV6Engine
from .render import (
    BlockRender,
    detect_stroke,
    extract_colors,
    paste_quad,
    render_text_block_checked,
)
from .textgroup import allocate_translation, group_blocks

log = logging.getLogger(__name__)

#: 中文渲染时框内留白比例（相对宽/高）
PAD_X_RATIO = 0.03
PAD_Y_RATIO = 0.06

#: 描边宽度上限（像素）。太粗的中文会糊在一起。
MAX_STROKE_WIDTH = 3


@dataclass
class BlockOutcome:
    """单块的最终结果，用于审校与统计。"""

    block_id: str
    source: str
    target: str
    ok: bool = False
    status: EntryStatus = EntryStatus.PENDING
    font_size: int = 0
    overflow: bool = False
    too_small: bool = False
    warnings: list[str] = field(default_factory=list)

    # --- 几何与识别信息：审校页要靠它把文字块画回原图上 ---
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    quad: list[tuple[float, float]] = field(default_factory=list)
    confidence: float = 0.0
    ocr_engine: str = ""


@dataclass
class TextureResult:
    """一张贴图的处理结果。"""

    asset: ImageAsset
    image: np.ndarray | None = None
    """处理后的 BGR 图（None 表示未处理/失败）。"""

    outcomes: list[BlockOutcome] = field(default_factory=list)
    inpaint_method: str = ""
    ocr_ms: float = 0.0
    total_ms: float = 0.0
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    """整图级别的提醒（区别于 `BlockOutcome.warnings` 的逐块提醒）。

    例如"跳过了 N 个疑似幻觉文字块"。放进结果里而不是只写日志，
    是因为这类信息解释了"为什么这张图没被处理" —— 用户需要看得见。
    """

    @property
    def ok(self) -> bool:
        return not self.error and self.image is not None

    @property
    def translated(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def needs_review(self) -> list[BlockOutcome]:
        return [o for o in self.outcomes if o.overflow or o.too_small or o.warnings]

    @property
    def changed(self) -> bool:
        return self.translated > 0


def _crop_quad(image: Any, quad: list[tuple[float, float]], *, pad: int = 3) -> Any:
    """按四边形裁剪出一个**正立的**文字区域。

    直接把外接矩形裁给 VLM 是不行的：斜排文字在外接矩形里仍然
    是斜的，模型读起来更吃力。这里做一次透视校正，把四边形
    拉成水平矩形 —— 相当于"把这块文字摆正了再给模型看"。
    """
    import cv2

    if not quad or len(quad) != 4:
        return None

    h, w = image.shape[:2]
    pts = np.asarray(quad, dtype="float32")

    # 目标宽高取四边形的边长，尽量不缩放（缩放会损失小字信息）
    top = float(np.linalg.norm(pts[1] - pts[0]))
    bottom = float(np.linalg.norm(pts[2] - pts[3]))
    left = float(np.linalg.norm(pts[3] - pts[0]))
    right = float(np.linalg.norm(pts[2] - pts[1]))
    tw = int(round(max(top, bottom)))
    th = int(round(max(left, right)))
    if tw < 2 or th < 2:
        return None

    # 要正立，四边形的顶点顺序必须是"顺时针、左上打头"。
    # RapidOCR 不保证顺序，先按角度排序再旋转到左上打头。
    ordered = _order_quad(pts)
    dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(ordered, dst)
    warped = cv2.warpPerspective(image, m, (tw, th), flags=cv2.INTER_CUBIC)

    # 补一圈边：模型对贴着边缘的字容易漏读
    if pad > 0:
        warped = cv2.copyMakeBorder(
            warped, pad, pad, pad, pad, cv2.BORDER_REPLICATE
        )
    del h, w
    return warped


def _order_quad(pts: Any) -> Any:
    """把四个点排成"顺时针、左上打头"。

    与 ``textgroup.quad_geometry`` 同一套约定：图像坐标 y 向下，
    所以按极角升序排列得到的就是顺时针。
    """
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    ordered = pts[np.argsort(ang)]
    # 旋转到 x+y 最小的点（左上角）打头
    start = int(np.argmin(ordered.sum(axis=1)))
    return np.roll(ordered, -start, axis=0)


def _norm(s: str) -> str:
    """比较用归一化：忽略空白与全半角差异。"""
    import unicodedata

    return unicodedata.normalize("NFKC", str(s or "")).replace(" ", "").replace("\n", "").strip()


#: 单个字母/数字/符号的"文字块"一律判为噪声 —— 见 `_is_plausible_text_block`
_SINGLE_GLYPH_RE = re.compile(r"^[\w\W]$", re.UNICODE)

#: 多字符块但占画面比例超过这个值 → 判为幻觉
_MAX_BLOCK_AREA_FRAC = 0.85


class TextureTranslator:
    """贴图汉化执行器。

    ``translate_fn`` 是"文本 → 译文"的回调，由翻译层提供，
    签名 ``(items: list[TranslateItem], target_lang: str) -> list[str]``。
    这样贴图管线不直接依赖 Ollama，方便单测和替换。
    """

    def __init__(self, ctx: Context, *, translate_fn: Callable[..., list[str]] | None = None) -> None:
        self.ctx = ctx
        self.cfg = ctx.config
        self._translate_fn = translate_fn
        self._ocr: PPOcrV6Engine | None = None
        self._lama: Any = None
        self._lama_tried = False
        self._vlm: Any = None
        self._vlm_tried = False
        #: VLM 裁剪图缓存：裁剪像素 sha1 → 读出的文字。
        #: 键存活于整个 service 实例（跨图片），因为"像素相同 ⇒ 文字相同"
        #: 与图片无关，重复的按钮/图标文字能直接复用。
        self._vlm_cache: dict[str, str] = {}
        self.calls = 0
        self.cache_hits = 0
        self.vlm_reads = 0
        self.vlm_fixes = 0
        self.vlm_cache_hits = 0

    # ------------------------------------------------------------------
    # 依赖惰性加载
    # ------------------------------------------------------------------

    @property
    def ocr(self) -> PPOcrV6Engine:
        if self._ocr is None:
            self._ocr = self.ctx.cache_get("ocr", lambda: PPOcrV6Engine(self.ctx))
        return self._ocr

    @property
    def vlm(self) -> Any:
        """视觉大模型兜底；不可用返回 ``None``（自动跳过兜底）。"""
        if not self._vlm_tried:
            self._vlm_tried = True
            ocr_cfg = getattr(self.cfg, "ocr", None)
            enabled = getattr(ocr_cfg, "vlm_fallback", True) if ocr_cfg else True
            if not enabled:
                self._vlm = None
                return None
            try:
                from ..translate.vision_ollama import OllamaVisionEngine

                eng = OllamaVisionEngine(self.ctx)
                ok, why = eng.available()
                if ok:
                    self._vlm = eng
                else:
                    log.info("视觉兜底不可用，跳过：%s", why)
                    self._vlm = None
            except Exception as exc:  # noqa: BLE001
                log.info("视觉兜底初始化失败，跳过：%s", exc)
                self._vlm = None
        return self._vlm

    @property
    def vlm_threshold(self) -> float:
        ocr_cfg = getattr(self.cfg, "ocr", None)
        if ocr_cfg is not None:
            return float(getattr(ocr_cfg, "vlm_threshold", 0.6))
        img_cfg = getattr(self.cfg, "image", None)
        if img_cfg is not None:
            return float(getattr(img_cfg, "vlm_threshold", 0.6))
        return 0.6

    def _vlm_reconsider(self, image: Any, block: Any) -> str:
        """对低置信度的块，让 VLM 重读一遍文字内容。

        **只用来纠正文字，绝不用来改框**：VLM 的定位精度比专用 OCR
        差两个数量级（实测旋转文本 Hmean 2.1 vs 93.8），
        一旦让它决定位置，排版就毁了。所以这里只取文本，
        四边形坐标原样保留。

        带**裁剪图缓存**：同一个按钮/图标在一张图（甚至多张图）里
        重复出现时，裁剪出的像素完全一样，VLM 的回答必然一样。
        缓存键用像素内容的 sha1 而不是文本，因为此时我们**还不知道
        文本是什么**（知道就不用问了）—— 而"像素完全相同"是"文字相同"
        的充分条件。命中的代价从一次 `/api/chat` 降为一次哈希。
        """
        vlm = self.vlm
        if vlm is None:
            return ""
        quad = getattr(block, "quad", None) or []
        if not quad:
            return ""

        try:
            crop = _crop_quad(image, quad)
        except Exception as exc:  # noqa: BLE001
            log.debug("裁剪待重读区域失败：%s", exc)
            return ""
        if crop is None or getattr(crop, "size", 0) == 0:
            return ""

        key = self._vlm_crop_key(crop)
        if key is not None and key in self._vlm_cache:
            self.vlm_cache_hits += 1
            return self._vlm_cache[key]

        self.vlm_reads += 1
        text = vlm.read_text(crop, hint="这是游戏贴图里的文字，请只输出文字本身")
        out = (text or "").strip()
        # 空结果也缓存：读不出来时再问一次通常还是读不出来，
        # 而重复的失败重问正是最浪费的那部分。
        if key is not None:
            self._vlm_cache[key] = out
        return out

    def _vlm_crop_key(self, crop: Any) -> str | None:
        """裁剪图的稳定性缓存键；算不出来时返回 ``None``（退化为不缓存）。

        用 PNG 编码而不是 ``tobytes()`` 作首选：裁剪尺寸或通道数稍有
        差异时，裸内存缓冲仍有极小概率撞上同样的字节序列，而 PNG 带
        尺寸与通道信息，且无损可复现。
        """
        try:
            import hashlib  # noqa: PLC0415

            buf: bytes | None = None
            try:
                import cv2  # noqa: PLC0415

                ok, enc = cv2.imencode(".png", crop)
                if ok:
                    buf = bytes(enc.tobytes())
            except Exception:  # noqa: BLE001 - 编码失败就用裸字节
                buf = None
            if not buf:
                buf = memoryview(crop.tobytes()).tobytes()
            if not buf:
                return None
            return hashlib.sha1(buf).hexdigest()
        except Exception as exc:  # noqa: BLE001
            log.debug("计算 VLM 缓存键失败（本次不缓存）：%s", exc)
            return None

    @property
    def lama(self) -> Any:
        """LaMa 修补器；不可用时返回 ``None``（自动降级到纯色/OpenCV）。"""
        if not self._lama_tried:
            self._lama_tried = True
            model = getattr(self.cfg, "data_root", "") or ""
            path = None
            if model:
                cand = Path(model) / "models" / "lama" / "lama.pt"
                path = cand if cand.exists() else None
            self._lama = load_lama(path)
        return self._lama

    def _reread_group(self, image: Any, group: Any) -> str:
        """对整组的包围盒重读一次，返回拼接后的文字（读不出则空串）。

        只在组内块互相重叠时调用 —— 那时 :attr:`TextGroup.source` 的拼接
        结果会把重叠区的文字数两遍。

        几何一律**不用**重读结果（`PP-OCRv6` 在定位上碾压其它方案，
        而这里是同一套 OCR，没理由换框），只取文字。
        多块时按 x 排序拼接，避免顺序抖动导致译文不稳定。
        """
        x1, y1, x2, y2 = group.box
        h, w = image.shape[0], image.shape[1]
        # 留一点边：紧贴笔画裁会切掉抗锯齿边缘，反而降低识别率
        pad = max(2, int(max(1, y2 - y1) * 0.08))
        crop = image[
            max(0, y1 - pad):min(h, y2 + pad),
            max(0, x1 - pad):min(w, x2 + pad),
        ]
        if crop.size == 0:
            return ""
        try:
            page = self.ocr.read(crop, asset_uid=f"{group.blocks[0].id}-reread")
        except Exception as exc:  # noqa: BLE001
            log.debug("重叠组重读失败：%s", exc)
            return ""
        if page.error:
            log.debug("重叠组重读报错：%s", page.error)
            return ""
        parts = [b.source.strip() for b in sorted(page.blocks, key=lambda b: b.box[0])]
        return " ".join(p for p in parts if p).strip()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _is_plausible_text_block(self, block: Any, img_w: int, img_h: int) -> bool:
        """这个"文字块"是否值得翻译并重绘。**宁可漏，不可错。**

        ## 为什么需要它（真实游戏实测）

        在一个真实 RPG Maker MV 游戏的 `www/img/pictures/stand_images/`
        （**人物立绘**，386x680）上跑 OCR，识别出单个字符并被自动"翻译"：

        ==================== ======== ======== ==========
        图片                 识别     置信度   区域占整图
        ==================== ======== ======== ==========
        damage_nomal.png     '2'      0.596    48%
        victory_near_pinch   'S'      0.598    17%
        victory_nomal.png    '2'      0.718    41%
        wait_pinch.png       '0'      0.546    40%
        ==================== ======== ======== ==========

        其中 `'0'` 被翻译层翻成了 `'VS'` —— 一个**误读**经过翻译被加固成
        一个像样的词，再重绘到立绘上。用户永远不会知道那里本来没有字。
        （本项目的硬约束是"贴图文字绝不来自生成模型"，但误读同样属于
        "往画面上加了原本不存在的东西"。）

        ## 两条判据，缺一不可

        1. **单字符一律不接受**。上表四条全是单字符。游戏 UI 里真有独立
           数字的情况（分数、金币）几乎都以变量渲染在引擎文本层，不是
           烘焙进贴图；为极少数例外放过这类块，代价就是上面的 `VS`。
        2. **多字符块占画面超过 85% 判为幻觉**。整张图都是一块"文字"
           在立绘上必然是把画面本身当成了字。

        注意判据 2 的阈值定得**很宽**（85%），因为实测
        `system/Loading.png`（400x100）的 `Now Loading...` 一个块就占
        **44%** —— 那张图本身就是一条 loading 条。占比不能用来卡真文字，
        卡住立绘幻觉的是判据 1。这就是为什么两条各自独立、阈值都很保守。
        """
        text = str(getattr(block, "source", "") or "")
        if not text.strip():
            return False

        # 判据 1：单字符
        if len(text.strip()) < 2:
            return False

        # 判据 2：区域占比（很宽的阈值，只挡"整张图当成一块字"）
        box = getattr(block, "box", None)
        if box and len(box) == 4 and img_w > 0 and img_h > 0:
            bw = max(0, int(box[2]) - int(box[0]))
            bh = max(0, int(box[3]) - int(box[1]))
            if bw <= 0 or bh <= 0:
                return False
            if (bw * bh) / float(img_w * img_h) > _MAX_BLOCK_AREA_FRAC:
                return False
        return True

    def process(
        self,
        image_path: str | Path,
        *,
        asset_uid: str = "",
        target_lang: str | None = None,
        existing: dict[str, str] | None = None,
        glossary: list[Any] | None = None,
        context_lines: list[str] | None = None,
        dry_run: bool = False,
    ) -> TextureResult:
        """处理一张贴图。

        ``existing`` 是"原文 → 译文"的既有记忆，命中就不必再问模型
        （贴图里重复出现的 "OK"、"Cancel" 很常见，能省大量时间）。
        """
        t_start = time.time()
        p = Path(image_path)
        lang = target_lang or getattr(self.cfg.translate, "target_lang", "zh-Hans")
        asset = ImageAsset(uid=asset_uid or p.stem, path=str(p))
        result = TextureResult(asset=asset)

        raw = imread_bgr(p)
        if raw is None:
            result.error = f"无法读取图片：{p}"
            return result
        if raw.ndim == 2:
            raw = np.stack([raw] * 3, axis=-1)
        elif raw.shape[2] == 4:
            raw = raw[:, :, :3]
        asset.width, asset.height = raw.shape[1], raw.shape[0]

        # ---- 1. OCR ----
        t0 = time.time()
        page = self.ocr.read(p, asset_uid=asset.uid)
        result.ocr_ms = (time.time() - t0) * 1000
        if page.error:
            result.error = page.error
            return result
        blocks = page.blocks
        min_box = int(getattr(self.cfg.ocr, "min_box_size", 6))
        blocks = [
            b for b in blocks
            if (b.box[2] - b.box[0]) >= min_box and (b.box[3] - b.box[1]) >= min_box
        ]

        # ---- 1a. 挡住"立绘上读出的单字符"这类幻觉 ----
        # 实测：真实游戏的人物立绘被读出 '2' / 'S' / '0'，
        # 其中 '0' 被翻译成 'VS' 并重绘进画面。见 `_is_plausible_text_block`。
        kept: list[Any] = []
        dropped: list[str] = []
        for b in blocks:
            if self._is_plausible_text_block(b, asset.width, asset.height):
                kept.append(b)
            else:
                dropped.append(str(getattr(b, "source", ""))[:20])
        if dropped:
            result.warnings.append(
                f"跳过 {len(dropped)} 个疑似幻觉文字块（单字符或占画面过大）："
                + "、".join(repr(d) for d in dropped[:8])
            )
        blocks = kept

        # ---- 1b. 视觉兜底：只对低置信度的块重读文字，不改框 ----
        # 专用 OCR 在定位上碾压 VLM（旋转文本 Hmean 93.8 vs 2.1），
        # 所以框一律用 OCR 的；但美术字/描边字 OCR 会给低置信度结果，
        # 这时让 VLM 重读一遍**内容**，能救回不少贴图。
        blocks = self._vlm_rescue(raw, blocks)

        asset.blocks = blocks
        asset.analyzed = True
        if not blocks:
            # 没文字，原图返回，不算失败
            result.image = raw
            result.total_ms = (time.time() - t_start) * 1000
            return result

        # ---- 2. 分组 ----
        groups = group_blocks(blocks)
        log.debug("贴图 %s：OCR %d 块 → %d 组", p.name, len(blocks), len(groups))

        # ---- 2b. 重叠组重读 ----
        # 组内块一旦互相重叠，就**不能**用拼接的文本去翻译：重叠区的文字
        # 会数两遍。实测 'NEW' + 'V GAME' 拼出 'NEW V GAME'，
        # 凭空多一个 'V' —— 这个文本送进翻译层，译文必然是错的。
        #
        # 改为对整组的包围盒**重读一次**。代价是每组一次额外 OCR
        # （实测约 0.2 秒），但重叠组很少见，而错译是永久留在产物里的。
        # 重读得到的框不用（几何一律以原 OCR 为准），只用它的文字。
        texts = [g.source for g in groups]
        for gi, g in enumerate(groups):
            if not g.has_overlap:
                continue
            reread = self._reread_group(raw, g)
            if reread:
                log.debug(
                    "贴图 %s：第 %d 组存在重叠块，重读 %r → %r",
                    p.name, gi, texts[gi], reread,
                )
                texts[gi] = reread
            else:
                log.debug(
                    "贴图 %s：第 %d 组重叠块重读失败，保留拼接文本 %r",
                    p.name, gi, texts[gi],
                )

        # ---- 3. 翻译 ----
        translations = self._translate_texts(
            texts, lang, existing or {}, glossary or [], context_lines or [], p, dry_run
        )
        if dry_run:
            result.image = raw
            result.outcomes = [
                BlockOutcome(block_id=b.id, source=b.source, target="")
                for b in blocks
            ]
            result.total_ms = (time.time() - t_start) * 1000
            return result

        # ---- 4. 实测样式（必须在去字之前做，字还在图里） ----
        styles: dict[str, TextBlockStyle] = {}
        for b in blocks:
            fg, bg, _sep = extract_colors(raw, b.box)
            st = b.style.model_copy(deep=True)
            st.text_color = fg
            stroke, _sw = detect_stroke(raw, b.box, fg)
            if stroke:
                st.stroke_color = stroke
                st.stroke_width = min(MAX_STROKE_WIDTH, 2)
            st.font_size = None  # 交给自动收缩，避免原字号撑破中文
            styles[b.id] = st

        # ---- 5. 去字 ----
        boxes_all = [b.box for b in blocks]
        quads_all = [list(b.quad) for b in blocks if b.quad and len(b.quad) == 4]
        dilate = int(getattr(self.cfg.image, "inpaint_dilate", 3))
        wiped: InpaintResult = inpaint_boxes(
            raw, boxes_all, dilate=dilate, quads=quads_all, lama=self.lama
        )
        result.inpaint_method = wiped.method
        canvas = wiped.image.copy()

        # ---- 6. 逐块重绘并贴回 ----
        font_path = self._pick_font()
        for gi, g in enumerate(groups):
            translated = translations.get(gi, "")
            if not translated.strip():
                for b in g.blocks:
                    result.outcomes.append(
                        BlockOutcome(
                            block_id=b.id, source=b.source, target="",
                            status=EntryStatus.SKIPPED,
                            warnings=["无译文"] if texts[gi].strip() else [],
                            box=tuple(b.box),  # type: ignore[arg-type]
                            quad=[(float(x), float(y)) for x, y in (b.quad or [])],
                            confidence=float(b.confidence or 0.0),
                            ocr_engine=b.ocr_engine or "",
                        )
                    )
                continue

            # 重叠组只能整体画一次：组内各块的框互相交叠，
            # 逐块画会互相覆盖，产物就是"中文 + 残留外文"的花字。
            draw_box = tuple(g.box) if g.has_overlap else None  # type: ignore[arg-type]
            for b, piece in allocate_translation(g, translated):
                outcome = self._draw_block(
                    canvas, b, piece, styles.get(b.id), font_path, draw_box=draw_box
                )
                result.outcomes.append(outcome)

        result.image = canvas
        result.total_ms = (time.time() - t_start) * 1000
        return result

    # ------------------------------------------------------------------

    def _vlm_rescue(self, image: Any, blocks: list[Any]) -> list[Any]:
        """对低置信度的块用 VLM 重读。返回（可能被修正过的）块列表。

        设计要点：

        * **阈值来自配置**（``ocr.vlm_threshold``，默认 0.6），不是拍脑袋；
        * **只改文字，不改框** —— 四边形原样保留；
        * **只在 VLM 给出非空结果时替换**，读不出来就保留 OCR 原结果，
          避免"兜底把好结果搞坏"；
        * 单块失败不影响整张图。
        """
        if not blocks or self.vlm is None:
            return blocks
        thr = self.vlm_threshold
        low = [b for b in blocks if float(getattr(b, "confidence", 0.0) or 0.0) < thr]
        if not low:
            return blocks

        log.debug("有 %d/%d 块置信度低于 %.2f，尝试视觉兜底", len(low), len(blocks), thr)
        for b in low:
            try:
                better = self._vlm_reconsider(image, b)
            except Exception as exc:  # noqa: BLE001
                log.debug("视觉兜底失败（保留 OCR 结果）：%s", exc)
                continue
            if not better:
                continue
            # 归一化后再比较：VLM 常把空格/换行处理得不一样，
            # 但内容相同的话没必要替换（也不会改善）
            if _norm(better) == _norm(b.source):
                continue
            b.source = better
            b.confidence = max(float(getattr(b, "confidence", 0.0) or 0.0), thr)
            warnings = list(getattr(b, "warnings", []) or [])
            if "vlm_reread" not in warnings:
                warnings.append("vlm_reread")
            b.warnings = warnings
            self.vlm_fixes += 1
        return blocks

    def _translate_texts(
        self,
        texts: list[str],
        target_lang: str,
        existing: dict[str, str],
        glossary: list[Any],
        context_lines: list[str],
        path: Path,
        dry_run: bool,
    ) -> dict[int, str]:
        """调翻译层；带既有译文命中，避免重复问模型。"""
        out: dict[int, str] = {}
        pending: list[tuple[int, str]] = []

        lookup = {k.strip().lower(): v for k, v in existing.items()}
        for i, t in enumerate(texts):
            hit = lookup.get(t.strip().lower())
            if hit:
                out[i] = hit
                self.cache_hits += 1
            else:
                pending.append((i, t))

        if dry_run or not pending:
            return out
        if self._translate_fn is None:
            log.warning("未提供翻译回调，贴图 %s 只做识别不做翻译", path.name)
            return out

        items: list[TranslateItem] = []
        for i, t in pending:
            items.append(
                TranslateItem(
                    unit=TextUnit(
                        uid=f"{path.stem}#{i}",
                        source=t,
                        kind=TextKind.IMAGE_TEXT,
                        location=TextLocation(file=str(path), pointer=f"block@{i}"),
                    ),
                    glossary=glossary,
                    context_lines=context_lines,
                )
            )
        self.calls += 1
        results = self._translate_fn(items, target_lang)
        # 翻译提供者返回的是 ``TranslationEntry`` 列表，而本模块内部只关心
        # 纯文本。早先这里直接把结果当字符串用，导致 ``.strip()`` 抛
        # AttributeError，异常又被上层吞掉，表现为"一张贴图都没汉化"，
        # 极难排查。所以这里统一做一次归一化，两种形态都接受。
        #
        # 数量必须对齐：``zip`` 在长度不等时**静默截断**，
        # 于是译文会被贴到**错误的文字块**上 —— 图上出现别的按钮的台词，
        # 而且不报任何错。内置 provider 会预填等长列表（见
        # ``translate_batch`` 的 ``out = [self._blank(it) for it in items]``），
        # 但外部/自定义回调没有这个保证，所以这里显式检查并告警。
        if len(results) != len(pending):
            log.warning(
                "翻译回调返回 %d 条，但请求了 %d 条（贴图 %s）："
                "长度不一致，已按最小长度对齐，多出的文字块保持未翻译",
                len(results), len(pending), path.name,
            )
        for (i, _t), res in zip(pending, results, strict=False):
            text = self._entry_text(res)
            if text:
                out[i] = text
        return out

    @staticmethod
    def _entry_text(res: Any) -> str:
        """把翻译结果归一化成纯文本（兼容 ``TranslationEntry`` 与 ``str``）。"""
        if res is None:
            return ""
        if isinstance(res, str):
            return res.strip()
        for attr in ("target", "text", "translation"):
            val = getattr(res, attr, None)
            if isinstance(val, str):
                return val.strip()
        return str(res).strip()

    def _pick_font(self) -> str:
        """挑一个中文字体用于重绘，返回**字体文件路径**。

        优先级：
        1. 配置里 ``font.ui_font`` / ``font.display_font`` / ``font.dialog_font``
           指定的家族 —— 先在本机已装字体里按家族名找，再走字体目录
           （必要时下载可再分发的 OFL 字体）；
        2. 本机已装的中文字体（系统字体，只在本机使用、不打包）；
        3. 字体缓存里的可再分发中文字体。

        找不到就抛错 —— **没有中文字体就绝对不该往图里画字**，
        否则画出来的是方块甚至空白。

        早先这里读 ``FontSpec.local_path`` 这个**不存在的字段**，
        ``getattr`` 带默认值所以永远拿到空串，目录查找分支从不执行，
        实际总是落到下面那串硬编码的 Windows 路径上 —— 非 Windows
        直接报"找不到可用的中文字体"。改为走 ``FontService`` 解析。
        """
        from ..fonts.catalog import find_by_family
        from ..fonts.service import FontService

        svc = FontService(self.ctx)

        # half 指的是字重倾向：标题/按钮常用粗体
        want = (
            getattr(self.cfg.font, "display_font", "")
            or getattr(self.cfg.font, "ui_font", "")
            or getattr(self.cfg.font, "dialog_font", "")
        )

        tried: list[str] = []
        if want:
            spec = find_by_family(want)
            if spec is not None:
                tried.append(spec.id)
                # 本机已装？
                for fam in (spec.family, spec.display_zh, want):
                    if not fam:
                        continue
                    local = svc._find_system_font(fam)
                    if local is not None:
                        return str(local)
                # 缓存 / 可再分发下载
                try:
                    got = svc.ensure_font(spec)
                except Exception as exc:  # noqa: BLE001
                    log.info("按配置获取字体 %s 失败：%s", spec.id, exc)
                    got = None
                if got is not None and Path(got).is_file():
                    return str(got)

        # 本机系统字体（不打包，仅本机渲染使用）。
        # 用 find_preferred_cjk_font **一次**选完，不再逐个家族名调用 ——
        # 后者即使有索引缓存也要遍历十几遍，贴在每张图上就是白付的开销。
        local = svc.find_preferred_cjk_font([
            "Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "DengXian", "等线",
            "Source Han Sans SC", "Noto Sans CJK SC", "Noto Sans SC",
            "PingFang SC", "WenQuanYi Micro Hei", "LXGW WenKai GB Screen",
            "LXGW Neo XiHei", "MS Gothic", "Yu Gothic",
        ])
        if local is not None:
            return str(local)

        # 最后：字体缓存里任何可用的中文字体（含补充候选池）
        for cand in svc.supplement_candidates(None):
            if Path(cand).is_file():
                return str(cand)

        raise RuntimeError(
            "找不到可用的中文字体，拒绝在贴图上绘制中文"
            + (f"（已尝试：{', '.join(tried)}）" if tried else "")
            + "。请先在设置里指定 font.ui_font，或运行 `novaloc fonts list` 查看可下载字体。"
        )

    def _draw_block(
        self,
        canvas: np.ndarray,
        block: ImageTextBlock,
        text: str,
        style: TextBlockStyle | None,
        font_path: str,
        *,
        draw_box: tuple[int, int, int, int] | None = None,
    ) -> BlockOutcome:
        """画一块并贴回。任何异常都收敛成 outcome，不打断整图。

        ``draw_box`` 省略时用 ``block.box``。**重叠组**会显式传入整组的
        包围盒：那时组内各块的框互相交叠，逐块画必然互相覆盖，只能
        当成一个整体画一次。注意 ``outcome.box`` 始终是**该块自己**的
        框（审校页要在原图上标出每块的位置），只有绘制几何用 ``draw_box``。
        """
        out = BlockOutcome(
            block_id=block.id,
            source=block.source,
            target=text,
            # 几何信息一路带着走：审校页要靠它在原图上画框，
            # 丢了它用户就只能看到一堆文本、不知道对应图上哪里。
            box=tuple(block.box),  # type: ignore[arg-type]
            quad=[(float(a), float(b)) for a, b in (block.quad or [])],
            confidence=float(block.confidence or 0.0),
            ocr_engine=block.ocr_engine or "",
        )
        if not text.strip():
            out.status = EntryStatus.SKIPPED
            return out
        if style is None:
            style = TextBlockStyle(text_color=(255, 255, 255))

        x1, y1, x2, y2 = draw_box or block.box
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        pad_x = max(2, int(bw * PAD_X_RATIO))
        pad_y = max(1, int(bh * PAD_Y_RATIO))
        rw, rh = max(8, bw - pad_x * 2), max(8, bh - pad_y * 2)

        try:
            br: BlockRender = render_text_block_checked(text, (rw, rh), font_path, style)
        except Exception as exc:  # noqa: BLE001
            out.status = EntryStatus.FAILED
            out.warnings.append(f"渲染异常：{exc}")
            return out

        if br.rgba is None:
            out.status = EntryStatus.FAILED
            out.warnings.append(f"渲染失败：{br.detail}")
            return out

        out.font_size = br.font_size
        out.overflow = br.overflow
        out.too_small = br.too_small
        if br.overflow:
            out.warnings.append("译文放不下（已缩到最小字号）")
        if br.too_small:
            out.warnings.append(f"字号过小（{br.font_size}px）难以辨认")

        # 只有"块的四边形正好等于绘制框"时才能拿它做透视贴回；
        # 重叠组用整组包围盒，块自己的四边形跟它不匹配，得走矩形分支。
        quad = (
            block.quad
            if (draw_box is None and block.quad and len(block.quad) == 4)
            else [
                (x1 + pad_x, y1 + pad_y),
                (x2 - pad_x, y1 + pad_y),
                (x2 - pad_x, y2 - pad_y),
                (x1 + pad_x, y2 - pad_y),
            ]
        )
        try:
            if not paste_quad(canvas, br.rgba, list(quad)):
                out.status = EntryStatus.FAILED
                out.warnings.append("贴回失败")
                return out
        except Exception as exc:  # noqa: BLE001
            out.status = EntryStatus.FAILED
            out.warnings.append(f"贴回异常：{exc}")
            return out

        out.ok = True
        out.status = EntryStatus.TRANSLATED
        return out

    # ------------------------------------------------------------------

    def process_many(
        self,
        paths: list[str | Path],
        *,
        out_dir: str | Path,
        existing: dict[str, str] | None = None,
        target_lang: str | None = None,
        on_progress: Callable[[int, int, TextureResult], None] | None = None,
    ) -> list[TextureResult]:
        """批量处理贴图，结果写到 ``out_dir``（原目录保持只读）。"""
        out_root = Path(out_dir)
        results: list[TextureResult] = []
        total = len(paths)
        for i, p in enumerate(paths, 1):
            res = self.process(p, existing=existing, target_lang=target_lang)
            if res.image is not None:
                dest = out_root / Path(p).name
                if not imwrite_bgr(dest, res.image):
                    res.error = res.error or f"写入失败：{dest}"
            results.append(res)
            if on_progress:
                try:
                    on_progress(i, total, res)
                except Exception:  # noqa: BLE001
                    log.debug("进度回调异常", exc_info=True)
        return results


__all__ = ["BlockOutcome", "TextureResult", "TextureTranslator"]
