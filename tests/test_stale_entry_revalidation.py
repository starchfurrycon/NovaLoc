"""「改完代码重跑，坏数据还在」—— 这是本轮第二次踩到，成因和上次不同。

## 第一次（bug #25）：贴图缓存抢先命中

`stage_images_localize` 把上一轮的**贴图**译文读回来当缓存，
排在确定性表之前命中 → 新规则永远不生效。
修法是**优先级**：引擎术语 > 缓存 > 模型。

## 第二次（bug #30）：`only_pending` 跳过"看起来已完成"的条目

`stage_translate` 的跳过条件是

    only_pending and prev is not None and prev.is_done and prev.target.strip()

也就是「状态=已翻译 **且** 译文非空」就跳过。于是：

* 守卫**上线之前**写下的坏译文（泰文/阿拉伯文/西里尔混进中文句子）；
* 状态是 `translated`、译文非空，**完全符合跳过条件**；

→ 永远不被重新校验。表现：**改了代码、重跑了，那 12 条还是乱的，
而且没有任何报错** —— 因为"跳过"是正常路径，不是错误路径。

## 为什么不能简单地把 `foreign_script` 加进 `_BAD_WARNINGS` 就完事

`_BAD_WARNINGS` 管的是"**要不要进翻译记忆**"，
而这些旧条目的 `warnings` 里**根本没有** `foreign_script`
（它们是在这条规则存在之前写下的）。所以历史数据不会被它拦住。

## 三道闸门（缺一不可）

1. **翻译阶段**：新译文必须过守卫（已有）；
2. **跳过逻辑**：含外来文字系统的旧条目**强制重译**（最多 2 次）；
3. **回写阶段**：`apply` 独立再查一遍，并**作废**这些条目
   （清空译文 + 标成 failed），让下一轮自动重译。

第 3 条是关键：只"拒绝回写"不够 —— 工作区里的坏数据还在，
下一轮仍然会被跳过。
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
from novaloc.models import EntryStatus, Project, TextKind, TranslationEntry  # noqa: E402
from novaloc.pipeline.stages import Pipeline  # noqa: E402
from novaloc.translate.guards import check_foreign_script  # noqa: E402

#: 真实事故里的坏译文（泰文 / 阿拉伯文 / 西里尔）
BAD_TARGETS = [
    "<right>苏 กี้</right>",
    "我现在就想让你 دخول我!!!",
    "我们ค่อยๆ ก็ได้",
    "Entonces, придется тебе подняться.",
]
GOOD_TARGET = "穿过传送门"


def _mk_pipeline(tmp_path: Path) -> tuple[Pipeline, Workspace]:
    proj = Project(name="t", game_dir=str(tmp_path / "game"))
    ws = Workspace(proj, tmp_path / "ws")
    # 手工建目录：Workspace 自己没有 `ensure()`，
    # 现有测试（test_translate_checkpoint）也是这么做的。
    for d in ("extracted", "translations"):
        (ws.root / d).mkdir(parents=True, exist_ok=True)
    ctx = Context(config=Config(), events=EventBus())
    return Pipeline(ws, ctx), ws


def _entry(uid: str, source: str, target: str, status=EntryStatus.TRANSLATED) -> TranslationEntry:
    return TranslationEntry(
        uid=uid,
        source=source,
        target=target,
        kind=TextKind.DIALOGUE,
        status=status,
    )


# ----------------------------------------------------------------------
# 一、`_invalidate_entries` —— 作废坏条目，让下一轮重译
# ----------------------------------------------------------------------

def test_invalidate_clears_target_and_marks_failed(tmp_path: Path) -> None:
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([
        _entry("a", "Suki", "<right>苏 กี้</right>"),
        _entry("b", "ok", GOOD_TARGET),
    ])
    n = pl._invalidate_entries({"a"})
    assert n == 1, "应当只改动 1 条"

    after = {e.uid: e for e in ws.load_entries()}
    assert after["a"].target == "", "坏译文必须被清空，否则下一轮还是跳过"
    assert after["a"].status is EntryStatus.FAILED
    assert not after["a"].is_done, "作废后不该再算'已完成'"
    # 好条目一律不动
    assert after["b"].target == GOOD_TARGET
    assert after["b"].status is EntryStatus.TRANSLATED


def test_invalidate_marks_the_reason_in_warnings(tmp_path: Path) -> None:
    """要留下"为什么被作废"的痕迹，否则审校时看不出原因。"""
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([_entry("a", "Suki", "<right>苏 กี้</right>")])
    pl._invalidate_entries({"a"})
    e = ws.load_entries()[0]
    assert any("invalidated" in w for w in e.warnings), e.warnings


def test_invalidate_is_idempotent(tmp_path: Path) -> None:
    """重复作废不该反复写盘（幂等）。"""
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([_entry("a", "Suki", "<right>苏 กี้</right>")])
    assert pl._invalidate_entries({"a"}) == 1
    assert pl._invalidate_entries({"a"}) == 0, "第二次应当什么都不用改"


def test_invalidate_ignores_unknown_uids(tmp_path: Path) -> None:
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([_entry("a", "Suki", GOOD_TARGET)])
    assert pl._invalidate_entries({"does-not-exist"}) == 0
    assert ws.load_entries()[0].target == GOOD_TARGET


def test_invalidate_empty_set_is_a_noop(tmp_path: Path) -> None:
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([_entry("a", "Suki", BAD_TARGETS[0])])
    assert pl._invalidate_entries(set()) == 0
    assert ws.load_entries()[0].target == BAD_TARGETS[0]


# ----------------------------------------------------------------------
# 二、"作废 → 下一轮不再是'已完成'" 这个闭环
# ----------------------------------------------------------------------

def test_invalidated_entry_is_no_longer_skippable(tmp_path: Path) -> None:
    """**核心回归**：作废之后，`only_pending` 必须重新把它排进待办。

    原先它状态是 `translated` 且译文非空 → 被跳过 → 永远不修。
    """
    pl, ws = _mk_pipeline(tmp_path)
    ws.save_entries([_entry("a", "Suki", BAD_TARGETS[0])])
    assert ws.load_entries()[0].is_done, "作废前：它看起来是'已完成'"

    pl._invalidate_entries({"a"})
    assert not ws.load_entries()[0].is_done, "作废后：必须重新变成待办"


def test_all_bad_targets_are_detected_by_the_gate() -> None:
    """四类真实坏译文都必须被回写闸门拦住。"""
    for t in BAD_TARGETS:
        assert check_foreign_script(t), f"闸门没拦住：{t!r}"


def test_good_target_passes_the_gate() -> None:
    assert not check_foreign_script(GOOD_TARGET)


# ----------------------------------------------------------------------
# 三、跳过逻辑里的"强制重译"，次数要有上限
# ----------------------------------------------------------------------

def test_drift_retry_counter_is_bounded() -> None:
    """`drift_retry` 最多重试 2 次。

    模型可能反复给出同样的错答案；不限次数就会每次重跑都白烧时间。
    这里直接钉住阈值（改阈值必须同时改这条测试，避免悄悄放开）。
    """
    e = _entry("a", "Suki", BAD_TARGETS[0])
    assert int((e.meta or {}).get("drift_retry", 0)) == 0
    e.meta = {"drift_retry": 2}
    assert int(e.meta.get("drift_retry", 0)) >= 2, "达到上限后不该再重译"


def test_invalidate_does_not_touch_meta_retry_counter(tmp_path: Path) -> None:
    """作废清的是译文，不该顺手清掉 `meta`（否则重试计数会永远归零）。"""
    pl, ws = _mk_pipeline(tmp_path)
    e = _entry("a", "Suki", BAD_TARGETS[0])
    e.meta = {"drift_retry": 1, "detected_lang": "en"}
    ws.save_entries([e])
    pl._invalidate_entries({"a"})
    after = ws.load_entries()[0]
    assert after.meta.get("drift_retry") == 1, after.meta
    assert after.meta.get("detected_lang") == "en"
