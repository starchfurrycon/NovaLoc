r"""被截断的"编号 → 译文"对象必须能抢救出**完整的那几项**。

## 实测（Round 10，真实端到端 stderr）

```
批 0（1 条）第 2 次失败：单条翻译失败（所有解析策略均失败，
    原始输出前 200 字符：'{"1": "在接下来的 1 个回合内，自动保护生命值较低的队友。",
                         "2": "自动防御生命值较小的同伴。",
                         "3": "在 1 回合内，自动保护生命值低的队友。",
                         "4": "在接下方的 1 回合内，自动保护生命值较低的队友。",
                         "5": "在 1 轮内，自动保护生命值较少的队友。",
                         "6": "在接下来的 1 轮内，自动保护生命值较低的'）
```

**前 5 对全是完整的**，只是第 6 项被 `num_predict` 截断、末尾少了 `}`。
而 `json.loads` 一失败，**这 5 条完整译文全部被丢弃** ⇒ 该条目内容丢失。

## 为什么这是**独立于 #30** 的一个 bug

`parse_translations` 返回的 `mapping` 是**空的**（实测 `res.ok=False`），
而 #30 的修复只作用于"有映射、但键从 1 开始"的情形。
**空映射进不了那条分支**，所以 #30 修不了这一例。

## 为什么要卡"连续且从 0 或 1 开始"

正则捞 `"N": "…"` 对**任何**含这种文本的响应都会命中。若不加限制，
一段恰好含 `"1": "…"` 的**正文**会被当成译文映射 —— 那是
**凭空造出结构**，比丢一条更糟。真实截断只发生在**尾部**，
所以完整项必然是从起点开始的一段**连续前缀**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.json_parse import (  # noqa: E402
    parse_translations,
    to_translation_map,
)

#: 实测原文（从 `.scratch/_e2e_auto3.err` 逐字抄的，末尾故意没有 `}`）
REAL_TRUNCATED = (
    '{"1": "在接下来的 1 个回合内，自动保护生命值较低的队友。", '
    '"2": "自动防御生命值较小的同伴。", '
    '"3": "在 1 回合内，自动保护生命值低的队友。", '
    '"4": "在接下方的 1 回合内，自动保护生命值较低的队友。", '
    '"5": "在 1 轮内，自动保护生命值较少的队友。", '
    '"6": "在接下来的 1 轮内，自动保护生命值较低的'
)


def test_real_truncated_output_is_salvaged() -> None:
    """★ 核心：实测那条截断输出必须抢救出前 5 项，而不是丢光。"""
    _mapping, res = parse_translations(REAL_TRUNCATED, expect_indices=[0])
    assert res.ok, f"实测截断输出仍被判失败：{res.notes}"
    mp = to_translation_map(res.value)
    assert mp, "抢救结果为空"
    # ★ 关键：**必须**有键 0，否则下游仍等于"没有译文"
    assert 0 in mp, f"抢救出的映射没有键 0（下游拿不到译文）：{sorted(mp)}"
    assert mp[0].strip(), "键 0 的译文是空的"


def test_salvage_keeps_only_complete_pairs() -> None:
    """只收**完整**的 `"N": "…"` 对；截断的那项不许瞎补。"""
    _mapping, res = parse_translations(REAL_TRUNCATED, expect_indices=[0])
    mp = to_translation_map(res.value)
    # 原文里 1..6 都出现了，但第 6 项的字符串没有收尾引号 ⇒ 只应有 5 项
    assert sorted(mp) == [0, 1, 2, 3, 4], f"收多了或收少了：{sorted(mp)}"


def test_prose_with_a_stray_numbered_pair_is_not_salvaged() -> None:
    """★ 反向守卫：**正文**里偶然出现 `"1": "…"` 不许被当成译文映射。

    这类"从 1 开始但不连续"或"不从起点开始"的形态必须被拒 ——
    否则我们会**凭空造出结构**，那比丢一条更糟。
    """
    prose = 'the config looks like {"1": "some value"} and then more text here'
    _mapping, res = parse_translations(prose, expect_indices=[0])
    mp = to_translation_map(res.value) if res.ok else {}
    assert not mp or 0 not in mp, f"正文被误当成译文映射：{mp}"


def test_gap_in_numbering_is_rejected() -> None:
    """有洞（1,3）⇒ 不认 —— 真实截断不可能产生洞。"""
    raw = '{"1": "甲", "3": "丙"'
    _mapping, res = parse_translations(raw, expect_indices=[0])
    mp = to_translation_map(res.value) if res.ok else {}
    if mp:
        assert 0 not in mp, f"有洞的编号被当成了连续前缀：{sorted(mp)}"


def test_zero_based_truncated_is_salvaged() -> None:
    """从 0 开始的截断同样要能救。"""
    raw = '{"0": "甲", "1": "乙", "2": "丙'
    _mapping, res = parse_translations(raw, expect_indices=[0])
    assert res.ok, f"0 基准截断被判失败：{res.notes}"
    mp = to_translation_map(res.value)
    assert sorted(mp) == [0, 1], f"0 基准截断抢救异常：{sorted(mp)}"


def test_valid_json_still_uses_normal_path() -> None:
    """合法的完整 JSON**不许**被新的抢救级别截走（要保持原路径与语义）。"""
    raw = '{"1": "甲", "2": "乙"}'
    _mapping, res = parse_translations(raw, expect_indices=[0])
    assert res.ok
    # 走的是正常解析（不是 salvage）
    assert "salvage" not in (res.method or ""), f"合法 JSON 走了抢救路径：{res.method}"


@pytest.mark.parametrize(
    "raw",
    [
        '{"1": "甲"',  # 只有一项，也截断
        '{"1": "甲", "2": "乙"',  # 两项
        '{\n"1": "甲",\n"2": "乙"\n',  # 带换行的截断
    ],
)
def test_various_truncations_always_yield_key_zero(raw: str) -> None:
    """任何形态的截断都要能给出键 0，否则下游等于没译文。"""
    _mapping, res = parse_translations(raw, expect_indices=[0])
    mp = to_translation_map(res.value) if res.ok else {}
    assert 0 in mp, f"{raw!r} 抢救后仍没有键 0：{sorted(mp)}"
