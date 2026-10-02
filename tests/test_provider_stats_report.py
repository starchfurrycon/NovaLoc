"""验证 `_report_provider_stats`：把一直没被输出的统计汇总出来。

## 为什么值得一条测试

`ollama_provider.stats` 里的计数**一直在记**，但流水线**从不打印**。
实测代价（一次真实端到端跑）：`批 12 条里只回 2 条` 出现 8 次、
`Ollama 返回 500: prediction aborted` 若干次 —— 每批要多花 11 次单条请求，
而收尾报告一个字都没提。

所以这条能力有两个必须守住的点：

1. **有降级时必须报出来** —— 否则又回到"藏在计数里"；
2. **接口不匹配时不能炸** —— 别的 provider 可能没有 `stats_snapshot()`，
   或者快照本身抛异常。翻译已经跑完了，不能因为打印统计而失败。
"""

from __future__ import annotations

import logging

import pytest

from novaloc.pipeline.stages import Pipeline


class _FakeProvider:
    def __init__(self, stats: dict | None = None, *, boom: bool = False) -> None:
        self._stats = stats or {}
        self._boom = boom

    def stats_snapshot(self) -> dict:
        if self._boom:
            raise RuntimeError("快照坏了")
        return dict(self._stats)


def _report(provider: object) -> tuple[str, str]:
    """调用被测方法，返回 (info 文本, warning 文本) 的拼接。"""
    p = Pipeline.__new__(Pipeline)
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("novaloc.pipeline.stages")
    h = _H()
    old = logger.level
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        p._report_provider_stats(provider)
    finally:
        logger.removeHandler(h)
        logger.setLevel(old)
    # ⚠️ 用 `record.getMessage()` —— 它已经把 `args` 格式化进去了。
    #    写成 `r.getMessage() % r.args` 会**二次格式化**，
    #    当文本里本来就有 `%` 时会抛 `TypeError: not all arguments converted`
    #    （实测踩过：`平均每批 %.1f 条` 被二次套用）。
    def _text(r: logging.LogRecord) -> str:
        return r.getMessage()

    info = " | ".join(_text(r) for r in records if r.levelno == logging.INFO)
    warn = " | ".join(_text(r) for r in records if r.levelno >= logging.WARNING)
    return info, warn


def test_summary_reports_batch_and_item_counts() -> None:
    info, _ = _report(_FakeProvider({"batches": 42, "items": 1200}))
    assert "1200" in info and "42" in info, f"应报出条数与批数，实际：{info!r}"


def test_nonzero_degradation_counters_are_warned() -> None:
    r"""★ 有降级时必须 **warn**，且要带上"多出多少请求"。

    这是本能力的全部意义 —— 把"藏在计数里的慢"变成一眼能看到的数字。
    """
    _, warn = _report(
        _FakeProvider(
            {
                "batches": 42,
                "items": 1200,
                "retries": 3,
                "single_fallbacks": 11,
                "batch_mostly_missing": 8,
            }
        )
    )
    assert warn, "有降级却没有 warning"
    for key in ("single_fallbacks=11", "batch_mostly_missing=8", "retries=3"):
        assert key in warn, f"warning 里缺 {key}：{warn!r}"
    assert "11" in warn, "应报出逐条降级带来的额外请求数"


def test_clean_run_does_not_warn() -> None:
    r"""★ 全零时**不能** warn —— 否则每条警告都失去意义。"""
    info, warn = _report(_FakeProvider({"batches": 42, "items": 1200}))
    assert warn == "", f"无降级却报了 warning：{warn!r}"
    assert info, "无降级时仍应有一条 info 统计"


def test_zero_valued_counters_are_omitted() -> None:
    r"""★ 全零项不该出现在 warning 里（噪音）。

    但 `batches`/`items` 作为分母要留着 —— 否则看不懂比例。
    """
    _, warn = _report(
        _FakeProvider({"batches": 5, "items": 100, "retries": 0, "single_fallbacks": 2})
    )
    assert "retries=0" not in warn, f"零值项不该出现：{warn!r}"
    assert "single_fallbacks=2" in warn


def test_provider_without_snapshot_is_ignored() -> None:
    r"""★ 没有 `stats_snapshot()` 的 provider（别的实现）不能让它炸。"""
    info, warn = _report(object())
    assert info == "" and warn == ""


def test_snapshot_exception_does_not_propagate() -> None:
    r"""★ 快照抛异常时**吞掉** —— 翻译已经跑完，不能因为打印统计而失败。"""
    info, warn = _report(_FakeProvider(boom=True))
    assert info == "" and warn == ""


@pytest.mark.parametrize("stats", [{}, None])
def test_empty_or_none_stats_are_noop(stats: dict | None) -> None:
    info, warn = _report(_FakeProvider(stats))
    assert warn == ""
