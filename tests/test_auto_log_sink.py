r"""★★★ `auto` 命令必须订阅每游戏的 `EventBus`，否则流水线日志全丢。

## 实测事故（这是日志沉默的真根因）

同一个诊断点，两条通道各发一次：

```
sys.stderr.write（绕过 bus）: **3 次**   ← 都落盘
self.bus.log(...)（走 bus） : **0 次**   ← 一次都没落盘
```

⇒ 不是"代码没执行"，不是缓冲，是 **`bus.log` 发出去没人接**。

### 根因

```python
# cli.py（auto 的每游戏循环）
bus = EventBus()                    # ← 每个游戏新建独立 bus
ctx = Context(config=cfg, events=bus, workspace=ws, logger=None)
pipe = Pipeline(ws, ctx)            # ← pipeline 用这个 bus
```

而 `auto` 的函数体里**没有任何 `bus.subscribe(...)`** ⇒ 全丢。

对比：`run` 命令建 bus **并**订阅（`bus.subscribe(on_event)`）
⇒ 所以 `novaloc run` 的日志一直正常，**只有 `auto` 有这个盲区**。

### 影响（远不止"看不见进度"）

`auto` 是**批量汉化整个库**的主路径，所以这些**全都静默**：

* `批次翻译失败（…）`（WARN）
* 质检阶段几十上百条"某条没有译文"（WARN）
* 字体/贴图阶段的告警
* 任何 `bus.log(..., severity=ERROR)`

## 本文件测什么

1. **★★ `auto` 的每游戏 bus 确实被订阅**（结构性守卫：
   源码里 `bus = EventBus()` 之后有 `bus.subscribe`）；
2. **★ 订阅者是 `_make_console_log_subscriber()`**
   （而不是某个只更新进度条、不落盘的东西）；
3. 订阅者**真的把 `log` 事件打到控制台**（用假 console 断言）；
4. **同类消息去重**（质检会发几十上百条 WARN，不能刷屏）；
5. **warn/error 有不同的前缀**（便于在日志里一眼看出）；
6. **只处理 `log` 事件**（不碰 progress/stage_*）；
7. 取消订阅会被调用（避免跨游戏累积）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.events import Event, EventBus  # noqa: E402
from novaloc.models import Severity  # noqa: E402

CLI_SRC = ROOT / "src" / "novaloc" / "cli.py"


# ----------------------------------------------------------------------
# 1. ★★ 结构性守卫：auto 的 bus 必须被订阅
# ----------------------------------------------------------------------
def test_auto_subscribes_its_per_game_bus() -> None:
    r"""★★ `auto` 里 `bus = EventBus()` 之后**必须**有 `bus.subscribe`。

    删掉它 ⇒ 流水线日志再次全部静默（那个 bug 回来了）。
    症状是"跑了几小时日志一行不打印，无法区分正常与卡死"。
    """
    src = CLI_SRC.read_text(encoding="utf-8")
    # 用缩进定位函数体（见 `_auto_body` 的说明：切片正则容易算错边界）
    _ = src
    body = _auto_body()
    assert "bus = EventBus()" in body, "auto 里没建 bus（实现变了？）"
    assert re.search(r"bus\.subscribe\(", body), (
        "auto 建了 EventBus 却**没有订阅** ⇒ pipeline 的所有 bus.log 会被丢弃"
    )


def _auto_body() -> str:
    r"""取出 `def auto(` 的**函数体**源码。

    ## 为什么不能简单用正则截

    我第一版写 `re.search(r"\ndef \w", src[m.end():])` —— 那个切片
    **从 `def auto(` 之后**开始，所以"下一个顶层 def"的**相对位置**
    必须再加回 `m.end()`，否则截出来的区间完全不对
    （实测截到了 `run` 命令里的 `bus.subscribe(on_event)`）。

    ⇒ 这里用**缩进**来判定：从 `def auto(` 起，收集到下一个
    **顶格**的 `def`/`class`/`@` 之前。
    """
    src = CLI_SRC.read_text(encoding="utf-8")
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"def auto\(", ln))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        # 顶格的 def / class / @decorator ⇒ 上一个函数结束
        if re.match(r"(def |class |@)", ln):
            break
        out.append(ln)
    return "\n".join(out)


def test_subscriber_is_the_console_logger() -> None:
    r"""★★ 订阅者必须是那个**落盘到控制台**的日志器。

    若换成"只更新 Rich 进度条"的东西（像 `run` 里的那部分），
    在输出重定向时**进度条不渲染** ⇒ 等于没订阅。
    """
    body = _auto_body()
    # ⚠️ `auto` 体内**先有**一个 `bus.subscribe(on_event)`（实时进度面板那段），
    #    所以不能取"第一个 bus.subscribe" —— 要取**每游戏 bus** 那个
    #    （它就是 `_unsub_log = bus.subscribe(_make_console_log_subscriber())`）。
    assert "_make_console_log_subscriber()" in body, (
        "auto 没有订阅**落盘到控制台**的日志器 ⇒ pipeline 的 bus.log 全丢"
    )
    sub = re.search(r"bus\.subscribe\(_make_console_log_subscriber\(\)\)", body)
    assert sub, (
        "auto 里没有 `bus.subscribe(_make_console_log_subscriber())`"
        f"（只有 {re.findall(r'bus[.]subscribe[(][^)]*[)]', body)}）"
    )


# ----------------------------------------------------------------------
# 2. 订阅者真的打印
# ----------------------------------------------------------------------
def _collect(monkeypatch, msgs: list[tuple[str, str, Severity]]) -> None:
    """把订阅者内部的 `console.print` 捕获下来。"""
    import novaloc.cli as cli

    def fake_print(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        msgs.append((" ".join(str(a) for a in args), kwargs.get("style", ""), Severity.INFO))

    monkeypatch.setattr(cli.console, "print", fake_print)


def test_subscriber_prints_log_events(monkeypatch) -> None:
    r"""★ `log` 事件必须真的被打出来。

    这是那个 bug 的核心：订阅者存在与否，决定日志是"有"还是"全无"。
    """
    import novaloc.cli as cli

    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    sub = cli._make_console_log_subscriber()
    bus = EventBus()
    bus.subscribe(sub)
    bus.log("开始翻译：49,733 条待处理", stage="translate")
    assert printed, "log 事件没被打出来"
    assert "开始翻译" in printed[0], f"内容不对：{printed[0]}"


def test_subscriber_ignores_non_log_events(monkeypatch) -> None:
    r"""★ 只处理 `log`；`progress`/`stage_*` 不归它管。

    `progress` 归 Rich 进度条（`run` 用），若这里也打会重复刷屏。
    """
    import novaloc.cli as cli

    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    sub = cli._make_console_log_subscriber()
    bus = EventBus()
    bus.subscribe(sub)
    bus.progress("translate", 0.5, "已翻译 100/200")
    bus.emit(Event("stage_start", stage="translate"))
    assert printed == [], f"非 log 事件被打出来了：{printed}"


# ----------------------------------------------------------------------
# 3. 去重与级别前缀
# ----------------------------------------------------------------------
def test_same_message_is_deduped(monkeypatch) -> None:
    r"""★★ 同类消息只打前 3 条 —— 质检会发几十上百条 WARN。

    不去重的话终端/日志被刷爆，真正重要的信息被淹掉
    （实测：质检阶段为每条没译文的串各发一条）。
    """
    import novaloc.cli as cli

    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    sub = cli._make_console_log_subscriber(max_same=3)
    bus = EventBus()
    bus.subscribe(sub)
    for _ in range(50):
        bus.log("这条没有译文：xxx", stage="qa", severity=Severity.WARN)
    assert len(printed) == 3, f"去重失效，打了 {len(printed)} 条"


def test_different_messages_all_print(monkeypatch) -> None:
    """不同内容的消息**不该**被去重（百分比不同的进度就是这类）。"""
    import novaloc.cli as cli

    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    sub = cli._make_console_log_subscriber(max_same=3)
    bus = EventBus()
    bus.subscribe(sub)
    for i in range(10):
        bus.log(f"进度 {i * 10}/100（{i * 10}%）", stage="translate")
    assert len(printed) == 10, f"不同消息被误去重：只打了 {len(printed)} 条"


def test_warn_and_error_have_prefixes(monkeypatch) -> None:
    r"""★ WARN/ERROR 要有不同前缀 —— 便于在日志里一眼看出严重度。"""
    import novaloc.cli as cli

    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    sub = cli._make_console_log_subscriber()
    bus = EventBus()
    bus.subscribe(sub)
    bus.log("批次翻译失败", stage="translate", severity=Severity.WARN)
    bus.log("回写失败", stage="apply", severity=Severity.ERROR)
    assert any("⚠" in p for p in printed), f"WARN 没前缀：{printed}"
    assert any("✗" in p for p in printed), f"ERROR 没前缀：{printed}"


# ----------------------------------------------------------------------
# 4. 取消订阅
# ----------------------------------------------------------------------
def test_unsubscribe_is_called() -> None:
    r"""★ 每个游戏结束后取消订阅，避免订阅者跨游戏累积。

    `subscribe` 返回一个 `unsub` 回调；不调用的话，
    处理 167 个游戏后会累积 167 个订阅者（每次都打一遍 ⇒ 日志爆炸）。
    """
    src = CLI_SRC.read_text(encoding="utf-8")
    assert "_unsub_log = bus.subscribe(" in src, "订阅的返回值没接住"
    assert "_unsub_log()" in src, "没有取消订阅 ⇒ 会跨游戏累积"
