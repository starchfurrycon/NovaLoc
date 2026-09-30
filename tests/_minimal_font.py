"""从 Pillow 自带的字体里抽出真正的字形轮廓，做成一个可读的测试字体。

## 为什么不能是"方块字形"

第一版最小字体给每个字符都画了同一个方块。它能被 PIL 加载、能画上墨迹，
所以**静态检查全绿** —— 但 OCR 一看就认不出来：`NEW GAME` 40px 时
"墨迹占 88.4% 的像素"，等于一整块实心矩形。后果是贴图汉化报告
"1/1 张贴图已汉化（0 处文字）"，端到端测试因此失败。

**这正是本项目最怕的失败模式**：看起来成功，实际什么都没做。

## 做法

Pillow 的 `load_default()` 返回的是一个**真正的矢量字体**
（Aileron Regular，嵌在 Pillow 包内）。用 fontTools 把它读出来，
只保留 ASCII 里需要的字形，重新组装成一个独立 TTF。
这样得到的字形是**真字母形状**，OCR 能认，而不需要往仓库里
放任何字体文件，也不依赖本机装了什么字体。
"""

from __future__ import annotations

import io
from pathlib import Path

from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from PIL import ImageFont

ASCII = [chr(c) for c in range(0x20, 0x7F)]


def _pil_default_ttf() -> TTFont:
    """取出 Pillow 内置字体的 TTFont 对象。"""
    f = ImageFont.load_default()
    buf = f.path
    if not isinstance(buf, io.BytesIO):
        raise RuntimeError(f"Pillow 默认字体不是内存中的 TTF：{buf!r}")
    return TTFont(io.BytesIO(buf.getvalue()))


def build_minimal_ttf(path: Path, *, upem: int | None = None) -> Path:
    """写出一个含可打印 ASCII 的**真实字形**测试字体，返回该路径。

    字形轮廓来自 Pillow 内置字体（Aileron，OFL），不做任何重采样 ——
    只是把需要的字符挑出来重新组装，所以形状是真的，OCR 能认。
    """
    src = _pil_default_ttf()
    src_upem = src["head"].unitsPerEm
    target_upem = upem or src_upem

    src_cmap = src.getBestCmap()
    src_glyphset = src.getGlyphSet()
    src_hmtx = src["hmtx"]

    # 收集可用字符
    have = [(c, src_cmap[ord(c)]) for c in ASCII if ord(c) in src_cmap]
    if len(have) < 80:
        raise RuntimeError(f"Pillow 内置字体缺字符：只有 {len(have)}/{len(ASCII)}")

    scale = target_upem / src_upem

    font = TTFont()
    font.setGlyphOrder([".notdef", *[f"c{ord(c):04x}" for c, _ in have]])
    # 字形顺序在 TTFont 里由 glyphOrder 决定
    glyf = {}
    metrics = {}

    empty = TTGlyphPen(None).glyph()
    glyf[".notdef"] = empty
    metrics[".notdef"] = (int(src_hmtx[".notdef"][0] * scale), 0)

    for ch, gname in have:
        pen = TTGlyphPen(src_glyphset)
        try:
            src_glyphset[gname].draw(pen)
            g = pen.glyph()
        except Exception:  # noqa: BLE001
            g = TTGlyphPen(None).glyph()
        glyf[f"c{ord(ch):04x}"] = g
        adv, lsb = src_hmtx[gname]
        metrics[f"c{ord(ch):04x}"] = (int(adv * scale), int(lsb * scale))

    # 用 fontBuilder 组装，保证各表自洽（比手写 glyf/loca/head 稳得多）
    from fontTools.fontBuilder import FontBuilder

    fb = FontBuilder(target_upem, isTTF=True)
    fb.setupGlyphOrder(font.getGlyphOrder())
    fb.setupCharacterMap({ord(c): f"c{ord(c):04x}" for c, _ in have})
    fb.setupGlyf(glyf)
    fb.setupHorizontalMetrics(metrics)

    src_hhea = src["hhea"]
    fb.setupHorizontalHeader(
        ascent=int(src_hhea.ascent * scale),
        descent=int(src_hhea.descent * scale),
        lineGap=int(getattr(src_hhea, "lineGap", 0) * scale),
    )
    fb.setupNameTable({
        "familyName": "NovaLoc Test Sans",
        "styleName": "Regular",
        "psName": "NovaLocTestSans-Regular",
    })
    src_os2 = src["OS/2"]
    fb.setupOS2(
        sTypoAscender=int(src_os2.sTypoAscender * scale),
        sTypoDescender=int(src_os2.sTypoDescender * scale),
        sTypoLineGap=int(getattr(src_os2, "sTypoLineGap", 0) * scale),
        usWinAscent=int(src_os2.usWinAscent * scale),
        usWinDescent=int(src_os2.usWinDescent * scale),
    )
    fb.setupPost()
    fb.font["head"].unitsPerEm = target_upem
    fb.save(str(path))
    return path


if __name__ == "__main__":
    import sys
    import tempfile

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp()) / "t.ttf"
    build_minimal_ttf(out)
    print("写出:", out, out.stat().st_size, "bytes")
