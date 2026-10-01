r"""★ 连续「内容记号」组被拆散 —— 顺序对、邻接错的一类沉默损坏。

## 事故经过

MV 的 `Actors.json` 角色 profile（真实记录）：

```
源：A resourceful and tactical demon girl.
    Weapon Type: \I[96]\I[97]\I[105]\I[100]   Armor Type: \I[129]\I[135]\I[136]\I[139]
译：一位机智且善于战术的恶魔少女。
    武器类型：[武器名称]； 防御\I[96]\I[97]\I[105]\I[100]类型：[防具名称]\I[129]\I[135]\I[136]\I[139]
```

**编号顺序是 96, 97, 105, 100, 129, 135, 136, 139 —— 递增，完全正确。**
记号一个没少。所以 `verify_restored` 原有的两类判据
（多重集、出现顺序）**全部通过**。

坏掉的是**相邻关系**：图标原本紧跟在 `Weapon Type:` 后面，
现在跑到了 `防具类型` 后面 —— 玩家看到"防具类型"配武器图标。

## 为什么这类损坏特别危险

`\I[96]`（画一个图标）挪位是**换内容**，不是换样式：

* `\C[29]`（改文字颜色）挪位 → 只影响配色，玩家基本看不出；
* `\I[96]` 挪位 → **图标本身有实义**，配错词就是错误信息。

而中文语序本来就和原文不同，所以**不能**对样式记号判邻接
（会误杀大量正常译文）。所以只对 `\I` / `\N` / `\V` / `\P` 这类
内容记号判。

## 实测

* 有「连续内容记号组」的条目：**13 条**
* 其中判为拆散：**13 条（100%）** —— 全部人工核对确认是真的坏了
* 误报：**0 条**

（"凡是这种形状都被打散"本身就是强证据：说明这是模型行为的
系统性结果，不是偶发。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import placeholders as ph  # noqa: E402

#: 真实事故里的源文（武器/防具图标码）
SRC = (
    "A resourceful and tactical demon girl.\n"
    "Weapon Type: \\I[96]\\I[97]\\I[105]\\I[100]   "
    "Armor Type: \\I[129]\\I[135]\\I[136]\\I[139]"
)

#: 真实事故里的坏译文（图标滑到「防具类型」后面）
BAD = (
    "一位机智且善于战术的恶魔少女。\n"
    "武器类型：[武器名称]； 防御⟦0⟧⟦1⟧⟦2⟧⟦3⟧类型：[防具名称]⟦4⟧⟦5⟧⟦6⟧⟦7⟧"
)

#: 正确译文（图标保持贴在标签后面）
GOOD = (
    "一位机智且善于战术的恶魔少女。\n"
    "武器类型：⟦0⟧⟦1⟧⟦2⟧⟦3⟧   防具类型：⟦4⟧⟦5⟧⟦6⟧⟦7⟧"
)


def _masked() -> ph.MaskResult:
    return ph.mask(SRC)


# ---------------------------------------------------------------------------
# 一、核心：坏的要判死，好的要放行
# ---------------------------------------------------------------------------


def test_split_content_run_is_fatal() -> None:
    """★ 核心回归：图标组滑到别的词旁边必须判死。"""
    m = _masked()
    _restored, check = ph.verify_restored(SRC, BAD, m.slots, masked_source=m.text)
    assert check.runs_split, f"没抓到拆散：{check.describe()}"
    assert check.fatal, "拆散必须判 fatal，否则会写回错位的图标"


def test_intact_content_run_passes() -> None:
    """★ 反向：图标保持原位必须放行。

    修假阳性最容易犯的错就是把判据削到什么都不报。
    """
    m = _masked()
    _restored, check = ph.verify_restored(SRC, GOOD, m.slots, masked_source=m.text)
    assert not check.runs_split, f"误报：{check.describe()}"
    assert not check.fatal, check.describe()


def test_order_check_alone_would_have_missed_it() -> None:
    """★ 说明"为什么必须新增判据"：原有序判据**抓不到**这个 bug。

    坏译文里的编号顺序是 0,1,2,3,4,5,6,7 —— 和源文完全一致。
    """
    m = _masked()
    _restored, check = ph.verify_restored(SRC, BAD, m.slots, masked_source=m.text)
    assert not check.order_changed, "这条的顺序确实是对的（所以旧判据放过它）"
    assert check.runs_split, "新判据必须补上这个缺口"


# ---------------------------------------------------------------------------
# 二、只对「内容记号」判 —— 样式记号挪位是正常的
# ---------------------------------------------------------------------------


def test_style_marks_may_move() -> None:
    r"""★ `\C[n]`（改颜色）挪位不算坏。

    中文语序本来就和原文不同，对样式记号判邻接会误杀大量正常译文。
    """
    src = "He said: \\C[6]Hello there\\C[0]"
    m = ph.mask(src)
    # 颜色记号挪到句首/句尾 —— 配色不完美，但内容没变
    moved = "⟦0⟧他说：你好啊⟦1⟧"
    _restored, check = ph.verify_restored(src, moved, m.slots, masked_source=m.text)
    assert not check.runs_split, f"样式记号挪位不该判坏：{check.describe()}"


def test_label_translation_does_not_trigger() -> None:
    """★ 记号前面的标签被翻译（`Weapon Type:` → `武器类型：`）不算坏。"""
    src = "Weapon Type: \\I[96]\\I[97]"
    m = ph.mask(src)
    ok = "武器类型：⟦0⟧⟦1⟧"
    _restored, check = ph.verify_restored(src, ok, m.slots, masked_source=m.text)
    assert not check.runs_split, f"标签翻译被误判：{check.describe()}"


def test_single_mark_run_is_not_checked() -> None:
    """单个记号谈不上"被拆散"。"""
    src = "Weapon Type: \\I[96]   Armor Type: \\I[129]"
    m = ph.mask(src)
    moved = "武器类型：⟦0⟧   防具类型：⟦1⟧"
    _restored, check = ph.verify_restored(src, moved, m.slots, masked_source=m.text)
    assert not check.runs_split


def test_punctuation_to_punctuation_is_fine() -> None:
    """`:` → `：` 是正常的中文标点转换，不该算拆散。"""
    src = "Weapon Type: \\I[96]\\I[97]"
    m = ph.mask(src)
    ok = "武器类型：⟦0⟧⟦1⟧"
    _restored, check = ph.verify_restored(src, ok, m.slots, masked_source=m.text)
    assert not check.runs_split


# ---------------------------------------------------------------------------
# 三、重查已有数据用入口
# ---------------------------------------------------------------------------


def test_restored_entry_detection_finds_the_real_case() -> None:
    """★ 工作区里躺着的**已还原**译文也要能查出来（用于重查旧数据）。"""
    restored_bad = (
        "一位机智且善于战术的恶魔少女。\n"
        "武器类型：[武器名称]； 防御"
        "\\I[96]\\I[97]\\I[105]\\I[100]类型：[防具名称]"
        "\\I[129]\\I[135]\\I[136]\\I[139]"
    )
    split = ph.content_runs_split_in_restored(SRC, restored_bad)
    assert split, "已还原的坏译文没被查出来 —— 旧数据会一直烂在工作区里"


def test_restored_good_entry_not_flagged() -> None:
    restored_ok = (
        "一位机智且善于战术的恶魔少女。\n"
        "武器类型：\\I[96]\\I[97]\\I[105]\\I[100]   "
        "防具类型：\\I[129]\\I[135]\\I[136]\\I[139]"
    )
    assert ph.content_runs_split_in_restored(SRC, restored_ok) == []


def test_repeated_slot_content_is_skipped() -> None:
    """★ 槽位内容重复时**不许**反推（会张冠李戴 ⇒ 误报 ⇒ 作废好译文）。

    宁可漏报：漏报只是"这一条继续烂着"，误报会把好译文作废并重译。
    """
    # 同一批图标码在源文里出现两次 —— 反推时无法确定第一处对应哪个编号
    src = "Weapon Type: \\I[96]\\I[97]\\I[96]\\I[97]"
    restored = "武器类型：\\I[96]\\I[97]\\I[96]\\I[97]"
    assert ph.content_runs_split_in_restored(src, restored) == []


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_inputs_are_safe(empty: str) -> None:
    assert ph.content_runs_split_in_restored(empty, empty) == []
    assert ph.content_runs_split_in_restored(SRC, empty) == []


def test_describe_mentions_the_problem() -> None:
    """报告要让人看懂"图标贴到别的词上了"，而不只是"占位符错误"。"""
    m = _masked()
    _restored, check = ph.verify_restored(SRC, BAD, m.slots, masked_source=m.text)
    text = check.describe()
    assert "拆散" in text or "图标" in text, f"说明文字没点出问题：{text!r}"
