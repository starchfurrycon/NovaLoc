r"""★★ 进度心跳：让"跑了很久"与"卡死了"在日志里**可区分**。

## 实测问题

worker 输出重定向到文件时，`bus.progress()` 的**唯一订阅者是
Rich 的交互式进度条**（`cli.py`：`progress.update(task_id, ...)`）
⇒ **重定向到文件时它不渲染** ⇒ 进度**完全不落盘**。

实测日志只有 **10,838 字节、28 个换行、含「已翻译」的片段 0 个**，
大游戏跑 **7.5 小时一行未打印** ⇒ 从外部无法区分"正常跑大游戏"
和"卡死"（我这次是靠 `entries.jsonl` 的 mtime 才确认它活着）。

⇒ 加了 `_ProgressHeartbeat`：绕开 Rich，**直接往 `bus.log` 写**。

## 本文件测什么

1. **★ 跨过间隔就写**（而不是每批都写 —— 那会刷爆日志）；
2. **★ 间隔内不写**（对吞吐零影响）；
3. 内容含**已译/总数/百分比**与**速率**；
4. **ETA 只在还有剩余时出现**（已完成就不该报"还需 0 小时"）；
5. **速率用本窗口的增长算** —— 这样"变慢了"能立刻看出来，
   而不是被历史平均掩盖；
6. `force=True` 能强制写（给收尾用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.events import Event, EventBus  # noqa: E402
from novaloc.pipeline.stages import _ProgressHeartbeat  # noqa: E402

STAGES_SRC = ROOT / "src" / "novaloc" / "pipeline" / "stages.py"


class _Recorder:
    """收集 `bus.log` 发出的消息。"""

    def __init__(self) -> None:
        self.msgs: list[tuple[str, str]] = []

    def __call__(self, ev: Event) -> None:
        if ev.kind == "log":
            self.msgs.append((ev.stage, ev.message or ""))

    @property
    def texts(self) -> list[str]:
        return [m for _s, m in self.msgs]


@pytest.fixture()
def bus_and_rec() -> tuple[EventBus, _Recorder]:
    bus = EventBus()
    rec = _Recorder()
    bus.subscribe(rec)
    return bus, rec


# ----------------------------------------------------------------------
# 1. ★ 间隔内不写、跨过就写
# ----------------------------------------------------------------------
def test_does_not_write_within_interval(bus_and_rec, monkeypatch) -> None:
    r"""★★ 间隔内**不写** —— 否则每批一行会刷爆日志（且拖慢吞吐）。

    实测：一个 4.5 万条的游戏有几千个批，每批一行日志会淹掉
    真正重要的信息（阶段头、完成、写回）。
    """
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    # 时间不动 ⇒ 无论调多少次都不该写
    for i in range(50):
        hb(i * 100, 10000)
    assert rec.texts == [], f"间隔内写了 {len(rec.texts)} 行"


def test_writes_after_interval(bus_and_rec, monkeypatch) -> None:
    """跨过间隔就写一行。"""
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    t = [1000.0]
    monkeypatch.setattr("novaloc.pipeline.stages.time.monotonic", lambda: t[0])
    hb._t0 = 1000.0
    hb._last = 1000.0
    t[0] = 1061.0  # 过了 61 秒
    hb(500, 10000)
    assert len(rec.texts) == 1, f"跨过间隔却没写：{rec.texts}"


# ----------------------------------------------------------------------
# 2. 内容
# ----------------------------------------------------------------------
def test_message_has_counts_and_percent(bus_and_rec, monkeypatch) -> None:
    r"""内容要含 **已译/总数/百分比** 与 **速率**。

    这样一眼就能看出"推进到哪了"和"快不快"，不需要再去算。
    """
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    t = [0.0]
    monkeypatch.setattr("novaloc.pipeline.stages.time.monotonic", lambda: t[0])
    hb._t0 = 0.0
    hb._last = 0.0
    t[0] = 60.0
    hb(3000, 12000)
    assert rec.texts, "没写"
    msg = rec.texts[0]
    assert "3,000" in msg and "12,000" in msg, f"缺条数：{msg}"
    assert "25%" in msg, f"缺百分比：{msg}"
    assert "条/分" in msg, f"缺速率：{msg}"


def test_rate_uses_current_window_not_history(bus_and_rec, monkeypatch) -> None:
    r"""★★ 速率用**本窗口**的增长算 —— 这样"变慢了"能立刻看出来。

    若用累计平均，一个先快后慢的游戏会一直显示"很快"，
    掩盖了实际的降速（那正是需要被发现的情形）。
    """
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    t = [0.0]
    monkeypatch.setattr("novaloc.pipeline.stages.time.monotonic", lambda: t[0])
    hb._t0 = 0.0
    hb._last = 0.0
    hb._last_done = 0
    # 第一个窗口：60 秒涨 6000 条 ⇒ 6000 条/分
    t[0] = 60.0
    hb(6000, 100000)
    # 第二个窗口：60 秒只涨 60 条 ⇒ 60 条/分（降速 100 倍）
    t[0] = 120.0
    hb(6060, 100000)
    assert len(rec.texts) == 2
    second = rec.texts[1]
    assert "60 条/分" in second, (
        f"第二个窗口的速率应为 60（本窗口增长），实际：{second}"
    )


def test_eta_only_when_remaining(bus_and_rec, monkeypatch) -> None:
    r"""★ 已完成时**不该**报"预计还需 0.0 小时"（那是噪声）。"""
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    t = [0.0]
    monkeypatch.setattr("novaloc.pipeline.stages.time.monotonic", lambda: t[0])
    hb._t0 = 0.0
    hb._last = 0.0
    t[0] = 60.0
    hb(10000, 10000)  # 全做完
    assert rec.texts
    assert "预计" not in rec.texts[0], f"完成时不该有 ETA：{rec.texts[0]}"


def test_force_always_writes(bus_and_rec) -> None:
    """`force=True` 必须绕过节流（给收尾用）。"""
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=9999.0)
    hb(1, 10, force=True)
    hb(2, 10, force=True)
    assert len(rec.texts) == 2


def test_zero_total_does_not_crash(bus_and_rec, monkeypatch) -> None:
    """`total=0` 不能除零崩掉（抽取失败的游戏会出现）。"""
    bus, rec = bus_and_rec
    hb = _ProgressHeartbeat(bus, "translate", interval_s=60.0)
    t = [0.0]
    monkeypatch.setattr("novaloc.pipeline.stages.time.monotonic", lambda: t[0])
    hb._t0 = 0.0
    hb._last = 0.0
    t[0] = 60.0
    hb(0, 0)  # 不该抛
    assert rec.texts


# ----------------------------------------------------------------------
# 3. 结构性守卫
# ----------------------------------------------------------------------
def test_heartbeat_is_called_in_batch_loop() -> None:
    r"""★★ 批循环里必须**真的调** `heartbeat`。

    否则这个类就是死代码，日志照样沉默（那个问题又回来了）。
    """
    src = STAGES_SRC.read_text(encoding="utf-8")
    assert "heartbeat = _ProgressHeartbeat(" in src, "没有创建心跳实例"
    assert "heartbeat(done, total)" in src, "批循环里没调用心跳"
    # 且必须在 throttle 之后（说明插在进度更新的同一位置）
    i_thr = src.find("throttle(done / max(1, total)")
    i_hb = src.find("heartbeat(done, total)")
    assert 0 <= i_thr < i_hb, "心跳没放在进度更新处"


def test_documents_why_rich_progress_is_not_enough() -> None:
    r"""源码里要记着根因（Rich 进度条在重定向时不渲染）。"""
    src = STAGES_SRC.read_text(encoding="utf-8")
    assert "Rich" in src and "不渲染" in src, "没记录根因：Rich 进度条不落盘"
