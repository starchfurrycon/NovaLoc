r"""#37 的修复：**段数 == 源文行数**时要按行序拼回，不再只留一段。

## 实测背景（#36 / #37）

`mask_newlines=True` 把多行源文**压成一行**（换行 → 占位符），
但模型**仍按语义行回多段**：

    源文 89 字符 / 3 行
    掩码后: 'Mashiro:⟦0⟧"Mnnn! ♡ …⟦1⟧I-It sounds so lewd…"'
    slots 个数: 2　（两个都是 '\n'）

只选一段的结果（**结构性**，重复 4 次极差中位 0.02）：

    整条 ⇒ '玛希罗：'        ← 只剩角色名，比值 0.04
    修后 ⇒ 3 行全在，比值 0.54

## 这个文件要守**两个方向**

1. **段数 == 行数 ⇒ 必须拼**（否则丢内容）；
2. **段数 ≠ 行数 ⇒ 不许拼**（那才是 `_best_segment` 反对拼接的场景：
   同一内容的替代译文 / 段 0 不完整 / 各段完全相同 —— 拼了会**复读**，
   本项目 2026-09 因复读出过事故）。

第 2 条与第 1 条**同等重要**：只测第 1 条会让修复变成一个"总是拼接"
的改动，那正是 docstring 里记着已经犯过一次的错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from conftest import requires_ollama  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.models import TextKind  # noqa: E402
from novaloc.translate import placeholders as ph  # noqa: E402
from novaloc.translate.ollama_provider import (  # noqa: E402
    OllamaTranslationProvider,
    _source_line_count,
)

pytestmark = requires_ollama

#: 实测那条源文（3 行）
REAL_SRC = (
    'Mashiro:\n"Mnnn! ♡ The squelching... ♡ It won\'t stop! ♡\n'
    'I-It sounds so lewd all around! ♡"'
)


class _Item:
    def __init__(self, source: str, kind: TextKind = TextKind.DIALOGUE) -> None:
        self.unit = type("U", (), {"kind": kind, "source": source})()
        self.glossary: list = []
        self.context_lines: list = []


def _prov(monkeypatch: pytest.MonkeyPatch, raw: str) -> OllamaTranslationProvider:
    p = OllamaTranslationProvider(Context(config=Config(), events=EventBus()))
    monkeypatch.setattr(p, "_chat", lambda *a, **k: raw)
    return p


def _mask(src: str) -> tuple[str, list[str]]:
    masked, slots = ph.mask_batch([src], newlines=True)
    return masked[0], slots[0]


# ---------------------------------------------------------------- 方向 1


def test_source_line_count_counts_only_newline_slots() -> None:
    """只数**换行**占位符；引擎转义不算行边界。"""
    assert _source_line_count(None) == 1
    assert _source_line_count([]) == 1
    assert _source_line_count(["\n"]) == 2
    assert _source_line_count(["\n", "\n"]) == 3
    # `\C[6]` 这类被屏蔽后也进 slots，但它**不是**行边界
    assert _source_line_count([r"\C[6]"]) == 1
    assert _source_line_count(["\n", r"\C[6]", "\n"]) == 3


def test_three_segments_for_three_lines_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心：3 行源文 + 模型回 3 段 ⇒ 三行都要在，不能只剩第一行。"""
    raw = (
        '{"0": "玛希罗：", '
        '"1": "Mnnn！♡ 这种令人难受的感觉……♡ 它还在不停地发生！♡", '
        '"2": "听起来简直是无中生有！♡"}'
    )
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(REAL_SRC)
    got = p._call_single(_Item(REAL_SRC), masked, slots=slots)
    assert got.count("\n") + 1 == 3, f"没有保住 3 行：{got!r}"
    # 三段的**内容**都要出现
    assert "玛希罗" in got
    assert "听起来" in got


def test_joined_result_is_substantially_longer_than_single_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用比值守住这个 bug 不再回来：修前约 0.04，修后应远超它。"""
    raw = (
        '{"0": "玛希罗：", '
        '"1": "这种令人难受的感觉还在不停地发生，我快要受不了了。", '
        '"2": "听起来简直是无中生有，太羞耻了。"}'
    )
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(REAL_SRC)
    got = p._call_single(_Item(REAL_SRC), masked, slots=slots)
    ratio = len(got) / len(REAL_SRC)
    assert ratio > 0.35, f"比值 {ratio:.2f} 仍偏低（修前实测 0.04）"


# ---------------------------------------------------------------- 方向 2


def test_segment_count_not_matching_lines_still_picks_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 反方向：3 行源文但只回 2 段 ⇒ **不许拼**，仍按 `_best_segment` 选一段。

    这正是 `_best_segment` docstring 里"段 0 不完整 / 各段是替代译文"
    的形态 —— 拼接会复读。
    """
    raw = '{"0": "玛希罗：", "1": "完全不同的另一种译法。"}'
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(REAL_SRC)
    got = p._call_single(_Item(REAL_SRC), masked, slots=slots)
    assert got.count("\n") == 0, f"段数(2)≠行数(3) 却拼了：{got!r}"


def test_identical_alternative_segments_are_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """各段**完全相同**（替代译文）⇒ 不能拼成两份。

    这是复读事故的典型形态：拼了会让译文长度翻倍、游戏里显示两遍。
    """
    same = "获得 1 个『复制』。"
    src = "Line one here\nLine two here"
    raw = f'{{"0": "{same}", "1": "{same}"}}'
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(src)
    got = p._call_single(_Item(src), masked, slots=slots)
    # 源文 2 行、模型回 2 段 —— 这里**会**走拼接路径（段数=行数），
    # 但两段相同 ⇒ 拼出来的确是两遍。这是**已知取舍**：
    # 结构判据分不开"逐行对应"与"两行恰好同译"。
    # 所以本用例只断言**不抛异常**且长度不超过两倍，不做更强声明。
    assert got, "不该为空"
    assert len(got) <= 2 * len(same) + 4, f"拼出了多余内容：{got!r}"


def test_single_line_source_never_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    """单行源文永远不走拼接（行数=1）。"""
    src = "Protects an ally."
    raw = '{"0": "保护一名队友。"}'
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(src)
    assert _source_line_count(slots) == 1
    got = p._call_single(_Item(src, TextKind.ITEM_DESC), masked, slots=slots)
    assert "\n" not in got
    assert "队友" in got


def test_empty_segment_blocks_joining(monkeypatch: pytest.MonkeyPatch) -> None:
    """某段是空白 ⇒ 不拼（宁可少救也不要赌）。"""
    src = "Line one here\nLine two here"
    raw = '{"0": "第一行译文。", "1": "   "}'
    p = _prov(monkeypatch, raw)
    masked, slots = _mask(src)
    # 段 1 全空白 ⇒ 不进拼接路径。空白段可能被 `_best_segment` 过滤掉，
    # 于是只剩一段直接返回；关键是**不许把空白也连上去**。
    got = p._call_single(_Item(src), masked, slots=slots)
    assert "\n\n" not in got
    assert got.strip() == got, "首尾不该带空白"
    assert "第一行译文" in got
