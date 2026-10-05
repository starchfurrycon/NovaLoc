r"""★ 并发派发：**必须与串行产生完全相同的结果**，只是更快。

## 为什么需要这个测试

实测并发收益（`.scratch/_probe_concurrency.py`，25 条批）：

| 客户端并发 | 吞吐(条/分) |
| --- | --- |
| 1 | 289 |
| **4** | **655** |

但并发改造有**两类**风险，都必须钉住：

1. **结果不一致**：批次结果按提交顺序回收（`sorted(_futs)`），
   校验段再按 `enumerate(batches)` 顺序跑 ⇒ 顺序必须与串行完全相同。
   如果变成"谁先完成谁先算"，`out` / `first_of` 的回填就乱了。
2. **并发下才暴露的状态竞争**：`_respect_gate()`、`self.stats` 的
   读-改-写在多线程下可能出问题（丢计数可接受，**异常不可接受**）。

## 本文件测什么

* 同一批输入，并发 1 与并发 4 的**译文结果逐条相同**；
* 并发下**不抛异常**；
* 并发**确实在并行**（用"人工延迟"制造可观测的重叠：
  若串行，总耗时≈N×delay；若并行 4 路，总耗时≈⌈N/4⌉×delay）。

最后一条是**行为性**的：它不依赖具体耗时数字，只看"总耗时明显小于
N×delay"，所以不会因为机器快慢而 flaky（阈值放得很宽）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context, TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402

N = 40
DELAY = 0.20


def _items(n: int) -> list[TranslateItem]:
    return [
        TranslateItem(
            unit=TextUnit(
                uid=f"u{i}",
                # 用**普通文本**类型（不进 _SHORT_KINDS/_CAREFUL_KINDS），
                # 让它按 25 条上限成批 ⇒ 40 条会切成 2 批。
                kind=TextKind.CHARACTER_NAME,
                source=f"sentence number {i} of the batch",
                location=TextLocation(file="Map.json", pointer=f"/e/{i}"),
            ),
            glossary=[],
            context_lines=[],
        )
        for i in range(n)
    ]


class _Slow(OllamaTranslationProvider):
    """`_call_batch` 睡 `delay` 秒再返回全量译文（模拟真实往返）。"""

    def __init__(self, ctx: Context, *, delay: float) -> None:
        super().__init__(ctx)
        self.delay = delay
        self.concurrent_peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def _call_batch(self, batch_items, masked):  # noqa: ANN001, ARG002
        with self._lock:
            self._live += 1
            self.concurrent_peak = max(self.concurrent_peak, self._live)
        try:
            time.sleep(self.delay)
            return {i: f"译{it.unit.source}" for i, it in enumerate(batch_items)}
        finally:
            with self._lock:
                self._live -= 1

    def _call_single(self, item, masked, **kwargs):  # noqa: ANN001, ARG002
        time.sleep(self.delay)
        return f"译{item.unit.source}"


def _ctx(workers: int) -> Context:
    cfg = Config()
    cfg.ollama.concurrency = workers
    return Context(config=cfg, events=EventBus())


def test_concurrent_results_match_serial() -> None:
    r"""★★ 并发 4 与串行 1 的**译文逐条相同**（顺序不能乱）。"""
    serial = _Slow(_ctx(1), delay=0.0)
    out_s = serial.translate_batch(_items(N), "zh-CN")

    par = _Slow(_ctx(4), delay=0.0)
    out_p = par.translate_batch(_items(N), "zh-CN")

    assert len(out_s) == len(out_p) == N
    ts = [e.target for e in out_s]
    tp = [e.target for e in out_p]
    assert ts == tp, (
        "并发与串行的译文顺序/内容不一致 —— "
        "首个不同位置 "
        f"{next((i for i, (a, b) in enumerate(zip(ts, tp, strict=False)) if a != b), None)}"
    )
    assert all(t.strip() for t in tp), "并发下有漏译"


def test_concurrency_actually_overlaps() -> None:
    r"""★★ 并发**确实在并行**（可观测的重叠），而不是白白配置。

    判据用"峰值同时进行的请求数"（`concurrent_peak`），
    而不是耗时 —— 后者容易被机器负载影响而 flaky。

    N=40 会被 `_make_batches` 切成若干批（普通文本上限 25）
    ⇒ 至少有 2 批。串行时峰值必然是 1；并发 4 时峰值应 ≥ 2。
    """
    par = _Slow(_ctx(4), delay=DELAY)
    par.translate_batch(_items(N), "zh-CN")
    assert par.concurrent_peak >= 2, (
        f"并发 4 时峰值同时请求数只有 {par.concurrent_peak} —— "
        "线程池没生效（仍在串行发批）"
    )


def test_serial_never_overlaps() -> None:
    """反向对照：并发=1 时**不该**出现重叠（否则说明并发没被正确关闭）。"""
    ser = _Slow(_ctx(1), delay=0.0)
    ser.translate_batch(_items(N), "zh-CN")
    assert ser.concurrent_peak == 1, (
        f"并发=1 时峰值 {ser.concurrent_peak} —— 不应该有重叠"
    )


def test_concurrency_one_still_works() -> None:
    """并发=1 是合法配置（小显存机器），必须照常出译文。"""
    p = _Slow(_ctx(1), delay=0.0)
    out = p.translate_batch(_items(N), "zh-CN")
    assert len(out) == N
    assert all((e.target or "").strip() for e in out)
