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
import re
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

#: **UI 缩写**的形状：纯 ASCII 字母数字（可带 `_ + - . / %`）。
#:
#: 用于"反向碰撞"判据 —— 只有缩写撞词才是真缺陷。
#: 语气词（`Eh?` `Hm?`）、短句（`你是谁？`）、简繁变体（`你是誰？`）
#: 都会在真实数据上造成大量误报，必须排除在外。
#: 详见 `qa_report` 里"反向碰撞"那一段的说明。
_ASCII_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+\-./%]*$")


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
    # ## 还要再收一层：只查 **ASCII 缩写**
    #
    # 原先只按"长度 <= 4 且无空格"筛，在真实数据上**误报 59 组**：
    #
    #     '操…' / '等等…'              → '喂…'
    #     'Eh?/为什么？/什么事？/什么？/啊？' → '怎么了？'
    #     '*脸红*' / '*臉紅*'           → '脸红'
    #     '你是誰？' / '你是谁？'         → '你是谁？'
    #
    # 这些**全都不是错译**，是这个判据的两类天然误报：
    #
    # 1. **语气词/短句**：`Eh?` `Hm?` `啊？` 译成"怎么了？"完全正确。
    #    短感叹词本来就一对多 —— 中文里"嗯/唔/呃"都可对应 `Hm`。
    # 2. **简繁+全半角变体**：`你是誰？` 与 `你是谁？` 是**同一条目**的
    #    两种写法，本来就该译成同一个词。
    #
    # 这条判据**本来要抓的**是 UI 缩写（原始事故：把 `MP` 译成"生命值"，
    # 应为"魔法值"）。缩写的形状很明确：**纯 ASCII 字母数字**，
    # 不含问号/波浪号/星号/汉字。所以只保留"至少两个不同的 ASCII 缩写
    # 撞到同一个译文"的组。
    #
    # 代价：`Hm` 与 `MP` 撞词这种"一个是缩写、一个是语气词"的情况不再报。
    # 这是**刻意的取舍** —— 报出来的 59 组里没有一组是真问题，
    # 一个永远在误报的 ERROR 级判据只会让人学会忽略质检。
    #
    # 判为 ERROR 而不是 WARN：**真**撞词的短标签一定是用户可见的缺陷，
    # 不存在"这样也可以"的解释空间。
    _COLLIDE_MAX_LEN = 4
    by_target: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        src, tgt = e.source.strip(), e.target.strip()
        if not src or not tgt:
            continue
        if len(src) > _COLLIDE_MAX_LEN or " " in src:
            continue  # 只查 UI 短标签
        if not _ASCII_LABEL_RE.match(src):
            continue  # 只查缩写（语气词/短句/简繁变体一律跳过）
        by_target[tgt].add(src)
    collisions = {t: sorted(s) for t, s in by_target.items() if len(s) > 1}
    if collisions:
        sample_c = [{"target": t, "sources": s} for t, s in list(collisions.items())[:10]]
        # ⚠️ 级别从 ERROR **降为 WARN**（真实数据校准后的决定）。
        #
        # 原来这里写的是 ERROR，理由是"**真**撞词的短标签一定是用户可见的
        # 缺陷，不存在'这样也可以'的解释空间"。那个理由对**真**撞词成立，
        # 但这个判据分不出"真撞词"和"巧合"—— 它只看"两个 ASCII 缩写
        # 是否落到同一个译文"，而那**不一定**是缺陷。
        #
        # 实测 DemonsRoots（45572 条）命中 2 组，**两组都不是错译**：
        #
        #     '是的。'  ← Aye. / Yes.    两个都是肯定回答，中文本来就同一个词
        #     '治疗药'  ← Cure / Heal    两个技能都叫这个名，译者的一致选择
        #
        # 而它**本来要抓**的事故（`HP` 译成"生命值"、应为"魔法值"）
        # 有一个共同点：那条错译是**一对标签中的一个被译错**，
        # 于是同族的另一个会跟着撞上来（`HP`/`MP`、`HP%`/`MP%`、
        # `ATK`/`DEF`、`ATK+`/`DEF+` —— 都是**结构同族**的标签）。
        # 上面那两组反例彼此毫无结构关系（长度相同只是巧合）。
        #
        # 想按"结构同族"（互为子串、或同长）去细化，实测**分不开**：
        # `Aye.`/`Yes.` 同长、`Cure`/`Heal` 同长，和 `HP`/`MP` 一模一样。
        # 所以只能二选一：
        #
        #   * 保持 ERROR ⇒ 每局游戏都因两组**正确**的翻译而质检不过，
        #     用户学会忽略质检（这正是这个文件前面反复警告的失败模式）；
        #   * 降为 WARN ⇒ 真撞词会被人看见（只是不拦流程）。
        #
        # 选后者：**拦下正确产物**比**漏放一个可疑项**更糟，
        # 而且这一项本来就是给人看的提示（`samples` 里两条都列出来了）。
        issues.append(_issue(
            Severity.WARN, "consistency",
            f"{len(collisions)} 个译文被多个不同的 UI 缩写共用"
            "（请确认这些缩写是不是同一个意思）："
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
        #
        # ⚠️ **必须和上面那段共用同一个去重键**。
        #
        # 上面 (a) 用的是 `_mark_once(fc.font_id)`（裸 font_id），
        # 这里原来用的是 `_mark_once(f"patch:{p.get('font_id')}")`
        # —— 前缀不同 ⇒ 去重集合里是两个不同的键 ⇒ **同一个字体报两遍**。
        #
        # 实测 DemonsRoots：4 个失败字体 → 报出 **8 条** error。
        # 重复不会改变"是否通过"的结论，但会让用户以为坏了 8 个字体，
        # 也让错误数失去参考价值（错误数是给人看的，翻倍就等于失真）。
        for p in patches:
            if p.get("ok") is False:
                font_ok = False
                if _mark_once(p.get("font_id")):
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
