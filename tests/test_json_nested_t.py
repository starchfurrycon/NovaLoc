r"""`{"t": {…}}` 这类**嵌套返回**的回归测试。

## 缺陷（真实端到端跑实测到，不是构造的）

单条翻译一条含换行的条目时，模型把译文按"批"的格式裹在 `t` 下面：

    {"t": {"0": "使用契约与恶魔签订后制作的盔甲。",
           "1": "提升所有能力，但恢复效果减半。"}}

结果是**三次重试 + 逐行救援全部失败**，整条翻译失败（保留原文）。
第三次重试还会退化成嵌套转义爆炸。

## 断点在哪（逐层量出来的，见 `.scratch/_nested_trace.py`）

这条链有**两处**都得改，只改一处仍是空映射：

| 层 | 行为 | 后果 |
| --- | --- | --- |
| `json.loads` | **能**解析（语法完全合法） | — |
| `_looks_useful({"t": {…}})` | 原先返回 **False** | 解析器**丢弃**这段合法 JSON |
| `to_translation_map` | 原先不认 `t` 作包装键 | 即使拿到也拆不开 |

也就是说：**能被 `json.loads` 解析的合法 JSON 被判成"没用"**，
这才是最主要的断点 —— 一个纯逻辑 bug，与模型质量无关。

## ★ 一个差点犯的错（已用测试钉住）

最自然的修法是"把 map 的值用 `\n` 拼起来"。但 `guard()` 对行数的判定
是**不对称**的（实测）：

| 源→译文行数 | guard 结果 |
| --- | --- |
| 2 → 2 | 正常 |
| 2 → 1 | **warn** `sentence_drop:2->1` |
| **1 → 2** | **无警告（fatal=False）** |

所以源文**单行**时凭空插 `\n` 会**悄悄多出一行且无人报警**，
比"整条失败并保留原文"更糟。

正确做法：把内层数字键 map 拆成 `{0: …, 1: …}` **多个条目**，
交给既有的 `_best_segment` 选段 —— 它本来就会拒绝把多段拼起来。
`_join_by_source_lines()` 只作为拿不到源文时的兜底，且**默认用空串拼**
（绝不凭空引入换行）。
"""

from __future__ import annotations

import pytest

from novaloc.translate import json_parse as jp

#: 真实观测到的那条（原文 2 行）
SRC_2LINE = (
    "Armor made by forming a contract with a demon.\n"
    "Raises all stats but halves healing."
)
WRAPPED = (
    '{"t": {"0": "使用契约与恶魔签订后制作的盔甲。", '
    '"1": "提升所有能力，但恢复效果减半。"}}'
)
FLAT = '{"0": "使用契约与恶魔签订后制作的盔甲。", "1": "提升所有能力，但恢复效果减半。"}'


# ---------------------------------------------------------------------------
# ★ 核心：`{"t": {…}}` 必须能解析出来
# ---------------------------------------------------------------------------


def test_looks_useful_accepts_t_holding_a_dict() -> None:
    r"""★ 能 `json.loads` 的合法 JSON，不能被判成"没用"。

    这是断点的**最主要**一处：`_looks_useful` 返回 False 会让解析器
    直接丢弃已解析成功的对象，后面所有层都拿不到数据。
    """
    assert jp._looks_useful({"t": {"0": "甲。"}}) is True


def test_looks_useful_still_rejects_t_holding_a_string_without_index() -> None:
    r"""★ 但 `{"t": "译文"}` 仍然**不算**"有用"。

    理由（原有的正确设计，不能破坏）：调用方是靠 `i` 归位的，
    没有 `i` 的条目会被 `to_translation_map` 丢掉，所以
    "有译文但没索引" 会让整批译文全丢 —— 必须拦住。
    """
    assert jp._looks_useful({"t": "甲。"}) is False


def test_looks_useful_still_rejects_bare_index() -> None:
    """`{"i": 2}` 这种"只有索引没有译文"的空壳仍要拦住。"""
    assert jp._looks_useful({"i": 2}) is False
    assert jp._looks_useful({}) is False


@pytest.mark.parametrize(
    "raw",
    [WRAPPED, FLAT],
    ids=["包裹成 t", "扁平数字键"],
)
def test_nested_and_flat_forms_both_parse(raw: str) -> None:
    r"""★ 两种观测形态都要解析出来（包裹 vs 扁平）。

    扁平形态本来就通；这条测试的价值是**把两者绑在一起** ——
    以后若有人为了修包裹形态而弄坏扁平形态，这里会红。
    """
    mapping, res = jp.parse_translations(
        raw, expect_indices=[0, 1], sources=[SRC_2LINE, "second"]
    )
    assert res.ok, f"应解析成功，实际 notes={list(res.notes)}"
    assert set(mapping) == {0, 1}, f"应有两段，实际 {mapping!r}"
    assert "契约" in mapping[0]
    assert "恢复效果" in mapping[1]


def test_real_observed_sample_is_recovered() -> None:
    r"""★ 真实端到端日志里那条样本必须被救回。"""
    mapping, res = jp.parse_translations(
        WRAPPED, expect_indices=[0], sources=[SRC_2LINE]
    )
    assert res.ok, "真实样本解析失败"
    assert mapping, "真实样本应至少产出一段"
    joined = " ".join(mapping.values())
    assert "契约" in joined and "恢复效果" in joined


# ---------------------------------------------------------------------------
# ★ 不能被"修坏"的正常形态
# ---------------------------------------------------------------------------


def test_normal_single_string_form_still_works() -> None:
    r"""★ `{"t": "译文"}` 是**正常形态**，改坏它会让所有普通翻译失效。

    我确实踩过这个错：第一版把 `t` 无条件当包装键展开，
    于是 `{"t": "甲。"}` 变成 `{}`。这条测试就是那个错误的哨兵。
    """
    mapping, res = jp.parse_translations('{"t": "甲。"}', expect_indices=[0])
    assert res.ok
    assert mapping == {0: "甲。"}, f"正常单条被改坏了：{mapping!r}"


def test_normal_batch_form_still_works() -> None:
    """`{"0": {"t": "甲"}, "1": {"t": "乙"}}` —— 标准批形态。"""
    mapping, res = jp.parse_translations(
        '{"0": {"t": "甲。"}, "1": {"t": "乙。"}}', expect_indices=[0, 1]
    )
    assert res.ok
    assert mapping == {0: "甲。", 1: "乙。"}


def test_bare_array_item_form_still_works() -> None:
    """首选形态 `[{"i": 0, "t": "甲"}]` 不受影响。"""
    mapping, res = jp.parse_translations(
        '[{"i": 0, "t": "甲。"}, {"i": 1, "t": "乙。"}]', expect_indices=[0, 1]
    )
    assert res.ok
    assert mapping == {0: "甲。", 1: "乙。"}


# ---------------------------------------------------------------------------
# ★ 行数安全：绝不凭空引入换行
# ---------------------------------------------------------------------------


def test_join_by_source_lines_uses_newline_only_when_counts_match() -> None:
    r"""★ 源文行数 == 段数 才用 `\n` 拼；否则用空串。

    这是"绝不凭空多出一行"的实现保证（`guard()` 对 1 行→2 行**不报警**）。
    """
    assert jp._join_by_source_lines(["甲", "乙"], "A\nB") == "甲\n乙"
    assert jp._join_by_source_lines(["甲", "乙"], "AB") == "甲乙"
    # 拿不到源文 → 保守用空串
    assert jp._join_by_source_lines(["甲", "乙"], None) == "甲乙"
    # 3 段但源文只有 2 行 → 不插换行
    assert jp._join_by_source_lines(["甲", "乙", "丙"], "A\nB") == "甲乙丙"
    assert jp._join_by_source_lines([], "A") == ""


def test_coerce_str_on_numeric_map_respects_source_lines() -> None:
    r"""★ `_coerce_str` 直接处理数字键 map 时也遵守行数规则。"""
    val = {"0": "甲", "1": "乙"}
    assert jp._coerce_str(val, source="A\nB") == "甲\n乙"
    assert jp._coerce_str(val, source="AB") == "甲乙", "单行源文绝不能多出换行"
    assert jp._coerce_str(val, source=None) == "甲乙", "无源文时保守用空串"


def test_numeric_map_keys_are_ordered_numerically_not_lexically() -> None:
    r"""★ 段序必须按**数字**排，不能按字符串排。

    `"10" < "2"` 在字符串序下成立 —— 段数上两位时会把顺序弄乱。
    """
    val = {str(i): f"段{i}" for i in range(12)}
    got = jp._coerce_str(val, source=None)
    assert got is not None
    # 按数字序：段0段1…段11
    assert got.startswith("段0段1段2")
    assert got.endswith("段11"), f"末段应是段11，实际 {got[-6:]!r}"


# ---------------------------------------------------------------------------
# 数字键 map 嵌在字符串里（正则抢救后的形态）
# ---------------------------------------------------------------------------


def test_numeric_map_as_json_string_is_unwrapped() -> None:
    r"""★ 字符串里还是数字键 map 时也要认。

    `parse_json_loose` 对嵌套结构整体失败、走正则抢救时，
    救出来的形态可能是 `{"t": '{"0": …}'}` —— 内层 map 变成**字符串**。
    """
    got = jp._coerce_str('{"0": "甲", "1": "乙"}', source=None)
    assert got == "甲乙", f"应拆开内层 map，实际 {got!r}"


def test_ordinary_translation_text_is_not_touched() -> None:
    r"""★ 普通译文里带花括号也不能被"再解析一次"弄坏。

    只在"确实像个数字键 map"时才尝试内层解析。
    """
    for text in (
        "甲。",
        "使用 {名称} 攻击。",
        '他说："{0}" 是编号。',
        "{这不是 JSON}",
    ):
        assert jp._coerce_str(text, source=None) == text, f"被改动了：{text!r}"
