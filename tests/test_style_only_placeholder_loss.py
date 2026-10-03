r"""★ 回归：**只丢样式码**（颜色/停顿/图标）不该判死，只丢内容码才判死。

## 这条修复解决了什么（实测）

真实 RPG Maker 对话大量长这样：

    \c[17]Lynn:\n\c[0]Oh! ♡Oh! ♡\nOops, oops♡This guy is not good♡

`\c[0]`/`\c[17]` 只决定**颜色**，玩家一个字都看不到它。
而 `translategemma:4b` **经常把它丢掉**：

    _call_single('\c[0]Hmm...♡')       → '嗯…♡'        ← 颜色码没了
    translate_batch(['\c[0]Hmm...♡'])  → '\c[0]嗯…♡'   ← 保住了

原先 `check_placeholders` 报 `placeholder_count:1->0`，而它在**致命**清单里
⇒ 整条判死。在逐行救援路径上被放大（`.scratch/_why_perline_fails.py` 实测）：

    修复前拒绝原因汇总：3 call ／ **2 guard:placeholder_count:1->0** ／ 1 ok
    修复后拒绝原因汇总：4 ok ／ 2 call        ← 救回率 1/6 → 4/6

## 判据的**结构**性质（不是阈值）

| 记号 | 性质 | 丢了 |
| --- | --- | --- |
| `\c[0]` `\i[4]` `\|` `<color=…>` `[b]` `{w=0.5}` | **纯样式**，玩家看不到 | 放行（游戏里显示默认颜色） |
| `\V[1]` `%s` `\n` `\N[1]` `\C[6]`（带实义？不，C 是颜色） | 占一个**实义位置** | **判死** |

⚠️ 关键安全边界：**只要混进一个内容类记号就照旧判死**，
而且**多出任何记号也判死**（那可能是模型自造的垃圾）。

⚠️ 本模块**不**尝试"把颜色码补回原位置"：实测位置偏差中位数 0.196、
P90 0.640（`.scratch/_colors_only_count.py`），按比例插会把颜色插进
半个词中间。丢掉的样式码就让它保持丢失。
"""

from __future__ import annotations

import pytest

from novaloc.translate.guards import (
    guard,
    is_style_only_placeholder,
    placeholders_lost,
    style_only_placeholder_loss,
)


# ---------------------------------------------------------------------------
# 1) 单记号分类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token",
    [
        r"\c[0]",
        r"\c[17]",
        r"\C[6]",
        r"\i[4]",
        r"\I[4]",
        r"\|",
        r"\.",
        r"\!",
        r"\>",
        r"\<",
        r"\^",
        r"\$",
        "<color=#ff0000>",
        "</color>",
        "[b]",
        "[/b]",
        "{w=0.5}",
        "{}",
    ],
)
def test_style_tokens_are_recognised(token: str) -> None:
    """纯样式记号必须被认出来。"""
    assert is_style_only_placeholder(token), f"{token!r} 应当算纯样式"


@pytest.mark.parametrize(
    "token",
    [
        r"\V[1]",  # 变量 —— 玩家看到的是变量的值
        r"\N[1]",  # 角色名
        r"\v[10]",
        r"\n",  # 换行 —— 行结构
        r"\n[1]",
        "%s",  # printf 参数
        "%1",
        r"\$1",
        "文本",
        "",
    ],
)
def test_content_tokens_are_not_style(token: str) -> None:
    """**内容类**记号绝不能被当成样式。

    ⚠️ 这是安全边界：把它们放行就等于"丢变量/丢换行也写回游戏"。
    特别注意 `\\$`（样式）与 `\\$1`（内容）只差一个数字，
    以及 `%s` 不属于任何 `\\` 系列 —— 判据必须靠**形状**而不是"含反斜杠"。
    """
    assert not is_style_only_placeholder(token), f"{token!r} 不该算纯样式"


# ---------------------------------------------------------------------------
# 2) 整体判定
# ---------------------------------------------------------------------------
def test_only_colour_codes_lost_is_style_only() -> None:
    """★ 本次修复的核心场景：只丢颜色码 ⇒ 只丢样式。"""
    assert style_only_placeholder_loss(r"\c[0]Hmm...\u2661", "嗯…♡")
    assert style_only_placeholder_loss(r"\c[17]Lynn:", "林恩：")
    assert style_only_placeholder_loss(r"\c[0]A\c[1]B", "甲乙")
    assert style_only_placeholder_loss(r"\c[0]A\|B\c[1]", "甲乙")


def test_nothing_lost_is_not_style_only() -> None:
    """**没丢**不是"只丢样式" —— 返回 False，不要混淆两种情形。"""
    assert not style_only_placeholder_loss(r"\c[0]A", r"\c[0]甲")
    assert not style_only_placeholder_loss("plain", "普通")


def test_extra_placeholder_blocks_style_only() -> None:
    """★ **多出**记号也判死 —— 那可能是模型自造的垃圾。

    实测背景：模型会凭空写出 `⟦0⟧`（`strip_unknown_masks` 的 docstring 有记录）。
    "多出来的"和"丢失的"性质完全不同，不能因为"丢的都是样式"就放过。
    """
    assert not style_only_placeholder_loss(r"\c[0]A", r"\c[0]甲乙\c[5]")
    assert not style_only_placeholder_loss("A", r"甲\c[0]")


@pytest.mark.parametrize(
    ("src", "tgt", "why"),
    [
        (r"\V[1] has X", "有 X", "丢变量"),
        (r"100%s here", "这里", "丢 printf 参数"),
        (r"\c[0]A \V[1]", "甲", "混丢样式 + 内容"),
        (r"\N[1] said", "说", "丢角色名"),
    ],
)
def test_content_loss_is_never_style_only(src: str, tgt: str, why: str) -> None:
    """★ 只要丢了任何一个**内容类**记号，就绝不算"只丢样式"。"""
    assert not style_only_placeholder_loss(src, tgt), f"{why} 必须判死"


def test_newlines_are_out_of_scope_but_stay_content_like() -> None:
    """★★ **换行不归本判据管** —— 且这一点很容易搞错（我当场搞错过）。

    实测：`extract_placeholders` 对**真实换行**返回 `[]`：

        extract_placeholders('A\\nB')  ==  []

    换行是靠 `mask_newlines=True` 变成记号 ``⟦0⟧`` 之后，由 provider 侧的
    `repair_missing_newlines` / `verify_restored` 单独把关的。

    ⇒ 本模块**既不该**把 `\\n` 列进样式表（否则等于声称"换行是装饰"，
    而它决定行结构），也**不能**声称"换行由本判据保护"。

    这条测试同时钉住两件事：
    1. `\\n` 与掩码记号 `⟦0⟧` 都**不是**样式；
    2. `extract_placeholders` 确实看不见真实换行（所以本判据看不到它）。
    """
    from novaloc.translate.guards import extract_placeholders

    assert not is_style_only_placeholder("\\n"), "换行决定行结构，绝不是样式"
    assert not is_style_only_placeholder("\u27e60\u27e7"), (
        "掩码记号 ⟦0⟧ 本身不是样式 —— 它代表的东西由槽位表决定"
    )
    # 真实换行不在 extract_placeholders 的结果里 ⇒ 本判据确实看不到它，
    # 所以"guard 会因换行丢失而判死"这个说法是**错的**（换行另有机制）。
    assert extract_placeholders("A\nB") == [], (
        "真实换行本就不在 extract_placeholders 里；"
        "若这条失败说明底层行为变了，本模块的注释与判据都要复查"
    )


def test_placeholders_lost_reports_both_directions() -> None:
    """`placeholders_lost` 必须同时给出"丢的"与"多的"。

    只给"丢的"会让 `style_only_placeholder_loss` 看不见"模型自造记号"，
    那正是上面那条安全边界的依据。
    """
    lost, extra = placeholders_lost(r"\c[0]A", r"甲\c[1]")
    assert r"\c[0]" in lost
    assert r"\c[1]" in extra


# ---------------------------------------------------------------------------
# 3) 端到端：guard() 的 fatal 行为
# ---------------------------------------------------------------------------
def _g(src: str, tgt: str):
    return guard(
        src, tgt, max_chars=None, length_ratio=2.2, target_lang="zh", hints={}
    )


def test_guard_allows_colour_only_loss() -> None:
    """★★ 本次修复：`\\c[0]Hmm...♡` → `嗯…♡` 不再判死。

    修复前这里 `fatal=True`，导致逐行救援 6 条里被这条判据拒掉 2 条
    （而那两条文字完全正确）。
    """
    r = _g(r"\c[0]Hmm...\u2661", "嗯…♡")
    assert not r.fatal, f"只丢颜色码不该判死：{r.warnings}"
    # ⚠️ 但**必须留下警告** —— 审校时要能看到"颜色码没了"。
    assert any("placeholder_count" in w for w in r.warnings), (
        "放行不等于不报告：警告必须保留"
    )


def test_guard_still_kills_content_loss() -> None:
    """★ 安全侧不变：丢变量/丢参数**仍然**判死。

    ⚠️ 这里**故意不测"丢换行"**：`extract_placeholders` 看不见真实换行
    （见 `test_newlines_are_out_of_scope_but_stay_content_like`），
    所以 `guard("A\\nB", "A B")` 本来就不 fatal —— 换行由 provider 侧的
    `repair_missing_newlines` / `verify_restored` 把关。
    写成"丢换行必须 fatal"是**错的判据**，我当场写错过一次。
    """
    assert _g(r"\V[1] X", "X").fatal, "丢变量必须判死"
    assert _g("100%s", "100").fatal, "丢参数必须判死"
    assert _g(r"\N[1] said", "说").fatal, "丢角色名必须判死"


def test_guard_still_kills_empty_and_untranslated() -> None:
    """空译文与"像是没翻"仍然判死。"""
    assert _g("Hello", "").fatal, "空译文必须判死"


def test_guard_still_kills_extra_and_partial_loss() -> None:
    """★ 多出记号 / 混丢 ⇒ 判死（安全边界在 guard 层同样成立）。"""
    assert _g(r"\c[0]A", r"甲\c[9]").fatal, "多出记号必须判死"
    assert _g(r"\c[0]A\V[1]", "甲").fatal, "混丢内容必须判死"


def test_guard_mixed_keep_and_lose_still_style_only() -> None:
    """保留了部分颜色码、丢了另一个 —— 仍是"只丢样式"，放行。"""
    r = _g(r"\c[0]A\c[1]B", r"\c[0]甲乙")
    assert not r.fatal, f"只丢颜色码不该判死：{r.warnings}"
