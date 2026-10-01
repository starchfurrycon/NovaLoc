r"""★ 字符集分两层：**真实文本需要的** vs **工具猜的**。

## 事故经过

`UI_SAFE_CHARS` 是一张"游戏界面**一定**会渲染到的字符"清单，
里面列了几百个装饰符号。默认它会整个并进所需字符集，
而需求字符集缺字会触发**硬失败**：

```
❌ ok=False  错误=字符集里有 4 个字符没有任何候选字体能提供，
   无法生成完整字体：※‥′″
```

问题在于这几个符号**本机任何一个 CJK 字体都没有**
（`lxgw-*`、`simhei`、`msyh`、`Deng`、`simsun`、`seguisym` 全试过）。
于是一整轮字体适配直接中止 —— 尽管数过之后它们在
**65570 条真实文本里出现 0 次**（981 个场景，一次都没有）。

**一句都用不到的装饰符号把整个流程拦死了。**

## 为什么不能靠"这个字符影响多少条"来判

原有代码已经有一条补救路径：补不上时，把"影响条目数 ≤ 3"的字符剔掉重试。
它的**结论**对（那几个符号确实只影响 0~1 条），但**判据是启发式的**：

* 一个**真的需要**的生僻字（人名用字）如果碰巧只出现在 1 条文本里，
  也会被同一个判据剔掉 ⇒ 那一行照样显示口口口；
* 判据的阈值（`≤ 3`）是拍的，换了项目规模就未必合适。

而我们其实**知道**答案 —— `build_charset_tiers` 就是按"这个字符来自
真实文本还是来自猜测清单"算出来的。用知道的事实，不用猜的启发式。

## 契约

| 层 | 来源 | 缺字后果 |
|---|---|---|
| `required`（严格） | 译文 + 原文 + `extra` | **硬失败**（会显示口口口） |
| `optional`（宽松） | `UI_SAFE_CHARS` | 只记警告，**不失败** |

⚠️ 但 `required` 层**仍然必须硬失败** ——
不能因为"补不上就算了"，那是把"保证不出现口口口"的契约废掉。
所以下面的测试里，真字符缺失必须照样报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.fonts.charset import (  # noqa: E402
    UI_SAFE_CHARS,
    CharsetCoverageError,
    assert_plannable,
    build_charset_tiers,
    build_required_charset,
    plan_charset,
)

FIXTURE_FONTS = ROOT / "tests" / "fixtures" / "fonts"


# ---------------------------------------------------------------------------
# 一、两层怎么分
# ---------------------------------------------------------------------------


def test_real_text_chars_are_strict() -> None:
    """真实文本里的字符必须在**严格**层。"""
    _all, strict = build_charset_tiers(translated_texts=["你好世界，这是一句中文。"])
    for ch in "你好世界这是一句中文，。":
        assert ch in strict, f"{ch!r} 是真实文本里的字符，必须在严格层"


def test_speculative_chars_are_optional() -> None:
    """★ 只有猜测清单里有、真实文本里没有的字符，**不许**进严格层。

    这一条就是本次事故的回归测试。
    """
    _all, strict = build_charset_tiers(translated_texts=["你好"])
    for ch in "※‥′″‰":
        assert ch in set(_all), f"{ch!r} 应在全部字符集里（猜测清单包含它）"
        assert ch not in set(strict), (
            f"{ch!r} 只出现在猜测清单里、真实文本里没有，"
            "不许进严格层 —— 为它硬失败会让整轮字体适配中止"
        )


def test_source_text_chars_are_strict_too() -> None:
    """**原文**里的字符也算严格 —— 没被翻译的串仍要用这个字体渲染。"""
    _all, strict = build_charset_tiers(
        translated_texts=["你好"], source_texts=["ABC 123"]
    )
    assert "A" in strict and "1" in strict


def test_extra_chars_are_strict() -> None:
    _all, strict = build_charset_tiers(translated_texts=["你好"], extra="甲乙")
    assert "甲" in strict and "乙" in strict


def test_all_is_superset_of_strict() -> None:
    allv, strict = build_charset_tiers(
        translated_texts=["你好"], source_texts=["World"], extra="甲乙"
    )
    assert set(strict) <= set(allv)


def test_ui_safe_off_makes_tiers_identical() -> None:
    """关掉猜测清单时两层应当完全一致（没有可放弃的东西）。"""
    allv, strict = build_charset_tiers(
        translated_texts=["你好"], include_ui_safe=False
    )
    assert allv == strict


def test_backward_compatible_wrapper() -> None:
    """`build_required_charset` 仍然返回**全部**字符（既有调用点不变）。"""
    got = build_required_charset(translated_texts=["你好"])
    allv, _strict = build_charset_tiers(translated_texts=["你好"])
    assert got == allv
    # 猜测清单确实还在里面（否则"全部"就没意义了）
    assert set(UI_SAFE_CHARS) & set(got)


# ---------------------------------------------------------------------------
# 二、规划器对两层的处理
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not FIXTURE_FONTS.is_dir(), reason="缺少字体夹具"
)
def test_optional_missing_does_not_fail() -> None:
    """★ 宽松层缺字**不许**导致失败。

    用一个真实字体当基准，把几个"任何字体都没有"的字符放进 ``optional`` ——
    规划必须成功。
    """
    fonts = sorted(FIXTURE_FONTS.glob("*.tt[fc]")) + sorted(
        FIXTURE_FONTS.glob("*.ot[fc]")
    )
    if not fonts:
        pytest.skip("字体夹具目录为空")
    base = fonts[0]
    # 这几个字符在常见 CJK 字体里都没有
    exotic = "შინ\u20e3"
    # 基准字体已经覆盖的字符当"严格"层（保证严格层一定可满足）
    from novaloc.fonts.coverage import load_font_info

    info = load_font_info(base)
    if info is None or not info.cmap:
        pytest.skip("夹具字体读不出 cmap")
    strict = "ABCabc123"
    strict = "".join(c for c in strict if info.has_char(c))
    if not strict:
        pytest.skip("夹具字体连 ASCII 都不覆盖")

    plan = plan_charset(strict, base, [], optional=exotic)
    assert plan.ok, f"宽松层缺字不该失败，实际 still_missing={plan.still_missing}"
    assert not plan.still_missing
    assert set(plan.optional_missing) <= set(exotic)


@pytest.mark.skipif(not FIXTURE_FONTS.is_dir(), reason="缺少字体夹具")
def test_strict_missing_still_fails() -> None:
    """★★ 反向：**严格**层缺字必须照样硬失败。

    修假阳性最容易犯的错就是把判据削到什么都不报。
    "保证不出现口口口"是核心契约，不能在修这个 bug 时被废掉。
    """
    fonts = sorted(FIXTURE_FONTS.glob("*.tt[fc]")) + sorted(
        FIXTURE_FONTS.glob("*.ot[fc]")
    )
    if not fonts:
        pytest.skip("字体夹具目录为空")
    base = fonts[0]
    # 没有任何候选字体 ⇒ 严格层的字符必然缺
    plan = plan_charset("你好世界ꙮ", base, [])
    assert not plan.ok, "严格层缺字却判成功了"
    with pytest.raises(CharsetCoverageError):
        assert_plannable(plan)


def test_assert_plannable_ignores_optional() -> None:
    """`assert_plannable` 只看严格层。"""
    from novaloc.fonts.charset import CharsetPlan

    plan = CharsetPlan(required="abc")
    plan.still_missing = []
    plan.optional_missing = ["※", "‥"]
    assert_plannable(plan)  # 不该抛
