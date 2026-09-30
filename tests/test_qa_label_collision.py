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
