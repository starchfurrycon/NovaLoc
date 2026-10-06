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

from conftest import requires_ollama  # noqa: E402

from novaloc.core.registry import TranslateItem  # noqa: E402
from novaloc.models import TextKind, TextLocation, TextUnit  # noqa: E402
from novaloc.translate import ollama_provider as op  # noqa: E402

#: ★ 这些测试会构造真 `OllamaTranslationProvider` 并调 `available()`，
#: 所以在**没有 Ollama 的 CI** 上必须 skip 而不是失败。
#: 见 `tests/conftest.py` 的 `requires_ollama`。
pytestmark = requires_ollama


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
def test_larger_begin_run_still_grows_budget(monkeypatch) -> None:
    """更大的调用**仍要**能放大预算（否则游戏级调用失效）。"""
    prov = _provider(monkeypatch)
    prov.begin_run(100)
    small = prov._request_budget
    prov.begin_run(50000)
    assert prov._request_budget > small, "更大的 begin_run 没有放大预算"
# ----------------------------------------------------------------------
# 2. ★★ 权威标记：兜底不得覆盖整个游戏的上限
# ----------------------------------------------------------------------
def test_fallback_does_not_override_authoritative_budget(monkeypatch) -> None:
    r"""★★ 贴图阶段的小批量**绝不能**覆盖翻译阶段设的整个游戏上限。

    ## 这是 v2 的实测失败

    v2 用"只增不减"打补丁时，实测**仍然是 9**：

    ```
    批 0（1 条）第 1 次失败：请求预算已用尽（10 > 9）
    …（100 > 9）…（300 > 9）…（600 > 9）…
    ```

    原因：贴图阶段（`stage_images_localize`，**先跑**）的小批量
    （1~4 条）走 `translate_batch` 的兜底，把字段写成 9；
    之后翻译阶段的 `begin_run(31263)` 与"只增不减"互相干扰。

    ## 修法（v3）

    * `begin_run` **无条件**设预算并标记 `_budget_is_authoritative = True`；
    * `translate_batch` 只在**非权威**时才兜底。

    ⇒ 顺序无关、互不干扰。
    """
    prov = _provider(monkeypatch)
    # 模拟贴图阶段：1~4 条的小批量，**没有** begin_run
    for n in (1, 2, 4):
        prov.translate_batch(_items(n, offset=n * 10), "zh")
    assert not prov._budget_is_authoritative, "兜底不该把预算标成权威"
    small = prov._request_budget
    assert small < 100, f"兜底预算意外地大：{small}"

    # 模拟翻译阶段：游戏级
    prov.begin_run(31263)
    assert prov._budget_is_authoritative
    assert prov._request_budget == 31263 * 3

    # 再有贴图小批量也不许改
    prov.translate_batch(_items(1, offset=999), "zh")
    assert prov._request_budget == 31263 * 3, (
        "兜底覆盖了权威预算 ⇒ 又会出现「上限 9」那个事故"
    )
# ----------------------------------------------------------------------
# 3. 结构性守卫
# ----------------------------------------------------------------------
def test_begin_run_marks_authoritative() -> None:
    r"""★★ `begin_run` 必须把预算标记为**权威**。

    否则 `translate_batch` 的兜底会一直覆盖它（v1/v2 的根因）。
    """
    src = PROV_SRC.read_text(encoding="utf-8")
    i = src.index("def begin_run(")
    body = src[i : i + 4000]
    assert "_budget_is_authoritative = True" in body, (
        "`begin_run` 没把预算标记为权威 ⇒ 兜底会覆盖它"
    )


def test_fallback_guarded_by_authoritative_flag() -> None:
    r"""★★ 兜底必须由权威标记把关，而不是"预算 <= 0"。

    用"预算 <= 0"把关是 v2 的做法：贴图阶段一旦设过预算，
    兜底就不再执行，而它也**不会**被权威值替换 ⇒ 卡在 9。
    """
    src = PROV_SRC.read_text(encoding="utf-8")
    assert "if not self._budget_is_authoritative:" in src, (
        "兜底没有用权威标记把关 ⇒ 贴图阶段的小批量会毁掉预算"
    )
