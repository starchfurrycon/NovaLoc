r"""超时/慢请求**必须留下痕迹** —— 一次 11 分钟的静默就是这么来的。

## 实测的"看起来卡死"

一次真实端到端跑（`Midnight Exhibitionist DX`）在某个时刻之后再无输出。
我 11.3 分钟后来查，看到的是：

| 观测项 | 值 |
| --- | --- |
| `novaloc` 进程 ΔCPU（12 秒内） | **0s** |
| 到 Ollama 的 TCP 连接 | **Established** |
| Ollama 进程 ΔCPU | **0s** |
| Ollama 里模型的到期时间 | 只剩 3 秒（⇒ 已空闲，没在生成） |

结论明确：**客户端在等一个不会来的响应。**

## 但"等了多久"这件事当时**无法从日志回答**

`request_timeout_s = 300`，所以单次最多等 5 分钟，而静默是 11.3 分钟
（≈ 2 次超时）。**这两种情况的修法完全不同**：

* "一次请求卡了 11 分钟" ⇒ 超时失效，要**补一道自己的总时限**；
* "两次各 5 分钟的超时" ⇒ 超时有效，只是**沉默**，要**把超时说出来**。

而当时日志里**既没有时间戳、也没有任何超时记录**，所以判据断在这里。

⇒ 先补**观测**：每次请求记耗时，慢的额外 WARNING，超时也 WARNING。
`SLOW_REQUEST_S = 60`（常态实测：40 条一批约 7s、12 条约 3s，60s 足够宽松）。

## 守什么

1. 成功但慢的请求 → 有 WARNING，且**写明耗时**；
2. 超时 → 有 WARNING（原先**一个字都没有**）；
3. 正常快请求 → **不**打 WARNING（否则日志会被淹掉）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.ollama_client import (  # noqa: E402
    SLOW_REQUEST_S,
    OllamaClient,
    OllamaError,
)


class _Clock:
    """可控时钟：`time.time()` 被调几次都不会 `StopIteration`。

    第一版用 `iter([0.0, 5.0])` 配 `next()`，结果 **3 条用例全
    `StopIteration`** —— 因为 `time.time()` 在 `_request` 里被调用的
    **次数不是我以为的 2 次**（httpx 自己会取时间、日志记录也会）。
    **固定长度的迭代器是脆的**，换成一个可以来回拨的时钟：
    `advance()` 显式跳时间，多调几次也无害。
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _client(monkeypatch: pytest.MonkeyPatch, handler) -> OllamaClient:  # noqa: ANN001
    """构造一个不真的连网络的 client。"""
    c = OllamaClient("http://127.0.0.1:11434", timeout=1.0)
    transport = httpx.MockTransport(handler)
    c._client = httpx.Client(transport=transport, timeout=1.0)  # noqa: SLF001
    return c


def test_slow_successful_request_logs_warning_with_elapsed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 成功但慢的请求必须有 WARNING，且写明耗时。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    c = _client(monkeypatch, handler)
    # 让时钟"跳"过慢阈值：先装好时钟，再让 handler 只推进一次。
    clock = _Clock()
    monkeypatch.setattr("novaloc.translate.ollama_client.time.time", clock)

    def slow_handler(request: httpx.Request) -> httpx.Response:
        clock.advance(SLOW_REQUEST_S + 5.0)
        return httpx.Response(200, json={"ok": True})

    c._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(slow_handler), timeout=1.0
    )

    with caplog.at_level(logging.WARNING):
        c._request("GET", "/api/tags")  # noqa: SLF001

    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "慢请求没有打 WARNING —— 那次 11 分钟静默就是这么来的"
    joined = "\n".join(warns)
    assert "/api/tags" in joined, f"WARNING 里没说是哪个请求：{warns}"
    assert f"{SLOW_REQUEST_S + 5.0:.1f}s" in joined, f"WARNING 里没有耗时：{warns}"


def test_timeout_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 超时必须留痕（原先只变成一句异常交给重试层，日志里看不到"等了多久"）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("模拟卡住", request=request)

    c = _client(monkeypatch, handler)
    # 真实的 5 分钟超时**同时**是"慢请求"，所以这里也让时钟跳一次 ——
    # 否则测的是"快 + 超时"这个不存在的组合。
    clock = _Clock()
    monkeypatch.setattr("novaloc.translate.ollama_client.time.time", clock)

    def slow_timeout(request: httpx.Request) -> httpx.Response:
        clock.advance(305.0)
        raise httpx.ReadTimeout("模拟卡住", request=request)

    c._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(slow_timeout), timeout=1.0
    )

    with caplog.at_level(logging.WARNING), pytest.raises(OllamaError):
        c._request("POST", "/api/chat")  # noqa: SLF001

    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "超时没有打 WARNING"
    assert "超时" in "\n".join(warns), f"WARNING 里没提超时：{warns}"


def test_fast_request_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """正常快请求**不许**打 WARNING —— 否则日志被淹，慢的也看不见了。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    c = _client(monkeypatch, handler)
    clock = _Clock()
    monkeypatch.setattr("novaloc.translate.ollama_client.time.time", clock)

    with caplog.at_level(logging.WARNING):
        c._request("GET", "/api/tags")  # noqa: SLF001

    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not warns, f"快请求不该有 WARNING：{warns}"


def test_timeout_raises_ollama_error_not_bare_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时要变成 `OllamaError`（在 `_RETRYABLE` 里），不能是裸 httpx 异常。

    否则重试层接不住 ⇒ 异常穿过整个翻译层 ⇒ 整轮作废（实测 27 分钟那次）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("模拟卡住", request=request)

    c = _client(monkeypatch, handler)
    with pytest.raises(OllamaError):
        c._request("POST", "/api/chat")  # noqa: SLF001
