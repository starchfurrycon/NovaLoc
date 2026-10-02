r"""单条请求收到**从 1 开始编号**的响应时，不许当成"解析失败"。

## 实测症状（Round 7，读 stderr 才发现的）

`.scratch/_e2e_auto3.err` 里同一批连报三次同一个症状：

```
批 0（1 条）第 1 次失败：单条翻译失败（无法解析为 JSON）：
    解析结果不是字符串：'{"1": "在 1 个回合内，自动保护生命值较低的队友。",
                         "2": "在 1 回合内，自动保护生命值较少的角色。"}'
批 0（1 条）第 2 次失败：…（同样的东西，只是译文措辞不同）
批 0（1 条）第 3 次失败：…
```

**1 条输入、3 次重试、3 次全废 ⇒ 这条内容丢失。**

## 根因：形状假设太窄，不是模型给了坏数据

判定多段的那行要求**必须有键 0**：

```python
joins = isinstance(mapping.get(0), str)      # {1:…, 2:…} ⇒ False
```

于是多段分支**整段被跳过**；`keys` 非空 ⇒ 报"解析结果不是字符串"。
可响应形状本身**完全合理**（连续编号的字符串片段），只是基准是 1。

## 为什么"平移"是安全修法

平移后交给**同一段**既有逻辑，等价于"模型当初回的就是 0 基准"：
仍然走 `_best_segment` **选段**（绝不拼接），连续性仍然被检查。

## 这个文件守什么

1. 从 1 开始且连续的编号 → 正常出译文，**不报错**；
2. 有洞的编号 → 仍然拒绝（安全性**没被放宽**）；
3. 0 基准的正常响应 → 行为不变。
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
from novaloc.core.registry import Context, ProviderError  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402

pytestmark = requires_ollama


class _FakeItem:
    """`_call_single` 只用到 `item.unit.kind` 与 `item.unit.source`。

    真 `TextUnit` 要一大串字段，这里给个最小替身就够 ——
    本文件测的是**解析形状**，不是模型或类型定义。
    """

    def __init__(self, source: str) -> None:
        from novaloc.models import TextKind

        self.unit = type("U", (), {"kind": TextKind.ITEM_DESC, "source": source})()
        self.glossary: list = []
        self.context_lines: list = []


def _make_provider(monkeypatch: pytest.MonkeyPatch, raw: str) -> OllamaTranslationProvider:
    prov = OllamaTranslationProvider(Context(config=Config(), events=EventBus()))

    def fake_chat(user: str, *a: object, **k: object) -> str:
        return raw

    monkeypatch.setattr(prov, "_chat", fake_chat)
    return prov


def test_one_based_numbering_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心：`{"1": …, "2": …}` 必须出译文，不许报"解析结果不是字符串"。"""
    raw = '{"1": "在 1 个回合内，自动保护生命值较低的队友。", "2": "在 1 回合内，自动保护生命值较少的角色。"}'
    prov = _make_provider(monkeypatch, raw)
    got = prov._call_single(_FakeItem("Protects an ally for 1 turn."), "Protects an ally for 1 turn.")
    assert isinstance(got, str), f"返回了 {type(got).__name__}，契约要求 str"
    assert got.strip(), "返回空串 —— 内容还是丢了"
    # 必须是**其中一段**，不许是两段拼接（拼接会把两行粘一起）
    assert got in (
        "在 1 个回合内，自动保护生命值较低的队友。",
        "在 1 回合内，自动保护生命值较少的角色。",
    ), f"不是单段结果，像是拼接：{got!r}"


def test_one_based_with_a_gap_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 安全性没被放宽：从 1 开始但**有洞**仍然拒绝。"""
    raw = '{"1": "第一段", "3": "第三段"}'
    prov = _make_provider(monkeypatch, raw)
    with pytest.raises(Exception) as ei:
        prov._call_single(_FakeItem("a\nb"), "a\nb")
    assert "字符串" in str(ei.value) or "不连续" in str(ei.value), (
        f"有洞的编号居然被接受了：{ei.value}"
    )


def test_two_based_numbering_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """从 2 开始不像"基准偏移"，更像真的编号错乱 ⇒ 宁可报错也不要猜。"""
    raw = '{"2": "甲", "3": "乙"}'
    prov = _make_provider(monkeypatch, raw)
    with pytest.raises(ProviderError):
        prov._call_single(_FakeItem("a\nb"), "a\nb")


def test_zero_based_behaviour_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 基准的正常响应行为不变。"""
    raw = '{"0": "甲", "1": "乙"}'
    prov = _make_provider(monkeypatch, raw)
    got = prov._call_single(_FakeItem("a\nb"), "a\nb")
    assert got in ("甲", "乙"), f"0 基准结果异常：{got!r}"


def test_single_segment_one_based_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有一段但从 1 开始，也要能用。"""
    raw = '{"1": "只有这一段"}'
    prov = _make_provider(monkeypatch, raw)
    got = prov._call_single(_FakeItem("only"), "only")
    assert got == "只有这一段", f"单段 1 基准结果异常：{got!r}"


def test_placeholder_completeness_still_picks_best_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 平移后**仍然**按占位符完整度选段（这条守住"不拼接"的实质）。

    第 2 段丢了一个占位符记号，所以应当选第 1 段。
    """
    raw = '{"1": "甲 ⟦0⟧ 乙 ⟦1⟧", "2": "丙 ⟦0⟧ 丁"}'
    prov = _make_provider(monkeypatch, raw)
    got = prov._call_single(_FakeItem("x\ny"), "x\ny", slots=["⟦0⟧", "⟦1⟧"])
    assert "⟦1⟧" in got, f"选中的段少了占位符（说明没按完整度选）：{got!r}"
