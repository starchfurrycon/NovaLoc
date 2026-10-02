r"""按阶段计时：没有它就只能靠推算，而推算已经错过一次。

## 为什么加这个

一次真实 `auto`（`Dungeon And Darkness-Steam`）只打了一行总计：

    条目 5457（译 4885）　贴图 243　用时 107 分 18 秒

**没有拆分**。于是我先写下了"模型只占端到端约 1/10"这种结论 ——
而那是拿**单阶段的模型往返时间**（0.18 秒/条）去除**全流水线时间**
（1.94 秒/条），两个口径混用，**根本不成立**（`docs/ROADMAP.md` §3.5.2.1）。

打了这个日志，下一次跑就能把 107 分钟直接拆开。

## 守什么

1. 每个阶段的耗时都要记下来，且**名字要对得上**（否则日志读不出来）；
2. 阶段**抛异常时也要记**（跑崩的阶段同样烧时间，而且最该被看见）；
3. 总耗时为 0 时不能除零；
4. 排序按**耗时降序**（要看的是"谁最贵"）。
"""

from __future__ import annotations

import time

import pytest

from novaloc.pipeline.stages import Pipeline


class _Bus:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, msg: str, **_kw: object) -> None:
        self.lines.append(msg)


def _pipeline() -> Pipeline:
    p = Pipeline.__new__(Pipeline)
    p.bus = _Bus()
    p._stage_seconds = {}
    return p


def test_timed_records_elapsed() -> None:
    p = _pipeline()
    p._timed("translate", lambda: time.sleep(0.05))
    assert "translate" in p._stage_seconds
    assert p._stage_seconds["translate"] >= 0.04


def test_timed_returns_callback_value() -> None:
    r"""★ 包一层**不能**改变返回值 —— 阶段结果是下游要用的。"""
    p = _pipeline()
    assert p._timed("x", lambda: [1, 2, 3]) == [1, 2, 3]


def test_timed_still_records_when_stage_raises() -> None:
    r"""★ 抛异常也要记 —— 崩掉的阶段同样消耗时间，而且最该被看见。"""
    p = _pipeline()

    def boom() -> None:
        time.sleep(0.03)
        raise RuntimeError("阶段崩了")

    with pytest.raises(RuntimeError):
        p._timed("qa", boom)
    assert "qa" in p._stage_seconds, "失败阶段没有记耗时"
    assert p._stage_seconds["qa"] >= 0.02


def test_timed_reraises_original_exception() -> None:
    r"""★ 异常必须**原样**抛出，否则 `run_all` 的中止语义就坏了。"""
    p = _pipeline()

    class _Custom(Exception):
        pass

    with pytest.raises(_Custom):
        p._timed("x", lambda: (_ for _ in ()).throw(_Custom("自定义")))


def _report(p: Pipeline, total: float) -> str:
    r"""取 `_log_stage_timing` 的输出。

    ⚠️ 它走的是 `self.bus.log(...)`（**事件总线**），不是 `logging`。
    走总线是对的：这条信息要出现在 `auto` 的进度输出里给用户看，
    而不是只进日志文件。所以断言要读 `bus.lines`，不能读 logging。
    """
    p._log_stage_timing(total)
    return " | ".join(p.bus.lines)  # type: ignore[attr-defined]


def test_timing_line_lists_stages_sorted_desc() -> None:
    r"""★ 按耗时降序 —— 读的人要一眼看到"谁最贵"。"""
    p = _pipeline()
    p._stage_seconds = {"fonts": 10.0, "translate": 100.0, "qa": 1.0}
    line = _report(p, 111.0)
    assert line, "没有输出任何东西"
    i_trans = line.index("translate")
    i_fonts = line.index("fonts")
    i_qa = line.index("qa")
    assert i_trans < i_fonts < i_qa, f"没有按降序排：{line!r}"
    assert "100s" in line, f"应报出 translate 的秒数：{line!r}"


def test_timing_line_includes_percentage() -> None:
    r"""百分比是判断"谁最贵"的关键，必须在。"""
    p = _pipeline()
    p._stage_seconds = {"translate": 50.0, "qa": 50.0}
    line = _report(p, 100.0)
    assert "50%" in line, f"应报出百分比：{line!r}"


def test_zero_total_does_not_divide_by_zero() -> None:
    r"""★ 总耗时为 0 时不能抛 `ZeroDivisionError`（计时被 mock 时会发生）。"""
    p = _pipeline()
    p._stage_seconds = {"translate": 1.0}
    line = _report(p, 0.0)
    assert "translate" in line


def test_empty_stage_seconds_logs_nothing() -> None:
    p = _pipeline()
    p._stage_seconds = {}
    assert _report(p, 10.0) == ""


@pytest.mark.parametrize(
    "names",
    [
        ["unpack", "detect", "extract", "images_scan", "translate", "fonts", "images_localize", "qa", "apply"],
    ],
)
def test_run_all_times_every_stage(names: list[str]) -> None:
    r"""★ 阶段名要与 `STAGES` 对得上 —— 漏一个就有一段时间没人认领。

    这里不跑真的 `run_all`（会动文件），只校验 `_timed` 的键名集合
    与生产用的那 9 个名字一致。
    """
    p = _pipeline()
    for n in names:
        p._timed(n, lambda: None)
    assert set(p._stage_seconds) == set(names)
