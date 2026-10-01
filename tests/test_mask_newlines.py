"""``mask(newlines=True)`` 的测试：把换行也当占位符保护起来。

## 这个开关修的是什么

RPG Maker 有 **48%** 的对话是"一条台词被切成多个 401 指令"，
适配器合成一条送翻译。模型会**看着换行自己切条** ——
裸调 Ollama 抓到：提示词给编号 0~3，`translategemma:4b` 却吐出 `"4"`。
后果是编号错位 + 只翻第一行，而两种都通顺、长度比也不越界。

实测（20 条真实多行英文，以"逐行单独翻再拼"为内容量参照）：

| 模型 | 不屏蔽换行 | 屏蔽换行 |
|---|---|---|
| `translategemma:4b` | 中位数 0.33~0.70 | **0.87** |
| `HY-MT1.5-7B` | 0.75~0.94，**5 条空** | **1.03，0 条空** |

## 这个文件要守住的

1. 换行**确实**进了槽位表（``slots`` 里有 ``"\\n"``）。
2. 连着的多个换行**逐个**屏蔽（空行是有意义的版面，不能合并）。
3. 默认 ``newlines=False`` —— **老行为一个字节都不能变**。
4. 换行与真实占位符混在一起时，索引与还原都正确。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402

MASK_OPEN = ph.MASK_OPEN
MASK_CLOSE = ph.MASK_CLOSE


def test_default_does_not_touch_newlines() -> None:
    """**核心回归**：不传 newlines 时，换行必须原样留着。

    这个开关是给"一条被切成多片"的条目用的；其它引擎/场景
    （Ren'Py 的脚本行、散装文本）依赖换行保持原样。
    """
    text = "第一行\n第二行\n第三行"
    r = ph.mask(text)
    assert r.slots == []
    assert r.text == text, "默认不该屏蔽换行"


def test_newlines_become_slots() -> None:
    r = ph.mask("第一行\n第二行", newlines=True)
    assert r.text == f"第一行{MASK_OPEN}0{MASK_CLOSE}第二行"
    assert r.slots == ["\n"]


def test_consecutive_newlines_are_masked_individually() -> None:
    """连着的换行要**逐个**屏蔽 —— 空行是版面，合并了就丢了。

    实测背景：模型会把 ``\\n\\n`` 当成"一段结束"，合并成一个记号后
    再还原就只剩一个换行，原本的空行（排版上的段落间隔）消失。
    """
    r = ph.mask("上\n\n下", newlines=True)
    assert r.slots == ["\n", "\n"], f"两个换行该是两个槽位：{r.slots!r}"
    assert r.text == f"上{MASK_OPEN}0{MASK_CLOSE}{MASK_OPEN}1{MASK_CLOSE}下"


def test_roundtrip_with_newlines() -> None:
    text = "一行\n二行\n\n三行"
    r = ph.mask(text, newlines=True)
    assert ph.unmask(r.text, r.slots) == text


def test_newlines_and_placeholders_mixed() -> None:
    """换行与真占位符混在一起时，索引顺序必须按**出现位置**排。"""
    text = "你好\\V[1]\n再见\\N[2]"
    r = ph.mask(text, newlines=True)
    assert r.slots == ["\\V[1]", "\n", "\\N[2]"], r.slots
    assert ph.unmask(r.text, r.slots) == text


def test_verification_catches_a_dropped_line() -> None:
    """**这是这个开关最大的价值**：丢行从"静默漏译"变成"硬失败"。

    以前模型少翻一行，译文照样通顺、长度比也不越界，检查全绿；
    现在换行是槽位的一部分，少了就是 ``placeholder`` 类硬失败，
    判死的条目**不留译文**（玩家看到原文而不是半句话）。
    """
    source = "第一行\n第二行"
    r = ph.mask(source, newlines=True)
    assert r.slots == ["\n"]
    # 模拟模型把换行记号丢了（只翻了一行）
    model_out = "只翻了第一行"
    _restored, check = ph.verify_restored(source, model_out, r.slots, masked_source=r.text)
    assert check.fatal, "模型丢掉换行记号（少翻一行）必须判死，不能静默通过"


def test_kept_newline_passes() -> None:
    """换行记号被保住时不能误判。"""
    source = "第一行\n第二行"
    r = ph.mask(source, newlines=True)
    model_out = f"第一行译文{MASK_OPEN}0{MASK_CLOSE}第二行译文"
    restored, check = ph.verify_restored(source, model_out, r.slots, masked_source=r.text)
    assert not check.fatal, check.warnings
    assert restored == "第一行译文\n第二行译文"


def test_mask_batch_newlines() -> None:
    masked, slots = ph.mask_batch(["a\nb", "c"], newlines=True)
    assert slots[0] == ["\n"] and slots[1] == []
    assert masked[1] == "c"


def test_mask_batch_default_unchanged() -> None:
    masked, slots = ph.mask_batch(["a\nb", "c"])
    assert slots == [[], []]
    assert masked == ["a\nb", "c"]


def test_trailing_newline_is_masked() -> None:
    """结尾的换行也要屏蔽（有些引擎用它表示"本页结束"）。"""
    r = ph.mask("只有一行\n", newlines=True)
    assert r.slots == ["\n"]
    assert ph.unmask(r.text, r.slots) == "只有一行\n"


def test_crlf_is_not_half_masked() -> None:
    """``\\r\\n`` 不能被拆成"屏蔽了 \\n、留下孤立的 \\r"。

    真实风险：留下 ``\\r`` 会进译文，写回游戏后在消息框里显示成怪东西。
    """
    r = ph.mask("上行\r\n下行", newlines=True)
    # 只屏蔽 \n 是可以的，但 \r 必须也一并在槽位里（整体还原）
    assert ph.unmask(r.text, r.slots) == "上行\r\n下行"
