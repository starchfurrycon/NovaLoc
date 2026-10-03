"""守卫和屏蔽器对"什么算占位符"必须**完全一致**。

## 真实事故

在一个真实 RPG Maker MV 游戏上跑翻译，**31 条**以
`placeholder_count:1->0` 失败，而且译文本身是好的：

    原文 = 'Deals \\D damage. Add 2 『\\S[88]』 cards to your deck.'
    译文 = '造成 D 点伤害。'          ← 后半句整段丢了
    警告 = ['placeholder_count:1->0']

丢掉的 `\\S[88]` 是**卡牌引用**（插件用它指向另一张卡）。
少一个字符，游戏行为就变了。

## 根因：两名"裁判"用了两套规则

| 文件 | 规则 | 认 `\\S[88]` 吗 |
| --- | --- | --- |
| `lang.py:_PLACEHOLDER_PATTERNS`（守卫用） | `\\[VvNnPpCcIiSs]\\[\\d+\\]` | **认** |
| `placeholders.py:PLACEHOLDER_PATTERNS`（屏蔽/修补用） | `\\[VNCPI]\\[\\d+\\]` | **不认** |

于是：守卫发现了 `\\S[88]` 并判 fatal；屏蔽器从没把它替换成掩码，
所以模型丢掉它之后 `repair_dropped_masks()` **没有东西可补** ——
一条本来完全可救的译文被整条判失败。

真实游戏里的量（`www/data/*.json` 全量统计）：

| 形式 | 出现次数 | 屏蔽器 |
| --- | --- | --- |
| `\\V[n]` | 239 | ✅ 认 |
| `\\S[n]` | **33** | ❌ 不认 |
| `\\v[n]` | **32** | ❌ 不认 |

两个都不认，合计 65 处，全都会走"判失败"这条路。
还有嵌套形式 `\\S[\\V[101]]`（`Obtained \\S[\\V[101]].`），
`\\V[101]` 会被当独立掩码、外层 `\\S[` 留在明文里。

## 修法

让屏蔽器认 `S`/`s`（和守卫对齐），并把嵌套形式放在基础规则**之前**
匹配，否则 `\\V[101]` 会先被单独吃掉，留下 `\\S[` 和 `]` 变成明文。

**顺序很重要**，所以下面有一条专门测"嵌套必须整体成一个掩码"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.lang import extract_placeholders  # noqa: E402
from novaloc.translate import placeholders as ph  # noqa: E402

#: 真实游戏里出现过的形式（`\D`/`\R` 是**这个游戏的插件**定义的，
#: 不是 RPG Maker 原生转义；它们没有 `[n]`，不在本节范围内）
REAL_FORMS = [
    "\\V[1]",
    "\\N[2]",
    "\\C[6]",
    "\\I[4]",
    "\\P[5]",
    "\\S[83]",     # ← 事故里丢的就是这个
    "\\S[88]",
    "\\s[110]",   # 小写变体也在真实数据里
    "\\v[122]",   # 同上，32 处
    # ★ RPG Maker **属性显示命令**（`[n]` 是小数位数）。
    # 全库实测 89 条条目含这类命令；漏掉是**静默损坏** ——
    # `【HP】\mhp[3]` 会被模型吃掉参数变成 `生命值：\mhp`，
    # 而所有检查都报成功（详见下面那组专门用例）。
    "\\mhp[3]",
    "\\atk[2]",
    "\\def[2]",
    "\\mag[3]",
    "\\mdf[2]",
    "\\agi[2]",
    "\\exp[4]",
]


# ----------------------------------------------------------------------
# 核心：两名裁判必须一致
# ----------------------------------------------------------------------

@pytest.mark.parametrize("token", REAL_FORMS)
def test_masker_agrees_with_guard_on_every_real_form(token: str) -> None:
    """守卫认的形式，屏蔽器必须也认。

    "一致"的定义要说清楚（不然这测试会变成一句空话）：
    **守卫抽出来的每一个 token，都不能作为明文残留在屏蔽结果里**。
    这是关键性质 —— 只要某个 token 留在明文里，模型就可能把它翻掉或
    吃掉，而修补层又无从补起（事故里 31 条就是这么废掉的）。
    """
    src = f"prefix {token} suffix"
    guard_sees = extract_placeholders(src)
    masked, slots = ph.mask_batch([src])
    masker_sees = slots[0] if slots else []

    assert guard_sees, f"守卫不认 {token!r}（测试前提就不成立）"
    assert masker_sees, (
        f"守卫认 {token!r} 但屏蔽器不认 —— 模型丢掉它之后无法修补，"
        f"整条译文会被判失败（真实事故里 31 条）。\n"
        f"  守卫: {guard_sees}\n  屏蔽器: {masker_sees}\n  屏蔽后: {masked[0]!r}"
    )
    for t in guard_sees:
        assert t not in masked[0], (
            f"{token!r}: 守卫认的 token {t!r} 仍以明文留在屏蔽结果里：{masked[0]!r}\n"
            f"  模型翻掉它之后，修补层没有任何依据把它补回来。"
        )
    assert ph.MASK_OPEN in masked[0], f"{token!r} 屏蔽后没有掩码记号：{masked[0]!r}"


@pytest.mark.parametrize("token", REAL_FORMS)
def test_masked_token_is_removed_from_plain_text(token: str) -> None:
    """屏蔽之后，明文里不能再残留这个 token 的字符。"""
    src = f"prefix {token} suffix"
    masked, _ = ph.mask_batch([src])
    assert token not in masked[0], f"{token!r} 没被屏蔽掉：{masked[0]!r}"
    # 掩码记号要还在
    assert ph.MASK_OPEN in masked[0]


# ----------------------------------------------------------------------
# 嵌套形式必须**整体**成一个掩码（顺序问题）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "nested",
    ["\\S[\\V[101]]", "\\S[\\V[130]]", "\\s[\\V[110]]", "\\S[\\V[101]]"],
)
def test_nested_placeholder_masks_as_one_unit(nested: str) -> None:
    """`\\S[\\V[101]]` 必须**整体**成为一个掩码。

    如果基础规则 `\\[VNCPI]\\[\\d+\\]` 先跑，它会先吃掉里面的 `\\V[101]`，
    于是外层留下 `\\S[` 和 `]` 变成可被翻译的明文 ——
    那样即使 token 数量对得上，结构也已经坏了。
    """
    src = f"Obtained {nested}."
    masked, slots = ph.mask_batch([src])
    assert len(slots[0]) == 1, (
        f"{nested!r} 应整体成一个掩码，实际切成了 {len(slots[0])} 个：{slots[0]}\n"
        f"  屏蔽后 = {masked[0]!r}"
    )
    assert slots[0][0] == nested, f"槽位内容不对：{slots[0][0]!r} != {nested!r}"
    assert "\\S[" not in masked[0] and "\\V[" not in masked[0], (
        f"明文里残留了嵌套标记：{masked[0]!r}"
    )


def test_nested_round_trip() -> None:
    """嵌套形式屏蔽 → 还原，必须一字不差。"""
    src = "Obtained \\S[\\V[101]]."
    masked, slots = ph.mask_batch([src])
    restored, chk = ph.verify_restored(src, masked[0], slots[0], masked_source=masked[0])
    assert not chk.fatal, chk
    assert restored == src, f"{restored!r} != {src!r}"


# ----------------------------------------------------------------------
# 之前的错误行为：整条被判失败。现在必须能**修补**
# ----------------------------------------------------------------------

def test_dropped_script_placeholder_can_be_repaired() -> None:
    """模型丢掉掩码时，修补层必须能把它补回来。

    这是修复的**用户可见收益**：修好之前 `repair_dropped_masks()` 返回
    `None`（因为没屏蔽过，无从补起），整条译文被丢弃；
    修好之后能补回来，用户看到的是完整译文 + 一个 `\\S[88]`。

    ⚠️ 用**句子译全了、只丢掩码**这个例子。不要用"后半句整段没译"
    那种例子去要求修补成功 —— 当模型把包含掩码的整个从句都丢掉时，
    掩码该插回哪里是没有依据的，那时候 `repair` 返回 `None`、
    条目被判失败是**正确**行为（宁可让用户看到"未翻译"，
    也不要猜一个位置把 `\\S[88]` 塞进去）。
    """
    src = "Deals \\D damage. Add 2 『\\S[88]』 cards to your deck."
    masked, slots = ph.mask_batch([src])
    assert slots[0], (
        "`\\S[88]` 仍然没被屏蔽 —— 修补无从下手，这条译文注定被判失败"
    )
    assert "\\S[88]" not in masked[0], f"掩码后明文里还残留：{masked[0]!r}"

    # 模型把句子译全了，只是没保留掩码记号（很常见）
    raw = "造成伤害。将 2 张『』卡加入你的牌组。"
    repaired = ph.repair_dropped_masks(masked[0], raw, slots[0])
    assert repaired is not None, (
        f"修补失败。\n  屏蔽后原文 = {masked[0]!r}\n  模型回复 = {raw!r}\n"
        f"  槽位 = {slots[0]}"
    )
    restored, chk = ph.verify_restored(
        src, repaired, slots[0], masked_source=masked[0]
    )
    assert not chk.fatal, f"修补后仍然 fatal：{chk}"
    assert "\\S[88]" in restored, f"补回来的译文里没有 \\S[88]：{restored!r}"


def test_whole_clause_dropped_still_fails_loudly() -> None:
    """**边界**：连掩码所在的整个从句都没译时，必须判失败而不是猜。

    修补层有能力"补回丢掉的记号"，但它不该在**没有任何位置依据**时
    硬插一个 `\\S[88]` 进去 —— 插错位置会改变游戏行为，
    比"这条没翻译、让用户看到"更糟。
    """
    src = "Deals \\D damage. Add 2 『\\S[88]』 cards to your deck."
    masked, slots = ph.mask_batch([src])
    raw = "造成伤害。"          # 后半句整段没了
    repaired = ph.repair_dropped_masks(masked[0], raw, slots[0])
    if repaired is None:
        return  # 正确：拒绝猜位置
    # 如果它选择补，那也必须补在合法位置且能通过校验
    restored, chk = ph.verify_restored(
        src, repaired, slots[0], masked_source=masked[0]
    )
    assert not chk.fatal, f"补出来的东西没通过校验：{chk} / {restored!r}"


def test_lowercase_s_variant_also_protected() -> None:
    """真实数据里有 `\\s[110]`（小写），同样必须保护。"""
    src = "Obtained \\s[110]."
    masked, slots = ph.mask_batch([src])
    assert slots[0] == ["\\s[110]"], f"小写变体没保护：{masked[0]!r} / {slots[0]}"


# ----------------------------------------------------------------------
# 不能过度屏蔽（把正常文本切碎）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "100% complete",
        "Press S to start",
        "Cost: 5 gold",
        "Use [item] now",
        "Add 2 cards to your deck.",
    ],
)
def test_normal_text_is_not_mangled(text: str) -> None:
    """普通文本不能被新规则误切。

    判据同上面那条：守卫和屏蔽器对"有没有占位符"必须一致，
    否则不是模型丢掉记号后无法修补（守卫认、屏蔽器不认），
    就是普通文本被切碎（屏蔽器认、守卫不认）。
    """
    masked, slots = ph.mask_batch([text])
    guard_sees = extract_placeholders(text)
    masker_sees = slots[0] if slots else []
    assert bool(masker_sees) == bool(guard_sees), (
        f"{text!r}: 守卫看到 {guard_sees}，屏蔽器看到 {masker_sees} —— 不一致\n"
        f"  屏蔽后 = {masked[0]!r}"
    )


# ----------------------------------------------------------------------
# 插件定义的裸字母占位符 `\D` / `\R`（真实游戏实测损失最惨的一类）
# ----------------------------------------------------------------------

PLUGIN_LETTER_FORMS = ["\\D", "\\R"]


@pytest.mark.parametrize("token", PLUGIN_LETTER_FORMS)
def test_plugin_letter_placeholder_is_protected(token: str) -> None:
    """`\\D` / `\\R` 必须被屏蔽。

    真实游戏实测：**92 条**含 `\\D`/`\\R`，其中 **91 条**在译文里彻底丢失 ——

        原文 = 'Deals \\D damage. Gain 4 「Defense」 until turn end.'
        译文 = '造成 D 点伤害。获得 4 点防御值，持续至回合结束。'
                                        ↑ 反斜杠没了，游戏里会显示一个裸字母 D

    更离谱的一条把 `\\R` 输成了 `\\textbackslash R`（LaTeX 写法）。

    这是本项目最怕的失败模式：**阶段全部报成功，产物已经坏了**。
    以前双方都不认识这个记号，所以没有任何检查能抓到它。
    """
    src = f"Deals {token} damage."
    masked, slots = ph.mask_batch([src])
    assert slots[0] == [token], (
        f"{token!r} 没被屏蔽：槽位={slots[0]}\n  屏蔽后={masked[0]!r}\n"
        f"模型会把它读成字母 {token[1]}，然后在译文里丢掉反斜杠。"
    )
    assert token not in masked[0], f"明文里还残留 {token!r}：{masked[0]!r}"
    assert token in extract_placeholders(src), "守卫也要认它（两侧必须同步）"


def test_plugin_letter_is_not_confused_with_literal_letter() -> None:
    """**边界**：裸字母 `D`/`R` 不能被当成占位符。

    只有**带反斜杠**的才是占位符。普通文本里的 `D`（例如 "Discard"、
    "Draw Pile"）绝不能被屏蔽 —— 那会把正常词切碎。
    """
    for text in ["Discard your hand.", "Draw Pile", "Deals damage.", "R times"]:
        masked, slots = ph.mask_batch([text])
        assert not (slots[0] if slots else []), (
            f"{text!r} 里的裸字母被误当成占位符：{slots[0]!r}"
        )
        assert masked[0] == text, f"{text!r} 被改动了：{masked[0]!r}"


def test_plugin_letter_round_trip() -> None:
    """`\\D` / `\\R` 屏蔽后还原必须一字不差。"""
    src = "Deals \\D damage \\R times."
    masked, slots = ph.mask_batch([src])
    restored, chk = ph.verify_restored(src, masked[0], slots[0], masked_source=masked[0])
    assert not chk.fatal, chk
    assert restored == src, f"{restored!r} != {src!r}"


def test_plugin_letter_loss_is_detected_by_the_guard() -> None:
    """模型丢掉 `\\D` 时，守卫必须发现（而不是静默写回坏产物）。"""
    src = "Deals \\D damage to all enemies."
    # 实测的真实坏译文：反斜杠没了
    broken = "对所有敌人造成 D 点伤害。"
    from novaloc.translate.guards import check_placeholders

    warnings = check_placeholders(src, broken)
    assert warnings, (
        "守卫没发现 `\\D` 丢失 —— 这条坏译文会被写回游戏，"
        "玩家看到的是一个裸字母 D，而所有检查都报成功。"
    )


# ----------------------------------------------------------------------
# ★ RPG Maker 属性显示命令 `\mhp[3]` —— 静默损坏（所有检查都报成功）
# ----------------------------------------------------------------------

STAT_CMDS = ["\\mhp", "\\atk", "\\def", "\\mag", "\\mdf", "\\agi", "\\exp"]


@pytest.mark.parametrize("cmd", STAT_CMDS)
def test_stat_display_command_is_protected(cmd: str) -> None:
    r"""★ `\mhp[3]` 这类属性显示命令必须被屏蔽、且守卫也认。

    ## 真实缺陷（实测）

    `072 Project` 的魔物/人物介绍里大量出现属性块：

        原文  '【HP】\mhp[3]   【攻撃力】\atk[2]   【防御力】\def[2] '
        槽位  []                    ← 屏蔽器完全没认出
        模型回 '生命值：\mhp'        ← 参数 `[3]` 被吃掉
        占位符校验 fatal=False        ← 没有任何检查能拦住

    模型把 `\mhp[3]` 读成"一个叫 mhp 的词"，于是只译了标签、
    把参数丢了。游戏里【HP】后面会显示成裸 `\mhp` 而不是数字，
    **而且阶段全部报成功** —— 这正是本项目最怕的失败模式。

    全库统计：**89 条**条目含这类命令（7 种命令各 89 次）。
    """
    src = f"【HP】{cmd}[3]"
    masked, slots = ph.mask_batch([src])
    assert slots[0] == [f"{cmd}[3]"], (
        f"{cmd}[3] 没被屏蔽：槽位={slots[0]}\n  屏蔽后={masked[0]!r}\n"
        f"模型会把参数 [3] 吃掉，游戏里显示成裸 {cmd}。"
    )
    assert f"{cmd}[3]" not in masked[0], f"明文里还残留：{masked[0]!r}"
    assert f"{cmd}[3]" in extract_placeholders(src), (
        "守卫也要认它（两侧必须同步，否则模型丢了也无从修补）"
    )


@pytest.mark.parametrize("cmd", STAT_CMDS)
def test_stat_command_loss_is_fatal(cmd: str) -> None:
    r"""★ 模型丢掉 `\mhp[3]` 时，必须判**致命**（而不是静默写回）。

    与"纯样式记号丢了不算坏"形成对照：属性命令是**内容类** ——
    少了它游戏里就没有数值可显示。没有这条断言，
    "保护"很容易退化成"屏蔽了但不检查"。
    """
    src = f"【HP】{cmd}[3]"
    broken = "【生命值】"          # 参数整块丢了（实测的坏产物形态）
    _restored, chk = ph.verify_restored(
        src, broken, [f"{cmd}[3]"], masked_source="【HP】⟦0⟧"
    )
    assert chk.fatal, f"{cmd}[3] 丢了却没判死 —— 判据被废掉了：{chk.describe()}"


def test_stat_command_survives_round_trip() -> None:
    r"""属性块屏蔽 → 还原必须一字不差（真实形态的一整行）。"""
    src = "【HP】\\mhp[3]   【攻撃力】\\atk[2]   【防御力】\\def[2] "
    masked, slots = ph.mask_batch([src])
    assert len(slots[0]) == 3, f"应屏蔽 3 个命令，实际 {slots[0]}"
    restored, chk = ph.verify_restored(src, masked[0], slots[0], masked_source=masked[0])
    assert not chk.fatal, chk.describe()
    assert restored == src, f"{restored!r} != {src!r}"


@pytest.mark.parametrize(
    "text",
    [
        r"C:\Users\name",          # Windows 路径，不能误切
        r"\n",                     # 换行转义（有专门规则处理）
        r"\d+",                    # 正则字面量，后面的 `[` 不是参数
        "ATK up",                  # 裸词，没有反斜杠
        r"\mhp",                   # 没有 [n] —— 不该被当占位符
        r"\mhpx[3]",               # 命令名不匹配，不该被切
    ],
)
def test_stat_rule_does_not_overmatch(text: str) -> None:
    r"""**边界**：新规则不能把普通文本误切。

    判据同上面那条：守卫和屏蔽器对"有没有占位符"必须一致。
    特别是 `\mhp`（**没有** `[n]`）不该被认 —— 我们的规则要求带参数。
    """
    masked, slots = ph.mask_batch([text])
    guard_sees = extract_placeholders(text)
    masker_sees = slots[0] if slots else []
    assert bool(masker_sees) == bool(guard_sees), (
        f"{text!r}: 守卫看到 {guard_sees}，屏蔽器看到 {masker_sees} —— 不一致\n"
        f"  屏蔽后 = {masked[0]!r}"
    )
    if text == r"\mhp":
        assert not masker_sees, (
            r"`\mhp`（无参数）被误当占位符了 —— 规则应要求 `[\d+]`"
        )


# ----------------------------------------------------------------------
# 纯样式记号：丢了**不算坏**（真实数据里 165 处 `\{`）
# ----------------------------------------------------------------------

STYLE_ONLY_FORMS = ["\\{", "\\}", "\\!", "\\.", "\\|", "\\>", "\\<", "\\^"]


@pytest.mark.parametrize("token", STYLE_ONLY_FORMS)
def test_style_only_mark_loss_is_not_fatal(token: str) -> None:
    r"""★ 纯样式记号丢了**不许判死**（反例：内容类必须照样判死）。

    ## 真实事故

    `Scenario.json` 里有 **165 处** `\{`（这一段放大显示），几乎都长这样：

        源：'\\{Ahahahaha!'        译：'啊哈哈哈哈！'
        源：'\\{Gwahahahaha! ...'   译：'哇哈哈哈！……'

    模型**正确地**丢掉了 `\{` —— 中文里没有"把这段字放大"的概念，
    留着反而会变成可翻译的明文。而原先把它算进 `missing` ⇒ 判死 ⇒
    **一条完全正确的译文被整条丢弃、对话框变空白**。

    实测：合并了多片的条目里，含记号的共 29 条，判死 8 条（27.6%）；
    剔除样式类之后降到 3 条（10.3%），剩下的 3 条是真丢了 `\\V[72]`。
    """
    src = f"{token}Ahahahaha!"
    translated = "啊哈哈哈哈！"  # 模型正确丢掉了样式记号
    restored, chk = ph.verify_restored(src, translated, [token], masked_source="⟦0⟧Ahahahaha!")
    assert not chk.fatal, (
        f"{token!r} 丢了就被判死 —— 这条正确的译文会被整条丢弃：{chk.describe()}"
    )
    assert token in chk.missing, "记一笔 missing 是可以的（便于观察），但不该致命"


@pytest.mark.parametrize("token", ["\\V[1]", "\\N[2]", "\\S[5]", "\\P[3]", "%s"])
def test_content_mark_loss_is_still_fatal(token: str) -> None:
    r"""★ **反例必须同批存在**：内容类记号丢了必须照样判死。

    没有这一条，"修复"很容易退化成"把判据废掉"。
    `\V[1]`（变量值）丢了游戏会显示错东西；`%s`（参数）丢了
    格式化会出错。

    ⚠️ `\C[3]` / `\I[96]` **不在这个列表里**（它们已改判为样式类）——
    理由见 `test_color_and_icon_loss_is_not_fatal`。
    """
    src = f"Weapon: {token}"
    translated = "武器："  # 记号丢了
    _restored, chk = ph.verify_restored(src, translated, [token], masked_source="Weapon: ⟦0⟧")
    assert chk.fatal, f"{token!r} 丢了却没判死 —— 判据被废掉了"
    assert token in chk.missing


@pytest.mark.parametrize("token", ["\\C[3]", "\\c[0]", "\\I[96]", "\\i[4]"])
def test_color_and_icon_loss_is_not_fatal(token: str) -> None:
    r"""★★ 颜色码与图标丢了**不该**判死（实测 2,239 条对话因此变空白）。

    ## 现场（工作区 `f4b03ca791a9`，9,777 条的游戏）

        失败 2,245 条，其中 **2,239 条**都是同一个原因：
            placeholder_broken: 丢失占位符：['\\c[0]', '\\c[2]', '⟦0⟧', '⟦1⟧', '⟦2⟧']
        而模型输出是**完整可用**的中文，只是没保留颜色码：
            源    '\c[2]【Passive】\c[0]…fatal damage. \nRequired LV:30.'
            模型  '【被动】当受到致命伤害时，有几率保持防御。'

    47% 的对话被整条丢弃 ⇒ `target = ''` ⇒ 玩家看到**空对话框**。

    ## 为什么改判是对的

    `\C[n]`（颜色）与 `\I[n]`（图标）只改变**外观**，文字一个字不少。
    `guards.py` 的 `_STYLE_ONLY_PLACEHOLDER_RE` **一直都**把它们算样式
    （那里写明"只是配色变了，文字一个字不少 ⇒ 不该判死"），
    但 `placeholders._is_style_only_mark` 判成内容类，
    而它**更早生效** ⇒ 那条宽容规则永远没机会执行。

    ⇒ 丢装饰远好于丢内容：没有颜色的正常中文 ≫ 空白对话框。

    ## 与它的镜像用例必须成对存在

    `test_content_mark_loss_is_still_fatal` 守住"别把判据废掉"。
    """
    src = f"Weapon: {token}"
    translated = "武器："  # 只丢了样式码，文字完整
    _restored, chk = ph.verify_restored(src, translated, [token], masked_source="Weapon: ⟦0⟧")
    assert not chk.fatal, (
        f"{token!r} 丢了就被判死 —— 这条可用的译文会被整条丢弃、对话框变空白："
        f"{chk.describe()}"
    )


def test_mask_form_of_droppable_slot_is_not_fatal() -> None:
    r"""★★ `missing` 里的**屏蔽记号**形态也必须按"能否丢"判。

    ## 这个 bug 很隐蔽（同一个槽位被判了两次、结论相反）

    `verify_restored` 第 4 层校验算的是
    `_is_droppable_mark(slots[i])` —— **只对内容类**报缺失。
    但它把结果写成 ``⟦i⟧`` 塞进 `check.missing`，
    接着 `fatal_missing` 又拿 ``⟦i⟧`` 去问 `_is_droppable_mark`：
    ``⟦1⟧`` 既不在样式表里、也不匹配 `\[cCiI]\[\d+\]`
    ⇒ 被判成**内容类** ⇒ 致命。

    ⇒ 同一个槽位：第 4 层说"可以丢"、`fatal_missing` 说"致命"，
      而**后者赢**。真实代价见 `test_color_and_icon_loss_is_not_fatal`。

    这里直接构造那种 `missing` 形态（``⟦1⟧``），断言它不再致命。
    `PlaceholderCheck` 现在记住 `slots`，能把 ``⟦i⟧`` 解回 `slots[i]` 再判。
    """
    from novaloc.translate.placeholders import PlaceholderCheck

    slots = ["\\c[2]", "\\c[0]"]
    # 形态 1：屏蔽记号指向**可丢**的槽位 ⇒ 必须不致命
    chk = PlaceholderCheck(missing=["⟦1⟧"], slots=slots)
    assert not chk.fatal_missing, (
        f"⟦1⟧ 对应的槽位是 {slots[1]!r}（颜色码，可丢），不该致命："
        f"{chk.describe()}"
    )
    assert not chk.fatal
    # ⚠️ 但要**留在 missing 里**（便于观察），别把它删掉
    assert "⟦1⟧" in chk.missing

    # 形态 2：屏蔽记号指向**内容类**槽位 ⇒ 必须致命
    content = PlaceholderCheck(missing=["⟦0⟧"], slots=["\\V[1]"])
    assert content.fatal_missing, "解出来是 \\V[1]（内容类）⇒ 必须致命"
    assert content.fatal

    # 形态 3：没有 slots 可解 ⇒ 保守判致命（宁可拒绝，不可误放）
    blind = PlaceholderCheck(missing=["⟦0⟧"], slots=[])
    assert blind.fatal_missing, "解不开时应当保守判死"

    # 形态 4：越界下标 ⇒ 同样保守判死
    oob = PlaceholderCheck(missing=["⟦9⟧"], slots=slots)
    assert oob.fatal_missing, "越界下标应当保守判死"


def test_mask_form_of_content_slot_is_still_fatal() -> None:
    r"""★ 镜像：``⟦i⟧`` 解出来是**内容类**时必须照旧判死。"""
    src = "HP: \\V[1]"
    slots = ["\\V[1]"]
    _restored, chk = ph.verify_restored(src, "生命值：", slots, masked_source="HP: ⟦0⟧")
    assert "⟦0⟧" in chk.missing
    assert chk.fatal_missing, "解出来是 \\V[1]（内容类）⇒ 必须致命"
    assert chk.fatal


def test_style_and_content_marks_are_both_masked() -> None:
    r"""★ 两侧必须一致：样式记号**照样要屏蔽**，只是丢了不算致命。

    屏蔽与判死是两回事：`\{` 仍然要被换成 `⟦n⟧`，否则它会留在明文里
    被模型当普通文字处理（实测模型会把它翻成"反斜杠加大括号"之类的怪东西）。
    """
    src = "\\{Hello \\V[1]}"
    masked, slots = ph.mask_batch([src])
    assert "\\{" not in masked[0], f"样式记号没被屏蔽：{masked[0]!r}"
    assert "\\V[1]" not in masked[0], f"内容记号没被屏蔽：{masked[0]!r}"
    assert len(slots[0]) == 2, f"应该屏蔽两个记号：{slots[0]}"


def test_style_mark_kept_is_also_fine() -> None:
    """模型若**保留**了样式记号，同样不该报错（多出才算错）。"""
    src = "\\{Hello"
    restored, chk = ph.verify_restored(src, "\\{你好", ["\\{"], masked_source="⟦0⟧Hello")
    assert not chk.fatal, chk.describe()
    assert restored == "\\{你好"


def test_extra_style_mark_is_still_fatal() -> None:
    r"""★ 边界：样式记号**多出来**仍然判坏。

    模型凭空加一个 `\{` 会让游戏里这段文字莫名其妙变大 ——
    "丢了不算坏"不等于"随便加都行"。
    """
    src = "Hello"
    _restored, chk = ph.verify_restored(src, "\\{你好", [], masked_source="Hello")
    assert chk.fatal, "凭空多出的样式记号没被判坏"
    assert "\\{" in chk.extra
