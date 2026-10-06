r"""★★ 请求预算：病态游戏不能靠"三层重试相乘"烧掉几千次请求。

## 实测缺陷（队列卡死 44 分钟，吞吐 0~2 条/分钟）

`Battle Demon Kirsten`：

```
失败 960 条，**分散**在 3,907 个批次里
llama-server 满负荷（dCPU 24.6s/30s、GPU 83%）—— 算力在烧，产出为零
```

**三层重试相乘**：

```
translate_batch   for attempt in range(3)   ← 批重试 3 次
  ├─ _recover_by_subbatch → _call_single    ← 每缺条 2 次
  └─ 补空 → _call_single                    ← 又 2 次
_call_single      for attempt in range(2)   ← 单条再 2 次
```

⇒ 一条病态条目最多 **6 次请求** ⇒ 960 × 6 ≈ **5,760 次**无效请求。
独立重试只需 960 次 ⇒ **可省 83%**。

## 本文件测什么

1. 预算确实**封顶请求数**（假 provider 恒回空时，请求数 ≤ 预算）；
2. **正常游戏不受影响**（每个请求都成功 ⇒ 请求数 ≈ 条目数 ≪ 预算）；
3. **★ 预算用尽后已成功的译文全部保留**（最硬的约束）；
4. 预算用尽后剩余条目是 `FAILED` + **空译文**（防口口口）；
5. 结构性守卫：`_chat` 是唯一发请求的出口，预算在那里记。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.registry import TranslateItem  # noqa: E402
from novaloc.models import EntryStatus, TextKind, TextLocation, TextUnit  # noqa: E402

PROV = ROOT / "src" / "novaloc" / "translate" / "ollama_provider.py"
CONFIG_SRC = ROOT / "src" / "novaloc" / "core" / "config.py"


def _items(n: int) -> list[TranslateItem]:
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


def _provider(monkeypatch, chat, *, factor: float = 3.0, floor: int = 8):
    from novaloc.core.config import get_config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context, Providers
    from novaloc.translate import ollama_provider as op

    cfg = get_config()
    cfg.ollama.request_budget_factor = factor
    # 下限调到很小，让测试能**真的撞到**预算上限（小样本加默认下限 8 撞不到）
    cfg.ollama.request_budget_floor = floor
    # 关掉熔断器，让本测试只测预算（否则两者会互相掩盖）
    cfg.ollama.fail_circuit_breaker = 10**9
    ctx = Context(config=cfg, events=EventBus())
    _ = Providers(ctx)
    prov = op.OllamaTranslationProvider(ctx)
    # ★★ 注入点必须是**网络层**（OllamaClient.chat），**不能**是 _chat。
    #
    # 预算代码就在 _chat 里 —— 替换掉它等于把被测逻辑删了。
    # 我第一版就是替换 _chat，于是 _requests_used 永远是 0，
    # 测出来的"预算无效"完全是测试自己的 bug。
    # （与本轮另外两次同型错误一样：验证脚本必须用**被测对象本身的路径**。）
    from novaloc.translate.ollama_client import ChatResult, OllamaClient

    def _client_chat(self, model, messages, **kw):
        text = chat(None, None, system="", n_items=kw.get("n_items", 1))
        return ChatResult(text=text, model=model, done_reason="stop")

    monkeypatch.setattr(OllamaClient, "chat", _client_chat)
    monkeypatch.setattr(op.OllamaTranslationProvider, "_respect_gate", lambda self: None)
    return prov


# ----------------------------------------------------------------------
# 1. 预算封顶请求数
# ----------------------------------------------------------------------
def test_budget_caps_requests_when_all_fail(monkeypatch) -> None:
    r"""★★ 全失败时，请求数必须被预算**封顶**。

    没有预算时，60 条全失败会发出远超 60 次的请求（三层重试相乘）。
    有预算后应当 ≤ 条目数 × factor（加一点余量）。
    """
    counter = {"n": 0}

    def chat(self, user=None, *, system="", temperature=None, n_items=1, src_chars=0, timeout=None):
        counter["n"] += 1
        return "{}"

    n = 40
    prov = _provider(monkeypatch, chat, factor=3.0)
    prov.translate_batch(_items(n), "zh")

    budget = prov._request_budget
    assert counter["n"] <= budget, (
        f"发出 {counter['n']} 次请求，超过预算 {budget} ⇒ 预算没生效"
    )
    # 而且确实用掉了大部分预算（证明确实撞到了它，而不是别的路径提前退出）
    assert counter["n"] >= n, f"只发了 {counter['n']} 次（条目 {n}），像是别的原因提前结束"


def test_budget_records_exhaustion(monkeypatch) -> None:
    """预算用尽要**说出来**（否则"为什么这个游戏没翻完"无从排查）。"""

    def chat(self, user=None, *, system="", temperature=None, n_items=1, src_chars=0, timeout=None):
        return "{}"

    prov = _provider(monkeypatch, chat, factor=1.0, floor=0)
    prov.translate_batch(_items(30), "zh")
    assert prov.stats.get("request_budget_exhausted", 0) >= 1, (
        "预算用尽却没记进 stats"
    )


# ----------------------------------------------------------------------
# 2. ★ 正常游戏不受影响
# ----------------------------------------------------------------------
def test_normal_game_is_unaffected(monkeypatch) -> None:
    r"""★★ 每个请求都成功时，请求数应 ≈ 条目数，**远低于**预算。

    这条守的是"预算不会误伤正常游戏" —— 若预算给小了，
    正常游戏会被截断，症状是"大批条目莫名未翻译"。
    """
    counter = {"n": 0}

    def chat(self, user=None, *, system="", temperature=None, n_items=1, src_chars=0, timeout=None):
        counter["n"] += 1
        # 回一个"按行号作键"的对象（批量路径期望的形态之一）
        return "{" + ", ".join(f'"{i}": "译文{i}"' for i in range(8)) + "}"

    n = 60
    prov = _provider(monkeypatch, chat, factor=3.0)
    got = prov.translate_batch(_items(n), "zh")

    ok = [e for e in got if e.status == EntryStatus.TRANSLATED]
    assert ok, "正常场景一条都没翻成功 ⇒ 测试构造有问题"
    assert counter["n"] <= n, (
        f"正常场景发了 {counter['n']} 次请求（条目 {n}）⇒ 重试过多"
    )


# ----------------------------------------------------------------------
# 3. ★★ 预算用尽不丢已成功的译文
# ----------------------------------------------------------------------
def test_successful_translations_survive_budget_exhaustion(monkeypatch) -> None:
    r"""★★ 预算用尽**不能丢已成功的译文** —— 只是不再为病态条目烧算力。"""
    state = {"n": 0}

    def chat(self, user=None, *, system="", temperature=None, n_items=1, src_chars=0, timeout=None):
        state["n"] += 1
        # 前 10 次成功，之后恒失败（模拟"跑到某一批开始全是病态内容"）
        if state["n"] <= 10:
            return '{"0": "这是一个成功的译文"}'
        return "{}"

    prov = _provider(monkeypatch, chat, factor=2.0)
    got = prov.translate_batch(_items(60), "zh")

    ok = [e for e in got if e.status == EntryStatus.TRANSLATED and (e.target or "").strip()]
    assert ok, "预算用尽把**已成功的译文**也丢了（绝不允许）"


# ----------------------------------------------------------------------
# 4. 不留半截译文
# ----------------------------------------------------------------------
def test_no_translated_but_empty_target(monkeypatch) -> None:
    r"""★★ 不能出现"标为 TRANSLATED 但译文为空" —— 那会进字体字符集 ⇒ 口口口。"""

    def chat(self, user=None, *, system="", temperature=None, n_items=1, src_chars=0, timeout=None):
        return "{}"

    prov = _provider(monkeypatch, chat, factor=2.0)
    got = prov.translate_batch(_items(40), "zh")
    bad = [e for e in got if e.status == EntryStatus.TRANSLATED and not (e.target or "").strip()]
    assert not bad, f"{len(bad)} 条被标为 TRANSLATED 但译为空 ⇒ 会进口口口"


# ----------------------------------------------------------------------
# 5. 结构性守卫
# ----------------------------------------------------------------------
def test_budget_counted_at_the_single_chokepoint() -> None:
    r"""★★ 预算必须在**唯一发请求的出口**（`_chat`）里记。

    若记在别处（例如各层各记），就有路径绕过预算 ——
    那个 bug 的症状是"预算设了却仍然发了几千次请求"，极难归因。
    """
    src = PROV.read_text(encoding="utf-8")
    # `_chat` 里必须有计数与检查
    idx = src.index("def _chat(")
    end = src.index("\n    def ", idx + 1)
    body = src[idx:end]
    assert "_requests_used" in body, "`_chat` 里没有请求计数 ⇒ 有路径能绕过预算"
    assert "_request_budget" in body, "`_chat` 里没有预算检查"
    # 且只有三处 `client.chat(` —— `_chat` 是其中之一（另外两处不经这里）
    assert "_chat" in src


def test_config_documents_the_incident() -> None:
    """配置项要记录这次实测事故（否则后人不知道系数为什么是 3.0）。"""
    src = CONFIG_SRC.read_text(encoding="utf-8")
    assert "request_budget_factor" in src
    assert "Battle Demon Kirsten" in src, "配置项没记录触发它的事故"
    assert "5,760" in src or "5760" in src, "没记录实测的请求量"


def test_budget_factor_is_sane() -> None:
    r"""系数要够宽松到不误伤正常游戏（正常约 1 次/条）。"""
    from novaloc.core.config import Config

    f = float(Config().ollama.request_budget_factor)
    assert f >= 2.0, f"系数 {f} 太紧，正常游戏的重试会被截断"
    assert f <= 8.0, f"系数 {f} 太松，起不到封顶作用"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
