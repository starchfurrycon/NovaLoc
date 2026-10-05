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

#: 大批规模 —— 必须 ≫ `_SUBBATCH_SIZE`，否则"固定小批"没有施展空间。
#: 40 接近真实普通文本批的上限（`_make_batches` 里普通文本上限 25、
#: 短标签 50），足以体现收益。
LARGE = [f"sentence number {i} of the batch" for i in range(40)]


class _SmallOk(OllamaTranslationProvider):
    r"""**大批抛异常、小批成功** —— 模拟"内容触发重复循环"的真实形态。

    本项目提示词历史里的实测：`批=4 → 4/4` 可靠，而陷入
    `token repeat limit` 时 `批 25 条只回 2 条`。

    ⇒ 关键性质是"**批越小越可能被完整回答**"。
      夹具必须表达这一点，否则测不出固定小批的收益
      （我第一版写成"无论批多大都只回 1 条"，结论就反了）。
    """

    def __init__(self, ctx: Context, *, reliable_upto: int = 4) -> None:
        super().__init__(ctx)
        self.reliable_upto = reliable_upto
        self.batch_calls = 0
        self.single_calls = 0

    def _call_batch(self, batch_items, masked):  # noqa: ANN001, ARG002
        self.batch_calls += 1
        if len(batch_items) <= self.reliable_upto:
            return {i: f"译{it.unit.source}" for i, it in enumerate(batch_items)}
        raise ValueError("大批模拟重复循环")

    def _call_single(self, item, masked, **kwargs):  # noqa: ANN001, ARG002
        self.single_calls += 1
        return f"译{item.unit.source}"


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


def test_subbatch_uses_far_fewer_requests_than_per_item() -> None:
    r"""★★ 核心收益：批量失败后的补救用**固定小批**，不是逐条。

    ## 改之前 vs 改之后

    旧实现（`for li, it in enumerate(batch_items): self._call_single(...)`）
    在"40 条缺 34 条"时要发 **34 次**请求。

    实测（run5 的 `auto9.err`）这个病态有多普遍：

    ```
    '只回少数'事件          1,067 次
    其中退化逐条            1,055 次（99%）
    期望 15,055 条 → 实回 2,333 条（回收率 15.5%）
    ```

    ⇒ **85% 的批次内容靠逐条补救**，GPU 利用率只有 13%。

    ## ★ 为什么必须用**大批**（≥ 小批阈值的好几倍）

    我第一版用 `SRCS`（4 条）测，断言不通过 —— 那是**夹具选错了规模**：
    `_SUBBATCH_SIZE = 4` 时，4 条只切成 1 个子批，"固定小批"根本没有
    施展空间，请求数与逐条持平。收益只在 **N ≫ _SUBBATCH_SIZE**
    时才出现（40 条 ⇒ 约 3 倍）。

    ## ★ 夹具必须像真货

    用 `_SmallOk`：**大批抛异常、小批成功**。这才是"内容触发重复循环"
    的真实形态（本项目提示词历史：批=4 → 4/4 可靠，批=25 → 只回 2 条）。

    如果夹具写成"无论批多大都只回 1 条"，子批永远不可能成功，
    测出来的"没收益"是**夹具的结论**，不是代码的结论 —— 我踩过这个坑。
    """
    p = _SmallOk(_ctx(), reliable_upto=4)
    p.translate_batch(_items(LARGE), "zh-CN")
    total = p.batch_calls + p.single_calls
    old = 3 + len(LARGE)  # 旧策略：3 次整批重试 + 每条一次单条
    assert total < old * 0.5, (
        f"总请求 {total} 次（整批 {p.batch_calls} + 单条 {p.single_calls}）"
        f"，旧策略约 {old} 次 —— 减少不足一半，固定小批没生效"
    )
    # 结果必须完整（不能为了快而漏译）
    out = p.translate_batch(_items(LARGE), "zh-CN")
    assert all((e.target or "").strip() for e in out), "有漏译"


def test_subbatch_never_asks_the_same_item_twice() -> None:
    r"""★ 二分补救**不能**把同一个条目重复问。

    `already_failed` 这个集合是跨递归层共享的 ——
    如果每一层都新建一个集合，失败的条目会在每层被重问一遍，
    请求数就会**超过**原来的逐条策略（那才是真退化）。
    """
    p = _Hybrid(_ctx(), ok_indices=set(), single_fails=True)
    p.translate_batch(_items(SRCS), "zh-CN")
    from collections import Counter

    counts = Counter(p.single_sources)
    over = {s: c for s, c in counts.items() if c > 1}
    assert not over, (
        f"这些条目被单条重复问了多次：{over}（调用序列 {p.single_sources}）"
        " —— already_failed 没在递归层之间共享"
    )


def test_batch_retry_still_attempts_multiple_rounds() -> None:
    r"""★ 重试**本身**要保留 —— 去掉重试会把"偶发抖动"变成漏译。

    场景：一条都救不回来。旧断言写的是"整批恰好重试 3 次"，
    但二分补救**自己也会发整批请求**（把缺的那半当批发），
    所以 `batch_calls` 不再等于外层重试次数。

    ⇒ 断言改成"外层重试确实发生了不止一轮"，这才是这条用例的**意图**
      （区分"没效果"和"没运行"）。
    """
    p = _Hybrid(_ctx(), ok_indices=set(), single_fails=True)
    p.translate_batch(_items(SRCS), "zh-CN")
    assert p.batch_calls > 1, (
        f"整批调用只发生了 {p.batch_calls} 次 —— 重试层没了，"
        "偶发抖动会变成漏译"
    )


def test_break_on_partial_rescue_avoids_excess_rounds() -> None:
    r"""救回来一部分就**不要再无谓重试整批** —— 那是浪费。

    这条与上一条是一对：想区分"没效果"和"没运行"，两种情形都得钉住。
    """
    p = _Hybrid(_ctx(), ok_indices={0})  # 第 0 条在补救里被救回
    p.translate_batch(_items(SRCS), "zh-CN")
    # 救回了一部分 ⇒ 外层提前 break，不该跑到第 3 轮
    # （外层每轮最多触发 1 次补救，补救内部有自己的批调用）
    assert p.batch_calls < len(SRCS) + 3, (
        f"整批调用 {p.batch_calls} 次，看起来在无谓重试"
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
