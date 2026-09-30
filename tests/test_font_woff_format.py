"""补丁后的字体必须**保持原包装格式**（``.woff`` 不能变成裸 sfnt）。

## 问题

真实 RPG Maker MZ 游戏里的字体是 ``.woff``（原文件头 ``77 4f 46 46`` = ``wOFF``），
但补丁产物是**裸 sfnt**（头 ``00 01 00 00``）—— 格式被换掉了。

为什么这很危险：游戏用
``new FontFace(family, "url(fonts/xxx.woff)")`` 加载字体，
**浏览器按文件内容识别格式、不看扩展名**，所以裸 sfnt 内容
"通常"也能加载 —— 这也是它没在第一时间暴露的原因。
但把一个 ``.woff`` 扩展名的文件换成别的格式内容是没必要的风险：
有加载器看 MIME/扩展名，有工具链按扩展名解析。

``fontTools`` 支持写回 woff（只要把 ``flavor`` 设回去），代价为零，
那就没有理由不保持原样。

## 修法

读原文件的头 4 字节判断包装格式（``wOFF`` / ``wOF2``），
在 ``save()`` 之前把 ``flavor`` 设回去。只在产物扩展名是
``.woff`` / ``.woff2`` 时生效 —— 免得把 ``.ttf`` 也包成 woff。

## 顺带修掉的另一处

`subset_font_for_chars` 以前把 ``0x09/0x0A/0x0D`` 硬编码进"要保留的码点"，
这跟 `is_ignorable`（换行不需要字形）的约定直接冲突，
会让覆盖率显示永远差几个字。已改成不加。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fontTools.ttLib import TTFont  # noqa: E402

from novaloc.fonts.merge import (  # noqa: E402
    _apply_flavor,
    _as_ttf_path,
    _detect_flavor,
    merge_fonts_multi,
    subset_font_for_chars,
)

#: 本机真实游戏里的 WOFF 字体（MZ 用 woff）
_REAL_WOFF = Path(r"D:\NovaLocData\real\BeyondPortal\fonts\mplus-1m-regular.woff")
#: 本机真实游戏里的 TTF 字体（MV 用 ttf）
_REAL_TTF = Path(r"D:\NovaLocData\real\ElfLifia\www\fonts\mplus-1m-regular.ttf")

requires_woff = pytest.mark.skipif(
    not _REAL_WOFF.is_file(), reason="本机没有真实 WOFF 字体样本"
)


def _find_supplement() -> Path | None:
    for p in (
        Path(r"D:\NovaLoc\fonts\cache\lxgw-wenkai-screen.ttf"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ):
        if p.is_file():
            return p
    return None


# ----------------------------------------------------------------------
# 一、格式探测
# ----------------------------------------------------------------------

def test_detect_flavor_on_real_woff() -> None:
    if not _REAL_WOFF.is_file():
        pytest.skip("无真实 WOFF")
    assert _detect_flavor(_REAL_WOFF) == "woff"


def test_detect_flavor_on_real_ttf() -> None:
    if not _REAL_TTF.is_file():
        pytest.skip("无真实 TTF")
    assert _detect_flavor(_REAL_TTF) is None


def test_detect_flavor_recognises_magic_bytes(tmp_path: Path) -> None:
    """按**内容**判断，不看扩展名。"""
    for magic, want in ((b"wOFF", "woff"), (b"wOF2", "woff2"), (b"\x00\x01\x00\x00", None)):
        p = tmp_path / f"x{magic.hex()}.bin"
        p.write_bytes(magic + b"\x00" * 60)
        assert _detect_flavor(p) == want, f"{magic!r} 判定错误"


def test_detect_flavor_missing_file_is_none(tmp_path: Path) -> None:
    assert _detect_flavor(tmp_path / "nope.woff") is None


def test_detect_flavor_short_file_is_none(tmp_path: Path) -> None:
    p = tmp_path / "tiny.woff"
    p.write_bytes(b"wO")
    assert _detect_flavor(p) is None


# ----------------------------------------------------------------------
# 二、真实 WOFF 的往返（关键证据）
# ----------------------------------------------------------------------

@requires_woff
def test_read_save_woff_keeps_woff_header(tmp_path: Path) -> None:
    """**核心回归**：读一个 woff、保住 flavor、存回去，头必须还是 ``wOFF``。

    旧行为（不设 flavor）会写出 ``00010000``，格式就被换掉了。
    """
    out = tmp_path / "out.woff"
    f = TTFont(_REAL_WOFF)
    _apply_flavor(f, _detect_flavor(_REAL_WOFF), out)
    f.save(out)
    assert out.read_bytes()[:4] == b"wOFF", (
        f"产物头是 {out.read_bytes()[:4].hex()}，不是 wOFF —— 包装格式被换掉了"
    )
    # 还能读回来，且 cmap 完整
    back = TTFont(out)
    assert getattr(back, "flavor", None) == "woff"
    assert len(back.getBestCmap()) == len(TTFont(_REAL_WOFF).getBestCmap())


@requires_woff
def test_merge_multi_preserves_woff_format(tmp_path: Path) -> None:
    """**核心断言**：真实的 merge_mult i流程产物必须是 WOFF。

    直接跑合并，不走流水线 —— 这条最接近真实失败场景。
    """
    sup = _find_supplement()
    if sup is None:
        pytest.skip("本机没有可用的补充中文字体")

    out = tmp_path / "merged.woff"
    rep = merge_fonts_multi(
        _REAL_WOFF, [sup], out, required_chars="你好世界等级金币", extra_symbols=" "
    )
    assert rep.ok, f"合并失败：{rep.warnings}"
    assert out.read_bytes()[:4] == b"wOFF", (
        f"合并产物头是 {out.read_bytes()[:4].hex()} —— "
        ".woff 输入却输出了裸 sfnt，格式被换掉了"
    )
    assert getattr(TTFont(out), "flavor", None) == "woff"


@requires_woff
def test_merge_multi_ttf_stays_sfnt(tmp_path: Path) -> None:
    """**反向契约**：TTF 输入不能凭空被包成 WOFF。"""
    if not _REAL_TTF.is_file():
        pytest.skip("无真实 TTF")
    sup = _find_supplement()
    if sup is None:
        pytest.skip("本机没有可用的补充中文字体")

    out = tmp_path / "merged.ttf"
    rep = merge_fonts_multi(
        _REAL_TTF, [sup], out, required_chars="你好世界", extra_symbols=" "
    )
    assert rep.ok, f"合并失败：{rep.warnings}"
    assert out.read_bytes()[:4] == b"\x00\x01\x00\x00", (
        "TTF 输入被包成了 WOFF —— 不该动它的格式"
    )
    assert getattr(TTFont(out), "flavor", None) is None


@requires_woff
def test_already_complete_base_also_preserves_format(tmp_path: Path) -> None:
    """**边界**：基础字体已全覆盖时会走"直接导出"的早退分支。

    那条分支也 save 一次，同样会丢格式 —— 旧代码在 `merge_fonts_multi`
    和 `merge_fonts` 里各有一处。这条专测早退分支。
    """
    # 只要求 ASCII，原字体本来就全都有 → 走早退
    out = tmp_path / "early.woff"
    rep = merge_fonts_multi(_REAL_WOFF, [], out, required_chars="ABCabc123", extra_symbols=" ")
    assert rep.ok
    assert "完整覆盖" in " ".join(rep.warnings), f"没走早退分支：{rep.warnings}"
    assert out.read_bytes()[:4] == b"wOFF", (
        f"早退分支产物头是 {out.read_bytes()[:4].hex()} —— 格式还是被换了"
    )


# ----------------------------------------------------------------------
# 三、_apply_flavor 的边界
# ----------------------------------------------------------------------

def test_apply_flavor_ignores_ttf_output_path(tmp_path: Path) -> None:
    """产物扩展名是 .ttf 时不该设 flavor（免得给 TTF 包上 woff）。"""
    f = TTFont()
    f.flavor = "woff"  # 故意先设脏
    got = _apply_flavor(f, "woff", tmp_path / "x.ttf")
    assert got is None and f.flavor is None


def test_apply_flavor_none_input_clears_flavor(tmp_path: Path) -> None:
    f = TTFont()
    f.flavor = "woff"
    assert _apply_flavor(f, None, tmp_path / "x.woff") is None
    assert f.flavor is None


def test_apply_flavor_woff2_without_brotli_degrades(tmp_path: Path) -> None:
    """没装 brotli 时 WOFF2 写不出来 —— 要降级并返回 None，不能抛异常。"""
    try:
        import brotli  # noqa: F401

        pytest.skip("本机装了 brotli，无法测降级路径")
    except ImportError:
        pass
    f = TTFont()
    path = tmp_path / "x.woff2"
    assert _apply_flavor(f, "woff2", path) is None
    assert f.flavor is None


def test_as_ttf_path_normalises_collections(tmp_path: Path) -> None:
    assert _as_ttf_path(tmp_path / "x.ttc").suffix == ".ttf"
    assert _as_ttf_path(tmp_path / "x.otc").suffix == ".ttf"
    assert _as_ttf_path(tmp_path / "x.woff").suffix == ".woff"


# ----------------------------------------------------------------------
# 四、subset_font_for_chars 不再硬塞换行码点
# ----------------------------------------------------------------------

@requires_woff
def test_subset_does_not_force_newline_codepoints(tmp_path: Path) -> None:
    """**回归**：不该把 ``0x09/0x0A/0x0D`` 当成"必须保留的字形"。

    这跟 `is_ignorable` 的约定冲突（换行不需要字形），
    而且会让覆盖率显示永远差几个字。

    注意：这里用的是 ``mplus`` 原字体**本来就有**的字符。
    它是个日文拉丁字体，``你好`` 这类汉字它根本没有 ——
    拿汉字来断言会误判成"子集把字裁掉了"，其实是原字体就没这个字形。
    """
    out = tmp_path / "sub.woff"
    wanted = "Now Loading"
    subset_font_for_chars(_REAL_WOFF, out, wanted, face_index=0)
    cmap = TTFont(out).getBestCmap()
    for cp in (0x09, 0x0A, 0x0D):
        assert cp not in cmap, (
            f"码点 U+{cp:04X} 被硬塞进了子集 —— 换行不需要字形"
        )
    missing = [c for c in wanted if c != " " and ord(c) not in cmap]
    assert not missing, f"这些字符被裁掉了：{missing}"


@requires_woff
def test_subset_preserves_woff_format(tmp_path: Path) -> None:
    out = tmp_path / "sub2.woff"
    subset_font_for_chars(_REAL_WOFF, out, "Now Loading", face_index=0)
    assert out.read_bytes()[:4] == b"wOFF", (
        f"子集产物头是 {out.read_bytes()[:4].hex()} —— 格式被换掉了"
    )
