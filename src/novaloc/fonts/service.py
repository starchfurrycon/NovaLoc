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

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.paths import bundled_fonts_dir, fonts_dir
from ..core.registry import Context
from . import catalog
from .charset import (
    CharsetCoverageError,
    CharsetPlan,
    assert_plannable,
    build_charset_tiers,
    build_required_charset,
    default_supplement_candidates,
    plan_charset,
)
from .coverage import load_font_info
from .downloader import DownloadError, download
from .merge import (
    MergeReport,
    merge_fonts_multi,
    replace_with_font,
    subset_font_for_chars,
)
from .qa import FontQAReport, verify_font

log = logging.getLogger(__name__)

#: 合法字体文件的开头（`sfntVersion` / WOFF / WOFF2 / TTC）。
#:
#: ## 为什么需要这个，而不是靠扩展名或文件大小
#:
#: 实测 `fonts/cache/lxgw-wenkai-screen.ttf` 的开头是 `<b'<!DO'`
#: —— 一个 **HTML 错误页**被当成了字体，且**永久缓存**在那里：
#: 它 157 KB，轻松通过了下载器那条 `min_bytes=10240` 的检查，
#: 之后每一轮 `fonts` 都认为"本机已有这个字体"，永远不再重新下载。
#:
#: **一次下载失败让这个字体永久坏掉**，而且直到合并阶段才报错
#: （`Not a TrueType or OpenType font`）—— 报错点离病因很远。
#:
#: 所以校验必须**按内容判**，不能按长度或扩展名判。
_FONT_MAGICS = (
    b"\x00\x01\x00\x00",  # TrueType
    b"OTTO",              # CFF / OpenType
    b"true",              # 旧 Mac TrueType
    b"ttcf",              # TrueType Collection
    b"wOFF",              # WOFF
    b"wOF2",              # WOFF2
)


def _looks_like_font_file(path: Path) -> bool:
    """按魔数判断这是不是一个真的字体文件（而不是 HTML 错误页）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in _FONT_MAGICS
    except OSError:
        return False


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


#: 系统字体的进程级索引缓存。
#: 键是"各系统字体目录的 mtime 摘要"，值是 ``[{path, family, cmap}]``。
#: 目的：把"每次按家族名找字体都要 rglob + 解析整个字体目录"
#: （实测 0.3~1.5 秒/次）降为**一次**扫描。
#: 系统字体目录列表复用 :func:`..fonts.coverage.system_font_dirs`，
#: 不再重复实现一份平台判断（早先差点写出两份不一致的实现）。
_SYSTEM_FONT_INDEX: dict[str, list[dict[str, Any]]] = {}


def _system_font_index(*, refresh: bool = False) -> list[dict[str, Any]]:
    """扫描系统字体目录并缓存。返回 ``[{path, family, cmap}, ...]``。

    缓存键由**每个字体文件的 (名字, mtime, 大小)** 摘要而成，
    不是目录自身的 mtime —— NTFS 上往目录里复制新文件**不会**改变
    该目录的 mtime（目录 mtime 只在目录项本身被改动时更新，
    而"新建文件"在 Windows 上并不稳定地触发它）。
    用目录 mtime 做键会让"用户刚装了字体但进程还没重启"时
    永远看不到新字体，而用户装字体的**唯一**目的就是让工具找到它。

    摘要计算只 stat，不读文件内容，所以比重新解析整个字体目录便宜得多。
    """
    from .coverage import system_font_dirs

    use = system_font_dirs()
    parts: list[str] = []
    for d in use:
        try:
            ents = sorted(
                (p.name, int(p.stat().st_mtime), p.stat().st_size)
                for p in d.iterdir()
                if p.is_file()
            )
            parts.append(f"{d}:" + ";".join(f"{n}@{m}#{s}" for n, m, s in ents))
        except OSError:
            parts.append(str(d))
    key = hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()
    if not refresh and key in _SYSTEM_FONT_INDEX:
        return _SYSTEM_FONT_INDEX[key]

    index: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in use:
        try:
            files = sorted(d.rglob("*"))
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                continue
            try:
                rp = str(f.resolve())
            except OSError:
                continue
            if rp in seen:
                continue
            seen.add(rp)
            try:
                info = load_font_info(f)
            except Exception:  # noqa: BLE001
                continue
            if info is None:
                continue
            index.append({
                "path": f,
                "family": info.family or "",
                "cmap": info.cmap or {},
            })
    _SYSTEM_FONT_INDEX.clear()
    _SYSTEM_FONT_INDEX[key] = index
    return index


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

    def build_charset_tiers(
        self,
        translated_texts: list[str] | None = None,
        source_texts: list[str] | None = None,
        extra: str = "",
        *,
        include_ui_safe: bool | None = None,
    ) -> tuple[str, str]:
        """返回 ``(全部字符, 真实文本需要的字符)``。

        第二个值是**硬失败**判据：只有它缺字才算玩家会看到口口口。
        两者之差是工具猜的装饰符号（`UI_SAFE_CHARS`），补不上只记警告。
        """
        ui_safe = True if include_ui_safe is None else include_ui_safe
        return build_charset_tiers(
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
                    # 缓存里可能有**坏文件**（历史版本下过一个 HTML 错误页进来）。
                    # 坏文件不能只看扩展名就当字体用，否则它对每一轮都
                    # "看起来是已缓存"，永远不重新下载 —— 一次失败永久坏掉。
                    # 先按魔数排掉，坏的就删掉让它有机会重新下载。
                    if not _looks_like_font_file(f):
                        log.warning(
                            "缓存里的 %s 不是合法字体（开头 %r），已删除以便重新获取",
                            f.name,
                            f.read_bytes()[:8],
                        )
                        f.unlink(missing_ok=True)
                        continue
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
            # ⚠️ **下载完必须验证它真的是字体。**
            #
            # ## 事故：一个 HTML 错误页被当成字体**永久缓存**下来
            #
            # 实测发现 `fonts/cache/lxgw-wenkai-screen.ttf` 的
            # `sfntVersion` 是 `<b'<!DO'` —— 一个 HTML 错误页。
            # 它 157 KB，**轻松通过了 `min_bytes=10240`**，
            # 于是被当成"已缓存的字体"写进磁盘。
            #
            # 后果不是"某个字体用不了"这么简单：
            # 它对**每一轮** `fonts` 阶段都是"本机已有这个字体"，
            # 于是永远不再重新下载 —— **一次失败让这个字体永久坏掉**。
            # 而且直到合并阶段才炸（`Not a TrueType or OpenType font`），
            # 报错点离病因很远。
            #
            # 所以：**只检查大小的校验挡不住错误页**。
            # 内容类型必须按内容本身判，不能按长度判。
            if not _looks_like_font_file(dest):
                head = dest.read_bytes()[:12]
                dest.unlink(missing_ok=True)
                raise DownloadError(
                    f"下载到的内容不是字体文件（开头 {head!r}，疑似错误页），已丢弃"
                )
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
        for info in _system_font_index():
            if family.lower() in info["family"].lower():
                return info["path"]
        return None

    def find_preferred_cjk_font(self, families: list[str] | None = None) -> Path | None:

        """**一次扫描**挑出最合适的中文字体。

        为什么必须一次扫描：``_find_system_font`` 每次都要 rglob 整个系统
        字体目录并逐个解析 name 表（实测 0.3~1.5 秒/次）。贴图管线要为
        十几二十个家族名各找一个字体，逐次调用就是**每张图十几秒**的纯浪费，
        而且是每个阶段都重复付。

        返回第一个匹配到的家族；都不匹配时，退而返回任何能覆盖
        "游戏常用汉字+符号" 的已装字体。
        """
        index = _system_font_index()
        for fam in (families or []):
            if not fam:
                continue
            low = fam.lower()
            for info in index:
                if low in info["family"].lower():
                    return info["path"]
        # 兜底：挑一个汉字覆盖最好的
        best: tuple[int, Path] | None = None
        probe = "你好世界游戏开始设置退出攻击生命魔法金币确定取消"
        for info in index:
            cmap = info.get("cmap") or {}
            hits = sum(1 for c in probe if ord(c) in cmap)
            if hits and (best is None or hits > best[0]):
                best = (hits, info["path"])
        if best and best[0] >= len(probe) * 0.8:
            return best[1]
        return None


    def supplement_candidates(self, base_font: Path | None = None) -> list[Path]:
        """可用的补充字体候选（按风格优先级排序）。

        搜索范围：随包字体目录 → ``<data_root>/fonts/cache`` →
        ``<data_root>/fonts`` 根目录 → 系统字体。

        早先漏了 ``fonts/`` **根目录**：用户按文档把字体丢进
        ``D:\\NovaLoc\\fonts\\`` 却完全不生效，而 ``fonts/cache``
        反而在扫描范围内 —— 表现是"我明明放了字体，工具说没有"。
        """
        cands = default_supplement_candidates(bundled_fonts_dir(), [self._font_cache_dir()])
        # fonts/ 根目录（用户手工放字体的地方）
        root = fonts_dir()
        for p in sorted(root.glob("*.tt[fc]")) + sorted(root.glob("*.ot[fc]")):
            if p.is_file() and p not in cands:
                cands.append(p)
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
        optional: str = "",
    ) -> PatchResult:
        """把中文字形注入游戏原字体，产出可直接替换的字体文件。

        ``strategy``：
        * ``merge``（默认）—— 保留原字体风格，只补缺字。**推荐**，
          因为游戏原有字体往往和 UI 设计是一套的。
        * ``replace`` —— 整体换成中文字体，适合原字体本身简陋或损坏的情况。
        * ``fallback_only`` —— 只产出中文字体，由引擎侧配置回退。

        ``optional``：**可放弃**的字符（工具猜界面会渲染的装饰符号）。
        它们能补就补，补不上只记警告，**不导致失败** ——
        详见 :func:`novaloc.fonts.charset.plan_charset`。

        ## ★ 空白字形的自动重试（``force_chars`` 由内部推导）

        见 :func:`_blank_glyph_retry` —— 基础字体 cmap 里有码点、
        但字形是空的那种情况（日文手写体常见）。
        """
        t0 = time.time()
        res = PatchResult()
        strat = strategy or getattr(self.cfg.font, "strategy", "merge")
        min_cov = float(getattr(self.cfg.font, "min_glyph_coverage", 0.999))
        #: 「顺带」那一侧的字符：能补就补，补不上**不该**让整份字体失败。
        #: 缺字判定与 QA 校验都必须把这一侧排除在外，否则分两层就白分了。
        opt_set = {c for c in optional if c}

        if not base_font.is_file():
            res.error = f"基础字体不存在：{base_font}"
            return res

        res.audit_before = self.audit(base_font, required, base_face_index)
        log.info("原字体覆盖：%s", res.audit_before.summary())

        # ---- 规划 ----
        cands = candidates if candidates is not None else self.supplement_candidates(base_font)

        # 基准字体**不能**当自己的补充候选。
        # 否则规划器会看到"所有缺字都能被覆盖"（其实就是基准字体自己），
        # 于是判定 100% 覆盖、source_paths 为空，随后合并阶段发现
        # "没有任何补充字体提供缺失字形"而报错 —— 一个纯粹自相矛盾的失败。
        # 而 `supplement_candidates()` 的扫描目录里**天然包含**基准字体
        # （用户把游戏原字体放进了 fonts/ 或 fonts/cache/），所以这不是
        # 假设性问题，是必然踩到的路径。
        try:
            base_resolved = base_font.resolve()
        except OSError:
            base_resolved = base_font
        cands = [
            c for c in cands
            if (c.resolve() if c.exists() else c) != base_resolved
        ]
        # 去重但保持顺序（同一字体可能同时出现在 cache 与 fonts/ 根目录）
        seen_c: set[str] = set()
        uniq: list[Path] = []
        for c in cands:
            try:
                k = str(c.resolve())
            except OSError:
                k = str(c)
            if k not in seen_c:
                seen_c.add(k)
                uniq.append(c)
        cands = uniq

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
            optional=optional,
        )
        res.plan = plan
        log.info("字符集规划：\n%s", plan.summary())

        if plan.still_missing:
            res.warnings.append(
                f"仍有 {len(plan.still_missing)} 个字符无字体覆盖：{''.join(plan.still_missing[:60])}"
            )
        if plan.optional_missing:
            # 只警告，不失败：这些是工具**猜**界面会渲染的装饰符号，
            # 不是任何真实文本里的字符。为它们中止整个字体阶段
            # 是判据用错了地方（真实事故：`※ ‥ ′ ″` 让整轮硬失败，
            # 而它们在 65570 条文本里出现 0 次）。
            res.warnings.append(
                f"{len(plan.optional_missing)} 个**猜测用**的装饰符号本机字体没有"
                f"（不影响任何真实文本）：{''.join(plan.optional_missing[:40])}"
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
            # ★ CFF（`.otf`）字体无法走合并 —— 退化为"整体替换"。
            #
            # 合并器要求两边都有 `glyf`/`loca`（TrueType 轮廓），
            # 而 CFF 字体的轮廓在 `CFF ` 表里，于是报
            # 「表集合归一化失败：字体缺少合并必需的表：['glyf', 'loca']」。
            # 实测 DemonsRoots 的 `ship.otf` 就是这样（4.7 MB，CFF）。
            #
            # 原先是**直接失败**，后果是这条字体原样留着：它只有 77.2%
            # 的中文覆盖 ⇒ 游戏里凡是用到它的地方就是一片口口口。
            # 而"整份字体换成一个完整中文字体"至少**不会缺字** ——
            # 代价是那个字体的字形风格不再保留，但缺字是"坏"，
            # 换风格是"不同"，两害相权取轻。
            #
            # 只在**确实是 CFF 轮廓**时才退化，别的合并失败原因照旧上报 ——
            # 否则一个真正的规划错误会被"替换"悄悄掩盖过去。
            if "glyf" in str(exc) and "loca" in str(exc):
                fallback = self._cff_replace_fallback(
                    base_font, required, dest, cands, opt_set, min_cov
                )
                if fallback is not None:
                    res.merge = fallback
                    res.warnings.append(
                        "该字体是 CFF（`.otf`）轮廓，无法与中文 TrueType 字体合并；"
                        "已按「整体替换」处理（不保留原字形风格，但不缺字）。"
                    )
                else:
                    res.error = f"字体合并失败：{exc}"
                    return res
            else:
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
        #
        # ★ 只校验 `required`（**真实文本**需要的字符），不校验 `optional`。
        #
        # 这是"分两层"契约在 QA 这一侧的对应实现，缺了它就会出现
        # **自相矛盾**：规划器按 `optional` 放行了那几个补不上的装饰符号
        # （`¢¥µ·¿í ※ ‥ ′ ″`），QA 却把它们算成"空白字形"而硬失败 ——
        # 实测 DemonsRoots 的 `koin.ttf` 因此报"空白 1259"，
        # 而其中**真实文本需要的字符已经全部有字形**。
        #
        # 症状极具误导性：日志说"空白 1259"，看起来字体烂得不能用，
        # 实际那 1259 个里绝大多数是**玩家永远看不到的**装饰符号
        # （在 45943 条文本里出现 0 次）。
        req_only = "".join(c for c in required if c not in opt_set)
        res.audit_after = self.audit(dest, required)
        if verify:
            try:
                res.qa = verify_font(dest, req_only, render_size=40)
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"QA 执行异常：{exc}")
            # optional 侧的空白单独记一条警告，别让它悄悄消失 ——
            # 用户需要知道"哪些猜出来的符号没补上"。
            self._warn_optional_blanks(dest, required, opt_set, res)

        # ★ 空白字形重试：base 的 cmap 里有这个码点、但字形是空的。
        # 详见 `_blank_glyph_retry` 的说明。只在"确实因此失败"时才做，
        # 且**只做一次**，避免把一次失败放大成多轮合并。
        if (
            strat == "merge"
            and res.qa is not None
            and not res.qa.ok
            and res.qa.blank
        ):
            retry = self._blank_glyph_retry(
                base_font=base_font,
                required=required,
                opt_set=opt_set,
                dest=dest,
                base_face_index=base_face_index,
                # ★ 传**全部候选**，不能只传 `plan.source_paths`。
                #
                # 摘 cmap 会把那些字符从"基础字体已覆盖"里拿掉，
                # 于是它们**重新**变成需要补充字体提供的字符 ——
                # 而规划器当初把它们算作"基础字体有"，
                # 所以 `plan.source_paths` 里可能根本没有能提供它们的字体。
                #
                # 实测 DemonsRoots `koin_nocut.ttf`：摘掉 2217 个空字形后，
                # `✔✕✖✘❤` 变成缺字，而规划器只选了一个
                # `lxgw-wenkai-screen.ttf`（它没有这 5 个符号）⇒
                # `merge_multi` 报"仍缺 5 个字符" ⇒ ok=False ⇒
                # 重试被丢弃、第一次的 2217 个空白原样保留。
                # 换成全部候选后 `seguisym.ttf` 正好能补上。
                sources=cands,
                blank=list(res.qa.blank),
                min_cov=min_cov,
            )
            if retry is not None:
                res.merge = retry
                res.audit_after = self.audit(dest, required)
                if verify:
                    try:
                        res.qa = verify_font(dest, req_only, render_size=40)
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
            # ⚠️ `summary` 是 **property**，不是方法。
            #
            # 这里原先写的是 `res.qa.summary()`，于是**一调用就抛
            # `TypeError: 'str' object is not callable`** ——
            # 因为 `str(summary)`（property 的返回值，一个字符串）
            # 被当成可调用对象去调了。
            #
            # 后果不是"少一条报错信息"，而是**整轮字体适配崩掉**：
            # 实测 DemonsRoots（MV）跑到第 4 个字体（`koin.ttf`）时崩，
            # 前 3 个字体**已经注入成功**却因为异常没被记进 patches，
            # `apply` 于是没有任何字体可回写 —— 玩家满屏口口口。
            #
            # 这条错误路径**只有在 QA 真的不通过时才会走到**，
            # 所以正常项目里永远测不到。修它的时候顺手确认了
            # `FontQAReport.summary` 与 `PatchResult.summary` 都是 property。
            res.error = f"QA 未通过：{res.qa.summary}"
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

    def _cff_replace_fallback(
        self,
        base_font: Path,
        required: str,
        dest: Path,
        cands: list[Path],
        opt_set: set[str],
        min_cov: float,
    ) -> MergeReport | None:
        """CFF 字体合并不了时的退路：整体换成覆盖最好的那个中文字体。

        返回 ``None`` 表示"换也就那样"（覆盖率不达标，或找不到候选）——
        调用方据此照旧上报原始的合并失败原因。
        """
        req_only = "".join(c for c in required if c not in opt_set)
        best, cov = self._best_single_font(cands, req_only)
        if best is None or cov < min_cov:
            log.warning(
                "CFF 字体替换退路不可用：最佳候选覆盖 %.1f%%（要求 %.1f%%）",
                cov * 100, min_cov * 100,
            )
            return None
        try:
            rep = replace_with_font(
                base_font, best, dest, required,
                optional_chars="".join(opt_set), min_coverage=min_cov,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("CFF 字体替换失败：%s", exc)
            return None
        if not rep.ok:
            return None
        log.info("CFF 字体 %s 已整体替换为 %s（覆盖 %.1f%%）",
                 base_font.name, best.name, rep.coverage_after * 100)
        return rep

    # ------------------------------------------------------------------

    def _warn_optional_blanks(
        self,
        dest: Path,
        required: str,
        opt_set: set[str],
        res: PatchResult,
    ) -> None:
        """把「顺带」那一侧渲染不出来的字符记成一条**警告**（不判失败）。

        这些是工具从 `UI_SAFE_CHARS` 猜"界面可能会渲染"的装饰符号
        （`¢¥µ·¿í ※ ‥ ′ ″ ‰` 之类）。实测它们在本机**任何**中文字体里
        都没有字形，而在 45943 条真实文本里出现 **0 次**。

        单独记一条是为了不让它们**悄悄消失**：用户需要知道
        "这几个猜出来的符号没补上"，同时又不该因此让整份字体判失败。
        """
        if not opt_set:
            return
        opt_req = [c for c in required if c in opt_set]
        if not opt_req:
            return
        try:
            qa = verify_font(dest, "".join(opt_req), render_size=40)
        except Exception:  # noqa: BLE001
            return
        blank = list(qa.blank or [])
        if not blank:
            return
        res.warnings.append(
            f"{len(blank)} 个**界面装饰符号**没有字形（不影响译文）"
            f"：{''.join(blank[:24])}"
            "。它们在真实文本里出现 0 次，只是工具猜界面可能会用到；"
            "本机字体都没有这些字形，属正常情况。"
        )

    # ------------------------------------------------------------------

    def _blank_glyph_retry(
        self,
        *,
        base_font: Path,
        required: str,
        opt_set: set[str],
        dest: Path,
        base_face_index: int,
        sources: list[Path],
        blank: list[str],
        min_cov: float,
    ) -> MergeReport | None:
        r"""合并后 QA 报"空白字形"时，**强制**让补充字体覆盖那些字符再合一次。

        ## 为什么会有"空白字形"这种状态

        默认的合并判据是 ``needed = required - base.cmap``：只要基础字体的
        cmap 里有这个码点，就认为它"已经有字形"、跳过注入。

        日文手写体**不满足**这个假设。实测 DemonsRoots 的 ``koin.ttf``：

        * cmap 里 21363 个码点，含大量汉字；
        * 抽样 400 个常用汉字，**264 个有字形、39 个字形是空的**
          （``到你而那之如没些她但只从`` 全空）；
        * 于是合并"成功"、覆盖率报 **100%**，QA 却数出 **1258 个空白字** ——
          字体看着补好了，游戏里那 1258 个字仍是一片空白。

        ## 为什么要重试而不是一开始就强制

        "base 的这个字形是不是空的"**只有渲染验证（QA）才知道**，
        合并器不该自己猜：把 cmap 里已有的字全部强制覆盖，等于把基础字体的
        字形全部换成补充字体的 —— 那就不再是"保留原字体风格"了，
        而"保留原风格"正是 ``merge`` 策略存在的理由。

        所以顺序是：先按原判据合一次 → QA 渲染验证 → **只有 QA 报出空白**，
        才把那些具体的字符强制覆盖、重合一次。这样风格该保留的地方保留，
        真正渲染不出来的才换掉。

        返回重试后的 :class:`MergeReport`；重试没意义或失败时返回 ``None``
        （调用方沿用第一次的结果，错误信息也还是第一次的 —— 那才是真相）。
        """
        # 只强制覆盖"确实需要渲染、且 base 的 cmap 里本来就有"的那些。
        # 不在 base cmap 里的字符本来就会走补充字体，force 它们没有意义。
        #
        # ⚠️ **`optional` 侧的字符排除在外**：它们本来就不该影响成败，
        # 把它们塞进 `force_chars` 只会让合并报"原字体 cmap 摘掉了 N 个字符"
        # 这种听起来很严重的警告，而结果是"猜出来的装饰符号还是补不上"。
        forced = [c for c in blank if c and c in required and c not in opt_set]
        if not forced:
            return None
        try:
            info = load_font_info(base_font, base_face_index)
        except Exception:  # noqa: BLE001
            return None
        if info is None or not info.cmap:
            return None
        forced = [c for c in forced if ord(c) in info.cmap]
        if not forced:
            return None

        log.info(
            "QA 报出 %d 个空白字形且它们本来就在原字体 cmap 里，"
            "强制让补充字体覆盖后重试合并", len(forced),
        )
        try:
            retry = merge_fonts_multi(
                base_font,
                sources,
                dest,
                required,
                base_face_index=base_face_index,
                force_chars="".join(forced),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("空白字形重试合并失败，沿用第一次的结果：%s", exc)
            return None
        if not retry.ok or retry.out_path is None:
            log.warning(
                "空白字形重试未产出结果（强制 %d 字，%s），沿用第一次的结果",
                len(forced), (retry.warnings or ["无告警"])[0],
            )
            return None
        # 只有重试**真的更好**才替换结果，避免"重试之后反而更差"。
        # 同样只看 `required` 侧 —— optional 缺字本来就不算失败。
        try:
            after = self.audit(dest, "".join(c for c in required if c not in opt_set))
        except Exception:  # noqa: BLE001
            return None
        if after.coverage < min_cov:
            log.warning(
                "空白字形重试后必需字符覆盖率只有 %.3f%%，沿用第一次的结果",
                after.coverage * 100,
            )
            return None
        return retry

    def ensure_charset_plannable(self, required: str, base_font: Path | None = None,
                                 candidates: list[Path] | None = None,
                                 *, optional: str = "") -> CharsetPlan:
        """规划并**在无法覆盖时抛错**，用于流程早期拦下问题。

        只有 ``required``（真实文本需要的字符）缺了才抛错；
        ``optional`` 缺了只记进 ``plan.optional_missing``。
        """
        cands = candidates if candidates is not None else self.supplement_candidates(base_font)
        plan = plan_charset(required, base_font, cands, optional=optional)
        assert_plannable(plan)
        return plan


__all__ = ["FontAudit", "FontService", "PatchResult"]
