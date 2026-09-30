"""流水线编排：把各子系统串成一个可断点续跑的汉化流程。

设计原则：

1. **每个阶段都是幂等的**，可以单独重跑。用户改完译文只想重跑回写，
   不该被迫重新 OCR 几千张图。
2. **阶段之间只通过工作区文件交换数据**，不靠内存里的对象。
   这样进程被杀掉（大型游戏汉化动辄几小时）也不会前功尽弃。
3. **顺序有硬性依赖**：
   - 字体补丁必须在**翻译之后** —— 它需要知道译文用到哪些字。
     这是"不出现口口口"的关键：先知道要写什么字，再决定字体怎么补。
   - 贴图重绘必须在**字体准备好之后**（重绘要用真实中文字体）。
   - 回写必须在最后，且只写工作区副本。
4. **任何一步失败都不继续往下走**，因为下游建立在错误数据上，
   继续跑只会产出更难排查的坏结果。
"""

from __future__ import annotations

import logging
import shutil
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.events import Event, EventBus, ProgressThrottle
from ..core.registry import Context, TranslateItem
from ..core.workspace import Workspace
from ..engines import EngineAdapter, get_adapter
from ..engines.base import ApplyResult
from ..fonts.service import FontService, PatchResult
from ..images.service import TextureResult, TextureTranslator
from ..models import (
    EntryStatus,
    ImageAsset,
    ImageTextBlock,
    ProjectCharset,
    Severity,
    TextUnit,
    TranslationEntry,
)

log = logging.getLogger(__name__)

#: 阶段顺序与中文名（也用于 UI 展示）
STAGES: tuple[tuple[str, str], ...] = (
    ("unpack", "解包资源"),
    ("detect", "识别引擎"),
    ("extract", "抽取文本"),
    ("images_scan", "扫描贴图"),
    ("translate", "翻译"),
    ("fonts", "字体适配"),
    ("images_localize", "贴图汉化"),
    ("qa", "质检"),
    ("apply", "回写产物"),
)

STAGE_LABELS = dict(STAGES)

#: 翻译阶段增量落盘的间隔（秒）。
#:
#: 一次真实游戏的翻译跑到 **27 分钟**时因为一个孤立代理项崩掉，而产物
#: 只在整个阶段结束时写一次 —— 27 分钟的成果全丢。改成每批之后按这个
#: 间隔落盘，崩溃时最多丢这么久。取 15 秒是因为写一次 `entries.jsonl`
#: 在千条量级上只要几毫秒，相对于单批 1~3 秒的模型耗时可以忽略。
_CHECKPOINT_INTERVAL_S = 15.0


@dataclass
class StageResult:
    stage: str
    ok: bool
    message: str = ""
    duration_s: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "label": STAGE_LABELS.get(self.stage, self.stage),
            "ok": self.ok,
            "message": self.message,
            "duration_s": round(self.duration_s, 3),
            "stats": self.stats,
            "error": self.error,
        }


class PipelineError(RuntimeError):
    """流水线阶段失败。带上阶段名，方便 UI 定位。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{STAGE_LABELS.get(stage, stage)}] {message}")
        self.stage = stage
        self.message = message


class Pipeline:
    """一个项目的汉化流水线。"""

    # 见文件末尾的 font_patch_records() —— 提成模块级函数是为了能单独测。

    def __init__(
        self,
        ws: Workspace,
        ctx: Context,
        *,
        translate_fn: Callable[..., list[TranslationEntry]] | None = None,
    ) -> None:
        self.ws = ws
        self.ctx = ctx
        self.bus: EventBus = ctx.events
        self._translate_fn = translate_fn
        self.results: list[StageResult] = []

    # ==================================================================
    # 通用执行包装
    # ==================================================================

    def _run(self, stage: str, fn: Callable[[], StageResult]) -> StageResult:
        """执行一个阶段并广播进度。失败抛 :class:`PipelineError`。"""
        label = STAGE_LABELS.get(stage, stage)
        self.bus.stage_start(stage, label)
        t0 = time.time()
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            dur = time.time() - t0
            detail = traceback.format_exc(limit=6)
            self.bus.stage_error(stage, f"{label} 失败：{exc}", detail=detail)
            self.ws.log_line("pipeline", f"[{stage}] 异常：{exc}\n{detail}")
            res = StageResult(stage=stage, ok=False, error=str(exc), duration_s=dur)
            self.results.append(res)
            raise PipelineError(stage, str(exc)) from exc
        res.duration_s = time.time() - t0
        self.results.append(res)
        if res.ok:
            self.bus.stage_end(stage, res.message or label, **res.stats)
        else:
            self.bus.stage_error(stage, res.error or f"{label} 未成功", **res.stats)
            raise PipelineError(stage, res.error or f"{label} 未成功")
        return res

    # ==================================================================
    # 阶段实现
    # ==================================================================

    def stage_unpack(self) -> StageResult:
        """把游戏目录里的归档解到工作区，供后续阶段读写。

        这一阶段**必须容忍"没有归档"**：多数游戏是明文目录，那才是常态。
        所以"没找到归档"是成功（`ok=True`）且说明清楚，不是失败。

        为什么解到工作区而不是就地解压：原游戏目录贯穿全项目的硬约束是
        **只读**。解包树在工作区里，适配器可以就地改文件；回写阶段再把
        改动过的文件打回归档，并给原归档留备份。
        """

        def go() -> StageResult:
            from ..archives import probe, unpack_into
            from ..archives.base import ArchiveError, UnsafeArchiveError

            # 必须读**原始**目录：解包阶段自己不能去读上一次的解包树，
            # 否则会"解包一个解包结果"，越滚越深。
            src = self.ws.source_dir
            pack_cfg = self.ctx.config.pack
            forced_off = bool(pack_cfg.no_unpack)

            # 先清掉上一次的解包树：否则"这次没有归档了"会读到上次的残留，
            # 产出一个和当前游戏无关的成果 —— 这种串味很难被用户发现。
            root = self.ws.unpacked_root
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            self.ws.write_json("unpacked.json", [])

            if forced_off:
                return StageResult(
                    stage="unpack", ok=True,
                    message="已按配置跳过解包（--no-unpack），按明文目录处理",
                    stats={"unpacked": 0, "skipped_by_config": True},
                )

            try:
                infos = probe(src, max_depth=pack_cfg.max_depth)
            except Exception as exc:  # noqa: BLE001
                # 探测失败不该让整条流水线挂掉：退回"明文目录"继续跑，
                # 但要把原因记下来，用户能看见。
                self.ws.log_line("unpack", f"归档探测失败，按明文目录处理：{exc}")
                return StageResult(
                    stage="unpack", ok=True,
                    message=f"归档探测失败，已按明文目录处理：{exc}",
                    stats={"unpacked": 0, "probe_error": str(exc)},
                )

            if not infos:
                return StageResult(
                    stage="unpack", ok=True,
                    message="没有发现归档，按明文目录处理",
                    stats={"unpacked": 0, "archives": 0},
                )

            records: list[dict[str, Any]] = []
            notes: list[str] = []
            for i, info in enumerate(infos):
                dest = root / f"{i:02d}_{info.path.stem}"
                only = tuple(pack_cfg.only_suffixes) or None
                try:
                    res = unpack_into(info.path, dest, only_suffixes=only)
                except UnsafeArchiveError as exc:
                    # 解压炸弹/危险条目：跳过并告知，继续处理别的归档
                    notes.append(f"{info.path.name}：拒绝解包（{exc}）")
                    self.ws.log_line("unpack", f"拒绝解包 {info.path.name}：{exc}")
                    continue
                except (ArchiveError, OSError) as exc:
                    notes.append(f"{info.path.name}：解包失败（{exc}）")
                    self.ws.log_line("unpack", f"解包失败 {info.path.name}：{exc}")
                    continue

                records.append({
                    "source": str(info.path),
                    "dest": str(dest),
                    "kind": info.kind,
                    "entries": res.entries,
                    "written": res.written,
                    "writable": info.writable,
                    "skipped": res.skipped,
                })
                if res.skipped:
                    notes.append(
                        f"{info.path.name}：跳过 {len(res.skipped)} 个可疑条目"
                        "（目录穿越等）"
                    )

            self.ws.write_json("unpacked.json", records)

            total_entries = sum(r["entries"] for r in records)
            total_written = sum(r["written"] for r in records)
            msg = (
                f"解包 {len(records)}/{len(infos)} 个归档，"
                f"写出 {total_written}/{total_entries} 个文件"
            )
            stats = {
                "unpacked": len(records),
                "archives": len(infos),
                "entries": total_entries,
                "written": total_written,
                "notes": notes,
            }
            self.ws.log_line("unpack", msg + (("；" + "；".join(notes)) if notes else ""))
            return StageResult(stage="unpack", ok=True, message=msg, stats=stats)

        return self._run("unpack", go)

    def stage_detect(self) -> StageResult:
        def go() -> StageResult:
            from ..engines import detect_engine

            info = detect_engine(self.ws.effective_source, self.ctx)
            proj = self.ws.project
            proj.engine = info.engine_id
            proj.engine_version = info.version
            self.ws.save()

            self.bus.log(
                f"识别结果：{info.display_name} {info.version or ''} "
                f"(置信度 {info.confidence:.0%})",
                stage="detect",
            )
            for e in info.evidence:
                self.bus.log(f"  · {e}", stage="detect")

            if not info.ok:
                return StageResult(
                    stage="detect",
                    ok=False,
                    error=(
                        "无法识别游戏引擎。请确认选择的是游戏根目录，"
                        "或改用「散装文件」模式（可先用 AssetStudio/UABEA 导出资源）。"
                    ),
                )
            return StageResult(
                stage="detect",
                ok=True,
                message=f"{info.display_name} {info.version}".strip(),
                stats={
                    "engine_id": info.engine_id,
                    "display_name": info.display_name,
                    "version": info.version,
                    "confidence": info.confidence,
                    "evidence": info.evidence,
                },
            )

        return self._run("detect", go)

    def _adapter(self) -> EngineAdapter:
        engine_id = self.ws.project.engine
        if not engine_id:
            self.stage_detect()
            engine_id = self.ws.project.engine
        ad = get_adapter(engine_id, self.ctx)
        if ad is None:
            raise PipelineError("detect", f"没有 id 为 {engine_id!r} 的引擎适配器")
        return ad

    def stage_extract(self) -> StageResult:
        def go() -> StageResult:
            ad = self._adapter()
            root = self.ws.effective_source
            self.bus.log(f"用 {ad.display_name} 适配器抽取文本…", stage="extract")
            units, rep = ad.extract_text(root)
            self.ws.save_units(units)
            self.ws.save_extract_report(rep)

            for err in rep.errors:
                self.bus.log(err, stage="extract", severity=Severity.WARN)

            live = sum(1 for u in units if u.source.strip())
            self.bus.log(
                f"抽出 {live} 条文本（扫描 {rep.files_scanned} 个文件）", stage="extract"
            )
            if not units:
                return StageResult(
                    stage="extract",
                    ok=False,
                    error=(
                        "没有抽到任何文本。可能该引擎的文本存放方式尚未支持，"
                        "请查看抽取报告中的说明。"
                    ),
                    stats={"files_scanned": rep.files_scanned},
                )
            return StageResult(
                stage="extract",
                ok=True,
                message=f"{len(units)} 条文本",
                stats={
                    "units": len(units),
                    "files_scanned": rep.files_scanned,
                    "files_matched": rep.files_matched,
                    "errors": len(rep.errors),
                },
            )

        return self._run("extract", go)

    def stage_images_scan(self) -> StageResult:
        def go() -> StageResult:
            ad = self._adapter()
            self.bus.log("扫描贴图资源…", stage="images_scan")
            assets, report = ad.extract_images(self.ws.effective_source)

            # 去重：同一个 path 只保留一条。
            # 少了这一步，同一个资产会被抽两遍，回写时译文被叠加。
            seen: set[str] = set()
            uniq: list[ImageAsset] = []
            for a in assets:
                if a.path in seen:
                    continue
                seen.add(a.path)
                uniq.append(a)
            self.ws.save_images(uniq)

            for err in report.errors:
                self.bus.log(err, stage="images_scan", severity=Severity.WARN)
            self.bus.log(f"发现 {len(uniq)} 张候选贴图", stage="images_scan")
            return StageResult(
                stage="images_scan",
                ok=True,
                message=f"{len(uniq)} 张候选贴图",
                stats={"images": len(uniq), "errors": len(report.errors)},
            )

        return self._run("images_scan", go)

    def stage_translate(self, *, only_pending: bool = True) -> StageResult:
        def go() -> StageResult:
            units = self.ws.load_units()
            if not units:
                return StageResult(
                    stage="translate", ok=False, error="还没有抽取文本，请先执行抽取"
                )

            existing = {e.uid: e for e in self.ws.load_entries()}
            # 刻意**不**自动并入内置游戏术语表。实测结论（数据在下面）：
            # 内置表能修好单条缩写（`MP` → `魔法值`），但代价是整体变差 ——
            # 同一批 27 条样本跑 3 遍：漏译从 0 升到 2.67/遍，
            # 并出现 `Load Game` → "重新开始"（应为"读档"）这类污染，
            # 因为提示词把 `Restart`/`Save`/`Load` 一起注入了，4B 模型会串。
            # 而"把源词已在文本里的术语过滤掉"这条安全规则，会**正好**
            # 滤掉 `HP`/`MP`/`Gold` —— 也就是唯一受益的那些条目，
            # 等于自己取消自己。
            # 所以内置表只作为**用户可查可抄的起点**（glossary.builtin_entries()），
            # 由用户按自己游戏的情况决定是否启用，而不是默认注入。
            glossary = self.ws.load_glossary()

            todo: list[TextUnit] = []
            for u in units:
                if not u.source.strip():
                    continue
                prev = existing.get(u.uid)
                if only_pending and prev is not None and prev.is_done and prev.target.strip():
                    continue
                todo.append(u)

            if not todo:
                return StageResult(
                    stage="translate",
                    ok=True,
                    message="所有条目已有译文，无需翻译",
                    stats={"translated": 0, "skipped": len(units)},
                )

            self.bus.log(
                f"待翻译 {len(todo)} 条（已有译文 {len(units) - len(todo)} 条）",
                stage="translate",
            )

            # 增量落盘：崩溃/断电时最多只丢 `_CHECKPOINT_INTERVAL_S` 秒的成果。
            # 之前只在阶段结束时写一次，一次 27 分钟的翻译因一个孤立代理项
            # 崩掉就全丢了。节流按时间而不是按批数 —— 批次大小差异很大
            # （对白 12 条、UI 40 条，单条耗时更是差几十倍）。
            last_save = [0.0]

            def _checkpoint(partial: list[TranslationEntry], final: bool) -> None:
                now = time.time()
                if not final and now - last_save[0] < _CHECKPOINT_INTERVAL_S:
                    return
                last_save[0] = now
                # 已有条目要合并保留：用户手工改过的译文不能被流水线覆盖
                merged_now = {**existing, **{e.uid: e for e in partial}}
                self.ws.save_entries(list(merged_now.values()))

            entries = self._translate_units(todo, glossary, on_progress=_checkpoint)
            # 已有条目要合并保留：用户手工改过的译文不能被流水线覆盖
            merged = {**existing, **{e.uid: e for e in entries}}
            self.ws.save_entries(list(merged.values()))

            done = sum(1 for e in entries if e.is_done)
            failed = [e for e in entries if e.status == EntryStatus.FAILED]
            if failed:
                self.bus.log(
                    f"{len(failed)} 条翻译失败，可在文本审校页重试",
                    stage="translate",
                    severity=Severity.WARN,
                )
            self.bus.log(f"翻译完成 {done}/{len(entries)} 条", stage="translate")
            return StageResult(
                stage="translate",
                ok=True,
                message=f"完成 {done}/{len(entries)} 条",
                stats={
                    "requested": len(entries),
                    "translated": done,
                    "failed": len(failed),
                    "changed": sum(
                        1 for e in entries if e.target.strip() and e.target != e.source
                    ),
                },
            )

        return self._run("translate", go)

    def _translate_units(
        self,
        units: list[TextUnit],
        glossary: list[Any],
        *,
        on_progress: Callable[[list[TranslationEntry], bool], None] | None = None,
    ) -> list[TranslationEntry]:
        """把文本单元交给翻译提供者，按类型分批并带进度。

        ``on_progress(已完成的条目, 是否收尾)`` 每批之后调用一次，
        用来**增量落盘**。加这个参数是因为一次真实游戏的翻译跑到
        **27 分钟**时崩掉，而产物只在整个阶段结束时才写一次 ——
        27 分钟的成果全丢了。重跑要再花 27 分钟（虽然 `only_pending`
        能把已完成的部分跳过，但那次**一条都没存下来**）。
        """
        from ..core.registry import Providers

        provider = None
        if self._translate_fn is None:
            provider = Providers(self.ctx).resolve_translate()
            ok, why = provider.available()
            if not ok:
                raise PipelineError("translate", f"翻译提供者不可用：{why}")
            self.bus.log(
                f"使用翻译提供者：{getattr(provider, 'provider_name', provider)}",
                stage="translate",
            )

        target_lang = self.ctx.config.translate.target_lang
        throttle = ProgressThrottle(self.bus, "translate", min_interval_s=0.4, min_delta=0.005)

        # 按类型分批：UI 短标签可以大批量，长对白必须小批量。
        # 混在一起会让模型把长句译成标签风格，或者把标签译得啰嗦。
        buckets: dict[str, list[TextUnit]] = {}
        for u in units:
            buckets.setdefault(u.kind.value, []).append(u)

        out: list[TranslationEntry] = []
        done = 0
        total = len(units)

        for kind, group in buckets.items():
            chunk_size = 12 if kind in ("dialogue", "narration") else 40
            for i in range(0, len(group), chunk_size):
                chunk = group[i : i + chunk_size]
                items = [
                    TranslateItem(
                        unit=u,
                        glossary=glossary,
                        context_lines=[u.context] if u.context else [],
                    )
                    for u in chunk
                ]
                try:
                    if self._translate_fn is not None:
                        res = self._translate_fn(items, target_lang)
                    else:
                        res = provider.translate_batch(items, target_lang)
                except Exception as exc:  # noqa: BLE001
                    # 一批失败不能让整个项目挂掉：标记该批为失败，继续后面的。
                    # 否则一条奇怪的文本就能让几小时的翻译白跑。
                    self.bus.log(
                        f"批次翻译失败（{kind} {i}-{i + len(chunk)}）：{exc}",
                        stage="translate",
                        severity=Severity.WARN,
                    )
                    res = [
                        TranslationEntry(
                            uid=u.uid,
                            source=u.source,
                            target="",
                            status=EntryStatus.FAILED,
                            kind=u.kind,
                            warnings=[str(exc)[:200]],
                        )
                        for u in chunk
                    ]
                out.extend(res)
                done += len(chunk)
                throttle(done / max(1, total), f"已翻译 {done}/{total}")
                if on_progress is not None:
                    # 每批之后给调用方一个落盘机会。回调自己负责节流，
                    # 所以这里不必判断时间。
                    on_progress(out, False)

        if on_progress is not None:
            on_progress(out, True)
        return out

    # ------------------------------------------------------------------

    def _collect_texts(self) -> tuple[list[str], list[str]]:
        """收集 (译文, 原文) 用于构建字符集。"""
        units = {u.uid: u for u in self.ws.load_units()}
        entries = self.ws.load_entries()
        translated: list[str] = []
        source: list[str] = []
        for e in entries:
            if e.target.strip():
                translated.append(e.target)
            u = units.get(e.uid)
            if u is not None:
                source.append(u.source)
            # 译文为空时把原文也算进去：UI 里可能直接显示未翻译的原文，
            # 那些字符同样需要字体覆盖，否则一样是口口口。
            if not e.target.strip() and e.source.strip():
                translated.append(e.source)

        # 贴图译文也参与字符集 —— 贴图重绘用的是真实字体，
        # 缺字会直接在图上画出口口口。
        for img in self.ws.load_images():
            for b in img.blocks:
                if b.target and b.target.strip():
                    translated.append(b.target)
                if b.source and b.source.strip():
                    source.append(b.source)

        return translated, source

    def stage_fonts(self, *, force: bool = False) -> StageResult:
        def go() -> StageResult:
            ad = self._adapter()
            fs = FontService(self.ctx)
            translated, source = self._collect_texts()
            if not translated:
                return StageResult(
                    stage="fonts",
                    ok=False,
                    error="还没有任何译文，字体无法规划（字体补丁依赖译文用到的字符）",
                )

            charset = fs.build_charset(translated, source)
            cs = ProjectCharset(
                text_chars=sorted(set("".join(translated))),
                ui_chars=sorted(set("".join(source)) - set("".join(translated))),
                all_chars=sorted(set(charset)),
                total=len(set(charset)),
            )
            self.ws.save_charset(cs)
            self.bus.log(f"项目字符集共 {cs.total} 个字符", stage="fonts")

            # 审计游戏自带字体
            game_fonts = ad.discover_fonts(self.ws.effective_source)
            audits: list[tuple[Any, Path, Any]] = []
            for gf in game_fonts:
                fp = self.ws.effective_source / gf.path
                if not fp.is_file():
                    continue
                try:
                    audit = fs.audit(fp, charset)
                except Exception as exc:  # noqa: BLE001
                    self.bus.log(
                        f"审计字体 {gf.path} 失败：{exc}",
                        stage="fonts",
                        severity=Severity.WARN,
                    )
                    continue
                gf.coverage_ratio = audit.coverage
                gf.missing = audit.missing[:500]
                gf.num_glyphs = audit.total
                audits.append((gf, fp, audit))
                tail = f"，缺 {len(audit.missing)} 字" if audit.missing else "，无缺字"
                self.bus.log(
                    f"字体 {audit.family or gf.path}：覆盖 {audit.coverage:.1%}{tail}",
                    stage="fonts",
                )

            self.ws.save_font_coverage(game_fonts)

            if not audits:
                self.bus.log(
                    "游戏没有自带可替换的字体文件。引擎将回退到系统/内置字体；"
                    "已记录该情况，贴图汉化仍使用本地中文字体渲染。",
                    stage="fonts",
                    severity=Severity.WARN,
                )
                return StageResult(
                    stage="fonts",
                    ok=True,
                    message="引擎无需字体补丁",
                    stats={"game_fonts": 0, "charset": cs.total, "patched": 0},
                )

            # 只补缺字的字体；全覆盖的不用动
            patches: list[dict[str, Any]] = []
            out_dir = self.ws.p("fonts", "patched")
            out_dir.mkdir(parents=True, exist_ok=True)
            n_patched = 0

            for gf, fp, audit in audits:
                if audit.ok and not force:
                    self.bus.log(f"{audit.family or gf.path} 已全覆盖，跳过", stage="fonts")
                    patches.append({
                        "font_id": gf.font_id,
                        "action": "none",
                        "out_path": "",
                        "ok": True,
                        "coverage_before": audit.coverage,
                        "coverage_after": audit.coverage,
                        "warnings": [],
                    })
                    continue

                out_path = out_dir / f"{fp.stem}.zh{fp.suffix}"
                self.bus.log(
                    f"为 {audit.family or gf.path} 注入中文字形"
                    f"（当前覆盖 {audit.coverage:.1%}）…",
                    stage="fonts",
                )
                pr: PatchResult = fs.patch_font(fp, charset, out_path=out_path)
                # 记录**实际用的策略**，不要把 action 写死成 "merge"。
                # 早先这里硬编码 "merge"，于是 replace / fallback_only
                # 策略产出的字体虽然 ok=True、out_path 也有值，却因为
                # `stage_apply` 按 `action == "merge"` 过滤而被**丢弃** ——
                # 产物落在工作区里，却永远回写不到游戏。
                # pr.merge.method 才是真实发生的事：
                # merge_multi / replace / fallback_only / merge。
                action = str(
                    getattr(pr.merge, "method", "")
                    or getattr(self.ctx.config.font, "strategy", "")
                    or "merge"
                )
                entry = {
                    "font_id": gf.font_id,
                    "action": action if pr.ok else "none",
                    "strategy": getattr(self.ctx.config.font, "strategy", ""),
                    "out_path": str(pr.out_path) if pr.out_path else "",
                    "ok": pr.ok,
                    "error": pr.error,
                    "coverage_before": pr.audit_before.coverage if pr.audit_before else 0.0,
                    "coverage_after": pr.audit_after.coverage if pr.audit_after else 0.0,
                    "warnings": list(pr.warnings),
                }
                patches.append(entry)
                for w in pr.warnings:
                    self.bus.log(w, stage="fonts", severity=Severity.WARN)
                if pr.ok:
                    n_patched += 1
                    self.bus.log(
                        f"✅ {audit.family or gf.path} 覆盖 "
                        f"{entry['coverage_before']:.1%} → {entry['coverage_after']:.1%}",
                        stage="fonts",
                    )
                else:
                    self.bus.log(
                        f"❌ 字体补丁失败：{pr.error}",
                        stage="fonts",
                        severity=Severity.ERROR,
                    )

            self.ws.write_json("fonts/patches.json", patches)

            # 只要"需要补丁的字体"全部失败，就不能声称"保证不出乱码"，
            # 这时候必须硬失败而不是继续回写一个缺字的游戏。
            needed = [p for p in patches if p["action"] != "none"]
            hard_fail = [p for p in needed if not p["ok"]]
            if needed and len(hard_fail) == len(needed):
                return StageResult(
                    stage="fonts",
                    ok=False,
                    error=(
                        f"{len(hard_fail)} 个字体补丁全部失败。"
                        "为避免游戏内出现口口口，已中止流程。"
                        "可尝试在设置里把字体策略改为 replace，或手动指定补充字体。"
                    ),
                    stats={"patched": 0, "charset": cs.total, "failed": len(hard_fail)},
                )

            return StageResult(
                stage="fonts",
                ok=True,
                message=f"{n_patched} 个字体已注入中文字形",
                stats={
                    "game_fonts": len(audits),
                    "patched": n_patched,
                    "charset": cs.total,
                    "failed": len(hard_fail),
                },
            )

        return self._run("fonts", go)

    def stage_images_localize(self, *, force: bool = False) -> StageResult:
        def go() -> StageResult:
            assets = self.ws.load_images()
            if not assets:
                return StageResult(
                    stage="images_localize",
                    ok=True,
                    message="没有需要汉化的贴图",
                    stats={"total": 0, "localized": 0},
                )

            tt = TextureTranslator(self.ctx, translate_fn=self._make_texture_translate())
            out_dir = self.ws.p("images", "rebuilt")
            out_dir.mkdir(parents=True, exist_ok=True)

            throttle = ProgressThrottle(self.bus, "images_localize", min_interval_s=0.4)
            ok_count = 0
            skipped = 0
            total_blocks = 0
            results: list[dict[str, Any]] = []
            src_root = self.ws.effective_source
            existing = self._existing_image_translations()
            by_uid = {a.uid: a for a in assets}

            from ..images.io import imwrite_bgr

            for i, asset in enumerate(assets, 1):
                src = src_root / asset.path
                if not src.is_file():
                    skipped += 1
                    results.append({
                        "uid": asset.uid,
                        "path": asset.path,
                        "ok": False,
                        "error": "源文件不存在",
                    })
                    throttle(i / max(1, len(assets)), f"贴图 {i}/{len(assets)}")
                    continue

                try:
                    res: TextureResult = tt.process(
                        src,
                        asset_uid=asset.uid,
                        target_lang=self.ctx.config.translate.target_lang,
                        existing=existing,
                    )
                except Exception as exc:  # noqa: BLE001
                    skipped += 1
                    self.bus.log(
                        f"贴图 {asset.path} 处理失败：{exc}",
                        stage="images_localize",
                        severity=Severity.WARN,
                    )
                    results.append({
                        "uid": asset.uid,
                        "path": asset.path,
                        "ok": False,
                        "error": str(exc),
                    })
                    throttle(i / max(1, len(assets)), f"贴图 {i}/{len(assets)}")
                    continue

                # 把逐块结果记回资产（供审校页显示与字符集统计）
                rec = by_uid.get(asset.uid)
                if rec is not None:
                    # 尺寸也从 OCR 结果回填：以前只置 `analyzed = True`，
                    # `width`/`height` 永远是 0 —— 审校页和报告里显示
                    # "0x0"，看着像图片读取失败，其实只是没回填。
                    if res.asset.width and res.asset.height:
                        rec.width = res.asset.width
                        rec.height = res.asset.height
                    rec.blocks = [
                        ImageTextBlock(
                            id=o.block_id,
                            box=tuple(o.box),  # type: ignore[arg-type]
                            quad=o.quad or None,
                            source=o.source,
                            target=o.target,
                            status=o.status,
                            confidence=o.confidence,
                            ocr_engine=o.ocr_engine,
                            warnings=list(o.warnings),
                        )
                        for o in res.outcomes
                    ]
                    rec.analyzed = True

                written = False
                if res.ok and res.changed and res.image is not None:
                    dst = out_dir / asset.path
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    written = imwrite_bgr(dst, res.image)
                    if written:
                        ok_count += 1
                    else:
                        self.bus.log(
                            f"贴图 {asset.path} 写入失败",
                            stage="images_localize",
                            severity=Severity.WARN,
                        )
                else:
                    skipped += 1

                total_blocks += res.translated
                results.append({
                    "uid": asset.uid,
                    "path": asset.path,
                    "ok": bool(res.ok and written),
                    "translated": res.translated,
                    "needs_review": len(res.needs_review),
                    "changed": res.changed,
                    "inpaint_method": res.inpaint_method,
                    "ocr_ms": round(res.ocr_ms, 1),
                    "total_ms": round(res.total_ms, 1),
                    "error": res.error,
                    # 整图级提醒要落盘：它解释了"这张图为什么没被处理"
                    # （例如"跳过了 2 个疑似幻觉文字块"）。只写日志的话，
                    # 用户在审校页看到"未处理"却不知道原因。
                    "warnings": list(res.warnings),
                    "blocks": [
                        {
                            "id": o.block_id,
                            "source": o.source,
                            "target": o.target,
                            "ok": o.ok,
                            "overflow": o.overflow,
                            "too_small": o.too_small,
                            "warnings": list(o.warnings),
                        }
                        for o in res.outcomes
                    ],
                })
                throttle(i / max(1, len(assets)), f"贴图 {i}/{len(assets)}")

            self.ws.save_images(assets)
            self.ws.write_json("images/localize.json", results)
            self.bus.log(
                f"贴图处理完成：{ok_count} 张已汉化，共替换 {total_blocks} 处文字，"
                f"{skipped} 张无需处理或跳过",
                stage="images_localize",
            )
            return StageResult(
                stage="images_localize",
                ok=True,
                message=f"{ok_count}/{len(assets)} 张贴图已汉化（{total_blocks} 处文字）",
                stats={
                    "total": len(assets),
                    "localized": ok_count,
                    "skipped": skipped,
                    "blocks": total_blocks,
                    "ocr_calls": tt.calls,
                    "cache_hits": tt.cache_hits,
                },
            )

        return self._run("images_localize", go)

    def _existing_image_translations(self) -> dict[str, str]:
        """从已保存的贴图资产里取出「原文 → 译文」映射，作为缓存复用。"""
        out: dict[str, str] = {}
        for img in self.ws.load_images():
            for b in img.blocks:
                if b.source and b.target and b.target.strip():
                    out[b.source.strip().lower()] = b.target
        return out

    def _make_texture_translate(self) -> Callable[..., list[TranslationEntry]]:
        """给贴图管线用的翻译回调：一次翻一小串，返回等长列表。"""
        from ..core.registry import Providers

        provider = None
        if self._translate_fn is None:
            provider = Providers(self.ctx).resolve_translate()

        def _fn(items: list[TranslateItem], target_lang: str) -> list[TranslationEntry]:
            if self._translate_fn is not None:
                return self._translate_fn(items, target_lang)
            return provider.translate_batch(items, target_lang)

        return _fn

    # ------------------------------------------------------------------

    def stage_qa(self) -> StageResult:
        def go() -> StageResult:
            from .qa import run_qa

            report = run_qa(self.ws, self.ctx)
            self.ws.write_json("qa/report.json", report)
            for issue in report.get("issues", [])[:200]:
                try:
                    sev = Severity(issue.get("severity", "info"))
                except ValueError:
                    sev = Severity.INFO
                self.bus.log(
                    f"[{issue.get('stage', '')}] {issue.get('message', '')}",
                    stage="qa",
                    severity=sev,
                )
            ok = bool(report.get("ok", True))
            return StageResult(
                stage="qa",
                ok=ok,
                message=(
                    f"质检通过（{report.get('stats', {}).get('entries', 0)} 条文本）"
                    if ok
                    else f"质检发现 {len(report.get('issues', []))} 个问题"
                ),
                error="" if ok else "质检未通过，详见质检报告",
                stats=report.get("stats", {}),
            )

        return self._run("qa", go)

    def stage_apply(self) -> StageResult:
        def go() -> StageResult:
            ad = self._adapter()
            units = self.ws.load_units()
            entries = {e.uid: e for e in self.ws.load_entries()}

            # 只有真正译出来**且占位符完好**的才回写；原文一律不动。
            translations: dict[str, str] = {}
            risky: list[str] = []
            for u in units:
                e = entries.get(u.uid)
                if e is None or not e.target.strip():
                    continue
                if not e.placeholder_ok:
                    risky.append(u.uid)
                    continue
                translations[u.uid] = e.target

            if risky:
                self.bus.log(
                    f"{len(risky)} 条译文占位符校验未通过，已**拒绝回写**"
                    "（避免游戏内文本错乱）。请在文本审校页修正后重试。",
                    stage="apply",
                    severity=Severity.WARN,
                )

            font_patches: dict[str, Path] = {}
            for patch in font_patch_records(self.ws):
                font_patches[patch["font_id"]] = patch["out_path"]

            rebuilt: dict[str, Path] = {}
            rebuilt_root = self.ws.p("images", "rebuilt")
            for rec in self.ws.read_json("images/localize.json", []) or []:
                if rec.get("ok") and rec.get("changed") and rec.get("path"):
                    fp = rebuilt_root / rec["path"]
                    if fp.is_file():
                        rebuilt[rec["path"]] = fp

            out_dir = self.ws.out_dir
            self.bus.log(
                f"回写 {len(translations)} 条文本、{len(rebuilt)} 张贴图、"
                f"{len(font_patches)} 个字体 → {out_dir}",
                stage="apply",
            )
            res: ApplyResult = ad.apply(
                self.ws.effective_source,
                out_dir,
                units,
                translations,
                font_patches=font_patches,
                rebuilt_images=rebuilt,
            )
            for w in res.warnings[:100]:
                self.bus.log(w, stage="apply", severity=Severity.WARN)
            if not res.ok:
                return StageResult(
                    stage="apply",
                    ok=False,
                    error=res.error or "回写失败",
                    stats={"written": res.files_written, "skipped": res.files_skipped},
                )
            # ---- 最后一步：把改动打回归档 ----
            # 顺序很重要：必须**在** ad.apply() 写进解包树之后再打回，
            # 否则归档里还是旧内容。这也是为什么回写阶段要负责这件事，
            # 而不是解包阶段。
            repack_stats = self._repack_archives()

            return StageResult(
                stage="apply",
                ok=True,
                message=f"已写入 {res.files_written} 个文件",
                stats={
                    "written": res.files_written,
                    "skipped": res.files_skipped,
                    "translations": len(translations),
                    "images": len(rebuilt),
                    "fonts": len(font_patches),
                    "refused": len(risky),
                    "out_dir": str(out_dir),
                    **repack_stats,
                },
            )

        return self._run("apply", go)

    def _repack_archives(self) -> dict[str, Any]:
        """把解包树里改过的文件打回原归档（带备份）。

        没解过包就什么都不做 —— 这是多数游戏的情况。

        **归档回写失败不抛异常**，只记警告：`out/` 目录里已经有一份完整
        的产物（那是用户真正交付的东西），归档回写只是"顺手帮你装回游戏"。
        因为一个归档写不进去就让整条流水线报失败，会让用户以为汉化没做成。
        """
        from ..archives import UnpackedArchive, repack

        if not self.ctx.config.pack.repack:
            return {"repacked": 0, "repack_skipped_by_config": True}

        records = self.ws.load_unpacked()
        if not records:
            return {"repacked": 0}

        done: list[str] = []
        failed: list[str] = []
        for rec in records:
            if not rec.get("writable", True):
                failed.append(f"{Path(rec['source']).name}（格式不支持回写）")
                continue
            unpacked = UnpackedArchive(
                source=Path(rec["source"]),
                dest=Path(rec["dest"]),
                kind=str(rec.get("kind", "")),
                entries=int(rec.get("entries", 0)),
                written=int(rec.get("written", 0)),
                writable=bool(rec.get("writable", True)),
            )
            try:
                # 必须传 out_dir 作为改动来源，**不能**让 repack 去比对
                # 解包树：流水线的 apply() 把产物写进 out/，
                # 解包树里其实一个字都没改。第一版就是漏了这个参数，
                # 于是 changed_members() 返回空 → repacked=0 →
                # 所有阶段都 ok，归档里却还是原文。
                changed, note = repack(unpacked, changes_from=self.ws.out_dir)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{unpacked.source.name}（{exc}）")
                self.ws.log_line("apply", f"归档回写失败 {unpacked.source.name}：{exc}")
                continue
            if changed:
                done.append(note)
                self.bus.log(note, stage="apply")
            else:
                self.ws.log_line("apply", f"{unpacked.source.name}：{note}")

        for f in failed:
            self.bus.log(
                f"归档未能回写：{f}。out/ 目录里的产物是完整的，"
                "可以把文件手工复制进游戏目录",
                stage="apply", severity=Severity.WARN,
            )
        return {
            "repacked": len(done),
            "repack_notes": done,
            "repack_failed": failed,
        }

    # ==================================================================
    # 全流程
    # ==================================================================

    def run_all(self, *, only_pending: bool = True) -> list[StageResult]:
        """按顺序跑完整流程。任一步失败即中止。

        中止而不是继续：下游阶段建立在错误数据上，继续跑只会产出
        更难排查的坏结果，还会白白烧几个小时的 GPU 时间。
        """
        self.results = []
        self.bus.log("=" * 60, stage="pipeline")
        self.bus.log(f"开始汉化项目：{self.ws.project.name}", stage="pipeline")
        self.bus.log("=" * 60, stage="pipeline")
        t0 = time.time()

        self.stage_unpack()
        self.stage_detect()
        self.stage_extract()
        self.stage_images_scan()
        self.stage_translate(only_pending=only_pending)
        self.stage_fonts()
        self.stage_images_localize()
        self.stage_qa()
        self.stage_apply()

        dur = time.time() - t0
        self.bus.log(f"全部完成，用时 {dur:.1f} 秒", stage="pipeline")
        self.bus.emit(
            Event(
                "done",
                stage="pipeline",
                message=f"用时 {dur:.1f} 秒",
                data={"out_dir": str(self.ws.out_dir), "duration_s": dur},
            )
        )
        return self.results

    def summary(self) -> dict[str, Any]:
        return {
            "project": self.ws.project.id,
            "stages": [r.to_dict() for r in self.results],
            "ok": all(r.ok for r in self.results) if self.results else False,
        }


def font_patch_records(ws: Workspace) -> list[dict[str, Any]]:
    """挑出**可以回写**的字体补丁，返回 ``[{font_id, out_path, action}]``。

    判据是"注入成功且有产物"，**不是**某个具体的 action 字符串。

    为什么值得单独写成函数：``stage_fonts`` 曾经把 ``action`` 硬编码成
    ``"merge"``，而 ``stage_apply`` 又按 ``action == "merge"`` 过滤 ——
    于是 ``replace`` 与 ``fallback_only`` 两种策略的产物虽然
    ``ok=True``、``out_path`` 也有值，却**永远不会被回写**：
    文件躺在工作区里，游戏里没有任何变化，而且不报错。
    这种"静默丢弃产物"的 bug 只能靠针对性测试守住，
    所以把判据提出来，让它可被单独断言。

    ``action == "none"`` 有两种含义，两种都不该回写：
    原字体已全覆盖（无需替换）、或注入失败。
    """
    out: list[dict[str, Any]] = []
    for p in ws.read_json("fonts/patches.json", []) or []:
        if not isinstance(p, dict):
            continue
        if not p.get("ok") or not p.get("out_path"):
            continue
        if p.get("action") in ("none", "", None):
            continue
        out.append(
            {
                "font_id": p.get("font_id") or Path(str(p["out_path"])).name,
                "out_path": Path(str(p["out_path"])),
                "action": str(p.get("action")),
            }
        )
    return out


__all__ = [
    "Pipeline",
    "PipelineError",
    "STAGES",
    "STAGE_LABELS",
    "StageResult",
    "font_patch_records",
]
