r"""★ 回归：**模型 500 绝不许逃出 `translate_batch`**（实测抓到的真 bug）。

## 抓到它的过程（值得留档）

端到端跑完 `Midnight Exhibitionist DX` 后，1480/3043 条 `failed`，
其中 **98.4% 带 `token repeat limit reached`**。按源文行数一列：

    1 行  → 成功率 97.5%
    2 行  → 成功率 26.3%
    3 行  → 成功率 23.2%

我做了三组对照（`.scratch/_batch_poison.py`，24 条真实失败源文）：

    S 逐条（批大小 1）    ：translated 22/24 = 92%
    M 批量（产品分批）第1次：translated  0/24 =  0%
    M 批量（产品分批）第2次：translated  0/24 =  0%

**0% 而不是 50%** 是不可能的"概率性失败" ⇒ 一定有一个**确定性**的东西。

抓完整调用栈（`.scratch/_trace_abort.py`）得到：

    translate_batch  → ollama_provider.py:1283  → _call_single
                     → _chat → client.chat → _request
    OllamaError: Ollama 返回 500：{"error":"prediction aborted, token repeat limit reached"}

## 根因

```python
class OllamaError(RuntimeError): ...        # ← 不是 ProviderError 的子类！
```

而两处"救援/重试"的 `except` 只写了 `ProviderError`：

* 块 3.5 的**标记重试**（`1283` 那次调用外面）；
* 块 3.7 的**逐行救援**。

⇒ 500 **逃出整个 `translate_batch`**。后果不只是"那几条 FAILED"，
而是**上层那条游戏的整轮翻译被中断** —— 比质量问题更严重。

两处都改成 `except _RETRYABLE`：`_RETRYABLE` 本就含 `ProviderError`
（是超集），所以不会放松任何既有行为，只是把漏掉的
`OllamaError`/`ModelMissing`/`OllamaNotRunning`/`UnicodeError`/`ValueError` 收进来。
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
from novaloc.core.registry import Context  # noqa: E402
from novaloc.models import EntryStatus, TextKind  # noqa: E402
from novaloc.translate.ollama_client import OllamaError  # noqa: E402
from novaloc.translate.ollama_provider import (  # noqa: E402
    _RETRYABLE,
    OllamaTranslationProvider,
)

pytestmark = requires_ollama

#: 实测触发该 bug 的真实源文形态：多行、含占位符（`\C[6]`）、>100 字符
REAL_SRC = (
    "Girl from the kingdom of Boheros.\n"
    r"Weapon Type: \C[6]Sword\C[0]" + "\n"
    "She is not good at sports."
)


class _Unit:
    def __init__(self, source: str, uid: str = "u1") -> None:
        self.kind = TextKind.ITEM_DESC
        self.source = source
        self.uid = uid
        self.max_chars = None


class _Item:
    def __init__(self, source: str, uid: str = "u1") -> None:
        self.unit = _Unit(source, uid)
        self.glossary: list = []
        self.context_lines: list = []


def _prov(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> OllamaTranslationProvider:
    p = OllamaTranslationProvider(Context(config=Config(), events=EventBus()))

    def boom(*a: object, **k: object) -> str:
        raise exc

    monkeypatch.setattr(p, "_call_single", boom)
    monkeypatch.setattr(p, "_call_batch", boom)
    return p


# ---------------------------------------------------------------- 分类学


def test_ollama_error_is_not_a_provider_error() -> None:
    """★ 钉住根因：`OllamaError` **不是** `ProviderError` 的子类。

    这条用例是"为什么不能只 `except ProviderError`"的**可执行证据**。
    哪天有人把 `OllamaError` 改成继承 `ProviderError`，这条会红 ——
    那时可以合并捕获类型，但仍然不该只写 `ProviderError`。
    """
    from novaloc.core.registry import ProviderError

    assert issubclass(OllamaError, RuntimeError)
    assert not issubclass(OllamaError, ProviderError)


def test_retryable_covers_ollama_error() -> None:
    """`_RETRYABLE` 必须是 `OllamaError` 的**超集**。"""
    assert issubclass(OllamaError, _RETRYABLE)


# ---------------------------------------------------------------- 端到端不逃出


def _real_500() -> OllamaError:
    return OllamaError(
        'Ollama 返回 500：{"error":"prediction aborted, token repeat limit reached"}'
    )


def test_ollama_500_does_not_escape_translate_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 核心回归：500 必须被吞掉，函数**正常返回**。"""
    p = _prov(monkeypatch, _real_500())
    out = p.translate_batch([_Item(REAL_SRC)], "zh-Hans")
    assert len(out) == 1
    assert out[0].status is EntryStatus.FAILED
    assert out[0].target == ""


def test_ollama_500_on_many_items_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多条 + 多行 + 占位符（就是实测 0/24 那个形态）也不许抛。"""
    p = _prov(monkeypatch, _real_500())
    items = [_Item(REAL_SRC, uid=f"u{i}") for i in range(6)]
    out = p.translate_batch(items, "zh-Hans")
    assert len(out) == 6
    assert all(e.status is EntryStatus.FAILED for e in out)


def test_other_retryable_errors_do_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """其余 `_RETRYABLE` 成员同样不许逃出（不只 500 一种）。"""
    from novaloc.core.registry import ProviderError
    from novaloc.translate.ollama_client import OllamaNotRunning

    for exc in (
        OllamaNotRunning("服务没起"),
        ProviderError("单条翻译失败"),
        ValueError("坏输入"),
    ):
        p = _prov(monkeypatch, exc)
        out = p.translate_batch([_Item(REAL_SRC)], "zh-Hans")
        assert out[0].status is EntryStatus.FAILED, f"{type(exc).__name__} 逃出了"


def test_unexpected_error_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 反方向：**非** `_RETRYABLE` 的异常必须照旧抛出去。

    不许把捕获放宽到 `except Exception` —— 那会把真 bug 吞成"翻译失败"，
    正是本项目最怕的"看起来正常其实坏了"。
    """
    p = _prov(monkeypatch, KeyError("这是真 bug，必须炸出来"))
    with pytest.raises(KeyError):
        p.translate_batch([_Item(REAL_SRC)], "zh-Hans")
