r"""★★ 连续失败熔断器：病态游戏不能把整个队列拖死。

## 实测缺陷（队列卡死 44 分钟）

`Battle Demon Kirsten` 有 **1,016 条**失败条目在无限重试：

```
全库吞吐 2 条/分钟（预期 150）
fail_streak 分布 streak=0 → 655, streak=1 → 362（一条都没到阈值 2）
队列卡在单个游戏上 44 分钟
```

失败内容是**短日文技能名**（模型确实不给译文、回显原文）：
`'一の型・焔斬'`、`'終の型・煌々一閃'`、`'チンゲリオン'`、`'ボニウスネーク'`。

### 重试放大链（已测绘）

```
translate_batch   for attempt in range(3)      ← 批重试 3 次
  ├─ _recover_by_subbatch → _call_single       ← 每缺条 2 次
  └─ 补空 → _call_single                       ← 又 2 次
_call_single      for attempt in range(2)      ← 单条再 2 次
```

⇒ 一条病态条目最多 **6 次请求**。

### 为什么 `fail_streak` 救不了

它**只在整轮结束时累加**，而这一轮永远结束不了。

### 为什么用"行为"而不是"内容"判据

试过给假名回显守卫加长度阈值，**被测试否决**：

```
'ポイズンガード'(7 字)  单独问 → '毒药卫'   ← 已验证能译
'一の型・焔斬'(6 字)    单独问 → 回显原文    ← 不译
```

**长度分不开** ⇒ 内容判据必误杀。

## 本文件测什么

1. 配置项存在且取值合理（够宽松不误伤、又能在几十秒内触发）；
2. **结构性守卫**：`translate_batch` 里确实有熔断逻辑
   （计数、阈值判定、提前退出、留作未翻译）；
3. 熔断后的条目是 `FAILED` + 空译文 + 明确警告
   （不能留半截译文 —— 那会进字符集导致口口口）；
4. **最重要的**：熔断**不丢已成功的译文**。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402

PROV = ROOT / "src" / "novaloc" / "translate" / "ollama_provider.py"
CONFIG_SRC = ROOT / "src" / "novaloc" / "core" / "config.py"


# ----------------------------------------------------------------------
# 1. 配置项
# ----------------------------------------------------------------------
def test_circuit_breaker_exists() -> None:
    """配置项必须存在。"""
    cb = int(Config().ollama.fail_circuit_breaker)
    assert cb >= 1


def test_circuit_breaker_is_not_too_tight() -> None:
    r"""★ 不能太紧 —— 会误伤正常游戏。

    正常游戏的失败是**分散的**（`placeholder_broken` / `kana_residue`
    等零星出现），不该因为偶尔几条失败就熔断。

    ⚠️ 下限是 **20**，不是 50：实测标定值是 **40**
    （`Battle Demon Kirsten` 的最长连续失败段是 72，取 40 有确定余量）。
    这条只守住"别小到误伤分散失败"。
    """
    cb = int(Config().ollama.fail_circuit_breaker)
    assert cb >= 20, f"{cb} 太小，正常游戏的零星失败会误触发熔断"


def test_circuit_breaker_is_not_too_loose() -> None:
    r"""也不能太松 —— 那样病态游戏还是会烧掉大量时间。

    病态游戏每条失败要花约 1~3 秒（含重试）。阈值 × 秒数
    应当控制在"几分钟以内"，而不是几十分钟。
    """
    cb = int(Config().ollama.fail_circuit_breaker)
    # 按每条最坏 3 秒估，cb × 3 秒 应 < 15 分钟
    assert cb * 3 <= 900, f"{cb} 太松：按每条 3 秒最坏要 {cb * 3 / 60:.0f} 分钟才熔断"


# ----------------------------------------------------------------------
# 2. 结构性守卫
# ----------------------------------------------------------------------
def test_translate_batch_has_circuit_breaker() -> None:
    r"""★★ 结构性守卫：`translate_batch` 里必须有熔断逻辑。

    防止有人后来把它删掉 —— 症状是"某个游戏莫名其妙卡死几十分钟"，
    很难归因（本轮就花了很久才定位）。
    """
    src = PROV.read_text(encoding="utf-8")
    for needle in (
        "fail_circuit_breaker",
        "_cb_tripped",
        "_consec_fail",
        "circuit_breaker_tripped",
    ):
        assert needle in src, f"provider 里找不到 {needle!r} —— 熔断逻辑可能被删了"


def test_consecutive_counter_resets_on_success() -> None:
    r"""★★ 计数器必须在**成功时清零**。

    否则它退化成"总失败数"，一个失败率 1% 的大游戏
    （31,263 条 ⇒ 约 300 条失败）也会被误熔断。
    这条守的是"连续"二字的语义。
    """
    src = PROV.read_text(encoding="utf-8")
    lines = src.splitlines()
    # 找 `_consec_fail += 1` 附近，确认有 `_consec_fail = 0`
    idx = next(i for i, ln in enumerate(lines) if "_consec_fail += 1" in ln)
    window = "\n".join(lines[max(0, idx - 6) : idx + 14])
    assert "_consec_fail = 0" in window, "成功路径没有清零 `_consec_fail` ⇒ 会退化成总失败数"


def test_tripped_batches_are_left_untranslated() -> None:
    r"""★★ 熔断后的条目要**明确留作未翻译**（FAILED + 空译文 + 警告）。

    ⚠️ 不能留半截译文 —— 残留文本会进入字体字符集 ⇒ 满屏口口口。
    这是本项目最硬的那条约束。
    """
    src = PROV.read_text(encoding="utf-8")
    lines = src.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "if _cb_tripped:" in ln)
    window = "\n".join(lines[idx : idx + 22])
    assert "EntryStatus.FAILED" in window, "熔断分支没有把条目置为 FAILED"
    assert 'entry.target = ""' in window, "熔断分支没有清空译文 ⇒ 残留文本会进口口口"


def test_circuit_breaker_makes_no_request() -> None:
    r"""★ 熔断分支必须**不发请求**（`continue` 在 `_one_batch` 之前）。

    若它仍然调用 `_one_batch`，熔断就只是"记账"，挡不住算力浪费。
    """
    src = PROV.read_text(encoding="utf-8")
    lines = src.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "if _cb_tripped:" in ln)
    window = "\n".join(lines[idx : idx + 22])
    assert "_one_batch(" not in window, "熔断分支仍然调用 _one_batch ⇒ 挡不住算力浪费"


def test_config_documents_the_incident() -> None:
    """配置项要记录这次实测事故（否则后人不知道阈值为什么是 120）。"""
    src = CONFIG_SRC.read_text(encoding="utf-8")
    assert "fail_circuit_breaker" in src
    assert "Battle Demon Kirsten" in src, "配置项没记录触发它的事故"
