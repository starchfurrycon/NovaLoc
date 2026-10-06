r"""★★★ 请求预算的作用域必须是**整个游戏**，不是**单批**。

## 实测 bug（让一个正常批次被整批拒掉）

```
★ 请求预算用尽（25 次 > 上限 24）
```

`Battle Demon Kirsten` 有 **31,263 条**，预算本该是 **93,789**。
"上限 24"说明它是**单批**预算（某批 16 条 + floor 8 = 24）。

### 机理

`stage_translate` 是**按批**调用 `translate_batch` 的：

```python
for idxs in batch_indices:
    provider.translate_batch(items, ...)   # ← 每批一次
```

而我第一版在 `translate_batch` 里**每次调用都重置**：

```python
self._requests_used = 0
self._request_budget = max(len(items) + _floor, ...)
```

⇒ 预算退化成"**单批**允许几次请求"。一个 16 条的批只要重试几次
（`_call_batch` 3 次 + 逐条降级 + 补空）就撞到 24
⇒ **整批被 `ProviderError` 拒** ⇒ 条目被判 `FAILED`。

**那不是内容问题，是我的预算太紧。**

### 我的设计错在哪

写"预算 = 条目数 × 系数"时，我**假设 `translate_batch` 收到全部条目**。
实际它收到的是**一批**（8~25 条）。**假设与实现不符。**

## 修法

* 新增 **`begin_run(total_items)`**：`stage_translate` 在开始翻一个游戏前
  调用一次，用**该游戏全部条数**设预算；
* `translate_batch` **不再重置**计数器与预算（只递增）；
* 没调用 `begin_run` 时（贴图管线的小批量回调）退回"按本次调用条数"设。

## 本文件测什么

1. **★ `begin_run` 按总条数设预算**（31,263 条 ⇒ 93,789 次）；
2. **★★ 跨多次 `translate_batch` 调用**时，计数器**累计**而不重置
   —— 这是那个 bug 的直接回归测试；
3. 没有 `begin_run` 时仍按本次条数设（安全网仍在）；
4. 结构性守卫：`stages._translate_all` 确实调用了 `begin_run`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.registry import TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate import ollama_provider as op  # noqa: E402
from novaloc.translate.ollama_client import ChatResult, OllamaClient  # noqa: E402

STAGES_SRC = ROOT / "src" / "novaloc" / "pipeline" / "stages.py"


def _items(n: int, *, offset: int = 0) -> list[TranslateItem]:
    out = []
    for i in range(n):
        unit = TextUnit(
            uid=f"t{offset + i:05d}",
            source=f"テスト{(offset + i)}のテキスト",
            location=TextLocation(file="a.txt", byte_offset=(offset + i) * 64, encoding="utf-8"),
            kind=TextKind.UNKNOWN,
        )
        out.append(TranslateItem(unit=unit))
    return out


def _provider(monkeypatch, chat, *, factor: float = 3.0, floor: int = 8):
    from novaloc.core.config import get_config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context, Providers

    cfg = get_config()
    cfg.ollama.request_budget_factor = factor
    cfg.ollama.request_budget_floor = floor
    cfg.ollama.fail_circuit_breaker = 10**9  # 关掉熔断，只测预算
    ctx = Context(config=cfg, events=EventBus())
    _ = Providers(ctx)
    prov = op.OllamaTranslationProvider(ctx)
    # ★ 注入点必须是**网络层**（`OllamaClient.chat`），不能是 `_chat`
    #   —— 预算代码就在 `_chat` 里，替换它等于把被测逻辑删了。
    def _client_chat(self, model, messages, **kw):
        return ChatResult(text=chat(), model=model, done_reason="stop")

    monkeypatch.setattr(OllamaClient, "chat", _client_chat)
    monkeypatch.setattr(op.OllamaTranslationProvider, "_respect_gate", lambda self: None)
    return prov


# ----------------------------------------------------------------------
# 1. ★ begin_run 按**总条数**设预算
# ----------------------------------------------------------------------
def test_begin_run_sets_budget_from_total(monkeypatch) -> None:
    r"""★★ 31,263 条 ⇒ 预算 93,789（而不是单批的几十）。

    这条直接对应当前那个 bug：实测报的是"25 次 > 上限 24"。
    """
    prov = _provider(monkeypatch, lambda: "{}", factor=3.0, floor=8)
    prov.begin_run(31263)
    assert prov._request_budget == 31263 * 3, (
        f"预算 {prov._request_budget} 不等于 31263×3 ⇒ 还是按批算的"
    )
    assert prov._requests_used == 0


# ----------------------------------------------------------------------
# 2. ★★ 跨调用累计（bug 的直接回归测试）
# ----------------------------------------------------------------------
def test_counter_accumulates_across_calls(monkeypatch) -> None:
    r"""★★ 连续多次 `translate_batch` 时，计数器必须**累计**而不是重置。

    这正是那个 bug：每批都重置 ⇒ 预算退化成"单批预算"。
    """
    prov = _provider(monkeypatch, lambda: "{}", factor=3.0, floor=8)
    prov.begin_run(300)  # 预算 900，足够宽松
    before = prov._request_budget
    for k in range(5):
        prov.translate_batch(_items(20, offset=k * 20), "zh")
    assert prov._request_budget == before, (
        "`translate_batch` 又重置了预算 ⇒ 退化成单批预算（那个 bug 回来了）"
    )
    assert prov._requests_used > 0, "计数器没有累计"


def test_budget_not_exhausted_by_normal_retries(monkeypatch) -> None:
    r"""★★ 一个**正常批次**（含若干重试）不该撞到预算。

    实测 bug：某批 16 条，重试几次就撞到上限 24 ⇒ 整批被拒。
    """
    prov = _provider(monkeypatch, lambda: "{}", factor=3.0, floor=8)
    prov.begin_run(20000)  # 一个 2 万条的游戏
    # 单批 16 条，就算每条重试 6 次也只有 96 次 << 60000
    prov.translate_batch(_items(16), "zh")
    assert prov._requests_used < 200, (
        f"单批正常重试就用了 {prov._requests_used} 次请求 ⇒ 预算会误伤正常批次"
    )
    assert prov.stats.get("request_budget_exhausted") is None, (
        "正常批次撞到了预算 ⇒ 预算还是太紧"
    )


# ----------------------------------------------------------------------
# 3. 没有 begin_run 时仍有安全网
# ----------------------------------------------------------------------
def test_without_begin_run_budget_falls_back(monkeypatch) -> None:
    r"""★ 没调用 `begin_run`（如贴图管线的小批量回调）⇒ 按本次条数设。

    安全网必须还在 —— 否则那些路径完全没有预算保护。
    """
    prov = _provider(monkeypatch, lambda: "{}", factor=3.0, floor=8)
    assert prov._request_budget == 0
    prov.translate_batch(_items(10), "zh")
    assert prov._request_budget > 0, "没设预算 ⇒ 安全网丢了"


# ----------------------------------------------------------------------
# 4. 结构性守卫
# ----------------------------------------------------------------------
def test_stage_translate_calls_begin_run() -> None:
    r"""★★ `stage_translate` 必须在开始游戏前调用 `begin_run`。

    否则预算又变成按批的（那个 bug 的根因）。
    """
    src = STAGES_SRC.read_text(encoding="utf-8")
    assert "begin_run" in src, (
        "`stages.py` 没有调用 `begin_run` ⇒ 预算退化成单批预算"
    )
    # 且要用**总条数**调用（不是某批的条数）
    assert "begin_run(total)" in src or "_begin(total)" in src, (
        "`begin_run` 没传该游戏的总条数"
    )


def test_begin_run_exists_on_provider() -> None:
    """`begin_run` 必须真的存在（不是靠 getattr 兜底悄悄跳过）。"""
    assert hasattr(op.OllamaTranslationProvider, "begin_run"), (
        "provider 上没有 begin_run"
    )
