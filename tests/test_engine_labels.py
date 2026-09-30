"""引擎内建的 UI 短标签必须**确定性**译对，不能交给模型自由发挥。

## 真实事故：游戏的三个核心资源被译成了同一个词

真实 RPG Maker MV 游戏 `System.json` 的 `terms.basic` 是：

    ["Level","Lv","HP","HP","MP","MP","TP","TP"]

模型（`translategemma:4b`）把 `HP`、`MP`、`TP` **全译成了"生命值"**。
玩家进游戏看到三个字段都叫"生命值" —— 这是**用户可见的硬缺陷**。

同类事故还有职业名：`僧侶` 和 `魔術師` 都被译成"法师"，
两个不同职业变成同一个（那个游戏里职业影响技能池）。

## 为什么模型做不好

这些是**单条、无上下文**的缩写标签（`System.json` 里就是独立的
`"HP"`、`"MP"`、`"TP"` 三个字符串）。模型只能猜，而三种猜法都
"像那么回事" —— 既不是漏译、也不是占位符丢失、源文也各不相同，
所以**任何传统规则质检都挑不出毛病**，写回游戏就是既成事实。
（`qa.py` 里那条"反向碰撞"检查就是为抓这个加的，它确实抓到了。）

## 为什么不用"把内置术语表整个注入提示词"

那是**已经实测过并否决**的方案（结论记在 `stage_translate` 的注释里）：
整体变差 —— 同一批 27 条样本跑 3 遍，漏译从 0 升到 2.67/遍，
还出现 `Load Game` → "重新开始"（应为"读档"）这类污染，
因为提示词把 `Restart`/`Save`/`Load` 一起灌进去，4B 模型会串。

所以问题**不是内置表错了**（它的 `HP→生命值`/`MP→魔法值`/
`TP→技巧值` 全是对的），而是**注入的粒度太粗**。

## 修法：只对"整条就是引擎标签"的条目做确定性覆盖

`System.json` 里的这些值**本身就是整条文本**（不是嵌在句子里）。
所以只在 `source` 归一化后**精确等于**某个已知引擎标签时覆盖译文；
**绝不做子串替换** —— 否则 `Restores 50 HP.` 会被搞成
"恢复50 生命值。"这种中英夹杂。

这个判据同时解决了"注入太粗"和"覆盖太危险"两个问题：
标签是**有限且固定**的引擎标识符，不是用户领域知识，
不该由模型猜。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.translate.glossary import (  # noqa: E402
    EngineLabel,
    engine_label_target,
    engine_labels,
)

#: RPG Maker MV/MZ `System.json` → `terms.basic` 的真实内容与标准译法。
#: 顺序就是这个数组的顺序，索引 2..7 分别是 HP/MP/TP 的大写与小写形式。
RM_TERMS_BASIC = ["Level", "Lv", "HP", "HP", "MP", "MP", "TP", "TP"]

EXPECTED = {
    "hp": "生命值",
    "mp": "魔法值",
    "tp": "技巧值",
}


# ----------------------------------------------------------------------
# 核心：三个资源必须互不相同
# ----------------------------------------------------------------------

def test_the_three_resources_get_distinct_targets() -> None:
    """**这是事故的核心断言**：HP/MP/TP 必须译成三个**不同**的词。

    修好之前模型把三个都译成"生命值"，玩家看到三个同名字段。
    """
    got = {k: engine_label_target(k) for k in ("HP", "MP", "TP")}
    assert all(got.values()), f"有引擎标签没有译法：{got}"
    assert len(set(got.values())) == 3, (
        f"HP/MP/TP 的译法有重复：{got}\n"
        "三者是游戏里三个**不同**的资源，撞成同一个词就是用户可见的硬缺陷。"
    )
    for k, v in EXPECTED.items():
        assert got[k.upper()] == v, f"{k} 应为 {v!r}，实际 {got[k.upper()]!r}"


def test_real_terms_basic_array_translates_distinctly() -> None:
    """拿真实的 `terms.basic` 数组走一遍：**资源项**译完不能再有碰撞。

    这条模拟了 `qa.py` 的"反向碰撞"检查（短标签撞词判 ERROR），
    所以它同时是"修好了"和"质检会通过"的证据。

    ⚠️ 只查**索引 2..7**（HP/MP/TP 三个资源的两种写法）。
    索引 0/1 是 `Level` 和 `Lv` —— 它们是**同一个东西**的缩写与全称，
    都译"等级"是正确的，拿它们判碰撞是**假阳性**。
    事故本身也正好出在 2..7。
    """
    resources = RM_TERMS_BASIC[2:]          # ["HP","HP","MP","MP","TP","TP"]
    assert len(resources) == 6, f"terms.basic 的布局变了：{RM_TERMS_BASIC}"

    by_target: dict[str, set[str]] = {}
    for raw in resources:
        t = engine_label_target(raw)
        assert t, f"资源项 {raw!r} 没有确定性译法"
        by_target.setdefault(t, set()).add(raw)

    collisions = {t: s for t, s in by_target.items() if len(s) > 1}
    assert not collisions, (
        f"仍然存在资源短标签撞词：{collisions}\n"
        "玩家会在界面上看到多个同名字段（事故原状：三个都叫「生命值」）。"
    )
    # 三个资源必须是三个不同的词
    assert len(set(by_target)) == 3, f"资源译法不是三个不同的词：{by_target}"


def test_level_and_lv_are_the_same_thing_not_a_collision() -> None:
    """**边界**：`Level` 与 `Lv` 译成同一个词是**对的**，不是碰撞。

    它们是同一个概念的缩写与全称（`terms.basic` 索引 0/1）。
    如果哪天有人为了"消除碰撞"把它们改成两个词，这条会失败 ——
    那会让中文界面出现"等级"和某个生造的缩写，反而更糟。
    """
    assert engine_label_target("Level") == engine_label_target("Lv"), (
        "Level 与 Lv 是同一个概念，译法应当一致"
    )


def test_japanese_character_classes_do_not_collide() -> None:
    """`僧侶` 与 `魔術師` 是**两个不同职业**，不能都译成"法师"。

    那个游戏里职业决定技能池，撞成同词会让玩家分不清。
    """
    a = engine_label_target("僧侶")
    b = engine_label_target("魔術師")
    assert a and b, f"日文职业名没有确定性译法：僧侶={a!r} 魔術師={b!r}"
    assert a != b, f"僧侶 和 魔術師 撞成同一个词 {a!r} —— 两个职业不能同名"


# ----------------------------------------------------------------------
# 边界：只覆盖"整条就是标签"的情况，绝不做子串替换
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "sentence",
    [
        "Restores 50 HP.",
        "Restores 50 HP",
        "Costs 10 MP to cast.",
        "Your TP is full.",
        "HP: 100",
        "HPx",
        "MP3 player",
        "Deals damage equal to your HP.",
    ],
)
def test_sentences_are_never_rewritten(sentence: str) -> None:
    """**关键边界**：标签出现在句子里时必须返回 `None`（不覆盖）。

    绝不做子串替换 —— 否则 `Restores 50 HP.` 会变成
    "恢复50 生命值。"这种中英夹杂。模型译句子本来就译得不错，
    我们只接管"整条就是一个引擎缩写"这种无上下文的情况。
    """
    assert engine_label_target(sentence) is None, (
        f"{sentence!r} 不该被当成引擎标签覆盖 —— 它不是整条标签，"
        "覆盖会破坏正常的句子翻译"
    )


# ----------------------------------------------------------------------
# 归一化：大小写、空白、全角标点
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "variant",
    ["HP", "hp", "Hp", " HP ", "ＨＰ", "HP.", "HP:"],
)
def test_label_normalization_variants(variant: str) -> None:
    """真实数据里标签写法不统一，这些变体都要认出来。

    `ＨＰ` 是全角写法（日文数据里常见），`HP.` / `HP:` 是带标点的写法。
    """
    assert engine_label_target(variant) == "生命值", (
        f"{variant!r} 没被识别成 HP"
    )


@pytest.mark.parametrize("bad", ["", "   ", "Hello", None])
def test_non_labels_return_none(bad) -> None:
    """不是引擎标签的一律返回 `None`。"""
    assert engine_label_target(bad) is None, f"{bad!r} 不该被当成标签"


def test_lv_is_covered_but_stays_short() -> None:
    """`Lv` 是 `terms.basic` 的真实成员，必须覆盖，且译法要短。"""
    t = engine_label_target("Lv")
    assert t, "`Lv` 在 terms.basic 里出现过，必须有确定性译法"
    assert len(t) <= 4, f"`Lv` 是 UI 上的窄标签，译法要短，实际 {t!r}"


# ----------------------------------------------------------------------
# 覆盖表本身要干净
# ----------------------------------------------------------------------

def test_labels_are_unique_and_nonempty() -> None:
    labels = engine_labels()
    assert labels, "引擎标签表为空"
    keys = [lbl.key for lbl in labels]
    assert len(keys) == len(set(keys)), f"标签 key 有重复：{keys}"
    for lbl in labels:
        assert isinstance(lbl, EngineLabel)
        assert lbl.key == lbl.key.strip().lower(), f"{lbl.key!r} 没归一化"
        assert lbl.target.strip(), f"{lbl.key!r} 的译法为空"
        assert lbl.note, f"{lbl.key!r} 缺 note（要说明为什么这个译法是准的）"


def test_no_two_labels_share_a_target_with_different_meaning() -> None:
    """不同标签不能共用同一个译法（同一事物的不同写法除外）。"""
    labels = engine_labels()
    by_target: dict[str, set[str]] = {}
    for lbl in labels:
        by_target.setdefault(lbl.target, set()).add(lbl.key)
    bad = {t: s for t, s in by_target.items() if len(s) > 1}
    # 这些是**同一个事物**的多种写法，译法相同是正确的，不算碰撞
    allowed = {
        frozenset({"xp", "exp"}),        # 经验值的两种写法
        frozenset({"g", "gold"}),        # 货币单位的两种写法
        frozenset({"lv", "level"}),      # 等级的缩写与全称
        # `MHP`/`MMP` 里的 M 是 Maximum，和 `HP`/`MP` 是**同一个资源**
        # （当前值 vs 最大值），不是两种资源。属性栏里这两者通常紧挨着
        # 显示，译成不同的词反而会让人以为是两套血条。
        # 这条是**刻意**的，不是碰撞 —— 所以列进白名单而不是改译法。
        frozenset({"hp", "mhp"}),
        frozenset({"mp", "mmp"}),
    }
    bad = {t: s for t, s in bad.items() if frozenset(s) not in allowed}
    assert not bad, f"不同标签撞同一个译法：{bad}"


def test_stat_abbreviations_are_covered_deterministically() -> None:
    """属性缩写必须走**确定性查表**，不能交给模型。

    真实贴图实测（173 个文字块，48% 是纯 ASCII 属性缩写）：
    同一个模型、同一批缩写，8 个里只有 2 个对 ——

    * ``'LUK'`` → ``'卢克'``（当**人名**音译了）；
    * ``'MHP'`` → ``'生命值'``（对）、``'ATK'`` → ``'攻击力'``（对）；
    * ``'AGI'``/``'DEF'``/``'MAT'``/``'MDF'``/``'MMP'`` **原样返回英文**，
      却被标成"已翻译"。
    """
    for key in ("mhp", "mmp", "atk", "def", "mat", "mdf", "agi", "luk"):
        t = engine_label_target(key)
        assert t, f"属性缩写 {key!r} 没有确定性译法"


def test_luk_is_luck_not_a_person_name() -> None:
    """**真实误译的回归**：``'LUK'`` 被音译成 ``'卢克'``。

    `LUK = Luck = 幸运`，和人名毫无关系。贴图上这个字错得很显眼
    （属性栏里写着一个人名），而它**通过了所有自动检查** ——
    非空、是中文、长度合理。
    """
    t = engine_label_target("LUK")
    assert t == "幸运", f"`LUK` 必须译成「幸运」，实际 {t!r}"
    assert t != "卢克"


def test_attack_and_magic_attack_are_distinguishable() -> None:
    """`ATK`/`MAT`、`DEF`/`MDF` 必须**成对可区分**。

    译成同一个词会让属性栏出现两个相同标签（和 `HP`/`MP`/`TP`
    全被译成"生命值"是同一类事故）。
    """
    atk, mat = engine_label_target("ATK"), engine_label_target("MAT")
    deff, mdf = engine_label_target("DEF"), engine_label_target("MDF")
    assert atk and mat and atk != mat, f"ATK={atk!r} 与 MAT={mat!r} 撞了"
    assert deff and mdf and deff != mdf, f"DEF={deff!r} 与 MDF={mdf!r} 撞了"


def test_stat_labels_stay_short_for_textures() -> None:
    """贴图上空间紧张，属性缩写译法要短（≤3 个汉字）。"""
    for key in ("atk", "def", "mat", "mdf", "agi", "luk", "mhp", "mmp"):
        t = engine_label_target(key)
        assert len(t) <= 3, f"{key!r} 的译法 {t!r} 太长，贴图会溢出"


def test_engine_label_target_is_pure_lookup_not_substitution() -> None:
    """**这个函数只能做查表**，不能有任何替换副作用。

    用"重复调用结果相同"和"绝不返回被改写的长句"两条来守：
    如果哪天有人把它改成 `re.sub`，长句就会被改掉，这条会失败。
    """
    sentence = "Restores 50 HP and 10 MP."
    assert engine_label_target(sentence) is None
    assert engine_label_target(sentence) is None     # 幂等
    # 真正的标签查表结果稳定
    assert engine_label_target("HP") == engine_label_target("HP") == engine_label_target(" hp ")
