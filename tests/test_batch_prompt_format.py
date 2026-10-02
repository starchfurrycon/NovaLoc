r"""★ 批量提示词的**输出格式**：必须是「行号为键的对象」，不能是 `[{"i":…}]` 数组。

## 真实事故：40 条只回 1 条，静默漏译 39 条

同一批 40 条真实游戏文本，只改提示词里的输出格式说明：

| 要求模型输出 | 生成 token | 返回条数 | 停止原因 |
|---|---|---|---|
| `[{"i": 0, "t": "…"}, …]` 数组 | 26 | **1** | `stop` |
| `{"0": "…", "1": "…"}` 对象 | 613 | **40** | `stop` |

模型把数组示例里的 `{"i": 0, "t": "…"}` 当成了
「要产出的**那一个**对象」—— 输出完一个就正常结束。

**批大小无关**：4 / 8 / 16 / 24 / 40 条都只回 1 条。

## 为什么这个 bug 特别难发现

它**不报错**。`_call_batch` 发现缺 39 条后走"逐条降级补漏"，
于是每批要发 `1 + 39` 次请求。表现是：

* 流水线一切"正常"，译文也确实是好的（逐条质量更高）；
* 只是**慢到看起来像卡死** —— 实测每批 200 秒以上；
* 我一开始把它误判成"显卡被游戏占了"，因为症状都是"卡住不动"。

## 修复与验证

改提示词后实测（真实数据、真实 provider）：

```
批=10 → single_fallbacks 0~1（原先每批 39）
批=40 → 10.1 秒回满 40 条  ≈ 14000 条/小时（旧路径约 6300 条/小时）
```

解析层本来就支持数字键对象（`to_translation_map` 认 dict 的整数键），
所以只改提示词。**旧数组形态仍必须兼容** —— 模型有时会自己换回去。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from conftest import requires_ollama  # noqa: E402

from novaloc.translate import prompts  # noqa: E402

# 本文件用真的 `OllamaTranslationProvider`（构造与探活都走网络）。
# 没有可用的 Ollama 就整体跳过 —— 见 conftest 里 `requires_ollama` 的说明。
pytestmark = requires_ollama



def _batch_prompt(n: int = 4) -> str:
    return prompts.build_batch_user_prompt(
        [f"line {i}" for i in range(n)], target_lang="zh-Hans"
    )


# ---------------------------------------------------------------------------
# 一、提示词必须要求「行号为键的对象」
# ---------------------------------------------------------------------------


def test_batch_prompt_asks_for_number_keyed_object() -> None:
    """★ 核心回归：批量提示词必须要求对象，不能要求数组。"""
    p = _batch_prompt()
    assert "行号" in p, "没告诉模型用行号作键"
    assert "键" in p, "没说明用键"


def test_batch_prompt_does_not_ask_for_i_t_array() -> None:
    """★ 反向：不能再出现「`{"i": …}` 数组」这种要求。

    这正是让模型只回 1 条的那个写法。
    """
    p = _batch_prompt()
    assert not ('"i"' in p and "数组" in p), (
        f"提示词又要求 i/t 数组了 —— 这会让模型只回 1 条：{p[:400]}"
    )


def test_system_prompt_matches_batch_prompt_format() -> None:
    """★ 系统提示与用户提示**必须说同一个格式**。

    两处矛盾时模型会二选一，等于格式不可控。
    """
    sysp = prompts.SYSTEM_PROMPT
    assert "键" in sysp, "系统提示没说明用行号作键"
    assert not ('"i"' in sysp and "数组" in sysp), "系统提示还要求 i/t 数组"


def test_batch_prompt_states_the_count() -> None:
    """条数必须写清楚（两头都写），否则模型对"还有几条"没有约束。"""
    p = _batch_prompt(7)
    assert p.count("7") >= 2, f"条数 7 在提示词里只出现 {p.count('7')} 次"


def test_batch_prompt_lists_every_item() -> None:
    """每条原文都必须出现在提示词里，且带行号。"""
    p = _batch_prompt(5)
    for i in range(5):
        assert f"{i} → line {i}" in p, f"第 {i} 条没出现在提示词里"


# ---------------------------------------------------------------------------
# 二、旧的数组形态**仍然必须能解析**
# ---------------------------------------------------------------------------


def test_parser_still_accepts_legacy_array() -> None:
    """★ 模型有时会自己换回数组形态 —— 解析层不能只认对象。"""
    from novaloc.translate.json_parse import parse_translations

    m, res = parse_translations(
        '[{"i": 0, "t": "你好"}, {"i": 1, "t": "世界"}]',
        expect_indices=[0, 1],
    )
    assert res.ok and m == {0: "你好", 1: "世界"}


def test_parser_accepts_number_keyed_object() -> None:
    """★ 新格式必须被解析（数字键在 JSON 里会变成字符串键）。"""
    from novaloc.translate.json_parse import parse_translations

    m, res = parse_translations('{"0": "你好", "1": "世界"}', expect_indices=[0, 1])
    assert res.ok and m == {0: "你好", 1: "世界"}


def test_parser_accepts_multiline_number_keyed_object() -> None:
    """真实模型输出是带换行缩进的，不是一行。"""
    from novaloc.translate.json_parse import parse_translations

    raw = '{\n"0": "你好。",\n"1": "你好吗？"\n}'
    m, res = parse_translations(raw, expect_indices=[0, 1])
    assert res.ok and m == {0: "你好。", 1: "你好吗？"}


# ---------------------------------------------------------------------------
# 三、系统性残缺必须留下可观测的痕迹
# ---------------------------------------------------------------------------


class _U:
    def __init__(self, s: str, uid: str) -> None:
        from novaloc.models import TextKind

        self.source, self.uid, self.kind, self.max_chars = (
            s,
            uid,
            TextKind.DIALOGUE,
            0,
        )


class _I:
    def __init__(self, s: str, uid: str) -> None:
        self.unit = _U(s, uid)
        self.glossary: list[object] = []
        self.context_lines: list[str] = []


def _provider():  # type: ignore[no-untyped-def]
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.translate.ollama_provider import OllamaTranslationProvider

    return OllamaTranslationProvider(Context(config=Config(), events=EventBus()))


def test_mostly_missing_batch_is_counted_and_logged(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """★ 整批只回 1 条时，必须**报警 + 计数**，不能只是悄悄逐条补漏。

    `single_fallbacks` 这个数字一直存在，但"每批都 +1"和
    "偶尔 +1" 在汇总里看不出区别。所以要单独记一笔。
    """
    import logging

    prov = _provider()
    monkeypatch.setattr(prov, "_chat", lambda *a, **k: '{"0": "只有第一条"}')
    items = [_I(f"line {i}", f"u{i}") for i in range(6)]
    with caplog.at_level(logging.WARNING):
        prov.translate_batch(items, "zh-Hans")

    assert prov.stats.get("batch_mostly_missing", 0) >= 1, (
        "整批只回 1/6 条却没有计数 —— 这种系统性残缺必须可观测"
    )
    assert "只回" in caplog.text or "格式" in caplog.text, (
        f"没有给出可诊断的警告：{caplog.text[:300]!r}"
    )


def test_partial_missing_does_not_alarm(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """★ 反向：只缺一小部分时**不该**报"格式没被遵守"。

    偶发漏一条是正常的，报警会让人对整个告警失去信任。
    """
    prov = _provider()
    five = '{"0":"a","1":"b","2":"c","3":"d","4":"e"}'  # 6 条缺 1 条
    monkeypatch.setattr(prov, "_chat", lambda *a, **k: five)
    items = [_I(f"line {i}", f"u{i}") for i in range(6)]
    prov.translate_batch(items, "zh-Hans")
    assert prov.stats.get("batch_mostly_missing", 0) == 0, (
        "只缺 1/6 不该报「格式没被遵守」"
    )
