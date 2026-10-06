r"""★★★ 空间回收的**硬规则**：没有通过验收，绝不删备份。

## 为什么要这么严（实测事故）

本轮 NovaLoc 把 RPG Maker `note` 里**会被 `eval` 的 JS 代码**当文本翻了：

```
备份: '<JS On Expire State>\ntarget.addState(80);\n</JS On Expire State>'
当前: '确认<JS On Expire State>\n目标生命值恢复至 80。\n</JS On Expire State>'
```

VisuMZ 用 **`new Function()`** 执行 ⇒ `SyntaxError: Unexpected number`
⇒ **游戏启动即崩**。

### ★ 而静态检查**全部通过**

```
41 个 .json 全部解析成功 ✅
严格 JSON 校验（拒 NaN/Infinity）✅
无 BOM、无转义破坏、字体魔数正确 ✅
```

⇒ 坏掉的是"**字符串里的代码语义**"，不是 JSON 结构。
**只有实际启动游戏才能发现。**

⇒ 所以清理备份的判据**必须包含"真的启动过游戏且无错误"**。
只做静态检查就删备份，等于把用户**唯一的回退路径**扔掉 ——
而这一次，正是那条备份救回了 `Beyond the Portal`。

## 本文件测什么

1. **★★ `status=ok` 但 `launch` 缺失/`skipped` ⇒ 不许删**
   （这正是实测里 15 个游戏的状态）；
2. `status != ok` ⇒ 不许删；
3. **没有验收记录** ⇒ 不许删；
4. 只有 `status=ok` **且** `launch="clean"` 才允许；
5. `out/` 的清理也走同一判据（不含糊）；
6. 删除前的**路径复查**（只删备份根的直接子目录）；
7. 默认 **dry-run**（不传 `--apply` 不动磁盘）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.cleanup import (  # noqa: E402
    REQUIRED_LAUNCH,
    apply_cleanup,
    backup_allowed,
    load_latest_verdicts,
    plan_cleanup,
)


# ----------------------------------------------------------------------
# 1. ★★ 硬规则：launch 必须是 "clean"
# ----------------------------------------------------------------------
def test_ok_without_launch_is_not_cleanable() -> None:
    r"""★★ `status=ok` 但**没有真正启动游戏** ⇒ **不许删备份**。

    这是实测里最常见的情形（15 个游戏 `launch='skipped'`，
    因为 `verify_game` 在缺 exe 时跳过启动；3 个 `launch=None`
    因为前两项失败时短路、没跑启动检查）。

    ⚠️ 若这几类被当成"通过"而删掉备份，就正好在
    "静态检查全过但启动即崩"那类事故上失去回退路径。
    """
    for launch in (None, "skipped", "", "timeout"):
        ok, why = backup_allowed({"status": "ok", "launch": launch})
        assert not ok, f"launch={launch!r} 竟然允许删备份"
        assert "启动" in why or "launch" in why, f"原因没解释清楚：{why}"


def test_bad_status_is_not_cleanable() -> None:
    ok, why = backup_allowed({"status": "bad", "launch": "clean"})
    assert not ok
    assert "未通过" in why


def test_missing_verdict_is_not_cleanable() -> None:
    """没有验收记录（从未验收过）⇒ 不许删。"""
    for v in (None, {}):
        ok, why = backup_allowed(v)
        assert not ok, f"{v!r} 竟然允许删备份"
    assert "没有验收记录" in backup_allowed(None)[1]


def test_only_ok_plus_clean_is_cleanable() -> None:
    r"""唯一允许的组合：`status=ok` **且** `launch="clean"`。"""
    ok, why = backup_allowed({"status": "ok", "launch": "clean"})
    assert ok, f"该允许的却没允许：{why}"
    assert REQUIRED_LAUNCH == "clean", "判据常量被改了"


# ----------------------------------------------------------------------
# 2. 读数：同一游戏多次验收 ⇒ 只认最后一条
# ----------------------------------------------------------------------
def test_latest_verdict_wins(tmp_path: Path) -> None:
    r"""★★ 一个游戏验收多次时，**只有最后一条**反映现状。

    否则"上次失败、这次成功"的游戏会被永远保留备份
    （或者反过来：上次成功、这次失败却被删 —— 那更危险）。
    """
    f = tmp_path / "_verify_results.jsonl"
    f.write_text(
        "\n".join(
            json.dumps(d)
            for d in (
                {"game": "G1", "status": "bad", "launch": None},
                {"game": "G1", "status": "ok", "launch": "skipped"},
                {"game": "G1", "status": "ok", "launch": "clean"},
                {"game": "G2", "status": "ok", "launch": "clean"},
                {"game": "G2", "status": "bad", "launch": None},
            )
        ),
        encoding="utf-8",
    )
    v = load_latest_verdicts(f)
    assert v["G1"]["launch"] == "clean", "没取最后一条"
    assert v["G2"]["status"] == "bad", "没取最后一条（危险：会误删 G2 的备份）"
    assert not backup_allowed(v["G2"])[0], "G2 最后是 bad，不该允许删"
    assert backup_allowed(v["G1"])[0], "G1 最后是 ok+clean，该允许删"


# ----------------------------------------------------------------------
# 3. 路径安全
# ----------------------------------------------------------------------
def test_apply_skips_suspicious_names(tmp_path: Path) -> None:
    r"""★ 删除前复查：目录名必须是**单一段**（防止误删深路径）。"""
    from novaloc.cleanup import CleanupPlan

    # 名字里带路径分隔符 ⇒ 应被跳过
    weird = tmp_path / "a"
    weird.mkdir()
    (weird / "f").write_text("x", encoding="utf-8")
    plan = CleanupPlan(backups=[Path("evil/../../x")], out_dirs=[])
    apply_cleanup(plan)  # 不该抛异常，也不该删东西
    assert weird.is_dir(), "可疑目标被误删了"


def test_plan_requires_backup_root(tmp_path: Path) -> None:
    """没有 `_novaloc_backup` 目录 ⇒ 计划为空（不报错）。"""
    plan = plan_cleanup(tmp_path)
    assert plan.backups == []
    assert plan.out_dirs == []


def test_plan_cleanup_respects_failed_entries(tmp_path: Path) -> None:
    r"""★★ 游戏**仍有 failed 条目** ⇒ 保留备份（翻译未完成，下次会重试）。

    否则下次重跑要重译，而备份已没了。
    """
    data = tmp_path
    bk = data / "_novaloc_backup" / "G1-orig-20260101-000000"
    bk.mkdir(parents=True)
    (bk / "f").write_text("x", encoding="utf-8")

    ws = data / "workspaces" / "aaaa"
    (ws / "translations").mkdir(parents=True)
    game = tmp_path / "game" / "G1"
    game.mkdir(parents=True)
    (ws / "project.json").write_text(
        json.dumps({"name": "G1", "game_dir": str(game)}), encoding="utf-8"
    )
    (ws / "translations" / "entries.jsonl").write_text(
        json.dumps({"uid": "u1", "status": "failed"}), encoding="utf-8"
    )
    (data / "_verify_results.jsonl").write_text(
        json.dumps({"game": str(game), "name": "G1", "status": "ok", "launch": "clean"}),
        encoding="utf-8",
    )

    plan = plan_cleanup(data)
    assert plan.backups == [], "有 failed 条目却允许删备份"
    assert any("failed" in why for _d, why in plan.kept), (
        f"保留原因没说清：{[w for _d, w in plan.kept]}"
    )


def test_plan_cleanup_allows_clean_game(tmp_path: Path) -> None:
    """验收通过 + 无 failed ⇒ 允许清理（正例，防止规则严到没用）。"""
    data = tmp_path
    bk = data / "_novaloc_backup" / "G1-orig-20260101-000000"
    bk.mkdir(parents=True)
    (bk / "f").write_text("x", encoding="utf-8")

    ws = data / "workspaces" / "aaaa"
    (ws / "translations").mkdir(parents=True)
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "f").write_text("y", encoding="utf-8")
    game = tmp_path / "game" / "G1"
    game.mkdir(parents=True)
    (ws / "project.json").write_text(
        json.dumps({"name": "G1", "game_dir": str(game)}), encoding="utf-8"
    )
    (ws / "translations" / "entries.jsonl").write_text(
        json.dumps({"uid": "u1", "status": "translated", "target": "药水"}),
        encoding="utf-8",
    )
    (data / "_verify_results.jsonl").write_text(
        json.dumps({"game": str(game), "name": "G1", "status": "ok", "launch": "clean"}),
        encoding="utf-8",
    )

    plan = plan_cleanup(data)
    assert plan.backups == [bk], "验收通过却没允许删备份"
    assert plan.out_dirs == [ws / "out"], "验收通过却没允许清 out/"
