r"""★ 复读判据的**假阳性**：掩码原文 vs 还原译文的形态差异。

## 事故经过

`check_repetition` 的运行时机很微妙 —— 在 provider 里被调用时，
**原文还是掩码状态**（``⟦0⟧``），而**译文已经还原**（引擎转义码回来了）。

```
掩码原文  '…Weapon Type: ⟦0⟧⟦1⟧   Armor Type:⟦2⟧⟦3⟧⟦4⟧'
还原译文  '…武器类型：\I[96]\I[97]   护甲类型：\I[129]\I[135]\I[139]'
```

`strip_placeholders` 认引擎转义码（``\I[96]``）与 ``%1``，
但**不认掩码记号**。早先只对 source 调用它，于是原文里的重复被减掉了，
而译文里还原出来的**同一批图标码**没被减掉 ⇒ 命中 n-gram
``']\I['``、``'\I[1'`` ⇒ **误报复读**，整条译文被清空。

实测这个游戏里误报 **13 条**，全部是 `Actors.json` 的角色 profile，
译文完全正确（含武器/护甲图标码）。

## 为什么之前的测试没抓到

因为这个形态**只在"掩码翻译 + 占位符还原"同时开启时**才出现。
用不带占位符的样本测，两侧都是裸文本，看不出问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.guards import check_repetition  # noqa: E402


def test_masked_source_vs_restored_target_is_not_repetition() -> None:
    """★ 核心回归：两侧括号形态不同、文字相同 ⇒ **不许**报复读。

    这一条必须用**掩码原文 + 还原译文**这一对，
    用两个裸文本测不出这个 bug。
    """
    masked_src = (
        "An inquisitive girl. \nWeapon Type: ⟦0⟧⟦1⟧   Armor Type:⟦2⟧⟦3⟧⟦4⟧"
    )
    restored_tgt = (
        "来自博赫洛斯王国的好奇女孩。\n"
        "武器类型：\\I[96]\\I[97]   护甲类型：\\I[129]\\I[135]\\I[139]"
    )
    assert check_repetition(masked_src, restored_tgt) == []


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        ("Hello there.", "你好你好你好你好你好你好你好你好"),
        ("Hello there.", "yes yes yes yes yes yes"),
        ("Open the door.", "开门开门开门开门开门开门"),
    ],
)
def test_real_repetition_is_still_caught(src: str, tgt: str) -> None:
    """★ 反向：真复读必须仍然抓得到。

    修假阳性最容易伤到的就是这条 —— 把判据削得什么都不报，
    测试也会"全绿"。所以真阳性用例必须和假阳性用例**放在一起**。
    """
    assert check_repetition(src, tgt), f"漏掉了真复读：{tgt!r}"


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        # 原文自己就重复，译文重复是正常翻译
        ("no no no", "不不不不不不不不不"),
        ("no no no", "no no no"),
        # 正常长句
        ("Would you like to save?", "你想保存吗？"),
        ("Quit to the Main Menu.", "退出到主菜单。"),
        # 带占位符的正常译文
        ("HP: %1 / %2", "生命：%1 / %2"),
    ],
)
def test_normal_translations_are_not_repetition(src: str, tgt: str) -> None:
    assert check_repetition(src, tgt) == [], f"误报：{src!r} → {tgt!r}"


def test_mask_marks_are_stripped_before_comparison() -> None:
    """掩码记号本身不该参与比较 —— 否则两侧形态差异会造出假重复。"""
    # 一样的文字，一侧带掩码记号、一侧带还原出的转义码
    a = "使用 ⟦0⟧攻击⟦1⟧ 和 ⟦2⟧防御⟦3⟧"
    b = "使用 \\C[1]攻击\\C[0] 和 \\C[2]防御\\C[0]"
    assert check_repetition(a, b) == []
