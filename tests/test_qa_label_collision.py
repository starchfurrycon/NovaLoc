"""质检的"反向碰撞"规则：**不同的短标签被译成了同一个词**。

## 这条规则要抓的真实现象

本机实测 `translategemma:4b` 对真实游戏 UI 字符串的翻译：

    HP  →  生命值   ✅
    MP  →  生命值   ❌ 应为"魔法值"

原因很朴素：单条 UI 缩写几乎不携带上下文，模型只能猜。而这类错误
**传统规则质检抓不到** —— 它不是漏译、不是占位符丢失、源文也各不相同，
"同一原文多种译法"那条也只看正向。但玩家一眼就能看出是错的，
写回游戏就是既成事实。

## 为什么只查短标签

长句撞词可能是正常的同义表述（"我明白了" / "原来如此"），用规则去压
会伤害翻译自由度。而 UI 短标签（无空格、长度 <= 4）是独立控件上的
固定文案，语义几乎必然互不相同 —— 撞成同词基本等于错译。

## 之前为什么不靠术语表解决

试过"内置游戏术语表"，实测是净亏：修好 `MP` 的同时，同一批 27 条样本
的漏译从 0 升到 2.67/遍，还出现 `Load Game` → "重新开始" 这类污染。
所以改成**事后检查**：不影响模型输出，只把问题**报出来**让人处理。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.core.workspace import Workspace  # noqa: E402
from novaloc.models import (  # noqa: E402
    EntryStatus,
    Project,
    TextKind,
    TextLocation,
    TextUnit,
    TranslationEntry,
)
from novaloc.pipeline.qa import run_qa  # noqa: E402


def _ws(tmp_path: Path) -> Workspace:
    """直接构造 Workspace，**不用** ``Workspace.create()``。

    `create()` 走 ``paths.workspaces_dir()``，会往用户真实的数据根目录里
    写东西 —— 测试污染真实环境比测试失败更糟。这里只手工建出
    `save_entries()` 需要的 ``translations/`` 目录。
    """
    ws = Workspace(Project(name="qa-collide", game_dir=str(tmp_path / "game")), tmp_path / "ws")
    (ws.root / "translations").mkdir(parents=True, exist_ok=True)
    return ws


def _ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def _save(ws: Workspace, pairs: list[tuple[str, str]]) -> None:
    units, entries = [], []
    for i, (src, tgt) in enumerate(pairs):
        uid = f"u{i}"
        units.append(TextUnit(
            uid=uid, source=src, kind=TextKind.UI_LABEL,
            location=TextLocation(file="ui.rpy", line=i + 1),
        ))
        entries.append(TranslationEntry(
            uid=uid, source=src, target=tgt, kind=TextKind.UI_LABEL,
            status=EntryStatus.TRANSLATED, provider="test", model="test",
        ))
    ws.save_units(units)
    ws.save_entries(entries)


def _consistency_issues(tmp_path: Path, pairs: list[tuple[str, str]]) -> list[dict]:
    ws = _ws(tmp_path)
    _save(ws, pairs)
    rep = run_qa(ws, _ctx())
    # `_issue()` 产出的键是 severity / stage / message / detail（**没有 kind**）。
    # 第一版我按 `kind` 过滤，于是规则明明生效、测试却报"没报出来" ——
    # 又一次"指标写错比没有指标更糟"。
    return [i for i in rep["issues"] if i.get("stage") == "consistency"]


def test_short_labels_sharing_one_translation_is_flagged(tmp_path: Path) -> None:
    """HP/MP 都译成"生命值" —— 必须报出来，且是 ERROR。

    这是本文件的**核心用例**，对应实测发现的真实错译。
    """
    issues = _consistency_issues(tmp_path, [
        ("HP", "生命值"),
        ("MP", "生命值"),  # 错译：应为"魔法值"
        ("Attack", "攻击"),
    ])
    assert issues, "不同的短标签共用一个译文，质检却没报"
    assert any(i.get("severity") == "error" for i in issues), (
        f"这必须是 ERROR（用户可见缺陷，不存在'也可以'的解释空间）：{issues}"
    )
    msg = " ".join(i.get("message", "") for i in issues)
    assert "MP" in msg and "HP" in msg, f"报错信息里应能看出是哪两条撞了：{msg}"


def test_distinct_short_labels_are_not_flagged(tmp_path: Path) -> None:
    """正常的短标签各译各的，不能误报 —— 否则规则会被无视。"""
    issues = _consistency_issues(tmp_path, [
        ("HP", "生命值"),
        ("MP", "魔法值"),
        ("EXP", "经验值"),
        ("Gold", "金币"),
    ])
    assert not issues, f"正常翻译被误报：{issues}"


def test_long_sentences_may_share_a_translation(tmp_path: Path) -> None:
    """**长句撞词不报**：那可能是合理的同义表述。

    "I see." 和 "I understand." 都可以是"我明白了" ——
    用规则去压这种会伤害翻译自由度。这条守住"只查短标签"的边界。
    """
    issues = _consistency_issues(tmp_path, [
        ("I see.", "我明白了"),
        ("I understand.", "我明白了"),
        ("Got it.", "我明白了"),
    ])
    assert not issues, f"长句撞词不该报（会压制合理翻译）：{issues}"


def test_identical_source_does_not_count_as_collision(tmp_path: Path) -> None:
    """同一条原文出现两次、译法相同 → 不是碰撞。

    碰撞的定义是"**不同**源文 → 同一译文"。如果实现里忘了去重，
    重复出现的普通标签会把规则刷屏。
    """
    issues = _consistency_issues(tmp_path, [
        ("HP", "生命值"),
        ("HP", "生命值"),
        ("HP", "生命值"),
    ])
    assert not issues, f"同一原文重复出现不该算碰撞：{issues}"


def test_forward_inconsistency_still_reported(tmp_path: Path) -> None:
    """正向规则（同源多译）不能被新规则挤掉。"""
    issues = _consistency_issues(tmp_path, [
        ("Attack", "攻击"),
        ("Attack", "进攻"),
    ])
    assert issues, "同一原文两种译法应该照旧报出来"
    assert any(i.get("severity") == "warn" for i in issues)


# ---------------------------------------------------------------------------
# 收紧判据：只查 **ASCII 缩写**，不查语气词 / 短句 / 简繁变体
# ---------------------------------------------------------------------------
#
# ## 事故：这条判据在真实数据上**误报 59 组**，而且是 ERROR 级
#
# 在一份 29,792 条的真实游戏上跑质检，得到 786 个问题，其中
# **唯一的一个 error** 就是这条规则，命中 59 组：
#
#     '操…' / '等等…'                    → '喂…'
#     'Eh?/为什么？/什么事？/什么？/啊？'   → '怎么了？'
#     '*脸红*' / '*臉紅*'                 → '脸红'
#     '你是誰？' / '你是谁？'               → '你是谁？'
#     '再見！' / '再见！'                   → '再见！'
#
# **没有一组是真问题。** 两类天然误报：
#
# 1. **语气词一对多**：`Eh?` `Hm?` `啊？` 译成"怎么了？"完全正确，
#    中文里"嗯/唔/呃"本来都可对应 `Hm`。
# 2. **简繁/全半角变体**：`你是誰？` 与 `你是谁？` 是**同一条目**的
#    两种写法，本来就该译成同一个词 —— 这不是"不同标签撞词"。
#
# ## 为什么必须收紧，而不是"接受 59 组误报"
#
# ERROR 级判据会让质检整体 `ok=False`（实测就是这个 error 卡住了
# `apply`）。而**一个永远在误报的判据只会教会人忽略质检** ——
# 那比没有判据更糟，因为它同时消耗了"报告可信度"和"人的注意力"。
#
# ## 收紧的边界：形状，不是数字
#
# 缩写的形状是明确的：**纯 ASCII 字母数字**（可带 `_ + - . / %`），
# 不含问号/波浪号/星号/汉字。所以判据从"长度 <= 4"改成
# "长度 <= 4 **且形如缩写**"。
#
# **代价是刻意接受的**：`Hm` 与 `MP` 撞词这种"一个是缩写、一个是
# 语气词"的情况不再报。换来的是这个判据在这一份真实数据上从
# 59 组误报降到 0 组，同时仍然抓住它本来要抓的 `HP`/`MP`。

#: 真实数据上被误报的 59 组里挑出来的代表（**全都不是错译**）
_FALSE_POSITIVES_FROM_REAL_DATA: list[tuple[str, str]] = [
    # 语气词一对多
    ("Eh?", "怎么了？"), ("Hm?", "怎么了？"), ("啊？", "怎么了？"),
    ("嗯？", "怎么了？"), ("什么？", "怎么了？"), ("¿Eh?", "喂？"),
    ("操…", "喂…"), ("等等…", "喂…"), ("呃！", "哎呀！"),
    ("天哪！", "哎呀！"), ("呃！", "哎！"), ("誒！", "哎！"),
    # 简繁 / 全半角变体（同一条目的两种写法）
    ("你是誰？", "你是谁？"), ("你是谁？", "你是谁？"),
    ("再見！", "再见！"), ("再见！", "再见！"),
    ("*脸红*", "脸红"), ("*臉紅*", "脸红"),
    ("該死…", "该死的…"), ("该死…", "该死的…"),
    # 中文原文的标点变体
    ("但是…", "但是..."), ("不过……", "不过..."),
]


def test_real_data_false_positives_are_not_flagged(tmp_path: Path) -> None:
    """真实数据上误报过的 22 组，现在一组都不能报。

    这条是这次收紧的**回归钉子** —— 判据放宽（或有人"顺手"把
    `_ASCII_LABEL_RE` 那段去掉）就会立刻变红。
    """
    issues = _consistency_issues(tmp_path, _FALSE_POSITIVES_FROM_REAL_DATA)
    errors = [i for i in issues if i.get("severity") == "error"]
    assert not errors, (
        f"这些是语气词/简繁变体，不是错译，不该报 ERROR："
        f"{[i.get('message') for i in errors]}"
    )


def test_ascii_abbreviations_are_still_flagged(tmp_path: Path) -> None:
    """收紧之后，**真正的 UI 缩写碰撞**仍必须报出来。

    这是"放宽判据"必须配的那一半 —— 只测"不报了"会掩盖判据被彻底关掉。
    """
    issues = _consistency_issues(tmp_path, [
        ("HP", "生命值"),
        ("MP", "生命值"),          # 缩写撞词 → 必须报
        ("ATK", "攻击力"),
        ("DEF", "攻击力"),         # 缩写撞词 → 必须报
        ("EXP", "经验值"),
    ])
    errors = [i for i in issues if i.get("severity") == "error"]
    assert errors, "真缩写碰撞被放宽掉了，判据等于失效"
    msg = " ".join(i.get("message", "") for i in errors)
    assert "MP" in msg and "HP" in msg, f"应报出 HP/MP：{msg}"
    assert "DEF" in msg and "ATK" in msg, f"应报出 ATK/DEF：{msg}"


def test_abbreviation_with_punctuation_counts(tmp_path: Path) -> None:
    """带下标点/百分号的缩写仍算缩写（`HP%`、`ATK+`、`E.G.`）。

    形状判据不能窄到把真实标签漏掉 —— 这些在真实游戏里很常见。
    """
    issues = _consistency_issues(tmp_path, [
        ("HP%", "生命值%"),
        ("MP%", "生命值%"),
    ])
    assert any(i.get("severity") == "error" for i in issues), (
        "带 % 的缩写应该照旧算缩写"
    )


def test_simplified_traditional_variant_is_not_a_collision(tmp_path: Path) -> None:
    """简繁变体是**同一条目的两种写法**，不是"不同标签"。

    这条单独钉出来，因为它在真实数据里出现了 20+ 次，
    是 59 组误报里占比最大的一类。
    """
    issues = _consistency_issues(tmp_path, [
        ("训练？", "训练？"),
        ("訓練？", "训练？"),
        ("骨頭…", "骨头…"),
        ("骨头……", "骨头…"),
    ])
    assert not [i for i in issues if i.get("severity") == "error"], (
        "简繁变体不该报 ERROR"
    )

