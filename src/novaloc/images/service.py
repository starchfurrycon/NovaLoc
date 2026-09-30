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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
from .ocr_ppocrv6 import PPOcrV6Engine, as_rgb_array
from .render import (
    BlockRender,
    detect_stroke,
    extract_colors,
    paste_quad,
    render_text_block_checked,
)
from .textgroup import TextGroup, allocate_translation, group_blocks

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
        self.calls = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------
    # 依赖惰性加载
    # ------------------------------------------------------------------

    @property
    def ocr(self) -> PPOcrV6Engine:
        if self._ocr is None:
            self._ocr = self.ctx.cache_get("ocr", lambda: PPOcrV6Engine(self.ctx))
        return self._ocr

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

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

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

        # ---- 3. 翻译 ----
        texts = [g.source for g in groups]
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
                        )
                    )
                continue

            for b, piece in allocate_translation(g, translated):
                outcome = self._draw_block(canvas, b, piece, styles.get(b.id), font_path)
                result.outcomes.append(outcome)

        result.image = canvas
        result.total_ms = (time.time() - t_start) * 1000
        return result

    # ------------------------------------------------------------------

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
        for (i, _t), text in zip(pending, results):
            if text:
                out[i] = text
        return out

    def _pick_font(self) -> str:
        """挑一个中文字体用于重绘。

        优先级：配置指定的显示字体 → 系统常见中文字体。
        找不到就抛错 —— 没有中文字体就绝对不该往图里画字。
        """
        from ..fonts.catalog import find_by_family

        want = getattr(self.cfg.font, "display_font", "") or getattr(self.cfg.font, "ui_font", "")
        spec = find_by_family(want) if want else None
        if spec is not None:
            local = Path(getattr(spec, "local_path", "") or "")
            if local.is_file():
                return str(local)

        for cand in (
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ):
            if Path(cand).is_file():
                return cand
        raise RuntimeError("找不到可用的中文字体，拒绝在贴图上绘制中文")

    def _draw_block(
        self,
        canvas: np.ndarray,
        block: ImageTextBlock,
        text: str,
        style: TextBlockStyle | None,
        font_path: str,
    ) -> BlockOutcome:
        """画一块并贴回。任何异常都收敛成 outcome，不打断整图。"""
        out = BlockOutcome(block_id=block.id, source=block.source, target=text)
        if not text.strip():
            out.status = EntryStatus.SKIPPED
            return out
        if style is None:
            style = TextBlockStyle(text_color=(255, 255, 255))

        x1, y1, x2, y2 = block.box
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

        quad = block.quad if block.quad and len(block.quad) == 4 else [
            (x1 + pad_x, y1 + pad_y),
            (x2 - pad_x, y1 + pad_y),
            (x2 - pad_x, y2 - pad_y),
            (x1 + pad_x, y2 - pad_y),
        ]
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
