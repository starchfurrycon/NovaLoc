"""质检：在回写前把"会出问题的地方"全部找出来。

**为什么质检必须是代码而不是模型**：让模型"检查一下自己翻得对不对"，
它会说"看起来很好"。质检要抓的是**确定性缺陷**：

* 占位符丢了/顺序变了（会导致游戏内文本错乱甚至崩溃）
* 译文没翻（跟原文一模一样）
* 还是原文语言（漏翻）
* 过长（UI 会溢出，虽然对白一般有自动换行）
* 空译文
* 同一原文出现多个不同译文（术语不一致）
* 字体缺字（会在游戏里显示成口口口）—— 这是本工具的核心承诺，必须查
* 贴图块绘制失败/过小/溢出

每个问题都带 ``severity``，``error`` 级的问题会让 ``ok=False``。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ..core.registry import Context
from ..core.workspace import Workspace
from ..models import EntryStatus, Severity, TextKind

log = logging.getLogger(__name__)

#: 短标签类文本的长度红线（超过就可能在 UI 上溢出）
SHORT_KIND_MAX_RATIO = 2.2
SHORT_KINDS = {
    TextKind.UI_LABEL, TextKind.MENU, TextKind.ITEM_NAME,
    TextKind.CHARACTER_NAME, TextKind.MAP_NAME, TextKind.SKILL,
}

#: 没有实际语义、不该要求"翻译结果必须不同"的串
_TRIVIAL = {"ok", "hp", "mp", "tp", "exp", "lv", "id", "no", "yes", "→", "←", "↑", "↓"}


def _issue(severity: Severity, stage: str, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "severity": severity.value,
        "stage": stage,
        "message": message,
        "detail": detail,
    }


def run_qa(ws: Workspace, ctx: Context) -> dict[str, Any]:
    """跑完所有检查，返回可直接给 UI 渲染的报告字典。"""
    issues: list[dict[str, Any]] = []
    units = {u.uid: u for u in ws.load_units()}
    entries = ws.load_entries()

    n_total = len(entries)
    n_done = 0
    n_empty = 0

    # ---- 1. 逐条检查 ----
    for e in entries:
        u = units.get(e.uid)
        target = (e.target or "").strip()

        if e.status == EntryStatus.FAILED:
            issues.append(_issue(
                Severity.WARN, "translate",
                f"翻译失败：{e.source[:40]}", uid=e.uid,
                warnings=e.warnings[:3],
            ))
            continue
        if not target:
            n_empty += 1
            issues.append(_issue(
                Severity.WARN, "translate", f"没有译文：{e.source[:40]}", uid=e.uid,
            ))
            continue

        n_done += 1

        # 占位符
        if not e.placeholder_ok:
            issues.append(_issue(
                Severity.ERROR, "placeholder",
                f"占位符校验未通过（回写会破坏游戏文本）：{e.source[:40]}",
                uid=e.uid, target=target[:80], warnings=e.warnings[:5],
            ))

        # 未翻译：译文与原文完全相同
        src = (e.source or "").strip()
        if (
            target == src
            and src.lower() not in _TRIVIAL
            and any(c.isalpha() for c in src)
        ):
            issues.append(_issue(
                Severity.WARN, "translate",
                f"译文与原文相同（可能漏翻）：{src[:40]}", uid=e.uid,
            ))

        # 目标语言残留检测交给 guards（需要原文与译文一起判断）
        from ..translate.guards import check_language_residue

        try:
            residue = check_language_residue(src, target, "zh-Hans")
            if residue:
                issues.append(_issue(
                    Severity.INFO, "translate",
                    f"译文可能仍含原文语言：{target[:40]}", uid=e.uid, detail=residue[:3],
                ))
        except Exception:  # noqa: BLE001
            pass

        # 长度：短标签溢出风险
        kind = u.kind if u else e.kind
        if kind in SHORT_KINDS and len(src) >= 4:
            ratio = len(target) / max(1, len(src))
            if ratio > SHORT_KIND_MAX_RATIO:
                issues.append(_issue(
                    Severity.WARN, "length",
                    f"短标签译文偏长（{len(target)}/{len(src)} 字符）：{src[:30]} → {target[:30]}",
                    uid=e.uid,
                ))

    # ---- 2. 术语一致性：同一原文多种译法 ----
    variants: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        if e.target.strip() and e.source.strip():
            variants[e.source.strip()].add(e.target.strip())
    inconsistent = {k: sorted(v) for k, v in variants.items() if len(v) > 1}
    if inconsistent:
        sample = list(inconsistent.items())[:10]
        issues.append(_issue(
            Severity.WARN, "consistency",
            f"{len(inconsistent)} 条原文存在多种译法（术语可能不统一）",
            samples=[{"source": k, "targets": v} for k, v in sample],
        ))

    # ---- 3. 字体覆盖：本工具的核心承诺 ----
    charset = ws.load_charset()
    n_missing_total = 0
    font_ok = True
    if charset is None:
        issues.append(_issue(
            Severity.WARN, "fonts", "还没有做过字体规划（可能尚未翻译）",
        ))
    else:
        coverage = ws.load_font_coverage()
        for fc in coverage:
            if fc.missing:
                n_missing_total += len(fc.missing)
                font_ok = False
                issues.append(_issue(
                    Severity.ERROR, "fonts",
                    f"字体 {fc.family or fc.path} 缺少 {len(fc.missing)} 个字符，"
                    f"游戏内会显示为口口口：{''.join(fc.missing[:40])}",
                    font_id=fc.font_id,
                ))
        # 补丁结果
        patches = ws.read_json("fonts/patches.json", []) or []
        for p in patches:
            if p.get("action") != "none" and not p.get("ok"):
                font_ok = False
                issues.append(_issue(
                    Severity.ERROR, "fonts",
                    f"字体 {p.get('font_id')} 补丁失败：{p.get('error') or '未知原因'}",
                    font_id=p.get("font_id"),
                ))
            after = p.get("coverage_after") or 0.0
            if p.get("ok") and p.get("action") != "none" and after < 0.999:
                font_ok = False
                issues.append(_issue(
                    Severity.ERROR, "fonts",
                    f"字体 {p.get('font_id')} 补丁后覆盖率仅 {after:.1%}，"
                    "仍有缺字风险（游戏内可能出现口口口）",
                    font_id=p.get("font_id"),
                ))

    # ---- 4. 贴图 ----
    img_records = ws.read_json("images/localize.json", []) or []
    img_failed = 0
    img_review = 0
    for rec in img_records:
        if not rec.get("ok") and rec.get("error"):
            img_failed += 1
        for b in rec.get("blocks", []):
            if b.get("overflow") or b.get("too_small") or b.get("warnings"):
                img_review += 1
    if img_failed:
        issues.append(_issue(
            Severity.WARN, "images", f"{img_failed} 张贴图处理失败（已保留原图）",
        ))
    if img_review:
        issues.append(_issue(
            Severity.INFO, "images",
            f"{img_review} 处贴图文字需要人工复核（过长/过小/有警告）",
        ))

    n_errors = sum(1 for i in issues if i["severity"] == Severity.ERROR.value)
    n_warns = sum(1 for i in issues if i["severity"] == Severity.WARN.value)

    return {
        "ok": n_errors == 0,
        "issues": issues,
        "stats": {
            "entries": n_total,
            "translated": n_done,
            "missing": n_empty,
            "errors": n_errors,
            "warnings": n_warns,
            "inconsistent": len(inconsistent),
            "charset": charset.total if charset else 0,
            "font_missing": n_missing_total,
            "font_ok": font_ok,
            "images": len(img_records),
            "images_failed": img_failed,
            "images_review": img_review,
        },
    }


__all__ = ["run_qa"]
