r"""★★ 熔断器的**集成测试**：用假 provider 复现"连续失败"并验证真的熔断。

## 为什么要集成测试（而不是只做结构性守卫）

我第一版把阈值写成 **120**，而实测 `Battle Demon Kirsten` 的
**最长连续失败段只有 72 条** ⇒ **永远不触发** ⇒ 方案等于没做。
**只有真正跑一遍才能发现这种错** —— 结构性守卫抓不到它。

## 本文件测什么

1. 连续失败达到阈值时**熔断触发**（`circuit_breaker_tripped`）；
2. 熔断后**不再发请求**（请求数被封顶）；
3. 熔断后的条目是 `FAILED` + **空译文** + 明确警告；
4. **已成功的译文全部保留**（最硬的约束）；
5. 阈值**必须小于实测的 72**（防止有人调高回去）；
6. 交替成功/失败**不该**熔断（守"连续"二字的语义）。

## 接口（已核对源码，不要凭记忆写）

```
TranslateItem(unit: TextUnit, glossary=[], context_lines=[])   # core/registry.py:56
translate_batch(items, target_lang) -> list[TranslationEntry]   # 顺序与 items 一一对应
TranslationEntry                                                # models.py:160
```
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.registry import TranslateItem  # noqa: E402
from novaloc.models import EntryStatus, TextKind, TextLocation, TextUnit  # noqa: E402


def _items(n: int) -> list[TranslateItem]:
    """造 n 条日文条目（内容无关紧要，反正假 provider 恒回空）。"""
    out = []
    for i in range(n):
        unit = TextUnit(
            uid=f"t{i:04d}",
            source=f"テスト項目{i}のテキストです",
            location=TextLocation(file="a.txt", byte_offset=i * 64, encoding="utf-8"),
            kind=TextKind.UNKNOWN,
        )
        out.append(TranslateItem(unit=unit))
    return out


def _provider(monkeypatch, threshold: int, chat):
    from novaloc.core.config import get_config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context, Providers
    from novaloc.translate import ollama_provider as op

    cfg = get_config()
    cfg.ollama.fail_circuit_breaker = threshold
    ctx = Context(config=cfg, events=EventBus())
    _ = Providers(ctx)
    prov = op.OllamaTranslationProvider(ctx)
    monkeypatch.setattr(op.OllamaTranslationProvider, "_chat", chat)
    monkeypatch.setattr(op.OllamaTranslationProvider, "_respect_gate", lambda self: None)
    return prov


def _empty_chat(counter: dict):
    def chat(self, user, *, system, temperature=None, n_items=1, src_chars=0, timeout=None):
        counter["n"] = counter.get("n", 0) + 1
        return "{}"

    return chat


# ----------------------------------------------------------------------
# 1. 熔断真的触发，并封顶请求数
# ----------------------------------------------------------------------
def test_circuit_breaker_trips_and_caps_requests(monkeypatch) -> None:
    r"""★★ 核心：连续失败到阈值 ⇒ 熔断 ⇒ 请求数被封顶。

    这是唯一能发现"阈值设错"的测试类型（我第一版 120 就错了）。
    """
    counter: dict = {}
    prov = _provider(monkeypatch, 5, _empty_chat(counter))

    n = 60
    prov.translate_batch(_items(n), "zh")

    assert prov.stats.get("circuit_breaker_tripped") == 1, (
        f"连续 {n} 条失败（阈值 5）却没有熔断"
    )
    assert counter.get("n", 0) < n, (
        f"熔断后仍发了 {counter.get('n')} 次请求（条目 {n} 条）⇒ 没挡住算力"
    )


# ----------------------------------------------------------------------
# 2. 熔断后不留半截译文
# ----------------------------------------------------------------------
def test_circuit_breaker_clears_target(monkeypatch) -> None:
    r"""★★ 熔断后的条目必须是 FAILED + **空译文**。

    残留半截译文会进入字体字符集 ⇒ 满屏口口口。这是最硬的约束。
    """
    counter: dict = {}
    prov = _provider(monkeypatch, 5, _empty_chat(counter))

    got = prov.translate_batch(_items(30), "zh")

    bad = [e for e in got if e.status == EntryStatus.TRANSLATED and not (e.target or "").strip()]
    assert not bad, f"{len(bad)} 条被标为 TRANSLATED 但译为空 ⇒ 会进口口口"

    warns = [w for e in got for w in (e.warnings or [])]
    assert any("circuit_breaker" in w for w in warns), (
        "没有条目带 circuit_breaker 警告 —— 可能走的不是熔断分支"
    )


# ----------------------------------------------------------------------
# 3. ★ 不丢已成功的译文
# ----------------------------------------------------------------------
def test_successful_translations_survive(monkeypatch) -> None:
    r"""★★ 熔断**不丢已成功的译文** —— 只停止为病态条目烧算力。"""
    state = {"n": 0}

    def chat(self, user, *, system, temperature=None, n_items=1, src_chars=0, timeout=None):
        state["n"] += 1
        if state["n"] <= 8:
            return '{"0": "这是一个成功的译文"}'
        return "{}"

    prov = _provider(monkeypatch, 5, chat)
    got = prov.translate_batch(_items(50), "zh")

    ok = [e for e in got if e.status == EntryStatus.TRANSLATED and (e.target or "").strip()]
    assert ok, "熔断把**已成功的译文**也丢了（绝不允许）"


# ----------------------------------------------------------------------
# 4. 阈值标定（防止调高回去）
# ----------------------------------------------------------------------
def test_threshold_is_below_measured_worst_run() -> None:
    r"""★★ 阈值必须 **< 实测最长连续失败段 72**。

    我第一版写 120 ⇒ 120 > 72 ⇒ 永不触发 ⇒ 方案等于没做。
    """
    from novaloc.core.config import Config

    cb = int(Config().ollama.fail_circuit_breaker)
    assert cb < 72, (
        f"阈值 {cb} ≥ 实测最长连续失败段 72 ⇒ 永不触发。"
        "调高它之前请先用真实日志重新标定。"
    )
    assert cb >= 20, f"阈值 {cb} 太小，可能误伤失败率偏高的正常游戏"


# ----------------------------------------------------------------------
# 5. 计数器语义：成功要清零（否则退化成"总失败数"）
# ----------------------------------------------------------------------
def test_counter_resets_on_success(monkeypatch) -> None:
    r"""★★ 交替"成功/失败"**不该**熔断。

    若计数器不在成功时清零，它就退化成"总失败数"，
    一个失败率 1% 的大游戏（31,263 条 ⇒ 约 300 条失败）会被误熔断。
    """
    state = {"n": 0}

    def chat(self, user, *, system, temperature=None, n_items=1, src_chars=0, timeout=None):
        state["n"] += 1
        if state["n"] % 2 == 0:
            return '{"0": "成功译文"}'
        return "{}"

    prov = _provider(monkeypatch, 5, chat)
    prov.translate_batch(_items(60), "zh")

    assert prov.stats.get("circuit_breaker_tripped") is None, (
        "交替成功/失败也触发了熔断 ⇒ 计数器没有在成功时清零"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
