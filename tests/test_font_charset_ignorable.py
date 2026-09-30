"""换行/制表/零宽字符 **不能** 算成"字体缺字"。

## 这个 bug 有多致命（真实游戏实测）

真实 RPG Maker MZ 游戏上跑字体阶段，**两个字体补丁全部失败、整个流水线中止**：

    为 M+ 1m 注入中文字形（当前覆盖 73.2%）…
    ✗ ❌ 字体补丁失败：字体合并未成功产出
    ✗ 2 个字体补丁全部失败。为避免游戏内出现口口口，已中止流程。

报错信息看着像"字体不够覆盖"，实际原因是**一个换行符**：

* 译文里天然带 ``\\n``，`build_required_charset` 逐字符汇总时把它收了进去；
* 没有任何字体有 ``\\n`` 的字形，于是合并后 ``missing_chars='\\n'``；
* 覆盖率停在 **99.934%**（549 字里"缺"1 个），判定 ``>= 0.999`` 不成立；
* 流水线按"缺字就硬失败"的契约中止 —— 而这个契约本身是对的，
  错的是把 ``\\n`` 当成了"字"。

所以修法是**在源头过滤**：字符集里根本不该出现不需要字形的字符。
另外把判定规则做成共享的（`fonts/textutil.py`），
让"汇总字符集 / 算覆盖率 / 算仍缺哪些字"三处用**同一套**规则 ——
以前 `coverage_ratio` 的分母和 `missing` 的分子各算各的，
页面上显示的覆盖率甚至和 `merge.py` 报的不是同一个数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.fonts.charset import (  # noqa: E402
    UI_SAFE_CHARS,
    build_required_charset,
    is_ignorable,
    plan_charset,
)
from novaloc.fonts.coverage import FontInfo  # noqa: E402
from novaloc.fonts.textutil import NON_RENDERING  # noqa: E402

NL, TAB, CR, ZWSP, ZWNJ, ZWJ, BOM = "\n", "\t", "\r", "\u200b", "\u200c", "\u200d", "\ufeff"
IGNORABLES = [NL, TAB, CR, ZWSP, ZWNJ, ZWJ, BOM]


# ----------------------------------------------------------------------
# 一、判定本身
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ch", IGNORABLES)
def test_is_ignorable_true_for_control_and_zero_width(ch: str) -> None:
    assert is_ignorable(ch) is True


@pytest.mark.parametrize("ch", list("aA0你。！？、…—♥♪①"))
def test_is_ignorable_false_for_real_glyphs(ch: str) -> None:
    """**反例**：真正要渲染的字符一个都不能被判成"可忽略"。

    这个方向错了比漏过滤更糟 —— 会让真需要的字不进字符集，
    游戏里直接显示口口口。
    """
    assert is_ignorable(ch) is False


def test_is_ignorable_handles_empty_string() -> None:
    assert is_ignorable("") is True


def test_non_rendering_constant_is_not_empty() -> None:
    assert len(NON_RENDERING) == 7
    assert NL in NON_RENDERING and BOM in NON_RENDERING


# ----------------------------------------------------------------------
# 二、源头过滤：字符集里不该出现它们
# ----------------------------------------------------------------------

def test_build_charset_drops_newlines_from_translations() -> None:
    """**核心回归**：译文里的 ``\\n`` 不能进字符集。

    这条在旧代码下必然失败 —— 而它正是让真实游戏字体阶段中止的原因。
    """
    cs = build_required_charset(translated_texts=["你好\n世界", "第二行\r\n结束"])
    for ch in IGNORABLES:
        assert ch not in cs, f"{ch!r} 不该出现在字符集里"
    assert "你" in cs and "界" in cs


def test_build_charset_drops_newlines_from_source_texts() -> None:
    """原文也要过滤 —— 没被翻译的串同样会带换行。"""
    cs = build_required_charset(source_texts=["Line one\nLine two"])
    assert NL not in cs
    assert "L" in cs


def test_build_charset_keeps_all_real_chars() -> None:
    """过滤只该拿走不需要字形的，别的必须一个不少。"""
    text = "ABCabc123你好，世界！？…—♥"
    cs = build_required_charset(translated_texts=[text])
    for ch in text:
        assert ch in cs, f"{ch!r} 被误删了"


def test_ui_safe_chars_contain_no_ignorables() -> None:
    """`UI_SAFE_CHARS` 本身就得是干净的（它是硬编码常量，容易手滑）。"""
    bad = [c for c in UI_SAFE_CHARS if is_ignorable(c)]
    assert not bad, f"UI_SAFE_CHARS 里混进了不可渲染字符：{bad!r}"


def test_build_charset_with_only_ignorables_is_empty() -> None:
    cs = build_required_charset(translated_texts=["\n\r\t"], include_ui_safe=False)
    assert cs == ""


# ----------------------------------------------------------------------
# 三、覆盖率：分子分母必须用同一套过滤
# ----------------------------------------------------------------------

def _info(cmap_cps: set[int]) -> FontInfo:
    """造一个只关心 cmap 的 FontInfo（字段名以 `coverage.py` 为准）。"""
    return FontInfo(
        path=Path("t.ttf"),
        face_index=0,
        family="t",
        subfamily="Regular",
        full_name="t",
        version="1.0",
        num_glyphs=len(cmap_cps),
        is_cff=False,
        units_per_em=1000,
        cmap={cp: f"g{cp}" for cp in cmap_cps},
    )


def test_missing_ignores_non_rendering_chars() -> None:
    """**核心回归**：``\\n`` 不算缺字。

    旧实现会返回 ``['\\n']``，于是 `missing_chars` 非空、判定失败。
    """
    info = _info({ord("你"), ord("好")})
    miss = info.missing("你好\n")
    assert NL not in miss, f"换行被当成了缺字：{miss!r}"
    assert miss == []


def test_missing_still_reports_real_missing_chars() -> None:
    """真的缺字必须照报，不能因为过滤把真缺的也吞掉。"""
    info = _info({ord("你")})
    assert info.missing("你好") == ["好"]


def test_missing_without_cmap_also_filters() -> None:
    info = _info(set())
    miss = info.missing("你\n好\t")
    assert NL not in miss and TAB not in miss
    assert set(miss) == {"你", "好"}


def test_coverage_ratio_is_one_with_trailing_newline() -> None:
    """**核心回归**：尾部换行不能让覆盖率低于 1.0。

    旧实现：分子把 ``\\n`` 算成缺（cmap 里没有），分母也算进总数，
    于是 2 个真字符 + 1 个换行 → 2/3 = 66.7%，判定必然失败。
    """
    info = _info({ord("你"), ord("好")})
    assert info.coverage_ratio("你好\n") == pytest.approx(1.0)


def test_coverage_ratio_accounts_for_real_missing() -> None:
    info = _info({ord("你")})
    # 两个真字符里覆盖 1 个 → 0.5（换行不参与）
    assert info.coverage_ratio("你好\n") == pytest.approx(0.5)


def test_coverage_ratio_all_ignorable_is_one() -> None:
    """全是不可渲染字符时，视作"无需求" → 100%，不能除以 0。"""
    assert _info({ord("你")}).coverage_ratio("\n\r\t") == pytest.approx(1.0)


def test_coverage_ratio_empty_is_one() -> None:
    assert _info({ord("你")}).coverage_ratio("") == pytest.approx(1.0)


def test_missing_and_ratio_agree() -> None:
    """**一致性契约**：两个方法必须对"缺了几个"给出兼容的结论。

    这条是在防"分子分母各算各的"这类 bug 再次出现。
    """
    info = _info({ord("你"), ord("好")})
    chars = "你好世界\n\t"
    real = {c for c in set(chars) if not is_ignorable(c)}
    miss = set(info.missing(chars))
    assert len(miss) + round(info.coverage_ratio(chars) * len(real)) == len(real)


# ----------------------------------------------------------------------
# 四、真实尺寸的端到端：不再因为换行中止
# ----------------------------------------------------------------------

def test_plan_charset_never_includes_ignorables() -> None:
    """`plan_charset` 也要干净（它自己有一道过滤，早于本次修复就有）。"""
    req = build_required_charset(translated_texts=["你好\n世界"])
    plan = plan_charset(req, base_font=None, candidates=[])
    for ch in IGNORABLES:
        assert ch not in plan.required
        assert ch not in plan.still_missing


@pytest.mark.parametrize("n", [1, 10, 100])
def test_no_ignorable_survives_any_charset_size(n: int) -> None:
    text = "".join(f"行{i}\n" for i in range(n))
    cs = build_required_charset(translated_texts=[text], include_ui_safe=False)
    assert NL not in cs
    assert len(cs) == len({c for c in text if c != NL})
