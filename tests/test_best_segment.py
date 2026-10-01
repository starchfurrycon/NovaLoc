"""单条请求收到"多段 JSON"时的选段逻辑。

## 这一组测试的数据**全部来自真实游戏**（ElfLifia, `55fdadab74a4a8`）

用产品真实路径（`_call_single` + 真实提示词）对 10 条多行条目各请求
一次，**10/10** 都返回 `{"0": …, "1": …}` 形态。下面每条 `SEGMENTS`
都是当时**原样抄下来**的模型输出。

用真实样本而不是编造样本，是因为本项目已经栽过四次"合成样本通过、
真实数据是错的"（见 `docs/ACCEPTANCE.md` §八之二）。选段规则尤其
不能靠想象：多段到底是"互补内容"还是"同一句的多个措辞"，只有真实
数据能说明。

## 三种形态（人工逐条判定过）

| 形态 | 样本 | 正确做法 |
|---|---|---|
| 各段是**同一内容的替代译文** | `Skills.json:/7` | 只取**一段** |
| 各段**互补**（但段 0 已含全部信息） | `Items.json:/6` | 只取**一段** |
| 各段**完全相同** | `Items.json:/22` | 只取**一段** |

三种形态的答案都是"取一段"，而**拼接**会在前两种上造成复读。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.ollama_provider import _best_segment  # noqa: E402

#: (uid, 源文, 模型原样返回的段落)  —— 全部真实抓取
SEGMENTS: list[tuple[str, str, list[str]]] = [
    (
        "Items.json:/26/description",
        "Gain 1 「Limit Break」. Add 2 『⟦0⟧』 to the Draw Pile.⟦1⟧「Limit Break」: All cards in hand cost 0, but extra draws are disabled.",
        [
            "获得 1 张『极限突破』牌。将 2 张牌加入牌堆。",
            "『极限突破』：手牌中的所有牌消耗 0 点资源，但无法额外抽牌。",
        ],
    ),
    (
        "Skills.json:/7/description",
        "Deals damage equal to 4x your 「Defense」.⟦0⟧All 「Defense」 is then removed.",
        [
            "造成 4 倍「防御」的伤害。随后所有「防御」效果消失。",
            "造成 4 倍你的「防御」值造成的伤害。\\n然后移除所有「防御」效果。",
        ],
    ),
    (
        "Items.json:/2/description",
        "Gain 5 「Regen」.⟦0⟧「Regen」: Recover HP equal to Regen value at Turn End.",
        [
            "恢复 5 点生命值。",
            "生命回复：每回合结束时回复等于生命回复值的生命值。",
        ],
    ),
    (
        "Items.json:/6/description",
        "Randomly acquire 1 card from all 「Attack Cards」. This card can be used for 0 Cost.⟦0⟧The card is removed from the Deck at Turn End.",
        [
            "随机获得一张「攻击卡」。该卡片消耗 0 点资源。",
            "这张卡在回合结束时会被移除。",
            "该卡属于『攻击卡』类别。",
        ],
    ),
    (
        "Items.json:/22/description",
        "Gain 1 「Clone」.⟦0⟧「Clone」: Automatically use 1 card from the top of the Draw Pile without cost at Turn Start.",
        [
            "获得 1 个『复制』。复制：在回合开始时，自动从牌堆顶使用 1 张牌，无需消耗。",
            "获得 1 个『复制』。复制：在回合开始时，自动从牌堆顶使用 1 张牌，无需消耗。",
        ],
    ),
    (
        "Skills.json:/159/description",
        "Scurry: Gain 「Halve Damage」.⟦0⟧Activates before player turn.",
        [
            "闪避：获得『半 피해』效果。在玩家回合开始前激活。",
            "闪避：获得「减少伤害」效果。在玩家回合开始前生效。",
        ],
    ),
    (
        "Items.json:/19/description",
        "Effect activates just by holding. When HP reaches 0, automatically revive with 30 HP.⟦0⟧However, all states are removed.",
        [
            "按住即可激活效果。当生命值降至 0 时，自动恢复 30 点生命值。\\n不过，所有状态都会被移除。",
            "只需按住就能启动效果。生命值为 0 时，会自动回复 30 点生命值。但此时所有状态都会消失。",
        ],
    ),
    (
        "Skills.json:/3/description",
        "Deals ⟦0⟧ damage 3 times to random targets.⟦1⟧Draw 1 card from the deck.",
        ["造成 3 次随机目标 3 点伤害。", "抽取一张牌。"],
    ),
    (
        "Armors.json:/23/description",
        "Holy Seal: Add 「Evil Bane」 card to hand at Battle Start.⟦0⟧Evil Bane: Deal 10 damage to all enemies at Turn Start.",
        [
            "圣印：在战斗开始时将「邪恶之刃」牌加入手牌。",
            "邪恶之刃：每回合开始时对所有敌人造成 10 点伤害。",
        ],
    ),
]


@pytest.mark.parametrize(("uid", "src", "segs"), SEGMENTS, ids=[s[0] for s in SEGMENTS])
def test_returns_exactly_one_segment(uid: str, src: str, segs: list[str]) -> None:
    """★ 结果必须是**某一段原文**，不能是几段拼起来的。

    判据刻意用"逐字相等"而不是"长度合理"：拼接产生的文本
    每一段单独看都合法，只有和原段逐个比才能抓住。
    """
    got = _best_segment(segs, ["⟦0⟧"])
    assert got is not None
    assert got in segs, f"选出来的是拼接产物，不是任何单独一段：{got!r}"


@pytest.mark.parametrize(("uid", "src", "segs"), SEGMENTS, ids=[s[0] for s in SEGMENTS])
def test_no_duplication(uid: str, src: str, segs: list[str]) -> None:
    """★ 不能复读 —— 同一句出现两遍比丢内容更糟。

    判据：12 字以上的子串不能在结果里出现两次。
    （取单段天然满足；这条测的是"以后有人改成拼接"时会被拦住。）
    """
    got = _best_segment(segs, ["⟦0⟧"]) or ""
    norm = "".join(got.split())
    for i in range(0, max(0, len(norm) - 12), 2):
        sub = norm[i : i + 12]
        assert norm.count(sub) <= 1, f"{uid} 出现复读：{sub!r} in {got!r}"


def test_tie_prefers_first_segment() -> None:
    """★ 并列时取**段 0**（模型对整条文本的主答案）。

    实测 10 条里 8 条各段都没丢占位符（占位符在源文里、模型直接吃掉了），
    此时若按"最长"选，`Items.json:/2` 会把**补充说明段**选成译文，
    读起来像"只有后半句"。以"编号最小"兜底才对。
    """
    segs = ["恢复 5 点生命值。", "生命回复：每回合结束时回复等于生命回复值的生命值。"]
    assert _best_segment(segs, ["⟦0⟧"]) == segs[0]


def test_placeholder_completeness_wins_over_length() -> None:
    """占位符丢得少的段优先 —— 即使它更短。

    占位符丢了译文就没法正确回写，这是最硬的约束，
    优先于"最长"。
    """
    with_mark = "短⟦0⟧"
    without = "这一句很长很长很长但是把占位符弄丢了"
    assert _best_segment([without, with_mark], ["⟦0⟧"]) == with_mark


def test_prefers_segment_containing_newline() -> None:
    """含换行的段优先（换行是作者定的版面）。

    在占位符完整度并列时，带换行的段能直接对上引擎槽位。
    """
    a = "这是一整行很长的译文没有换行符所以槽位对不上版面会坏"
    b = "第一行\\n第二行"
    assert _best_segment([a, b], ["⟦0⟧"]) == b


def test_single_segment_passthrough() -> None:
    assert _best_segment(["就一条"], ["⟦0⟧"]) == "就一条"


def test_empty_segments_returns_none() -> None:
    assert _best_segment([], ["⟦0⟧"]) is None
    assert _best_segment(["", "   "], ["⟦0⟧"]) is None


def test_no_slots_still_works() -> None:
    """没有占位符表时也要能用。

    ▲ 这里断言"取**第一段**"而不是"取最长的" —— 这是刻意的：
    并列时取段 0 是规则（见 `_best_segment` 的说明），因为段 0 是
    模型对整条文本的主答案，后续段是它自己拆出来的补充/替代。
    """
    assert _best_segment(["甲", "乙乙乙"]) == "甲"
    assert _best_segment(["甲甲甲", "乙"]) == "甲甲甲"


def test_no_slots_still_prefers_newline_segment() -> None:
    """没有占位符表时，含换行的段仍然优先于段 0。"""
    assert _best_segment(["一整行没有换行", "第一行\\n第二行"]) == "第一行\\n第二行"


def test_identical_segments_collapse_to_one() -> None:
    s = "获得 1 个『复制』。复制：在回合开始时，自动从牌堆顶使用 1 张牌，无需消耗。"
    assert _best_segment([s, s], ["⟦0⟧"]) == s
