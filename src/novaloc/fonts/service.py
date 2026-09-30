"""字体服务：把"游戏里不会出现口口口"这件事做成一条可复现的流水线。

这是本工具**最核心的承诺**。整个流程是：

1. **收集项目真实字符集** —— 译文 + 原文 + 游戏 UI 必然会渲染到的安全字符
   （标点、箭头、货币、图形符号等）。
   只按译文算是不够的：游戏里总有没被翻译的串（人名、型号、代码），
   它们仍然要用这个字体渲染出来。漏掉它们 = 界面上出现口口口。

2. **审计原字体的覆盖情况** —— 逐字符查 cmap，列出缺哪些。

3. **贪心规划补充来源** —— 因为**没有任何单一字体能覆盖全部字符**
   （实测：SimHei 缺 ``¶†‡•‹›↔↕♪♫✓✔✕✖❤``；
   SimSun/微软雅黑/等线缺 ``↔↕♪♫✓✔✕✖❤``；
   霞鹜新晰黑缺 ``✔✕✖❤``），必须挑最少的一组字体做集合覆盖。

4. **按优先级合并** —— 每个缺字取"第一个拥有它的字体"的字形。

5. **QA 硬校验** —— 逐字验证：在 cmap 里、能渲染出墨迹、和 .notdef 不同、
   没有字形 ID 冲突、能被 FreeType 加载。

6. **覆盖不达标就硬失败**（:class:`CharsetCoverageError`）——
   **绝不写出半成品字体**。宁可报错让用户处理，也不能让用户进游戏看到口口口。

关于字体授权（重要）
--------------------
如果本工具只产出**渲染好的贴图**、从不随包分发字体文件，那么 OFL 的
署名与传染条款都不触发。所以默认策略是 `bundle_fonts=False`：
字体只在本机下载与合并，产物留在用户自己的数据目录里。
`BUNDLE_OK_LICENSES` 之外的字体（IPA、CC-BY-ND、以及微软/华为等系统字体）
一律只允许本机使用，禁止再分发。
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import bundled_fonts_dir, fonts_dir
from ..core.registry import Context
from . import catalog
from .charset import (
    CharsetCoverageError,
    CharsetPlan,
    assert_plannable,
    build_required_charset,
    default_supplement_candidates,
    plan_charset,
)
from .coverage import load_font_info
from .downloader import download
from .merge import MergeReport, merge_fonts_multi, replace_with_font, subset_font_for_chars
from .qa import FontQAReport, verify_font

log = logging.getLogger(__name__)


@dataclass
class FontAudit:
    """一次字体覆盖审计的结果。"""

    font_path: Path
    family: str = ""
    total: int = 0
    covered: int = 0
    missing: str = ""

    @property
    def coverage(self) -> float:
        return self.covered / self.total if self.total else 1.0

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        s = f"{self.family or self.font_path.name}: {self.coverage:.3%}（{self.covered}/{self.total}）"
        if self.missing:
            s += f" 缺 {len(self.missing)} 字：{self.missing[:60]}"
        return s


@dataclass
class PatchResult:
    """一次字体注入的完整结果。"""

    ok: bool = False
    out_path: Path | None = None
    audit_before: FontAudit | None = None
    audit_after: FontAudit | None = None
    plan: CharsetPlan | None = None
    merge: MergeReport | None = None
    qa: FontQAReport | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def summary(self) -> str:
        if self.error:
            return f"失败：{self.error}"
        bits = [f"coverage {self.audit_before.coverage:.2%}→{self.audit_after.coverage:.2%}"
                if self.audit_before and self.audit_after else "coverage ?"]
        if self.plan:
            bits.append(f"补充字体 {len(self.plan.sources)} 个")
        if self.qa:
            bits.append("QA 通过" if self.qa.ok else f"QA 有 {len(self.qa.issues)} 项问题")
        return "  ".join(bits)


class FontService:
    """字体下载、审计、合并、QA 的编排器。"""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.config
        self._downloaded: dict[str, Path] = {}
        # 记录"已经试过但失败"的字体，避免同一次运行里反复联网重试。
        # 本机 hosts 屏蔽了 GitHub，重试一次要几秒钟、还会刷屏，
        # 不缓存失败会让整个流程慢到不可用。
        self._failed: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def audit(self, font_path: Path, required: str, face_index: int = 0) -> FontAudit:
        """审计一个字体对 ``required`` 的覆盖情况。"""
        info = load_font_info(font_path, face_index)
        if info is None or not info.cmap:
            return FontAudit(
                font_path=font_path, total=len(set(required)),
                covered=0, missing=required,
            )
        chars = sorted(set(required))
        missing = [c for c in chars if not info.has_char(c)]
        return FontAudit(
            font_path=font_path,
            family=info.family,
            total=len(chars),
            covered=len(chars) - len(missing),
            missing="".join(missing),
        )

    def build_charset(
        self,
        translated_texts: list[str] | None = None,
        source_texts: list[str] | None = None,
        extra: str = "",
        *,
        include_ui_safe: bool | None = None,
    ) -> str:
        """汇总项目需要的字符集。"""
        ui_safe = True if include_ui_safe is None else include_ui_safe
        return build_required_charset(
            translated_texts=translated_texts,
            source_texts=source_texts,
            extra=extra,
            include_ui_safe=ui_safe,
        )

    # ------------------------------------------------------------------
    # 字体获取
    # ------------------------------------------------------------------

    def ensure_font(self, spec: catalog.FontSpec, *, allow_download: bool | None = None) -> Path | None:
        """确保某个字体在本机可用，返回路径；不可用返回 ``None``。

        查找顺序：已下载缓存 → 项目自带 → 系统已安装 → 联网下载。
        """
        if spec.id in self._downloaded and self._downloaded[spec.id].is_file():
            return self._downloaded[spec.id]
        if spec.id in self._failed:
            return None

        for d in (self._font_cache_dir(), bundled_fonts_dir()):
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                    if self._family_matches(f, spec.family):
                        self._downloaded[spec.id] = f
                        return f

        sys_font = self._find_system_font(spec.family)
        if sys_font is not None:
            self._downloaded[spec.id] = sys_font
            return sys_font

        allow = self.cfg.font.allow_download if allow_download is None else allow_download
        if not allow or not spec.direct_url:
            self._failed[spec.id] = "不允许下载或没有直链"
            log.debug("字体 %s 不在本机且不允许下载", spec.display_zh)
            return None
        try:
            dest = self._font_cache_dir() / f"{spec.id}{Path(spec.direct_url).suffix or '.ttf'}"
            download(spec.direct_url, dest, min_bytes=10240)
            self._downloaded[spec.id] = dest
            return dest
        except Exception as exc:  # noqa: BLE001
            self._failed[spec.id] = str(exc)[:200]
            log.warning("字体 %s 下载失败（本次运行不再重试）：%s", spec.display_zh, str(exc)[:160])
            return None

    def _font_cache_dir(self) -> Path:
        d = fonts_dir() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _family_matches(self, path: Path, family: str) -> bool:
        info = load_font_info(path)
        if info is None:
            return False
        return family.lower() in (info.family or "").lower()

    def _find_system_font(self, family: str) -> Path | None:
        """在系统字体目录里按家族名找。找不到返回 None。"""
        import platform

        dirs: list[Path] = []
        if platform.system() == "Windows":
            dirs.append(Path(r"C:\Windows\Fonts"))
        elif platform.system() == "Darwin":
            dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts")]
        else:
            dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
        for d in dirs:
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                    if self._family_matches(f, family):
                        return f
        return None

    def supplement_candidates(self, base_font: Path | None = None) -> list[Path]:
        """可用的补充字体候选（按风格优先级排序）。"""
        cands = default_supplement_candidates(bundled_fonts_dir(), [self._font_cache_dir()])
        # 把配置里指定的字体排到前面，保证风格优先
        prefer = [
            getattr(self.cfg.font, "ui_font", ""),
            getattr(self.cfg.font, "display_font", ""),
            getattr(self.cfg.font, "dialog_font", ""),
        ]
        prefer = [p for p in prefer if p]
        if not prefer:
            return cands
        head: list[Path] = []
        tail: list[Path] = []
        for c in cands:
            info = load_font_info(c)
            fam = (info.family if info else "") or ""
            if any(p.lower() in fam.lower() for p in prefer):
                head.append(c)
            else:
                tail.append(c)
        return head + tail

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def _best_single_font(self, candidates: list[Path], required: str) -> tuple[Path | None, float]:
        """挑出**单独一个**覆盖最好的字体，返回 ``(路径, 覆盖率)``。

        ``replace`` / ``fallback_only`` 策略只能用一个字体，
        所以不能像 merge 那样"谁有就取谁的"。这里必须实测覆盖，
        否则会出现"换了字体反而缺了 7 个字"的情况 ——
        实测霞鹜新晰黑缺 ``░▒✔✕✖✘❤``，直接拿来当唯一字体就不达标。
        """
        best: Path | None = None
        best_cov = -1.0
        for p in candidates:
            try:
                a = self.audit(p, required)
            except Exception:  # noqa: BLE001
                continue
            if a.coverage > best_cov:
                best, best_cov = p, a.coverage
        return best, max(0.0, best_cov)

    def patch_font(
        self,
        base_font: Path,
        required: str,
        *,
        out_path: Path | None = None,
        base_face_index: int = 0,
        candidates: list[Path] | None = None,
        max_sources: int = 4,
        strategy: str | None = None,
        verify: bool = True,
        allow_download: bool | None = None,
    ) -> PatchResult:
        """把中文字形注入游戏原字体，产出可直接替换的字体文件。

        ``strategy``：
        * ``merge``（默认）—— 保留原字体风格，只补缺字。**推荐**，
          因为游戏原有字体往往和 UI 设计是一套的。
        * ``replace`` —— 整体换成中文字体，适合原字体本身简陋或损坏的情况。
        * ``fallback_only`` —— 只产出中文字体，由引擎侧配置回退。
        """
        t0 = time.time()
        res = PatchResult()
        strat = strategy or getattr(self.cfg.font, "strategy", "merge")
        min_cov = float(getattr(self.cfg.font, "min_glyph_coverage", 0.999))

        if not base_font.is_file():
            res.error = f"基础字体不存在：{base_font}"
            return res

        res.audit_before = self.audit(base_font, required, base_face_index)
        log.info("原字体覆盖：%s", res.audit_before.summary())

        # ---- 规划 ----
        cands = candidates if candidates is not None else self.supplement_candidates(base_font)
        # 需要联网获取的字体：**只考虑可再分发的**
        # （OFL/Apache/MIT/公有领域）。IPA、CC-BY-ND 以及微软/华为等
        # 系统字体只允许本机已装的情况下使用，绝不自动下载。
        if allow_download is not False and self.cfg.font.allow_download:
            for spec in catalog.redistributable():
                if not spec.direct_url:
                    continue
                p = self.ensure_font(spec, allow_download=allow_download)
                if p is not None and p not in cands:
                    cands.append(p)
        if not cands:
            res.error = "没有任何可用的中文字体候选（请检查网络或字体目录）"
            return res

        plan = plan_charset(
            required, base_font, cands,
            base_face_index=base_face_index,
            max_sources=max_sources,
        )
        res.plan = plan
        log.info("字符集规划：\n%s", plan.summary())

        if plan.still_missing:
            res.warnings.append(
                f"仍有 {len(plan.still_missing)} 个字符无字体覆盖：{''.join(plan.still_missing[:60])}"
            )

        # ---- 合并 / 替换 ----
        dest = out_path or (fonts_dir() / "patched" / f"{base_font.stem}_novaloc{base_font.suffix or '.ttf'}")
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            if strat == "replace":
                # 单字体策略必须挑覆盖最好的那个，不能盲取规划器的第一个
                src, cov = self._best_single_font(
                    list(dict.fromkeys(cands + plan.source_paths)), required
                )
                if src is None:
                    res.error = "replace 策略需要至少一个中文字体候选"
                    return res
                if cov < min_cov:
                    res.error = (
                        f"没有任何单个字体能覆盖 {min_cov:.1%} 的字符集"
                        f"（最好的 {src.name} 只有 {cov:.3%}）。"
                        f"请改用 merge 策略，或补充更多中文字体候选。"
                    )
                    return res
                shutil.copy2(src, dest)
                res.merge = MergeReport(
                    ok=True, out_path=dest, method="replace",
                    coverage_before=res.audit_before.coverage, coverage_after=cov,
                    sources_used=[{"path": str(src), "covers": "all"}],
                )
            elif strat == "fallback_only":
                src, cov = self._best_single_font(
                    list(dict.fromkeys(cands + plan.source_paths)), required
                )
                if src is None:
                    res.error = "fallback_only 策略需要至少一个中文字体候选"
                    return res
                if cov < min_cov:
                    res.error = (
                        f"没有任何单个字体能覆盖 {min_cov:.1%} 的字符集"
                        f"（最好的 {src.name} 只有 {cov:.3%}）。请改用 merge 策略。"
                    )
                    return res
                subset_font_for_chars(src, dest, required)
                res.merge = MergeReport(
                    ok=True, out_path=dest, method="fallback_only",
                    coverage_before=res.audit_before.coverage, coverage_after=cov,
                )
            else:
                res.merge = merge_fonts_multi(
                    base_font,
                    plan.source_paths,
                    dest,
                    required,
                    base_face_index=base_face_index,
                )
        except CharsetCoverageError:
            raise
        except Exception as exc:  # noqa: BLE001
            res.error = f"字体合并失败：{exc}"
            return res

        if res.merge is None or not res.merge.ok or res.merge.out_path is None:
            # 合并阶段就失败了。最常见的原因是"规划阶段就没找到能覆盖全部
            # 字符的字体"。这时要把**真正的原因**（缺哪些字、多不多）
            # 报出来，而不是只说一句"合并未成功产出" —— 后者会让用户
            # 完全不知道该补什么字体。
            if plan.still_missing:
                res.error = (
                    f"字符集里有 {len(plan.still_missing)} 个字符没有任何候选字体能提供，"
                    f"无法生成完整字体：{''.join(plan.still_missing[:60])}"
                )
                res.warnings.append("为避免游戏内出现口口口，本次结果**未采用**")
            else:
                res.error = "字体合并未成功产出"
            return res
        dest = res.merge.out_path
        res.warnings.extend(res.merge.warnings)

        # ---- QA 硬校验 ----
        res.audit_after = self.audit(dest, required)
        if verify:
            try:
                res.qa = verify_font(dest, required, render_size=40)
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"QA 执行异常：{exc}")

        # ---- 达标判定：这是"防口口口"的最后一道闸 ----
        if res.audit_after.coverage < min_cov:
            res.error = (
                f"覆盖率 {res.audit_after.coverage:.3%} 低于要求的 {min_cov:.3%}；"
                f"缺 {len(res.audit_after.missing)} 字：{res.audit_after.missing[:60]}"
            )
            res.warnings.append("为避免游戏内出现口口口，本次结果**未采用**")
            return res
        if res.qa is not None and not res.qa.ok:
            res.error = f"QA 未通过：{res.qa.summary()}"
            res.warnings.append("为避免游戏内出现口口口，本次结果**未采用**")
            return res
        if plan.still_missing:
            res.error = (
                f"字符集规划阶段仍有 {len(plan.still_missing)} 字无法覆盖："
                f"{''.join(plan.still_missing[:60])}"
            )
            res.warnings.append("为避免游戏内出现口口口，本次结果**未采用**")
            return res

        res.ok = True
        res.out_path = dest
        res.elapsed_s = time.time() - t0
        log.info("字体注入完成：%s（%s）", dest, res.summary)
        return res

    # ------------------------------------------------------------------

    def ensure_charset_plannable(self, required: str, base_font: Path | None = None,
                                 candidates: list[Path] | None = None) -> CharsetPlan:
        """规划并**在无法覆盖时抛错**，用于流程早期拦下问题。"""
        cands = candidates if candidates is not None else self.supplement_candidates(base_font)
        plan = plan_charset(required, base_font, cands)
        assert_plannable(plan)
        return plan


__all__ = ["FontAudit", "FontService", "PatchResult"]
