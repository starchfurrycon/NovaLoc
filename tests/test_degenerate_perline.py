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
    e = _mk_entry("店员 1：\n店员 1：\n店员 1：\n店员 1：", source="短源文。")
    n, stats = _gate([e])
    assert n == 1, "闸门没拦下退化产物"
    assert e.status is EntryStatus.FAILED
    assert e.target == "", f"拦下后必须清空译文：{e.target!r}"
    assert "degenerate_repetition" in e.warnings
    assert stats.get("degenerate_blocked") == 1


def test_final_gate_counts_what_it_blocks() -> None:
    """拦下要有计数 —— 否则线上看不出它在起作用。

    ⚠️ 这里的"正常"条目**必须**配一个短源文：若沿用长 `FAKE_SRC`，
    它的译文 `'第一行。\\n第二行。'`（13 字符）会落进
    `_is_label_only_output` 的判据（长源文 + 译文 ≤12）而被拦 ——
    那条路径有它自己的用例，这里只想隔离"重复结构"这一条。
    """
    entries = [
        _mk_entry("店员 1：\n店员 1："),
        _mk_entry("教授：\n教授："),
        _mk_entry("第一行。\n第二行。", source="短源文。"),  # 正常，不该被拦
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


# ------------------------------------------------- 「只回标签」判据的**撤回**
#
# 这里的用例守的是**撤回本身**：判据已停用，且必须保持停用。
# 详见 `_is_label_only_output` 的 docstring（#42）。


def test_label_only_criterion_is_retracted() -> None:
    """★ 撤回的判据必须恒返回 False —— 绝不能因为误调用而误杀。"""
    from novaloc.translate.ollama_provider import _is_label_only_output

    # 连"确认是垃圾"的样本也必须不再被拦（判据已停用）
    for src, tgt in [
        ("Man 1:\n" + "x" * 60, "男人 1\n："),
        ("Clerk 1:\n" + "y" * 90, "人 1\n："),
        ("Clerk 1:\n" + "z" * 130, "店员 1\n："),
    ]:
        assert not _is_label_only_output(tgt, src)


def test_KNOWN_LIMITATION_length_bands_overlap() -> None:
    r"""★ **撤回的可执行证据**：垃圾与合法译文的长度区间**重叠**。

    实测（源文都 ≥60 字符）::

        4   '人 1\n：'                         ← 垃圾
        5   '店员 1\n：'                       ← 垃圾
        7   '女店员：\n欢迎。'                  ← 垃圾
        **10  '男行人:\n嗯？怎么了？'             ← 合法简明译文！**
        12  '男人：\n哼，我马上就要了。'
        13  '马西罗:\n喂！♡ 亲爱的！♡'           ← 垃圾

    **垃圾 13 > 合法 10** ⇒ 任何"译文长度"阈值都必然误杀一边。
    我把阈值调到 ≤12 时能抓到全部确认垃圾，但**仍然误杀**
    `'男行人:\n嗯？怎么了？'`。

    ⇒ 这是 `_best_segment`（2026-09）、#39 之后**第三次**同源现象。
    本用例把这个"分不开"的事实钉住：谁想再加这类阈值，
    先看这条，并准备好解释为什么这一次不会误杀。
    """
    garbage_max = 13  # '马西罗:\n喂！♡ 亲爱的！♡'
    legit_min_at_that_size = 10  # '男行人:\n嗯？怎么了？'
    assert garbage_max > legit_min_at_that_size, (
        "若哪天垃圾最短 > 合法最长，就可以重新考虑阈值判据了"
    )


# ---------------------------------------------------------------- 已知局限


def test_KNOWN_LIMITATION_content_loss_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""★ **已知局限（故意让它"通过"，把缺口写成可执行的记录）**。

    ## 内容丢失**没有**被修好，本轮也没能修

    实测那条 `'店员 1\n：'` 所在的**整类**"只回说话人标签"，
    经三轮尝试后确认**无法用判据可靠区分**（详见
    `_is_label_only_output` 的撤回说明）：

    * 比值判据 —— 垃圾 0.04~0.43 vs 合法 0.17~0.37，**重叠**；
    * 绝对长度判据 —— 垃圾 13 vs 合法 10，**重叠**；
    * 行数判据 —— 与"合理合并"**重叠**。

    ⇒ 本用例断言**现状**：这类条目最终是 `translated`，
    只带 `sentence_drop` 警告。**如果哪天它变成 FAILED，这条会红**，
    提醒来更新 ROADMAP/CHANGELOG —— 那是好消息，不是回归。

    ⚠️ 唯一本项目能可靠抓住的是**同一短片段复制多遍**
    （`_is_degenerate_repetition`，零误杀），它对这类**不适用**。
    """
    p = _prov(monkeypatch, {"__default__": FAKE_LABEL, "__whole__": FAKE_LABEL})
    out = p.translate_batch([_Item(FAKE_SRC)], "zh-Hans")
    e = out[0]
    # 记录现状：内容丢失**不会**被判死（这是局限，不是期望）
    assert e.status is EntryStatus.TRANSLATED, (
        "现状变了（内容丢失被拦下了）—— 好消息！请更新 ROADMAP #42 并改掉本用例"
    )
    assert any(w.startswith("sentence_drop") for w in e.warnings), (
        f"连警告都没有了，那才是真退步：{e.warnings}"
    )
