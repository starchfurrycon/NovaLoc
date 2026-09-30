"""贴图文字处理：OCR 识别 → 去字修补 → 用真实中文字体重绘 → 贴回。

设计原则（最重要的一条）：
**贴图上的中文一律用真实字体光栅化，绝不用生成式模型"画"文字。**
生成模型画出来的字会缺笔画、串字、糊成一片，本质上是"口口口"的另一种形态。

标准流程：
    识别原文 → 实测字色/底色/描边 → inpaint 抹掉原文字
    → 按原位置与样式渲染中文 → 单应变换贴回任意四边形
"""

from __future__ import annotations

from .inpaint import (
    InpaintResult,
    LamaInpainter,
    build_mask,
    inpaint_boxes,
    load_lama,
    sample_background,
)
from .ocr_ppocrv6 import (
    OcrPage,
    PPOcrV6Engine,
    as_rgb_array,
    order_quad,
    quad_geometry,
    resize_rgb,
)
from .render import (
    BlockRender,
    RenderResult,
    detect_stroke,
    extract_colors,
    fit_font_size,
    measure_text,
    paste_quad,
    render_text_block,
    render_text_block_checked,
    wrap_text,
)
from .service import BlockOutcome, TextureResult, TextureTranslator
from .textgroup import TextGroup, allocate_translation, group_blocks

__all__ = [
    # OCR
    "PPOcrV6Engine",
    "OcrPage",
    "as_rgb_array",
    "order_quad",
    "quad_geometry",
    "resize_rgb",
    # 去字
    "InpaintResult",
    "LamaInpainter",
    "build_mask",
    "inpaint_boxes",
    "load_lama",
    "sample_background",
    # 重绘
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
    # 分组与管线
    "TextGroup",
    "allocate_translation",
    "group_blocks",
    "BlockOutcome",
    "TextureResult",
    "TextureTranslator",
]
