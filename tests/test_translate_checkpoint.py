"""翻译阶段必须**增量落盘**：跑到一半崩掉，已完成的成果不能丢。

## 为什么

一次真实游戏的翻译跑了 **27 分 6 秒**后崩掉（孤立代理项导致的
`UnicodeEncodeError`，见 `test_surrogate_sanitize.py`），而当时的实现
只在**整个阶段结束时**才 `save_entries()` 一次 —— 27 分钟的结果一条没存。
重跑要再花 27 分钟。

修法：`_translate_units(..., on_progress=...)` 每批之后回调一次，
`stage_translate` 按时间节流落盘。

这个测试用假 provider 模拟"第 3 批抛异常"，然后断言
**前两批的译文已经落盘**。断言打在产物（`entries.jsonl`）上，
不是打在状态上 —— 这正是真实游戏验证教的那条。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.core.config import Config  # noqa: E402
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
from novaloc.pipeline.stages import Pipeline  # noqa: E402


def _ctx() -> Context:
    from novaloc.core.events import EventBus

    return Context(config=Config(), events=EventBus())


def _ws(tmp_path: Path) -> Workspace:
    """造一个只够跑翻译阶段的工作区（不碰真实数据根）。"""
    ws = Workspace(Project(name="ckpt", game_dir=str(tmp_path / "game")), tmp_path / "ws")
    for d in ("extracted", "translations"):
        (ws.root / d).mkdir(parents=True, exist_ok=True)
    return ws


def _units(n: int) -> list[TextUnit]:
    return [
        TextUnit(
            uid=f"u{i}",
            source=f"Skill number {i}",
            kind=TextKind.ITEM_NAME,
            location=TextLocation(file="Skills.json", pointer=f"/{i}/name"),
        )
        for i in range(n)
    ]


def test_partial_progress_is_persisted_before_a_later_crash(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    # 65 条同类型 → chunk_size 40 → 2 批。第 2 批炸，第 1 批的 40 条必须先落盘。
    # （别用多种 kind：`_translate_units` 按 kind 分批，每种 kind 只剩一批时
    #   "第 3 批失败" 这种安排根本不会发生。）
    units = _units(65)
    ws.save_units(units)

    calls = {"n": 0}
    seen: list[list[str]] = []

    def fake_translate(items, target_lang):  # type: ignore[no-untyped-def]
        """第 2 批抛异常（模拟真实崩溃），第 1 批正常返回。"""
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟：第 2 批炸了")
        seen.append([it.unit.uid for it in items])
        return [
            TranslationEntry(
                uid=it.unit.uid,
                source=it.unit.source,
                target=f"技能{i}",
                status=EntryStatus.TRANSLATED,
                kind=it.unit.kind,
            )
            for i, it in enumerate(items)
        ]

    pipe = Pipeline(ws, _ctx(), translate_fn=fake_translate)

    # 阶段本身不该抛：批失败只标记该批为失败（这是既有的正确行为）
    res = pipe.stage_translate()
    assert res.ok, f"阶段不该整体失败：{res.error}"
    assert calls["n"] == 2, f"应恰好调用 2 批，实际 {calls['n']}"

    # **关键断言**：第 1 批那 40 条必须已经落盘。
    # 注意"失败那一批没有落盘"是**已知且正确**的：`_checkpoint` 只在每批
    # 之后被调用，而第 2 批是在回调之前抛的异常 —— 所以它压根不在 `out` 里。
    # 真正要防的是"27 分钟成果全丢"，也就是第 1 批必须留下。
    saved = {e.uid: e for e in ws.load_entries()}
    ok = [u for u in saved.values() if u.status == EntryStatus.TRANSLATED and u.target]
    assert len(ok) == 40, (
        f"崩溃前已完成的那一批（40 条）没有全部落盘 —— 增量落盘没生效。\n"
        f"落盘且成功 = {len(ok)}，落盘总数 = {len(saved)}"
    )


def test_successful_run_persists_everything(tmp_path: Path) -> None:
    """正常跑完时，全部条目都要落盘（增量不能替代最终那次写）。"""
    ws = _ws(tmp_path)
    units = _units(7)
    ws.save_units(units)

    def fake_translate(items, target_lang):  # type: ignore[no-untyped-def]
        return [
            TranslationEntry(
                uid=it.unit.uid,
                source=it.unit.source,
                target="技能",
                status=EntryStatus.TRANSLATED,
                kind=it.unit.kind,
            )
            for it in items
        ]

    pipe = Pipeline(ws, _ctx(), translate_fn=fake_translate)
    res = pipe.stage_translate()
    assert res.ok, res.error

    saved = ws.load_entries()
    assert len(saved) == len(units), f"应落盘 {len(units)} 条，实际 {len(saved)}"
    assert all(e.target for e in saved)


def test_existing_hand_edits_are_not_clobbered_by_checkpoints(tmp_path: Path) -> None:
    """增量落盘不能覆盖用户手工改过的译文。

    `stage_translate` 用 `{**existing, **new}` 合并，增量落盘也必须用
    同一个方向合并，否则"每 15 秒写一次"会把用户的修改抹掉。
    """
    ws = _ws(tmp_path)
    units = _units(3)
    ws.save_units(units)

    # 用户手工改过 u0（而且它已是 done，所以不会进 todo）
    ws.save_entries([
        TranslationEntry(
            uid="u0",
            source="Skill number 0",
            target="我手改的译文",
            status=EntryStatus.TRANSLATED,
            kind=TextKind.ITEM_NAME,
        )
    ])

    def fake_translate(items, target_lang):  # type: ignore[no-untyped-def]
        return [
            TranslationEntry(
                uid=it.unit.uid,
                source=it.unit.source,
                target="机器译文",
                status=EntryStatus.TRANSLATED,
                kind=it.unit.kind,
            )
            for it in items
        ]

    pipe = Pipeline(ws, _ctx(), translate_fn=fake_translate)
    res = pipe.stage_translate()
    assert res.ok, res.error

    saved = {e.uid: e for e in ws.load_entries()}
    assert saved["u0"].target == "我手改的译文", (
        f"用户手工译文被机器译文覆盖了：{saved['u0'].target!r}"
    )
    # u1/u2 是这次翻译出来的
    assert saved["u1"].target == "机器译文"


@pytest.mark.parametrize("n", [0, 1])
def test_tiny_inputs_do_not_crash(tmp_path: Path, n: int) -> None:
    """0 条 / 1 条都要有确定行为。

    `n == 0` 时抽取产物是空的，阶段会明确报"还没有抽取文本"并抛
    `PipelineError` —— 这**不是崩溃**，是一条清晰的提示；0 条文本
    本来就该走"先抽取"这条路。1 条则必须正常翻译并落盘。
    """
    ws = _ws(tmp_path)
    ws.save_units(_units(n))

    def fake_translate(items, target_lang):  # type: ignore[no-untyped-def]
        return [
            TranslationEntry(
                uid=it.unit.uid,
                source=it.unit.source,
                target="x",
                status=EntryStatus.TRANSLATED,
                kind=it.unit.kind,
            )
            for it in items
        ]

    pipe = Pipeline(ws, _ctx(), translate_fn=fake_translate)
    if n == 0:
        from novaloc.pipeline.stages import PipelineError

        with pytest.raises(PipelineError, match="还没有抽取文本"):
            pipe.stage_translate()
        return

    res = pipe.stage_translate()
    assert res.ok, res.error
    assert len(ws.load_entries()) == n
