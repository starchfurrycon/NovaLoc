r"""★★★ 请求预算必须**只增不减** —— 否则被小批量调用压到极低。

## 实测事故（我修作用域时引入的第二个 bug）

```
批 0（1 条）第 1 次失败：请求预算已用尽（10 > 9）
批 0（2 条）…（19 > 9）…（100 > 9）…（300 > 9）…（600 > 9）…
```

上限只有 **9**，**连续拒绝了几百批**。

### 机理

贴图管线的回调（`_make_texture_translate`）拿到的 items 是
**一张图里的文字块**（常常 1~4 条），它也会走 `translate_batch` /
`begin_run`。而 `begin_run(total)` 的公式是
`max(total + floor, total × factor)`：

* `begin_run(1)` ⇒ `max(9, 3) = **9**`

⇒ 只要有一次用很小的 items 调 `begin_run`，预算就被压到 9
**并且不再恢复** ⇒ 后续所有批次都被拒。

## 修法

只在**新预算更大**时才更新；缩小的请求**直接忽略**。
`_requests_used` 也不因缩小而清零（否则计数器与预算脱节）。

## 本文件测什么

1. **★★ 小批量调用不能压小预算**（`begin_run(1)` 后预算不变）；
2. 更大的调用**仍能**放大预算；
3. 缩小时**不清零**计数器（否则"已用"与上限脱节）；
4. 结构性守卫：`begin_run` 里有"只增不减"的早退。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.registry import TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate import ollama_provider as op  # noqa: E402

PROV_SRC = ROOT / "src" / "novaloc" / "translate" / "ollama_provider.py"


def _provider(monkeypatch):
    from novaloc.core.config import get_config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context, Providers
    from novaloc.translate.ollama_client import ChatResult, OllamaClient

    cfg = get_config()
    cfg.ollama.request_budget_factor = 3.0
    cfg.ollama.request_budget_floor = 8
    cfg.ollama.fail_circuit_breaker = 10**9
    ctx = Context(config=cfg, events=EventBus())
    _ = Providers(ctx)
    prov = op.OllamaTranslationProvider(ctx)
    monkeypatch.setattr(
        OllamaClient,
        "chat",
        lambda self, model, messages, **kw: ChatResult(text="{}", model=model, done_reason="stop"),
    )
    monkeypatch.setattr(op.OllamaTranslationProvider, "_respect_gate", lambda self: None)
    return prov


def _items(n: int, *, offset: int = 0) -> list[TranslateItem]:
    out = []
    for i in range(n):
        unit = TextUnit(
            uid=f"t{offset + i:05d}",
            source=f"テスト{(offset + i)}",
            location=TextLocation(file="a", byte_offset=(offset + i) * 8, encoding="utf-8"),
            kind=TextKind.UNKNOWN,
        )
        out.append(TranslateItem(unit=unit))
    return out


# ----------------------------------------------------------------------
# 1. ★★ 小批量不能压小预算
# ----------------------------------------------------------------------
def test_small_begin_run_does_not_shrink_budget(monkeypatch) -> None:
    r"""★★ `begin_run(1)` 之后预算**必须不变**。

    这条直接对应那个事故：上限被压到 9 ⇒ 连续拒绝几百批。
    """
    prov = _provider(monkeypatch)
    prov.begin_run(31263)
    big = prov._request_budget
    assert big == 31263 * 3, f"游戏级预算不对：{big}"

    for small in (1, 2, 4, 0):
        prov.begin_run(small)
        assert prov._request_budget == big, (
            f"`begin_run({small})` 把预算从 {big} 压到了 {prov._request_budget} "
            f"⇒ 小批量会毁掉预算（实测事故）"
        )


def test_larger_begin_run_still_grows_budget(monkeypatch) -> None:
    """更大的调用**仍要**能放大预算（否则游戏级调用失效）。"""
    prov = _provider(monkeypatch)
    prov.begin_run(100)
    small = prov._request_budget
    prov.begin_run(50000)
    assert prov._request_budget > small, "更大的 begin_run 没有放大预算"


def test_shrink_does_not_reset_counter(monkeypatch) -> None:
    r"""★ 缩小时**不清零** `_requests_used` —— 否则"已用"与上限脱节。

    若清零了，攻击性场景下"用了很多次但显示 0"⇒ 预算永远不触发。
    """
    prov = _provider(monkeypatch)
    prov.begin_run(20000)  # 预算 60000
    prov.translate_batch(_items(20), "zh")  # 会消耗若干次
    used = prov._requests_used
    assert used > 0, "测试没消耗请求，无法验证"
    prov.begin_run(1)  # 应被忽略
    assert prov._requests_used == used, (
        f"缩小时把计数器清零了（{used} → {prov._requests_used}）"
    )


# ----------------------------------------------------------------------
# 2. 结构性守卫
# ----------------------------------------------------------------------
def test_begin_run_has_monotonic_guard() -> None:
    r"""★★ `begin_run` 里必须有"只增不减"的早退。

    防止有人后来把它删掉 —— 症状是"上限被压到个位数、
    连续拒绝几百批"，而日志里只看到"预算已用尽"，**极难归因**。
    """
    src = PROV_SRC.read_text(encoding="utf-8")
    i = src.index("def begin_run(")
    body = src[i : i + 3000]
    assert "只增不减" in body, "`begin_run` 里没有记录'只增不减'"
    assert "new_budget <= self._request_budget" in body, (
        "`begin_run` 缺少'新预算更小则忽略'的判定 ⇒ 小批量会压小预算"
    )
    assert "return" in body, "缺少早退"
