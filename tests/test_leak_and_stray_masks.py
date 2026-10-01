"""两个"判据太宽 / 太严"的真实 bug，都造成了静默的译文丢失。

## bug A：`prompt_leak` 把"对不起"当成模型在道歉

`_LEAK_PATTERNS` 里有一条**裸的** ``r"抱歉"``（任意位置匹配）。
真实数据里 **34 条译文被它判成 prompt_leak 并整条丢弃**，而它们**全是误杀**：

    原文 "I'm sorry..."              → 译文 "抱歉……"        ❌ 被丢
    原文 '對不起……但是你的陰道很棒……！'  → 译文 "对不起……"      ❌ 被丢
    原文 '... Lo lamento...'          → 译文 "对不起……"       ❌ 被丢

**"对不起"是剧情里最常见的台词之一**，而"抱歉"就是它的标准译文。
后果是玩家在游戏里看到 34 处**空白对话框**，而所有检查都是绿的。

判据要匹配**形状**，不只是**词**：
"模型在道歉"的形状是"抱歉 + 我无法/不能"，
光有"抱歉"两个字说明不了任何事。

同理 ``r"以下是"`` 会把 `'以下是你的奖励。'`（正常台词）判成泄漏。

## bug B：凭空多出来的屏蔽记号把整条译文判死

源文**根本没有占位符**时，模型偶尔会凭空写出 ``⟦0⟧``：

    原文 '啊...乌鲁拉 别那么快，不然我就要射了！'
    → 模型回 '啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧'

这条撞进 `verify_restored` 的"多出占位符"分支，整条判为 **fatal**、
译文清空。实测 **14 条**这类失败，全部可以救 ——
因为"多出来的记号"和"丢失的占位符"**性质不同**：

* 丢失的是**真信息**（变量/换行/颜色码），必须拒绝；
* 多出来的是**模型幻觉**，删掉不丢任何东西。

所以 `strip_unknown_masks()` 只删**越界**的记号，
合法的（``⟦0⟧`` 在 n_slots=3 时）原样保留交给后续校验。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import guards  # noqa: E402
from novaloc.translate import placeholders as ph  # noqa: E402
from novaloc.translate.guards import check_leak  # noqa: E402

#: 反斜杠。**不在源码里写反斜杠字面量** —— 历史上因为转义层数反复踩坑
#: （见 `placeholders._BS` 的说明），统一从被测模块取。
BS = ph._BS

# ----------------------------------------------------------------------
# bug A：剧情里的"对不起"不是泄漏
# ----------------------------------------------------------------------

REAL_DIALOGUE_APOLOGIES = [
    "抱歉……",
    "对不起……",
    "对不起，Linda。我到目前为止遇到了许多美好的人，",
    "……对不起……",
    "对不起；实际上，我在那个时候丢下了你。",
    "對不起……我應該早點警告你……",
    "哈啊... 对不起",
    "抱歉，我来晚了。",
    "I am sorry, my friend.",
    "Sorry, I did not mean that.",
    "Haaaah... I'm sorry",
    "Perdón si estoy siendo una molestia.",
]


@pytest.mark.parametrize("text", REAL_DIALOGUE_APOLOGIES)
def test_dialogue_apology_is_not_a_leak(text: str) -> None:
    """**核心回归**：这 34 条曾全部被丢弃，玩家看到空白对话框。"""
    assert not check_leak(text), f"剧情台词被误判成 prompt_leak：{text!r}"


def test_meta_answer_apology_is_still_a_leak() -> None:
    """但"道歉 + 我做不到"确实是拒答，必须挡住。"""
    assert check_leak("抱歉，我无法翻译这段文字")
    assert check_leak("抱歉，我不能处理这个请求")
    assert check_leak("抱歉，我没办法完成这个请求")


def test_following_is_not_a_leak_by_itself() -> None:
    """``以下是`` 单独出现是正常台词，必须跟着"翻译/译文/结果"才算泄漏。"""
    assert not check_leak("以下是你的奖励。")
    assert not check_leak("以下是任务奖励，请收下。")
    assert check_leak("以下是翻译结果：")
    assert check_leak("以下是译文")


@pytest.mark.parametrize(
    "text",
    [
        "要求：把下面的文本翻译成中文",
        "译文：你好",
        "翻译：你好",
        "输出：你好",
        "```json",
        "作为一个人工智能，我无法完成这个请求",
        "无法确定具体含义",
        "无法识别这段文本",
        "I'm sorry, but I cannot translate this.",
        "Unable to translate the given text",
        "No text was found in this image",
    ],
)
def test_real_leaks_are_still_caught(text: str) -> None:
    assert check_leak(text), f"真泄漏漏过了：{text!r}"


def test_leak_regex_compiles_with_scoped_flags() -> None:
    """英文模式用局部标志 `(?i:...)`。

    这些模式被 `"|".join()` 拼成一条大正则，
    全局 `(?i)` 只要不在最开头就会抛
    ``global flags not at the start of the expression`` ——
    这是实际踩过的错，所以专门钉一条"模块能正常导入并且能匹配"。
    """
    assert check_leak("I'm sorry, but I cannot translate this.")
    assert not check_leak("Sorry I'm late.")


# ----------------------------------------------------------------------
# bug C：补回占位符把拒答"撑出"窗口，导致 prompt_leak 漏掉
# ----------------------------------------------------------------------

def test_refusal_is_caught_even_with_markup_inserted() -> None:
    r"""**核心回归**：补回占位符**不得**让拒答漏网。

    ## 事故经过

    拒答判据的形状是"道歉 + 做不到同时出现、且隔得不远"，
    中间用 `[^。！？\n]{0,12}?` 限制窗口。这是为 `请稍等，我现在无法确定`
    这类写法留的余地。

    但补回占位符会把标签**插进这个窗口**：

        模型回复: '抱歉，我无法完成这个请求。'          → 命中 ✅
        补回标签: '抱歉，<color=#ff0000></color>我无法…' → **漏掉** ❌

    `<color=#ff0000></color>` 是 25 个字符，把窗口撑爆了。
    而玩家在游戏里**一个字都看不到它**。

    修法：`check_leak` 除了查原样文本，还把**纯样式记号**剥掉再查一遍。
    这条测试就是那次修复的钉子 —— 它是在
    `tests/test_translate_integration.py` 的"完全不是 JSON"场景
    被改红之后才发现的（**改好了补回，却把这道闸门撑漏了**）。
    r"""
    plain = "抱歉，我无法完成这个请求。"
    tagged = "抱歉，<color=#ff0000></color>我无法完成这个请求。"
    assert check_leak(plain), "基准：不带标签的拒答必须命中"
    assert check_leak(tagged), "带标签的拒答也**必须**命中（这就是回归）"
    # RPG Maker 颜色码同理
    assert check_leak(BS + "C[29]".join(["抱歉，", "我没办法。"]))


def test_markup_stripping_does_not_create_false_positives() -> None:
    """剥样式记号**不能**把正常道歉台词变成拒答（34 条误杀的历史）。"""
    for text in [
        "对不起……",
        "抱歉……但是你的阴道很棒……！",
        "对不起，我没办法不爱上你。",
        BS + "C[29]抱歉……" + BS + "C[0]",
        "勇者" + BS + "V[1]，你终于醒了！",
        "造成 5 点伤害。",
    ]:
        assert not check_leak(text), f"误杀：{text!r}"


def test_variable_placeholders_are_not_stripped_from_the_gap() -> None:
    r"""`\V[1]` / `%s` 这类**实义**占位符**不能**从窗口里剔掉。

    它们在句子里占一个真实位置（会显示成玩家名/数字）。
    剔掉它们会让窗口里凭空少一段，反而更容易误判 ——
    所以只有"纯样式"的那批（颜色/图标/停顿/标签）才剔。
    r"""
    stripped = guards._LEAK_GAP_MARKUP_RE.sub("", BS + "V[1] 和 %s 与 " + BS + "C[3]")
    assert BS + "V[1]" in stripped, "变量占位符不该被剔掉"
    assert "%s" in stripped, "参数占位符不该被剔掉"
    assert BS + "C[3]" not in stripped, "颜色码应当被剔掉"


# ----------------------------------------------------------------------
# bug B：凭空多出来的屏蔽记号
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,n_slots,expect",
    [
        # 源文没有占位符 → 所有记号都是凭空造的，全删
        ("啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧", 0, "啊…乌鲁拉，别急，我还没瞄准呢！”} "),
        ("哎… 嗯… 是… 请帮帮我…”} ⟦0⟧, {", 0, "哎… 嗯… 是… 请帮帮我…”} , {"),
        # 合法记号必须**原样保留**
        ("伤害值 ⟦0⟧ 点", 3, "伤害值 ⟦0⟧ 点"),
        ("用 ⟦0⟧ 换 ⟦2⟧", 3, "用 ⟦0⟧ 换 ⟦2⟧"),
        # 越界的删掉，合法的留着
        ("用 ⟦0⟧ 换 ⟦7⟧", 3, "用 ⟦0⟧ 换 "),
        ("没有记号", 0, "没有记号"),
        ("", 0, ""),
    ],
)
def test_strip_unknown_masks(text: str, n_slots: int, expect: str) -> None:
    assert ph.strip_unknown_masks(text, n_slots) == expect


def test_stripping_rescues_a_fatal_verification() -> None:
    """端到端：原本 fatal 的条目，删掉凭空记号后应该通过。

    这 14 条之前的结果是**译文清空**（玩家看到空白对话框）。
    """
    src = "啊...乌鲁拉 别那么快，不然我就要射了！"
    raw = "啊…乌鲁拉，别急，我还没瞄准呢！”} ⟦0⟧"
    masked, slots = ph.mask_batch([src])
    assert len(slots[0]) == 0, "这条本来就没有占位符"

    _r1, c1 = ph.verify_restored(src, raw, slots[0], masked_source=masked[0])
    assert c1.fatal, "应当先失败（多出占位符）"

    cleaned = ph.strip_unknown_masks(raw, len(slots[0]))
    _r2, c2 = ph.verify_restored(src, cleaned, slots[0], masked_source=masked[0])
    assert not c2.fatal, f"删掉凭空记号后应当通过：{c2.describe()}"


def test_missing_placeholder_is_still_fatal() -> None:
    """**边界**：丢失占位符仍然必须 fatal —— 那是真信息，不能靠删记号蒙过去。"""
    src = "Attack \\V[1] for 10 damage"
    masked, slots = ph.mask_batch([src])
    assert slots[0], "这条应当有占位符"
    # 模型把占位符整个删了
    _r, c = ph.verify_restored(
        src, "攻击造成 10 点伤害", slots[0], masked_source=masked[0]
    )
    assert c.fatal, "丢失占位符必须仍然 fatal"


def test_legit_masks_survive_stripping() -> None:
    """合法记号不能被误删，否则会变成"丢失占位符"。"""
    src = "Attack \\V[1] for \\V[2] damage"
    masked, slots = ph.mask_batch([src])
    n = len(slots[0])
    assert n == 2, f"应当有 2 个占位符，实际 {n}"
    text = "攻击造成 ⟦0⟧ 到 ⟦1⟧ 点伤害"
    assert ph.strip_unknown_masks(text, n) == text
    _r, c = ph.verify_restored(src, text, slots[0], masked_source=masked[0])
    assert not c.fatal, f"合法记号不该被判失败：{c.describe()}"
