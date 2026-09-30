"""质检必须审 **打过补丁之后** 的字体，不是补丁之前的。

## 真实事故：明明修好了，质检却硬失败

真实 RPG Maker MV 游戏跑完 `fonts` 阶段，产物是：

    fonts/analysis.jsonl
      {"family":"mplus-1m-regular","coverage_ratio":0.9169,
       "missing":"ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ∏∑∞∫∴∵≠☀☁☂☎♀♂♪♫❤载"}

    fonts/patches.json
      {"action":"merge_multi","ok":true,
       "coverage_before":0.9169,"coverage_after":1.0}
    fonts/patched/mplus-1m-regular.zh.ttf   （1411 KB，已生成）

补丁**成功了**，覆盖 91.7% → **100%**。但 `qa` 阶段报：

    ✗ 字体 mplus-1m-regular 缺少 27 个字符，游戏内会显示为口口口

然后整个流程中止。

## 根因：qa 审的是补丁**之前**的字体

`stage_fonts` 里 `save_font_coverage()` 在**做补丁之前**调用，
所以 `analysis.jsonl` 记的是**游戏原字体**的覆盖情况。
补丁结果在 `patches.json` 里，`qa.py` 没有关联这两份数据 ——
它拿原字体的 `missing` 判 ERROR。

缺字集合里那个 **`载`** 尤其说明问题：译文里有"正在加载"，
`载` 必须存在。补丁已经把这个字形注入了，质检却还在抱怨它缺失。

## 为什么这个 bug 要紧

它不只是"多报一个错"。它是**核心承诺的反向失效**：
用户在设置里看到"字体已补全 100%"，然后质检说"会显示口口口"，
流程中止 —— 用户无从判断到底哪个是真的。
一个**永远失败**的质检等于没有质检（本项目已有
"指标写错比没有指标更糟"的教训）。

## 判据（也是修复后必须保住的性质）

* 补丁 `ok=True` 且 `coverage_after >= 0.999` → **不得**再报缺字。
* 补丁 `ok=True` 但 `coverage_after < 0.999` → 仍要报（确实还有缺字）。
* 补丁失败（`ok=False`）→ 仍要报。
* 没有补丁记录（还没跑过 fonts 阶段）→ 退回用 `analysis.jsonl` 判。
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
from novaloc.models import FontCoverage, Project  # noqa: E402
from novaloc.pipeline.qa import run_qa  # noqa: E402

MISSING = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ∏∑∞∫∴∵≠☀☁☂☎♀♂♪♫❤载"


def _ws(tmp_path: Path) -> Workspace:
    ws = Workspace(Project(name="t", game_dir=str(tmp_path / "game")), tmp_path / "ws")
    for sub in ("fonts", "translations", "extracted", "qa", "images"):
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    return ws


def _ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def _seed_font(ws: Workspace, *, missing: str = MISSING, ratio: float = 0.9169) -> None:
    ws.save_font_coverage([
        FontCoverage(
            font_id="www/fonts/mplus-1m-regular.ttf",
            path="www/fonts/mplus-1m-regular.ttf",
            family="mplus-1m-regular",
            is_game_font=True,
            coverage_ratio=ratio,
            missing=missing,
        )
    ])
    ws.write_json("fonts/charset.json", {"chars": list("正在加载"), "total": 5})


def _font_msgs(report: dict) -> list[str]:
    return [i["message"] for i in report["issues"] if i["stage"] == "fonts"]


def _font_errors(report: dict) -> list[str]:
    return [m for m in _font_msgs(report) if "缺少" in m]


# ----------------------------------------------------------------------
# 核心：补丁成功后不得再报缺字
# ----------------------------------------------------------------------

def test_patched_font_at_full_coverage_is_not_reported_missing(tmp_path: Path) -> None:
    """**这是本次修复的核心断言**：补丁后 100% 覆盖 → 不能再报缺字。

    修好之前这里必然失败（qa 拿 analysis.jsonl 的原字体判 ERROR）。
    """
    ws = _ws(tmp_path)
    _seed_font(ws)
    ws.write_json("fonts/patches.json", [{
        "font_id": "www/fonts/mplus-1m-regular.ttf",
        "action": "merge_multi",
        "out_path": str(tmp_path / "ws" / "fonts" / "patched" / "mplus-1m-regular.zh.ttf"),
        "ok": True,
        "coverage_before": 0.9169,
        "coverage_after": 1.0,
        "warnings": [],
    }])

    report = run_qa(ws, _ctx())
    errs = _font_errors(report)
    assert not errs, (
        "补丁已经覆盖到 100%，质检却仍报缺字 —— 用户会看到"
        "\"字体已补全\"和\"会显示口口口\"两条互相矛盾的信息，流程还会中止。\n"
        f"  实际报错：{errs}"
    )


def test_patched_but_still_incomplete_is_reported(tmp_path: Path) -> None:
    """**边界**：补丁成功了但覆盖没到 100%，仍然必须报。

    修的时候很容易顺手写成"只要有补丁就不再检查" —— 那就把
    "no 口口口"这条核心承诺弄丢了。仍有缺字就必须说。
    """
    ws = _ws(tmp_path)
    _seed_font(ws)
    ws.write_json("fonts/patches.json", [{
        "font_id": "www/fonts/mplus-1m-regular.ttf",
        "action": "merge_multi",
        "ok": True,
        "coverage_before": 0.80,
        "coverage_after": 0.97,      # 还没到 100%
        "warnings": ["补充字体里也没有这些字形"],
    }])

    report = run_qa(ws, _ctx())
    msgs = [i["message"] for i in report["issues"] if i["stage"] == "fonts"]
    assert any("97" in m or "缺" in m for m in msgs), (
        f"补丁后仍缺字却没报：{msgs}"
    )
    assert not report["ok"], "补丁后仍有缺字，质检不该通过"


def test_failed_patch_is_reported(tmp_path: Path) -> None:
    """**边界**：补丁失败必须报，且不能因为"有补丁记录"就被吞掉。"""
    ws = _ws(tmp_path)
    _seed_font(ws)
    ws.write_json("fonts/patches.json", [{
        "font_id": "www/fonts/mplus-1m-regular.ttf",
        "action": "merge_multi",
        "ok": False,
        "error": "写字体文件失败：磁盘满",
        "coverage_before": 0.9169,
        "coverage_after": 0.0,
        "warnings": [],
    }])

    report = run_qa(ws, _ctx())
    msgs = [i["message"] for i in report["issues"] if i["stage"] == "fonts"]
    assert any("磁盘满" in m or "失败" in m for m in msgs), (
        f"补丁失败没报出来：{msgs}"
    )
    assert not report["ok"]


def test_failed_patch_recorded_as_action_none_is_still_reported(
    tmp_path: Path,
) -> None:
    """**真实产物的形态**：`stage_fonts` 对失败的补丁写 `action="none"` + `ok=False`。

    这是历史实现留下的一个陷阱：

        "action": action if pr.ok else "none",     # ← 失败时被写成 "none"
        "ok": pr.ok,
        "error": pr.error,

    于是"补丁失败"和"不需要补丁"在 `action` 上**长得一样**，只能靠
    `ok` + `error` 区分。质检必须认这个形态 —— 否则真实游戏里
    补丁失败会被完全静默（用户以为没事，进游戏看到一片口口口）。
    """
    ws = _ws(tmp_path)
    _seed_font(ws)
    ws.write_json("fonts/patches.json", [{
        "font_id": "www/fonts/mplus-1m-regular.ttf",
        "action": "none",           # ← 失败时 stage_fonts 真的这么写
        "ok": False,
        "error": "注入中文字形失败：没有可用的补充字体",
        "coverage_before": 0.9169,
        "coverage_after": 0.0,
        "warnings": [],
    }])

    report = run_qa(ws, _ctx())
    msgs = [i["message"] for i in report["issues"] if i["stage"] == "fonts"]
    assert any("没有可用的补充字体" in m or "失败" in m for m in msgs), (
        f"`action=none` + `ok=False` 这种真实形态的补丁失败没报出来：{msgs}"
    )
    assert not report["ok"], "补丁失败的质检不该通过"


def test_no_patch_record_falls_back_to_analysis(tmp_path: Path) -> None:
    """**边界**：还没跑过 fonts 阶段（没有 patches.json）时，按原字体判。

    不能因为"没有补丁记录"就当成"没问题"。
    """
    ws = _ws(tmp_path)
    _seed_font(ws)
    # 不写 patches.json

    report = run_qa(ws, _ctx())
    errs = _font_errors(report)
    assert errs, (
        "没有补丁记录、原字体又缺字时，质检必须报缺字 —— "
        "否则'还没补'会被当成'不用补'。"
    )


def test_patch_for_a_different_font_does_not_excuse_this_one(tmp_path: Path) -> None:
    """**边界**：补丁是按 font_id 对应的，不能张冠李戴。

    如果游戏有两个字体，只补了其中一个，另一个缺字仍必须报 ——
    否则就是"补了 A 就以为 B 也没事"，玩家照样看到口口口。
    """
    ws = _ws(tmp_path)
    ws.save_font_coverage([
        FontCoverage(
            font_id="www/fonts/a.ttf", path="www/fonts/a.ttf",
            family="a", is_game_font=True,
            coverage_ratio=1.0, missing="",
        ),
        FontCoverage(
            font_id="www/fonts/b.ttf", path="www/fonts/b.ttf",
            family="b", is_game_font=True,
            coverage_ratio=0.5, missing="载好心",
        ),
    ])
    ws.write_json("fonts/charset.json", {"chars": list("正在加载"), "total": 5})
    ws.write_json("fonts/patches.json", [{
        "font_id": "www/fonts/a.ttf",     # 只补了 a
        "action": "merge_multi",
        "ok": True,
        "coverage_before": 0.99,
        "coverage_after": 1.0,
        "warnings": [],
    }])

    report = run_qa(ws, _ctx())
    errs = _font_errors(report)
    assert any("b" in m for m in errs), (
        f"只补了 a，b 仍缺字却没报：{errs}"
    )
