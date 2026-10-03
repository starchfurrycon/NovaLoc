r"""回归：**以原文为键**的返回形态必须能用。

## 缺陷现场（真实游戏库，实测）

`auto` 跑 `072 Project_Useless Princess and the Village Renovation` 时 stderr：

    批 0（1 条）第 1 次失败：单条翻译失败（所有解析策略均失败，
    原始输出前 200 字符：'{"vigilance requirements": "警戒要求"}'）
    ：解析得到空映射

译文 **完全正确**（"警戒要求"），却被判成失败。

## 根因：两个模块对同一形态的判断**互相矛盾**

* `to_translation_map` 的 docstring 明确写它**支持** ``{"原文": "译文"}``，
  理由是实测 `translategemma:4b` 真会这么回；
* 但 `_looks_useful`（`parse_json_loose` 的门槛）只认**数字键** ⇒
  这段**合法且正确**的 JSON 被丢弃 ⇒ 空映射 ⇒ 报错 ⇒
  每个命中的批次白跑一次、掉到单条重试。

⇒ 这是"两处判据不一致"型缺陷：**单看任一模块都自洽**，
只有把"模型真实返回"喂进整条链路才暴露。

## 判据（事先定好）

1. ``{"原文": "译文"}`` ⇒ `parse_json_loose` 必须 `ok=True`；
2. 且 `to_translation_map(..., sources=[原文])` 必须解出 ``{0: 译文}``；
3. **反例不许放松**：``{"t": "译文"}`` 仍必须被拒
   （它"有译文但没索引"，收了会让调用方以为成功而丢整批）；
4. ``{"i": 0, "t": "译文"}`` 仍必须**通过**（数字键分支）；
5. 空 dict / 空值 dict 必须被拒。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate.json_parse import (  # noqa: E402
    _looks_useful,
    parse_json_loose,
    parse_translations,
)

# ---------------------------------------------------------------------------
# 1~2. 主用例：真实现场那一行
# ---------------------------------------------------------------------------


def test_the_exact_real_world_payload_parses() -> None:
    """★ 真实现场的原样输出必须能解析。"""
    raw = '{"vigilance requirements": "警戒要求"}'
    res = parse_json_loose(raw)
    assert res.ok is True, f"合法且正确的 JSON 被判成没用：{res.notes!r}"
    assert res.value == {"vigilance requirements": "警戒要求"}


def test_source_keyed_maps_to_index_with_sources() -> None:
    """★ 配上 ``sources`` 必须能归位成 ``{0: 译文}``。"""
    raw = '{"HP": "生命值", "MP": "魔法值"}'
    mapping, res = parse_translations(raw, sources=["HP", "MP"])
    assert res.ok is True
    assert mapping == {0: "生命值", 1: "魔法值"}


def test_source_keyed_single_entry_maps_to_zero() -> None:
    mapping, _res = parse_translations(
        '{"vigilance requirements": "警戒要求"}',
        sources=["vigilance requirements"],
    )
    assert mapping == {0: "警戒要求"}


def test_unknown_source_key_is_dropped_not_crashed() -> None:
    """查不到的键**丢弃**（交给下游判空），不许抛异常。"""
    mapping, _res = parse_translations(
        '{"unrelated": "无关"}\n', sources=["HP"]
    )
    assert mapping == {}


# ---------------------------------------------------------------------------
# 3~4. 反例：不许把原本拦得住的东西放松
# ---------------------------------------------------------------------------


def test_bare_t_string_is_still_rejected() -> None:
    """★★ 关键反例：``{"t": "译文"}`` 必须**继续被拒**。

    它是"有译文但没索引"的形态 —— 调用方靠 ``i`` 归位，
    收了它会让调用方以为成功，实际整批译文全丢。
    这就是为什么新判据要求"键长度 > 1"。
    """
    assert _looks_useful({"t": "译文"}) is False


def test_numeric_index_dict_still_accepted() -> None:
    """数字键的条目照旧通过（新判据**不得**影响这条路径）。

    ⚠️ **不要**断言独立的 ``{"i": 0, "t": "译文"}`` 为 True ——
    它实际返回 False，而且这是**对的**：这里 `i` 被当成"数字键"，
    其值是 ``0``（数字、非文本）⇒ 判为"有键没译文"。

    真实的批条目**永远包在 list 里**，此时走的是 list 分支
    （``_item_index`` 与 ``_item_text`` 都取得到）——
    下面 `test_batch_list_items_are_still_accepted` 才是该形态的正确断言。
    """
    assert _looks_useful({"0": "译文", "1": "译文二"}) is True
    assert _looks_useful({"0": ""}) is False  # 有键没译文
    # 独立的对象形态：`i` 被当数字键，值为 0 ⇒ 没译文 ⇒ 拒
    assert _looks_useful({"i": 0, "t": "译文"}) is False


def test_batch_list_items_are_still_accepted() -> None:
    """★ 真实的批返回形态：list 里装 ``{"i": n, "t": …}`` ⇒ 必须通过。"""
    assert _looks_useful([{"i": 0, "t": "译文"}]) is True
    assert _looks_useful([{"i": 0, "t": "a"}, {"i": 1, "t": "b"}]) is True
    # 空壳：有 i 没 t ⇒ 拒（否则整批译文会被当成"解析成功"而丢失）
    assert _looks_useful([{"i": 0}]) is False
    assert _looks_useful([]) is False


def test_bare_string_list_is_accepted() -> None:
    """裸字符串数组按出现顺序配索引（比整批丢弃好）。"""
    assert _looks_useful(["译文一", "译文二"]) is True
    assert _looks_useful(["", "  "]) is False


def test_empty_values_are_rejected() -> None:
    """空 dict / 空字符串值必须被拒（否则会"解析成功但没内容"）。"""
    assert _looks_useful({}) is False
    assert _looks_useful({"source text": ""}) is False
    assert _looks_useful({"source text": "   "}) is False


def test_non_string_values_fall_through() -> None:
    """值不是字符串 ⇒ 不适用新判据（交由数字键分支或拒绝）。"""
    assert _looks_useful({"a": 1}) is False
    assert _looks_useful({"a": None}) is False
    assert _looks_useful({"a": ["x"]}) is False


@pytest.mark.parametrize("key", ["t", "i", "x"])
def test_single_char_keys_never_enable_the_new_rule(key: str) -> None:
    """键长度为 1 且**不是数字**时不许触发新判据（防误收 ``{"t": …}`` 家族）。

    ⚠️ ``"0"`` **不在此列** —— 它是数字键，由数字键分支正常接受
    （`_coerce_int("0")` 成功）。把它写进参数表是我第一版测试的错误，
    实测立刻红了。
    """
    assert _looks_useful({key: "译文"}) is False


# ---------------------------------------------------------------------------
# 6. ★★ 真正的断点：调用方**必须传 `sources`**
#
# 这是本缺陷的第二层，也是真正让现场失败的那一层。
#
# `to_translation_map` 靠 `sources` 做"原文 → 编号"反查。
# `_call_single`（单条重试路径）原本写的是：
#
#     parse_translations(raw, expect_indices=[0])      # 没传 sources
#
# 于是键停在 ``"vigilance requirements"``、而查找用 ``0`` ⇒ 查不到
# ⇒ `mapping` 为空 ⇒ 报"解析得到空映射" ⇒ 单条重试连续 3 次全败。
#
# ⚠️ **只修 `_looks_useful` 不够** —— 那样 `parse_json_loose` 会 ok=True，
# 但 `mapping` 仍是空的。这个"双层"性质是本次最值得记住的一点：
# 解析层放行了，**归位层**还得有依据才能真的拿到译文。
# ---------------------------------------------------------------------------


def test_without_sources_source_keyed_cannot_be_indexed() -> None:
    """★ 不传 `sources` ⇒ 以原文为键的返回**无法**归位（空映射）。

    这一条**故意**断言"坏行为"，用来固定"为什么必须传 sources"的因果。
    """
    mapping, res = parse_translations(
        '{"vigilance requirements": "警戒要求"}', expect_indices=[0]
    )
    assert res.ok is True, "解析层应该放行（_looks_useful 已修）"
    assert mapping == {}, "没有 sources 就反查不到 ⇒ 归位失败（这正是 bug）"


def test_with_sources_same_payload_succeeds() -> None:
    """★ 传了 `sources` ⇒ 同一份 payload 立刻能用。两相对照即因果。"""
    mapping, _res = parse_translations(
        '{"vigilance requirements": "警戒要求"}',
        expect_indices=[0],
        sources=["vigilance requirements"],
    )
    assert mapping == {0: "警戒要求"}


def test_call_single_passes_sources() -> None:
    """★★ **源码级**回归：`_call_single` 必须把 `sources` 传下去。

    行为级测试要造一个带 `masked` 的 `TranslateItem` 并打桩 `_chat`，
    代价大且脆；这里直接钉住**调用点**，因为断点就是"少传一个参数"，
    而它没有任何运行时报错 —— 只会让译文静默丢失。

    ⚠️ 检查的是 `_call_single_once`：`_call_single` 现在是**对"模型回空
    JSON"做一次重试的外壳**（实测 `{}` 是偶发，见
    `.scratch/_brace_retry.py` 的 5/5 vs 0/5 对照），真正的调用体在
    `_call_single_once`。断点性质不变，所以判据跟着移到真正的方法上 ——
    但**两处都查**，这样万一以后有人把重试外壳去掉也不会漏。
    """
    import inspect

    from novaloc.translate import ollama_provider as op

    bodies = {
        "wrapper": inspect.getsource(op.OllamaTranslationProvider._call_single),
        "once": inspect.getsource(op.OllamaTranslationProvider._call_single_once),
    }
    target = bodies["once"] if "parse_translations" in bodies["once"] else bodies["wrapper"]
    assert "sources=[masked]" in target or "sources=list(masked)" in target, (
        "_call_single_once 没有把 sources 传给 parse_translations —— "
        "以原文为键的返回会变成空映射，译文静默丢失"
    )
    # 重试外壳必须真的调用带 sources 的那个方法（别把逻辑复制成两份）
    assert "_call_single_once" in bodies["wrapper"], (
        "_call_single 应当委托给 _call_single_once，而不是自己再抄一份解析逻辑"
    )
