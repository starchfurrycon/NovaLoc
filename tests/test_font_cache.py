"""验证系统字体索引缓存会在"新装字体"后失效。

背景：`_system_font_index()` 用"各字体文件的 (名字, mtime, 大小)"摘要做
缓存键。早先用的是**目录自身的 mtime**。实测在本机 NTFS 上往目录复制
文件**确实**会更新目录 mtime，所以旧写法不是坏的；但文件级摘要更精确
（能覆盖"文件被替换/改名但目录 mtime 恰好没变"的情况），且实测代价可忽略。

这个测试不依赖任何外部字体：用 fontTools 现场生成一个最小 TTF。
（早先版本拿 `tests/fixtures/wire_mv/fonts/mplus-1m-regular.ttf` 当捐助字体，
但那是个占位文件、并不是有效字体，于是"新字体被索引到"永远为假 ——
一个恒假的断言。）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from fontTools.fontBuilder import FontBuilder  # noqa: E402
from fontTools.pens.ttGlyphPen import TTGlyphPen  # noqa: E402

from novaloc.fonts import coverage as cov  # noqa: E402
from novaloc.fonts import service as svc  # noqa: E402


def make_minimal_ttf(path: Path, family: str, ch: str = "A") -> None:
    """生成一个只有单个字母的最小有效 TTF。"""
    upem = 1000
    fb = FontBuilder(upem, isTTF=True)
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    glyphs = {".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()}
    fb.setupGlyphOrder([".notdef", "A"])
    fb.setupCharacterMap({ord(ch): "A"})
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({".notdef": (600, 0), "A": (600, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "psName": family.replace(" ", "") + "-Regular",
            "fullName": family,
            "version": "1.0",
        }
    )
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.save(str(path))


class _FakeDirs:
    """把 system_font_dirs() 临时换成一个可写目录。"""

    def __init__(self, d: Path) -> None:
        self.d = d
        self._orig = None

    def __enter__(self):
        self._orig = cov.system_font_dirs
        cov.system_font_dirs = lambda: [self.d]
        return self

    def __exit__(self, *exc):
        cov.system_font_dirs = self._orig
        return False


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory(prefix="nvfonts_") as td:
        d = Path(td)
        make_minimal_ttf(d / "donor.ttf", "NovaLoc Donor")

        with _FakeDirs(d):
            svc._SYSTEM_FONT_INDEX.clear()
            first = svc._system_font_index()
            key1 = next(iter(svc._SYSTEM_FONT_INDEX))
            print(f"  初始扫描：{len(first)} 个字体")
            print(f"    家族：{[f['family'] for f in first]}")
            ok &= len(first) >= 1 and any(x["path"].name == "donor.ttf" for x in first)

            # 模拟"用户装了新字体"
            make_minimal_ttf(d / "newfont.ttf", "NovaLoc Added")
            second = svc._system_font_index()
            key2 = next(iter(svc._SYSTEM_FONT_INDEX))
            print(f"  加入 newfont.ttf 后：{len(second)} 个字体")
            changed = key1 != key2
            print(f"  缓存键变化：{'✅' if changed else '❌ 缓存未失效'}")
            ok &= changed
            found = any(x["path"].name == "newfont.ttf" for x in second)
            print(f"  新字体被索引到：{'✅' if found else '❌'}")
            ok &= found
            names = sorted(x["family"] for x in second)
            print(f"    家族：{names}")

            # 状态未变时重复调用必须命中缓存（键不变）
            third = svc._system_font_index()
            key3 = next(iter(svc._SYSTEM_FONT_INDEX))
            stable = key2 == key3 and len(third) == len(second)
            print(f"  重复调用命中缓存：{'✅' if stable else '❌'}")
            ok &= stable

            # refresh=True 必须无视缓存重扫
            svc._SYSTEM_FONT_INDEX.clear()
            forced = svc._system_font_index(refresh=True)
            print(f"  refresh=True 重扫：{len(forced)} 个字体 "
                  f"{'✅' if len(forced) == len(second) else '❌'}")
            ok &= len(forced) == len(second)

    print(f"\n结论：{'✅ 字体索引缓存行为正确' if ok else '❌ 有问题'}")
    return 0 if ok else 1


pytestmark = [pytest.mark.needs_fonts]


def test_suite() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
