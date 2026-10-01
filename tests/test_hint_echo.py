"""hint 回声判据（``check_hint_echo``）的测试。

## 这个判据要解决的事故

在**真实**游戏文本上（DemonsRoots 的 36 条英文多行文本，
模型 ``demonbyron/HY-MT1.5-7B``）实测到 **2 条译文就是提示词本身**：

    源: "Demon:\\nStill, we can't count on weapons to win our battles for us!…"
    译: "角色对白。保持说话者的语气、性格与语域；原文若是粗鲁/亲昵/敬语，
         中文也要对应。不要添加原文没有的称呼。"

玩家在对话里看到的是"翻译须知"，而所有检查都是绿的 ——
因为 ``check_leak`` 只匹配 ``要求：`` / ``抱歉…无法`` 这类**通用**元话形状，
抓不到"复述我们自己的 kind hint"。

## 这个测试文件的两条主线

1. **必须抓住**：实测抓到的真实泄漏样本，一条都不能漏。
2. **必须放过**：正常译文（尤其**短标签**）一条都不能误杀。

第 2 条同样重要 —— 这个项目已经因为**误杀**出过一次事故：
34 条正常的"对不起……"被当成模型道歉而清空，玩家看到空白对话框。
（见 ``tests/test_leak_and_stray_masks.py`` 的第一组用例。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.models import TextKind  # noqa: E402
from novaloc.translate.guards import (  # noqa: E402
    check_hint_echo,
    guard,
)
from novaloc.translate.prompts import KIND_HINT  # noqa: E402

DIALOGUE_HINT = KIND_HINT[TextKind.DIALOGUE]
ITEM_DESC_HINT = KIND_HINT[TextKind.ITEM_DESC]
UI_HINT = KIND_HINT[TextKind.UI_LABEL]


# ----------------------------------------------------------------------
# 一、必须抓住：实测的真实泄漏
# ----------------------------------------------------------------------

#: 每项 = (原文, 译文, 这条目收到的 hint)
#: 译文全部是**逐字抄自实测日志**的，不是编的。
REAL_LEAKS: list[tuple[str, str, str]] = [
    (
        "Demon:\nStill, we can't count on weapons to win our battles for us!\n"
        "We'll protect Bohelos with our own hands!",
        "角色对白。保持说话者的语气、性格与语域；原文若是粗鲁/亲昵/敬语，"
        "中文也要对应。不要添加原文没有的称呼。",
        DIALOGUE_HINT,
    ),
    (
        "Plana Citizen:\nLooks like the coliseum's being shut down... My life's over.",
        "角色对白。请保持说话者的语气、性格和语域；如果原文使用粗鲁、亲昵或"
        "敬语的表达方式，中文也应相应采用。切勿添加原文中没有的称呼。",
        DIALOGUE_HINT,
    ),
    (
        "Demonkind is short on hands. Everyone is divided into their area of\n"
        "expertise. It's the most efficient way.",
        "角色对白。需保持说话者的语气、性格和语域；若原文使用粗鲁、亲昵或"
        "敬语，中文也应相应体现。不得添加原文中没有的称呼。",
        DIALOGUE_HINT,
    ),
    (
        "Militia上がりの大男。\n口は悪いが面倒見はいい。",
        "这是物品/技能说明。完整保留数值与单位，不要省略任何一句。"
        "句式可以调整得更符合中文习惯。",
        ITEM_DESC_HINT,
    ),
    (
        "Some long English line about a shield.\nArte: Bloqueo Débil",
        "这是界面上的短标签（按钮、菜单、标题）。要求极短：优先 2~4 个汉字，"
        "最多不超过 6 个汉字，绝对不要为了完整而变长。",
        UI_HINT,
    ),
]


@pytest.mark.parametrize(("source", "target", "hint"), REAL_LEAKS)
def test_real_hint_echo_is_caught(source: str, target: str, hint: str) -> None:
    """**核心回归**：实测抓到的"译文=提示词"必须一条不漏。"""
    warnings = check_hint_echo(target, [hint], source=source)
    assert warnings, (
        "模型复述了提示词里的风格规则，判据却放过了 —— "
        f"玩家会在游戏里看到翻译须知。\n原文={source!r}\n译文={target!r}"
    )
    assert warnings[0].startswith("hint_echo")


@pytest.mark.parametrize(("source", "target", "hint"), REAL_LEAKS)
def test_hint_echo_is_fatal_and_leaves_no_text(
    source: str, target: str, hint: str
) -> None:
    """判死的同时**必须不留译文**。

    真实事故：一条被判坏的译文如果 ``target`` 还留着文字，
    下游 ``fonts`` 的字符集收集会把它收进去（见 provider 里那段注释），
    最终导致整轮字体适配中止、游戏满屏口口口。
    """
    res = guard(source, target, hints=[hint])
    assert res.fatal
    assert res.text == "", "判死的条目不能留下任何文字"


# ----------------------------------------------------------------------
# 二、必须放过：正常译文与诱饵
# ----------------------------------------------------------------------

#: 每项 = (原文, 正常译文, hint)
MUST_PASS: list[tuple[str, str, str]] = [
    ("Demon:\nWe'll protect Bohelos!", "恶魔：\n我们会亲手保护博埃洛斯！", DIALOGUE_HINT),
    ("I'm sorry... I can't do it.", "对不起……我做不到。", DIALOGUE_HINT),
    # 诱饵：译文里**真的**出现了"角色对白"四个字，但它是在引用这个词
    ("What does 'role dialogue' mean?", "「角色对白」是什么意思？", DIALOGUE_HINT),
    (
        "The soldier said, 'keep your tone respectful'.",
        "士兵说：「注意你的语气。」",
        DIALOGUE_HINT,
    ),
    ("Attack", "攻击", UI_HINT),
    ("Guard", "防御", UI_HINT),
    ("Yes", "是", UI_HINT),
    ("Potion", "药水", KIND_HINT[TextKind.ITEM_NAME]),
    ("Fireball", "火球术", KIND_HINT[TextKind.SKILL]),
    ("レベル", "等级", UI_HINT),
    (
        "An inquisitive girl who grew up in the Kingdom of Bohelos.\nWeapon Type:",
        "在博埃洛斯王国长大的好奇少女。\n武器类型：",
        ITEM_DESC_HINT,
    ),
]


@pytest.mark.parametrize(("source", "target", "hint"), MUST_PASS)
def test_normal_translations_are_not_flagged(source: str, target: str, hint: str) -> None:
    """正常译文一条都不能误杀（误杀 = 玩家看到空白对话框）。"""
    assert not check_hint_echo(target, [hint], source=source), (
        f"正常译文被误判成 hint 回声：{target!r}"
    )


def test_short_labels_are_never_flagged() -> None:
    """**核心回归**：短标签绝不能误杀。

    用 2-gram 之后必然的代价 —— ``"攻击"``（1 个 bigram）碰上
    UI_LABEL 的 hint（里面举例写了 ``Attack→攻击``）重合率就是 **100%**。
    实测 5 个汉字以内的短标签成片误杀。
    """
    for label in ["攻击", "防御", "道具", "设置", "返回", "保存", "读取", "是", "否"]:
        assert not check_hint_echo(label, [UI_HINT], source="x"), (
            f"短标签 {label!r} 被误判"
        )


def test_source_sharing_the_rule_is_exempt() -> None:
    """原文**本身就是**这句规则（source 与 hint 重合）时不算泄漏。

    这条守护的是 ``source`` 参数的作用：hint 与 source 共有的 n-gram
    被排除在比对之外。日文原文里出现中文规则字样的情形很少，
    但"原文是中文"的游戏（国产游戏的英文版回译）是真实存在的。
    """
    rule = "保持说话人的语气、性格与语域。"
    assert not check_hint_echo(rule, [DIALOGUE_HINT], source=rule)


# ----------------------------------------------------------------------
# 三、判据的边界行为
# ----------------------------------------------------------------------


def test_no_hint_means_no_check() -> None:
    """没有 hint 就不做判断（老调用方行为不变）。"""
    assert not check_hint_echo(DIALOGUE_HINT, None)
    assert not check_hint_echo(DIALOGUE_HINT, [])
    assert not check_hint_echo(DIALOGUE_HINT, ["", "   "])


def test_empty_target_is_not_flagged() -> None:
    """空译文交给 ``empty_translation`` 处理，不该报 hint_echo。"""
    assert not check_hint_echo("", [DIALOGUE_HINT])


def test_guard_without_hints_keeps_old_behaviour() -> None:
    """不传 hints 时 ``guard`` 不应报 hint_echo（向后兼容）。"""
    res = guard("Demon:\nWe'll protect Bohelos!", DIALOGUE_HINT)
    assert not any(w.startswith("hint_echo") for w in res.warnings)


def test_ratio_direction_is_hint_coverage() -> None:
    """判据量的是"**规则**被复现了多少"，不是"译文里多少像规则"。

    方向搞反会漏判改写型复述：实测那条改写过的泄漏，
    "译文里多少像规则" 只有 **44.4%**（够不到 50% 的线），
    而"规则被复现了多少" 是 44.2% —— 但正常译文只有 **4.7%**，
    两个数量级的空隙让阈值可以稳稳取 35%。
    """
    from novaloc.translate.guards import _ngrams

    target = (
        "角色对白。请保持说话者的语气、性格和语域；如果原文使用粗鲁、亲昵或"
        "敬语的表达方式，中文也应相应采用。切勿添加原文中没有的称呼。"
    )
    hint_grams = _ngrams(DIALOGUE_HINT, 4)
    target_grams = _ngrams(target, 4)
    coverage = len(target_grams & hint_grams) / len(hint_grams)
    # "译文里多少像规则" 这个反向指标明显偏低，所以不能拿它当判据
    reverse = len(target_grams & hint_grams) / len(target_grams)
    assert coverage > reverse, "两个方向必须区分开，否则判据形同虚设"
    assert check_hint_echo(target, [DIALOGUE_HINT], source="Plana Citizen:\nhi")
