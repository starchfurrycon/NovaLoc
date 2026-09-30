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

    # ---- 2b. 反向碰撞：**不同的短标签**被译成了**同一个词** ----
    #
    # 这是上一条的镜像问题，而上面那条抓不到它。
    #
    # 实测来源：本机 `translategemma:4b` 把 `MP` 译成"生命值"（应为"魔法值"）。
    # 原因很清楚：单条 UI 缩写几乎不携带上下文，模型只能猜。两种译法都
    # "像那么回事"，既不是漏译、也不是占位符丢失、源文也各不相同，
    # 所以传统的规则质检**挑不出任何毛病** —— 但玩家一眼就能看出是错的，
    # 而一旦写回游戏就是既成事实。
    #
    # 只查"短标签"（无空格、长度 <= 4）：这类条目是 UI 上的独立标签，
    # 语义几乎必然互不相同，撞成同词基本等于错译。长句撞词可能是正常的
    # 同义表述，所以不查，避免用规则压住合理的翻译自由度。
    #
    # 判为 ERROR 而不是 WARN：撞词的短标签**一定**是用户可见的缺陷，
    # 不存在"这样也可以"的解释空间。
    _COLLIDE_MAX_LEN = 4
    by_target: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        src, tgt = e.source.strip(), e.target.strip()
        if not src or not tgt:
            continue
        if len(src) > _COLLIDE_MAX_LEN or " " in src:
            continue  # 只查 UI 短标签
        by_target[tgt].add(src)
    collisions = {t: sorted(s) for t, s in by_target.items() if len(s) > 1}
    if collisions:
        sample_c = [{"target": t, "sources": s} for t, s in list(collisions.items())[:10]]
        issues.append(_issue(
            Severity.ERROR, "consistency",
            f"{len(collisions)} 个译文被多个不同的短标签共用（缩写很可能译错）："
            + "；".join(f"{t} ← {'/'.join(s)}" for t, s in list(collisions.items())[:5]),
            samples=sample_c,
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
        # ⚠️ `analysis.jsonl` 记的是**补丁之前**的游戏原字体。
        # `save_font_coverage()` 在 stage_fonts 里是**做补丁前**调用的，
        # 补丁结果单独写在 `patches.json`（含 coverage_after）。
        #
        # 早先这里只读 analysis.jsonl，于是**补丁明明成功、覆盖已到 100%**，
        # 质检还是报"缺少 27 个字符、会显示口口口"，然后中止整个流程。
        # 用户会同时看到"字体已补全 100%"和"会显示口口口"两条互相矛盾的信息
        # —— 一个永远失败的质检等于没有质检。
        #
        # 所以先按 font_id 把补丁结果查出来，**补丁之后的真实覆盖**才算数。
        patches = ws.read_json("fonts/patches.json", []) or []
        patch_by_id: dict[str, dict[str, Any]] = {}
        for p in patches:
            fid = p.get("font_id")
            if fid:
                patch_by_id[str(fid)] = p

        reported: set[str] = set()

        def _mark_once(font_id: str) -> bool:
            """同一个字体只报一次；返回 True 表示可以报。"""
            if font_id in reported:
                return False
            reported.add(font_id)
            return True

        for fc in ws.load_font_coverage():
            p = patch_by_id.get(fc.font_id)

            # 判定"这个字体到底有没有被补丁处理过"。
            # ⚠️ **不能只看 action**：老版本的 `stage_fonts` 在补丁失败时
            # 写的是 `"action": "none"`（和"无需补丁"同形），所以
            # **`ok is False` 永远算补丁失败**，不管 action 是什么。
            is_failure = p is not None and p.get("ok") is False
            handled_ok = (
                p is not None
                and p.get("ok") is True
                and p.get("action") not in (None, "none", "")
            )

            # (a) 有补丁记录且**失败了** —— 原字体缺字依然存在，必须报。
            if is_failure:
                font_ok = False
                if _mark_once(fc.font_id):
                    issues.append(_issue(
                        Severity.ERROR, "fonts",
                        f"字体 {p.get('font_id')} 补丁失败："
                        f"{p.get('error') or '未知原因'}",
                        font_id=p.get("font_id"),
                    ))
                continue

            # (b) 有补丁记录且成功了 —— 以**补丁之后**的覆盖率为准。
            if handled_ok:
                after = float(p.get("coverage_after") or 0.0)
                if after < 0.999:
                    font_ok = False
                    n_missing_total += len(fc.missing)
                    if _mark_once(fc.font_id):
                        issues.append(_issue(
                            Severity.ERROR, "fonts",
                            f"字体 {p.get('font_id')} 补丁后覆盖率仅 {after:.1%}，"
                            "仍有缺字风险（游戏内可能出现口口口）"
                            + (f"：{fc.missing[:40]}" if fc.missing else ""),
                            font_id=p.get("font_id"),
                        ))
                # after >= 0.999：补丁已解决，**不再报缺字**
                continue

            # (c) 没有补丁记录（还没跑 fonts 阶段，或该字体无需补丁）
            #     —— 退回按原字体分析判，不能当成"没问题"。
            if fc.missing:
                n_missing_total += len(fc.missing)
                font_ok = False
                if _mark_once(fc.font_id):
                    issues.append(_issue(
                        Severity.ERROR, "fonts",
                        f"字体 {fc.family or fc.path} 缺少 {len(fc.missing)} 个字符，"
                        f"游戏内会显示为口口口：{fc.missing[:40]}",
                        font_id=fc.font_id,
                    ))

        # 补丁记录本身的完整性（没有对应 FontCoverage 的条目也要看）。
        # 同样用 `ok is False` 判失败，不依赖 action 的取值。
        for p in patches:
            if p.get("ok") is False:
                font_ok = False
                if _mark_once(f"patch:{p.get('font_id')}"):
                    issues.append(_issue(
                        Severity.ERROR, "fonts",
                        f"字体 {p.get('font_id')} 补丁失败：{p.get('error') or '未知原因'}",
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
