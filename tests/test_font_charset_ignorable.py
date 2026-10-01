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


# ----------------------------------------------------------------------
# 五、★ 变体选择符（U+FE0F）也必须算"本来就没有字形"
# ----------------------------------------------------------------------

#: 译文里真实出现过的组合记号。`✌️`/`❤️` 这类是"基础字符 + U+FE0F"。
VARIATION_SELECTORS = ["\U0000FE0E", "\U0000FE0F", "\U0000FE00", "\U000E0100"]


@pytest.mark.parametrize("ch", VARIATION_SELECTORS)
def test_variation_selectors_are_blank_by_design(ch: str) -> None:
    r"""变体选择符没有自己的字形，渲染成空是**正确**的。

    ## 真实事故：一个看不见的字符吓住了每一个字体

    `U+FE0F`（把前一个字符选成 emoji 呈现）出现在译文里，
    而没有任何字体"提供"它 ⇒ `plan.still_missing` 里永远留着它 ⇒
    **每一个**游戏字体都报

        字符集里有 1 个字符没有任何候选字体能提供，无法生成完整字体：️

    （注意冒号后面**看着是空的** —— 那个字符本来就不可见。）

    后果有三层：

    1. 每个字体都白跑一遍"剔除 → 重试"的弯路；
    2. `ship.otf` 的**真实**失败原因（缺 `glyf`/`loca`，CFF 字体
       无法合并）被这条噪音盖住，差点没查出来；
    3. 如果 `still_missing` 的判据更严一点，它会让**整轮字体适配失败** ——
       也就是"因为一个玩家看不见的字符，让全部中文变口口口"。

    `unicodedata.category(U+FE0F)` 是 `Mn`（Nonspacing Mark），
    而原来的判据只认 `("Zs","Zl","Zp","Cc","Cf")`，所以漏了。
    """
    from novaloc.fonts.qa import _is_blank_by_design

    assert _is_blank_by_design(ch) is True, f"U+{ord(ch):04X} 没有字形却被当成缺字"


@pytest.mark.parametrize("ch", ["\U0000FE10", "A", "的", "，", "♥", "①", "…"])
def test_blank_by_design_does_not_swallow_real_chars(ch: str) -> None:
    r"""★ **反例**：真需要渲染的字符一个都不能被判成"空白"。

    这个方向错了比漏掉变体选择符更糟 —— 会让真需要的字
    不进"必翻"集合，游戏里直接显示口口口。

    特意包含 `U+FE10`（垂直形式的逗号，**紧邻**变体选择符区间的
    真字形）和 `①`（`No` 类别，容易被"非字母就算空白"这类粗糙判据误伤）。

    注：组合记号（如 `U+0301` 组合尖音符）**确实**属于"本来就没有字形"
    （它是接在别的字上的），所以**不在**这个反例表里 ——
    它由 `test_variation_selectors_are_blank_by_design` 那侧的同类断言覆盖。
    """
    from novaloc.fonts.qa import _is_blank_by_design

    assert _is_blank_by_design(ch) is False, f"{ch!r} 被误判成「本来就没有字形」"


@pytest.mark.parametrize("ch", ["\u0301", "\u0300", "\u20dd"])
def test_combining_marks_are_blank_by_design(ch: str) -> None:
    r"""组合记号（`Mn`/`Me`）没有独立字形，渲染成空是正确的。

    它们必须和变体选择符一起被放行 —— 否则同样是
    "因为一个看不见的字符让每个字体都报缺字"。
    """
    from novaloc.fonts.qa import _is_blank_by_design

    assert _is_blank_by_design(ch) is True


# ----------------------------------------------------------------------
# 五之二、★ 两个判据**故意不同**的地方（想合并它们会炸掉真实需求）
# ----------------------------------------------------------------------

#: 这两类字符上，"需要字形吗"与"渲染成空白可以接受吗"的答案**必须不同**。
MUST_BE_IN_CHARSET_BUT_RENDER_BLANK = [
    "\u3000",  # 全角空格：占宽度 ⇒ 字符集里该有它
    "\u00a0",  # NBSP：同上
    "\u0e48",  # 泰文声调符号：有宽度、要渲染 ⇒ 不能从字符集剔掉
    "\u0e35",  # 泰文元音 II：同上
    "\u064b",  # 阿拉伯文元音符号：同上
]


@pytest.mark.parametrize("ch", MUST_BE_IN_CHARSET_BUT_RENDER_BLANK)
def test_ignorable_and_blank_by_design_are_not_the_same_question(ch: str) -> None:
    r"""★ **契约**：这两个判据在空格与泰文/阿拉伯记号上**必须**给出不同答案。

    ## 为什么会想去合并它们（以及合并后炸了什么）

    起因是 `U+FE0F` 在两个判据下结论不一致，于是造成一条永远消不掉的
    "缺 1 个字符"噪音提示，把 `ship.otf` 的真实失败原因盖住了。
    直觉修法是"让它们共用一份实现"。

    照做之后**两条既有测试同时变红**：

    * `test_ui_safe_chars_contain_no_ignorables` —— `UI_SAFE_CHARS`
      特意列了 `\u00a0`/`\u3000`，合并后它们被判成"可忽略"；
    * `test_incident_chars_would_be_source_only` —— 事故字符集里的
      泰文记号（`\u0e48` `\u0e35` …）被判成"可忽略"而剔出字符集。

    原因是这两个问题的答案**本来就不一样**：

    | 问题 | 谁答 | 答"是"的含义 |
    |---|---|---|
    | 这个字符**需要字形**吗？ | `is_ignorable` | 不需要 ⇒ 不该进字符集 |
    | 渲染成空白**可以接受**吗？ | `_is_blank_by_design` | 可以 ⇒ 不算缺字 |

    全角空格占宽度（要在字符集里），但渲染出来确实是空白（QA 不该报）。
    泰文声调符号有宽度要渲染（要在字符集里），渲染差异极小（QA 放过）。

    正确的修法是**只把 `U+FE0F` 这一处**补进 `is_ignorable`
    （它确实没有字形），而不是把两个函数合成一个。
    """
    from novaloc.fonts.qa import _is_blank_by_design

    assert is_ignorable(ch) is False, (
        f"{ch!r} 被判成「不需要字形」 ⇒ 会被踢出字符集，"
        "而它事实上是要渲染的（占宽度）"
    )
    assert _is_blank_by_design(ch) is True, (
        f"{ch!r} 渲染成空白被当成了缺字 ⇒ 又会制造噪音"
    )


def test_ui_safe_chars_survive_the_charset_filter() -> None:
    """`UI_SAFE_CHARS` 里那两个空格必须真的留在字符集里（真实需求）。"""
    cs = build_required_charset(translated_texts=["你好"], include_ui_safe=True)
    assert "\u3000" in cs, "全角空格被踢出字符集 —— 界面里会变口口口"
    assert "\u00a0" in cs


# ----------------------------------------------------------------------
# 六、★ `summary` 是 property，不是方法（调用它会让整轮字体适配崩掉）
# ----------------------------------------------------------------------

def test_font_summaries_are_properties_not_methods() -> None:
    r"""`FontQAReport.summary` / `PatchResult.summary` 都是 **property**。

    ## 真实事故

    `service.py` 在"QA 未通过"这条错误路径上写的是
    `res.qa.summary()` —— 而 `summary` 是 property，取值已经是 `str`，
    再调一次就是 `TypeError: 'str' object is not callable`。

    后果不是"少一条报错信息"，而是**整轮字体适配崩掉**：
    实测 DemonsRoots（MV）跑到第 4 个字体（`koin.ttf`）时抛异常，
    前 3 个字体**已经注入成功**却因为异常没被记进 `patches.json`，
    `apply` 于是没有任何字体可回写 —— **游戏里满屏口口口**。

    这条路径**只在 QA 真的不通过时才会走到**，所以正常项目里永远测不到；
    而 `koin.ttf` 恰好是一个 base 字体本身就有空白字形的真实案例。

    断言方式刻意选择"取值必须是 `str`"而不是"不能调用" ——
    前者同时钉住了"它是 property"，也钉住了"它给的是人能读的文本"。
    """
    from novaloc.fonts.qa import FontQAReport
    from novaloc.fonts.service import PatchResult

    qa = FontQAReport(path="x.ttf")
    assert isinstance(qa.summary, str), "FontQAReport.summary 必须是 property（取值即 str）"
    pr = PatchResult()
    assert isinstance(pr.summary, str), "PatchResult.summary 必须是 property（取值即 str）"


def test_patch_result_summary_reports_qa_issues() -> None:
    """`summary` 的文本必须真的能反映 QA 状态（不是一句套话）。"""
    from novaloc.fonts.qa import FontQAReport, GlyphIssue
    from novaloc.fonts.service import PatchResult

    qa = FontQAReport(path="x.ttf", ok=False, loaded=True, checked=100)
    qa.blank = ["你", "魔"]
    qa.issues = [GlyphIssue("你", ord("你"), "blank", "渲染为空白")]
    pr = PatchResult(qa=qa)
    assert "QA" in pr.summary
    assert "1" in pr.summary, f"没报出问题条数：{pr.summary!r}"
