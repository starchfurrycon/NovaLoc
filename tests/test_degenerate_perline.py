r"""#39：**逐行译文的退化产物**必须被拦住，不许冒充 `translated`。

## 实测背景

模型对**每一行**都回同一个说话人标签时，3.7 逐行路径会拼出
`'店员 1：\n店员 1：\n店员 1：'`。每行单独看都"是合法中文、
长度也在范围内"，所以**逐行守卫会全部放行** —— 拼起来却是
把垃圾复制了三遍，还挂着 `status=translated` 写回游戏。**静默的。**

## ★ 本文件的核心：为什么判据只能是"重复"，不能是"长度/比值"

我用**真实工作区**量过两类条目的 译文/原文 比值：

| 类别 | 比值 |
| --- | --- |
| 人工确认的垃圾（只回说话人标签） | 0.28 / 0.17 / **0.04** / 0.43 / 0.38 |
| 合理合并译文（中文把三行合成一句） | **0.17** / 0.26 / 0.29 / 0.29 / 0.31 / 0.37 |

**垃圾最大 0.43 > 合理最小 0.17 ⇒ 完全重叠。** 任何阈值都必然误杀一边。
我先后取过三个阈值（`len>=40`、拉丁字母>=25、拉丁字母>=60），
**三次都在误伤**，每次都比不上"不动"。

⇒ 判据必须是**结构性**的："同一个短片段重复了 N 遍"。
它不受语种、紧凑程度、源文长短影响 —— 合理的合并译文里
不会出现三行一模一样的短句。

## 还量到一件重要的事

真实工作区里 `translated` 且"源文多行 + 译文单行"的**只有 6 条**，
逐条看过**全是合理的合并译文**，不含垃圾。
而真正只回标签的那条（比 0.04）**本来就不是 `translated`** ——
它带 `sentence_drop:6->1` 警告，**已有守卫就抓到了**。

⇒ **"假成功"不是这个语料的主要缺口**；48.6% 的 `failed` 才是。
本文件守的是**回归**，不是效果证明。
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
from novaloc.translate.ollama_provider import (  # noqa: E402
    OllamaTranslationProvider,
    _is_degenerate_repetition,
)

pytestmark = requires_ollama

#: 实测那条源文（142 字符 / 3 行）
FAKE_SRC = (
    'Clerk 1:\n"Ahh, feels so good. It\'s like having my cock eaten.\n'
    'You\'ve been twitching the whole time. Top gal. Can I post this?"'
)
#: 实测那条"只回说话人标签"的响应
FAKE_LABEL = "店员 1："


class _Unit:
    def __init__(self, source: str) -> None:
        self.kind = TextKind.DIALOGUE
        self.source = source
        self.location = None
        self.uid = f"test:{abs(hash(source)) % 10**8}"
        self.max_chars = None


class _Item:
    def __init__(self, source: str) -> None:
        self.unit = _Unit(source)
        self.glossary: list = []
        self.context_lines: list = []


def _prov(monkeypatch: pytest.MonkeyPatch, single_map: dict[str, str]) -> OllamaTranslationProvider:
    """构造 provider；`single_map` 用来替换 `_call_single`（可选）。

    空 dict ⇒ 不替换 `_call_single`，改用 `_chat` 驱动（见 `_gate`），
    这样"模型原始响应"完全由用例控制，不经过内容匹配。
    """
    p = OllamaTranslationProvider(Context(config=Config(), events=EventBus()))

    if single_map:

        def fake_single(
            item: object, masked: str, *, slots: object = None, **kwargs: object
        ) -> str:
            for key, val in single_map.items():
                if key != "__default__" and key in masked:
                    return val
            return single_map.get("__default__", "")

        monkeypatch.setattr(p, "_call_single", fake_single)
    monkeypatch.setattr(
        p,
        "_call_batch",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("不该走批路径")),
    )
    return p


# ---------------------------------------------------------------- 判据本身


def test_degenerate_repetition_detected() -> None:
    """★ 核心判据：同一个短片段复制多遍 ⇒ 判为退化。"""
    assert _is_degenerate_repetition(["店员 1：", "店员 1：", "店员 1："])
    assert _is_degenerate_repetition(["教授：", "教授："])


def test_distinct_lines_are_not_degenerate() -> None:
    """内容不同的多行 ⇒ 正常（这是拼回路径的常见情形）。"""
    assert not _is_degenerate_repetition(["店员 1：", "啊，感觉真好。", "你一直在抽搐。"])


def test_single_line_is_never_degenerate() -> None:
    """单行不适用（`'莉菲娅'` 这种"整条翻成一个词"是合法正解）。"""
    assert not _is_degenerate_repetition(["莉菲娅"])
    assert not _is_degenerate_repetition([])


def test_long_repeated_lines_are_not_flagged() -> None:
    """三行都是**长句**且相同 ⇒ 更可能是合理的重复修辞，不当垃圾。

    宁可漏也不误杀 —— 这正是"比值判据"翻车的地方，所以这里刻意留余地。
    """
    long_line = "这是一句相当长的重复出现的台词，用于避免被误判为退化产物。"
    assert len(long_line) >= 20
    assert not _is_degenerate_repetition([long_line, long_line, long_line])


def test_blank_lines_ignored_in_repetition_check() -> None:
    """空行不参与判断。"""
    assert _is_degenerate_repetition(["店员 1：", "   ", "店员 1："])


# ---------------------------------------------------------------- 最终闸门
#
# ⚠️ 这里**不用"内容驱动"的假模型**（早先版本用 `key in prompt` 匹配，
# 结果被**提示词模板自身**的示例文本命中 —— 模板里就有
# `例：'⟦0⟧: Confirm'` 和 `Lifia:⟦0⟧「I have to go.」` 这两处，
# 于是无论送哪一行都返回同一个值，测试**假失败**）。
# 改成直接驱动抽出来的 `_block_degenerate_entries` ——
# 确定性、不依赖提示词措辞、也不依赖模型桩。


def _gate(entries: list) -> tuple[int, dict]:
    from novaloc.translate.ollama_provider import _block_degenerate_entries

    stats: dict[str, int] = {}
    n = _block_degenerate_entries(entries, stats)
    return n, stats


def _mk_entry(target: str, source: str = FAKE_SRC) -> object:
    from novaloc.models import TranslationEntry

    return TranslationEntry(
        uid="u1",
        source=source,
        target=target,
        status=EntryStatus.TRANSLATED,
        kind=TextKind.DIALOGUE,
        provider="test",
        model="test",
    )


def test_final_gate_blocks_degenerate_translation() -> None:
    """★ 3.9 闸门必须拦下"同一短句复制多遍"的**最终**译文。"""
    e = _mk_entry("店员 1：\n店员 1：\n店员 1：\n店员 1：")
    n, stats = _gate([e])
    assert n == 1, "闸门没拦下退化产物"
    assert e.status is EntryStatus.FAILED
    assert e.target == "", f"拦下后必须清空译文：{e.target!r}"
    assert "degenerate_repetition" in e.warnings
    assert stats.get("degenerate_blocked") == 1


def test_final_gate_counts_what_it_blocks() -> None:
    """拦下要有计数 —— 否则线上看不出它在起作用。"""
    entries = [
        _mk_entry("店员 1：\n店员 1："),
        _mk_entry("教授：\n教授："),
        _mk_entry("第一行。\n第二行。"),  # 正常，不该被拦
    ]
    n, stats = _gate(entries)
    assert n == 2, f"应拦下 2 条，实际 {n}"
    assert stats.get("degenerate_blocked") == 2
    assert entries[2].status is EntryStatus.TRANSLATED, "误杀了正常多行译文"


def test_final_gate_leaves_single_line_alone() -> None:
    """单行不适用 —— `'莉菲娅'` 这种"整条翻成一个词"是合法正解。"""
    e = _mk_entry("莉菲娅")
    n, _ = _gate([e])
    assert n == 0
    assert e.status is EntryStatus.TRANSLATED
    assert e.target == "莉菲娅"


def test_final_gate_ignores_already_failed() -> None:
    """已经是 FAILED 的不重复处理（也不重复计数）。"""
    e = _mk_entry("")
    e.status = EntryStatus.FAILED
    n, stats = _gate([e])
    assert n == 0
    assert stats == {}


def test_final_gate_ignores_long_repeated_lines() -> None:
    """三行都是**长句**且相同 ⇒ 更可能是合理修辞，不拦（宁可漏不误杀）。"""
    long_line = "这是一句相当长的重复出现的台词，用于避免被误判为退化产物。"
    assert len(long_line) >= 20
    e = _mk_entry(f"{long_line}\n{long_line}")
    n, _ = _gate([e])
    assert n == 0
    assert e.status is EntryStatus.TRANSLATED


# ---------------------------------------------------------------- 已知局限


def test_KNOWN_LIMITATION_content_loss_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""★ **已知局限（故意让它"通过"，把缺口写成可执行的记录）**。

    实测那条 `'店员 1\n：'`（源 142 字符 / 3 行，只译出说话人标签）
    在真实路径上的**最终状态是 `translated`**，只带
    `sentence_drop:6->1` 警告。本用例**断言这个现状** ——
    如果哪天它变成 FAILED，这条用例会红，提醒我们来更新 ROADMAP。

    为什么**没有**修好它：我量过三类"假成功"判据，全部**无判别力**：

    * 译文/原文比值 —— 垃圾 0.04~0.43 与合理译文 0.17~0.37 **重叠**；
    * `sentence_drop` 判死 —— 实测开火率 10.7%、**大部分是假阳性**
      （语气词合并），且与"真丢内容"的 len_ratio 区间重叠（见 guards.py）；
    * 源文行数 - 译文行数 —— 与"合理合并"同样重叠。

    唯一有判别力的是**结构性**的"同一段重复"（3.9 已守），
    而这条垃圾**不是**重复结构，所以守不住。
    ⇒ **报出局限，而不是上一个会误杀正确译文的启发式。**
    （拦下正确产物比漏放一个可疑项更糟 —— 与 `guards.py` 里
    `sentence_drop` 的取舍同源。）
    """
    p = _prov(monkeypatch, {"__default__": FAKE_LABEL, "__whole__": FAKE_LABEL})
    out = p.translate_batch([_Item(FAKE_SRC)], "zh-Hans")
    e = out[0]
    # 记录现状：内容丢失**不会**被判死（这是局限，不是期望）
    assert e.status is EntryStatus.TRANSLATED, (
        "现状变了（内容丢失被拦下了）—— 好消息，请更新 ROADMAP #39 并改掉本用例"
    )
    assert any(w.startswith("sentence_drop") for w in e.warnings), (
        f"连警告都没有了，那才是真退步：{e.warnings}"
    )
