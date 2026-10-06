r"""★★ 单条请求必须有**更短的**超时上限（否则病态条目烧掉整个队列）。

## 实测缺陷（算力黑洞）

`CrossdresserKiller`（**582 条的小游戏**）卡在最后 **6 条**超长多行
韩文/日文条目上（880~3,196 字，`placeholder_broken`）：

```
全库吞吐：0 条/分钟（3 分钟窗口）
llama-server：满负荷在算（120 秒烧 113.5s CPU、GPU 78%）
⇒ 每条消耗 56.8s CPU（正常 0.3~0.5s）—— 浪费约 100~190 倍
```

**机制**：

```
单条与批次共用 `request_timeout_s`（300 秒）
调用方重试 3 次
⇒ 一条病态条目最多烧 900 秒 = 15 分钟
```

跨轮次的 `_MAX_FAIL_STREAK = 2` **只在下一轮生效**，
管不了同一轮内这 15 分钟。

## 修法

新增 `single_request_timeout_s = 90.0`（正常单条 3~10 秒 ⇒ 9 倍余量），
并只在 `_call_single_once` 里传它 —— **批次仍用 300 秒**
（批次可能很大，需要更长）。

## 本文件测什么

1. 配置项存在且取值合理（比 `request_timeout_s` 短、又不至于误杀）；
2. `chat()` 接受 `timeout` 参数且默认 `None`（不传 = 用客户端默认）；
3. 结构性守卫：`_call_single_once` 传了 `timeout=`，而
   `_call_batch` **没有**传（批次要保持长上限）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.translate.ollama_client import OllamaClient  # noqa: E402

PROV = ROOT / "src" / "novaloc" / "translate" / "ollama_provider.py"


# ----------------------------------------------------------------------
# 1. 配置项
# ----------------------------------------------------------------------
def test_single_timeout_exists_and_is_shorter() -> None:
    r"""单条上限必须**短于**批次上限，否则修了等于没修。"""
    o = Config().ollama
    single = float(o.single_request_timeout_s)
    batch = float(o.request_timeout_s)
    assert single < batch, f"单条 {single}s 不短于批次 {batch}s ⇒ 病态条目仍会烧满"


def test_single_timeout_has_safe_headroom() -> None:
    r"""不能太短 —— 长条目的正常单条翻译要 20~30 秒。

    实测正常单条 3~10 秒，但最长条目（3,196 字）会更久。
    取 90 秒 ⇒ 至少 3 倍余量（对 30 秒的长条目）。
    """
    single = float(Config().ollama.single_request_timeout_s)
    assert single >= 60.0, f"{single}s 对长条目（20~30s）余量不足，会误杀正常请求"
    assert single <= 150.0, f"{single}s 太长，省不下多少算力"


# ----------------------------------------------------------------------
# 2. chat() 的 timeout 参数
# ----------------------------------------------------------------------
def test_chat_accepts_optional_timeout() -> None:
    """`chat()` 要能接受 `timeout`，且**默认 None**（不改变既有行为）。"""
    import inspect

    sig = inspect.signature(OllamaClient.chat)
    assert "timeout" in sig.parameters, "chat() 没有 timeout 参数 ⇒ 单条无法用短上限"
    assert sig.parameters["timeout"].default is None, "默认值应为 None（用客户端默认）"


# ----------------------------------------------------------------------
# 3. 结构性守卫：只给单条加，不给批次加
# ----------------------------------------------------------------------
def test_single_path_passes_timeout() -> None:
    """`_call_single_once` 必须传 `timeout=`。"""
    src = PROV.read_text(encoding="utf-8")
    assert "single_request_timeout_s" in src, "_call_single_once 可能没传单条超时"


def test_batch_path_keeps_long_timeout() -> None:
    r"""★★ `_call_batch` **不该**传 `timeout=` —— 批次需要长上限。

    批次里可能有很多条目，300 秒是合理的。若它也传了 90 秒，
    大批次会被误杀 ⇒ 退化成逐条 ⇒ 更慢（正是本项目修过多次的坑）。
    """
    src = PROV.read_text(encoding="utf-8")
    lines = src.splitlines()
    # 找 _call_batch 函数体，直到下一个 def
    start = next(i for i, ln in enumerate(lines) if "def _call_batch" in ln)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("    def ")),
        len(lines),
    )
    body = "\n".join(lines[start:end])
    assert "_chat(" in body, "_call_batch 里找不到 _chat 调用"
    assert "timeout=" not in body, "_call_batch 不该传 timeout=（批次需要 300 秒长上限）"
