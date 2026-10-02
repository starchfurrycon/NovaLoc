r"""逐条降级**不许重复问同一条** —— 实测浪费 400+ 次请求。

## 实测数据（两次真实端到端跑，读的是 UTF-16 日志）

    降级事件（'批 N 条里只回 M 条'）      48 次
    缺的条数合计                          403
    ⇒ 额外单条请求                       约 403 次
    批级失败尝试                          24 次（其中第 2+ 次 8 次）
    'Ollama 返回 500'                     14 次

## 浪费在哪

`translate_batch` 里那个 `for attempt in range(3)`：

* `attempt=0` 失败 → 重试；
* **`attempt>=1` 失败 → 逐条降级**（每条一次 `_call_single`）；
* 若逐条降级**一条都没救回来**，`if singles:` 为假 ⇒ **不 break** ⇒
  `attempt=2` 再失败 ⇒ **又逐条降级一次** ⇒ 同一批每一条又被问一遍。

相关地，紧接着的"补空"（2.5 节）拿的是
`raw_by_index` 里**没值**的下标 —— 而逐条降级失败过的条目正是没值的，
于是它们**第三次**被单独问。

触发源是**内容**（`token repeat limit reached` 是内容触发的服务端阀），
不是偶发抖动，所以"再问一次"基本不会变好 —— 纯粹烧时间。

## 守什么

1. 逐条降级里失败的条目，**下一轮不再进逐条降级**；
2. 同一批里"逐条降级失败过"的条目，**补空阶段也不再问**；
3. ★ 但**不能**顺手把补空关掉：批量调用成功、个别条目返回空串
   （`MP: {mp}` 这类短词 + 占位符）**没有**被逐条试过，
   补空对它们是真有效的（3 次运行 3 次复现）。这条也要守。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from conftest import requires_ollama  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context, TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402

# 真的 `OllamaTranslationProvider` 构造时会探活（走网络）。
# 没有 Ollama 就整体跳过 —— CI 上不该红成一片。
pytestmark = requires_ollama


def _items(srcs: list[str]) -> list[TranslateItem]:
    return [
        TranslateItem(
            unit=TextUnit(
                uid=f"u{i}",
                source=s,
                # CHARACTER_NAME 不进 `_CAREFUL_KINDS`，也不进 `_SHORT_KINDS`，
                # 这样整批会走"普通文本"分支，便于构造统一的批。
                kind=TextKind.CHARACTER_NAME,
                location=TextLocation(file="Map.json", pointer=f"/e/{i}"),
            ),
            glossary=[],
            context_lines=[],
        )
        for i, s in enumerate(srcs)
    ]


class _Hybrid(OllamaTranslationProvider):
    r"""`_call_batch` 永远失败；`_call_single` 只对指定下标成功。"""

    def __init__(self, ctx: Context, *, ok_indices: set[int], single_fails: bool = False) -> None:
        super().__init__(ctx)
        self.ok_indices = ok_indices
        self.single_fails = single_fails
        self.single_sources: list[str] = []
        self.batch_calls = 0

    def _call_batch(self, batch_items, masked):  # noqa: ANN001, ARG002
        self.batch_calls += 1
        raise ValueError("模拟整批失败")

    def _call_single(self, item, masked, **kwargs):  # noqa: ANN001, ARG002
        self.single_sources.append(item.unit.source)
        if self.single_fails:
            raise ValueError("模拟单条也失败")
        # 用 fx 前缀定位是第几条；下标从 unit.uid 取
        idx = int(item.unit.uid[1:])
        if idx in self.ok_indices:
            return "译" + item.unit.source
        raise ValueError("这条模拟失败")


def _ctx() -> Context:
    return Context(config=Config(), events=EventBus())


SRCS = ["alpha", "bravo", "charlie", "delta"]


def test_failed_per_item_entries_are_not_retried_in_next_round() -> None:
    r"""★ 核心断言：一条都救不回来时，每个条目**只被单独问一次**。

    ## 为什么必须用 `ok_indices=set()`

    我第一版这条用例写的是 `ok_indices={1}`（只有第 1 条能救回来），
    结果它在**有 bug 的代码上也是绿的** —— 因为：

        逐条降级救回了第 1 条 ⇒ `if singles:` 为真 ⇒ `break`

    于是**根本走不到 `attempt=2`**，那条"重复逐条"的路径没被执行，
    断言自然通过。**一条在 bug 上为绿的回归测试等于没有测试。**

    `ok_indices=set()`（一条都救不回）才会让外层再循环一次，
    从而真正走到重复逐条那条路径上。
    """
    p = _Hybrid(_ctx(), ok_indices=set(), single_fails=True)
    p.translate_batch(_items(SRCS), "zh-CN")

    from collections import Counter

    counts = Counter(p.single_sources)
    print(f"    单条调用次数分布：{dict(counts)}")
    over = {s: c for s, c in counts.items() if c > 1}
    assert not over, f"这些条目被逐条重复问了多次：{over}（调用序列 {p.single_sources}）"


def test_all_per_item_failures_still_called_once_each() -> None:
    r"""全都失败也要**每条至少试一次** —— 不能因为去重而漏掉条目。"""
    p = _Hybrid(_ctx(), ok_indices=set(), single_fails=True)
    p.translate_batch(_items(SRCS), "zh-CN")
    from collections import Counter

    counts = Counter(p.single_sources)
    assert set(counts) == set(SRCS), f"有条目一次都没被逐条试过：{counts}"
    assert all(c == 1 for c in counts.values()), f"有重复：{dict(counts)}"


def test_batch_retry_is_preserved_when_nothing_rescued() -> None:
    r"""★ 重试**本身**要保留 —— 去掉重试会把"偶发抖动"变成漏译。

    场景：一条都救不回来 ⇒ 逐条降级不 `break` ⇒ 整批应当**重试满 3 次**。
    （若某条被救回来，`if singles:` 会 `break`，那就只试 2 次 ——
    那是正确行为，见下一条用例。）
    """
    p = _Hybrid(_ctx(), ok_indices=set(), single_fails=True)
    p.translate_batch(_items(SRCS), "zh-CN")
    assert p.batch_calls == 3, f"一条都没救回时整批应重试 3 次，实际 {p.batch_calls}"


def test_break_on_partial_rescue_avoids_third_batch_call() -> None:
    r"""救回来一部分就**不要**再重试整批 —— 那是浪费。

    这条与上一条是一对：想区分"没效果"和"没运行"，两种情形都得钉住。
    """
    p = _Hybrid(_ctx(), ok_indices={0})  # 第 0 条在逐条降级里被救回
    p.translate_batch(_items(SRCS), "zh-CN")
    assert p.batch_calls == 2, (
        f"已救回一部分就不该再整批重试，实际 {p.batch_calls} 次"
    )


def test_empty_recovery_still_runs_for_batch_blank_items() -> None:
    r"""★ 补空**不许**被一起去掉。

    场景：整批调用**成功**，但个别条目返回空串（真实存在的
    `MP: {mp}` / `Gold: {gold}` 这类"短词 + 占位符"）。
    这些条目**没有**被逐条试过，补空必须救它们。
    """

    class _Blank(OllamaTranslationProvider):
        def __init__(self, ctx: Context) -> None:
            super().__init__(ctx)
            self.single_calls: list[str] = []

        def _call_batch(self, batch_items, masked):  # noqa: ANN001, ARG002
            # 第 0 条返回空串，其余正常 —— 整批调用是"成功"的
            out = {}
            for i, it in enumerate(batch_items):
                out[i] = "" if i == 0 else "译" + it.unit.source
            return out

        def _call_single(self, item, masked, **kwargs):  # noqa: ANN001, ARG002
            self.single_calls.append(item.unit.source)
            return "补" + item.unit.source

    p = _Blank(_ctx())
    out = p.translate_batch(_items(SRCS), "zh-CN")
    assert p.single_calls == ["alpha"], (
        f"补空应只针对返回空串的那条，实际 {p.single_calls}"
    )
    alpha = next(e for e in out if e.uid == "u0")
    assert alpha.target.strip(), "空串条目没有被补救"
