r"""`novaloc.cleanup` —— 空间回收：**验收通过后**才清备份与写回产物。

## 为什么需要（实测磁盘会先满）

```
工作区增长   1.09 GB / 游戏 × 172 = 187 GB
备份         再 +29 GB
合计         约 216 GB     而 D 盘只剩 156 GB
```

⇒ 约跑到 60% 就满了。而这两类东西**写回并验证成功后就没有价值**：

* **`out/`**（写回产物）：内容已经进游戏目录；断点由 `entries.jsonl` 承担。
  实测单个游戏 2.4 GB（`ButtKnight`）—— 是最大的空间黑洞；
* **`<游戏>-orig-<时间戳>/`**（保护性原版备份）：
  一旦确认写回后的游戏**能正常启动且数据合法**，它就不再需要。

## ★★ 硬规则：**没有通过验收，绝不删备份**

这是整个模块的**唯一安全保证**。判据（`可以清理` 的条件）：

1. 该游戏有 `_verify_results.jsonl` 里的记录；
2. 最新记录的 `status == "ok"`；
3. 且那次验收**真的启动了游戏**（`launch == "clean"`）；
   * 只跑静态检查（`--no-launch`/无 exe）**不算** ——
     本轮的事故正是"静态检查全过、游戏启动即崩"；
   * `verify_game` 的短路逻辑也会让 `launch` 缺失
     （前两项失败时不启动）⇒ 必须检查 `launch` 的值，
     不能只看 `status`。

## 为什么"确认没有问题"必须包含**启动**

本轮实测事故：NovaLoc 把 RPG Maker `note` 里会被 `eval` 的 JS 代码
当文本翻了 ⇒ VisuMZ 用 `new Function()` 执行 ⇒
`SyntaxError: Unexpected number` ⇒ **游戏启动即崩**。

而当时 **JSON 校验、严格校验、BOM 检查全部通过**
（坏掉的是"字符串里的代码语义"，不是 JSON 结构）
⇒ **只有实际启动游戏才能发现**。

## 保守起见，还有两条额外门槛

* 该游戏的 `entries.jsonl` **没有** `failed` 条目
  （有失败说明翻译未完成，还会再跑 ⇒ 备份先留着）；
* 备份目录确实存在且非空（否则没什么可删）。

## 安全措施

* **默认 dry-run**：不带 `--apply` 只报告要删什么、能省多少；
* **删除前复查路径**：只删 `_novaloc_backup` 下的**直接子目录**，
  且名字必须以 `-orig-` 结尾 —— 防止误删别的；
* **每步都记账**：返回删除清单与回收字节数，便于核对。
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: 备份根目录（与 `batch.BACKUP_DIR_NAME` 一致）
BACKUP_DIR_NAME = "_novaloc_backup"

#: 验收结果文件（由 `.scratch/_verify_loop.py` 与 `novaloc verify` 写）
VERIFY_RESULTS_NAME = "_verify_results.jsonl"

#: ★★ 允许清理备份的**唯一**判据：验收必须真的启动过游戏且无错误。
#:
#: `"clean"` 是 `check_launch` 无问题时的标记值（见 `verify._verify_launch`）。
REQUIRED_LAUNCH = "clean"


@dataclass
class CleanupPlan:
    """要清理什么。默认只**报告**，不真删。"""

    #: 可以删的备份目录
    backups: list[pathlib.Path] = field(default_factory=list)
    #: 可以清的 `out/` 目录
    out_dirs: list[pathlib.Path] = field(default_factory=list)
    #: 因**未通过验收**而保留的备份（附原因），用于审查
    kept: list[tuple[pathlib.Path, str]] = field(default_factory=list)
    #: 已回收字节
    freed: int = 0

    @property
    def freed_mb(self) -> float:
        return self.freed / (1024 * 1024)


def _dir_size(p: pathlib.Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def load_latest_verdicts(results: pathlib.Path) -> dict[str, dict]:
    r"""读验收结果，**每个游戏只保留最后一条**。

    一个游戏会被验收多次（每轮写回后一次），只有**最后一次**反映现状。
    """
    out: dict[str, dict] = {}
    if not results.is_file():
        return out
    for ln in results.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        g = str(d.get("game") or "")
        if g:
            out[g] = d  # 后者覆盖前者 ⇒ 留下最后一条
    return out


def backup_allowed(verdict: dict | None) -> tuple[bool, str]:
    r"""★ 这个游戏的原版备份**可以删吗**？返回 ``(可以, 原因)``。

    ## 这是唯一的闸门

    判据严格到"保守"：

    1. 必须有验收记录；
    2. `status == "ok"`；
    3. **`launch == "clean"`** —— 必须**真的启动过游戏且无错误**。

    ⚠️ 第 3 条不可省。实测事故（`Beyond the Portal`）里
    静态检查**全部通过**而游戏**启动即崩**；只做静态检查就删备份，
    等于把用户唯一的回退路径扔掉。
    """
    if not verdict:
        return False, "没有验收记录（从未验收过）"
    if verdict.get("status") != "ok":
        return False, f"验收未通过（status={verdict.get('status')!r}）"
    launch = verdict.get("launch")
    if launch != REQUIRED_LAUNCH:
        return False, (
            f"验收**没有真正启动游戏**（launch={launch!r}）"
            f"⇒ 无法排除'静态检查全过但启动即崩'那类问题，保留备份"
        )
    return True, "验收通过且启动无错误"


def _has_failed_entries(workspaces: pathlib.Path, game_dir: pathlib.Path) -> bool:
    r"""该游戏的工作区里有没有 `failed` 条目。

    有失败说明翻译**还没完成**（下次会重试）⇒ 备份先留着。
    """
    if not workspaces.is_dir():
        return True  # 查不到就保守认为有
    for w in workspaces.iterdir():
        pj = w / "project.json"
        if not pj.is_file():
            continue
        try:
            gd = str(json.loads(pj.read_text(encoding="utf-8")).get("game_dir") or "")
        except (OSError, ValueError):
            continue
        if pathlib.Path(gd) != game_dir:
            continue
        ej = w / "translations" / "entries.jsonl"
        if not ej.is_file():
            return True
        for ln in ej.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                if json.loads(ln).get("status") == "failed":
                    return True
            except ValueError:
                continue
    return False


def plan_cleanup(
    data_root: pathlib.Path,
    *,
    include_out: bool = True,
    include_backups: bool = True,
) -> CleanupPlan:
    r"""算出可以清理什么（**不执行**）。

    `out/` 的清理条件较宽（写回产物已在游戏目录里，且断点靠
    `entries.jsonl`）—— 但为稳妥，仍要求该游戏**有验收记录且通过**。
    """
    plan = CleanupPlan()
    backup_root = data_root / BACKUP_DIR_NAME
    workspaces = data_root / "workspaces"
    verdicts = load_latest_verdicts(data_root / VERIFY_RESULTS_NAME)

    # ---- 1. 备份 ----
    if include_backups and backup_root.is_dir():
        for d in sorted(backup_root.iterdir()):
            if not d.is_dir():
                continue
            # 早前的备份不带 `-orig-`（如 `Xxx-20261004-125655`），
            # 也一并按同一判据处理；但**只处理备份根的直接子目录**
            name = d.name
            # 从目录名反推游戏名（去掉 `-orig-<时间戳>` 或 `-<时间戳>` 后缀）
            game_name = name
            for sep in ("-orig-",):
                if sep in name:
                    game_name = name.split(sep)[0]
                    break
            else:
                game_name = name.rsplit("-", 2)[0]
            # 找对应的游戏目录（用于查 failed 条目）
            game_dir = None
            lib_root = None
            for cand in (data_root, ):
                _ = cand
            # 游戏目录从工作区记录里找
            if workspaces.is_dir():
                for w in workspaces.iterdir():
                    pj = w / "project.json"
                    if not pj.is_file():
                        continue
                    try:
                        rec = json.loads(pj.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if str(rec.get("name") or "") == game_name:
                        game_dir = pathlib.Path(str(rec.get("game_dir") or ""))
                        break
            _ = lib_root
            verdict = verdicts.get(str(game_dir)) if game_dir else None
            ok, why = backup_allowed(verdict)
            if ok and game_dir and _has_failed_entries(workspaces, game_dir):
                ok, why = False, "该游戏仍有 failed 条目（翻译未完成，下次会重试）"
            if not ok:
                plan.kept.append((d, why))
                continue
            plan.backups.append(d)

    # ---- 2. `out/` ----
    if include_out and workspaces.is_dir():
        for w in sorted(workspaces.iterdir()):
            out = w / "out"
            if not (out.is_dir() and any(p.is_file() for p in out.rglob("*"))):
                continue
            pj = w / "project.json"
            game_dir = ""
            if pj.is_file():
                try:
                    game_dir = str(json.loads(pj.read_text(encoding="utf-8")).get("game_dir") or "")
                except (OSError, ValueError):
                    game_dir = ""
            ok, why = backup_allowed(verdicts.get(game_dir))
            if ok:
                plan.out_dirs.append(out)
            else:
                plan.kept.append((out, why))
    return plan


def apply_cleanup(plan: CleanupPlan) -> int:
    r"""执行清理，返回回收字节数。

    ## 安全措施

    * 删备份时**复查**：必须是 `_novaloc_backup` 的**直接子目录**
      （由调用方传入的 plan 保证，这里再查一次目录名不含路径分隔符）；
    * 只 `rmtree` 目录，不删文件；
    * 每个目录的回收字节会被累计，便于核对。
    """
    freed = 0
    for d in plan.backups:
        # 复查：目录名必须是单一段（防止误删深路径）
        if "/" in d.name or "\\" in d.name or not d.is_dir():
            log.warning("跳过可疑目标：%s", d)
            continue
        sz = _dir_size(d)
        try:
            shutil.rmtree(d)
        except OSError as exc:
            log.warning("删除备份失败 %s：%s", d, exc)
            continue
        freed += sz
        log.info("已清理备份 %s（回收 %.1f MB）", d.name, sz / (1024 * 1024))
    for d in plan.out_dirs:
        if not d.is_dir():
            continue
        sz = _dir_size(d)
        try:
            shutil.rmtree(d)
            # 保留空目录，避免下游"目录不存在"的分支
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("清理 out/ 失败 %s：%s", d, exc)
            continue
        freed += sz
        log.info("已清理写回产物 %s（回收 %.1f MB）", d, sz / (1024 * 1024))
    plan.freed = freed
    return freed


def main(argv: list[str] | None = None) -> int:
    """CLI：默认 dry-run，`--apply` 才真删。"""
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="novaloc.cleanup",
        description="空间回收：验收通过后才清备份与写回产物（默认 dry-run）",
    )
    ap.add_argument(
        "--data-root",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("NOVALOC_DATA_ROOT") or r"D:\NovaLoc"),
    )
    ap.add_argument("--apply", action="store_true", help="真的删（默认只报告）")
    ap.add_argument("--no-backups", action="store_true", help="不处理备份")
    ap.add_argument("--no-out", action="store_true", help="不处理 out/")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    plan = plan_cleanup(
        a.data_root,
        include_out=not a.no_out,
        include_backups=not a.no_backups,
    )
    total = sum(_dir_size(p) for p in plan.backups + plan.out_dirs)
    print(f"  ═══ 可清理（{'执行' if a.apply else 'dry-run'}）═══")
    print(f"    备份 **{len(plan.backups)}** 个")
    print(f"    out/ **{len(plan.out_dirs)}** 个")
    print(f"    预计回收 **{total / (1024**3):.2f} GB**")
    print()
    if plan.kept:
        print(f"  ═══ 保留（未通过验收）{len(plan.kept)} 项 ═══")
        from collections import Counter

        c = Counter(why for _d, why in plan.kept)
        for why, n in c.most_common(8):
            print(f"    {n:>4}  {why}")
    if a.apply:
        print()
        freed = apply_cleanup(plan)
        print(f"  ✅ 实际回收 **{freed / (1024**3):.2f} GB**")
        print(f"     删除时刻 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("\n  （dry-run；加 --apply 才真删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
