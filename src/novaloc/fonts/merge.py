"""字体合并 / 补字。

这是"保证不出现口口口"的第二道防线。策略（按优先级）：

1. ``merge`` —— 把中文字体的**缺失字形**并入游戏原本使用的字体，
   保留原字体的拉丁字形与整体风格。界面观感变化最小。
2. ``replace`` —— 直接用某个中文字体替换原字体。当原字体没有嵌入权限、
   结构怪异（CFF2 可变字体）或者本身就是图片字体时使用。
3. ``fallback_only`` —— 不动原字体，改为给引擎注入一个"回退字体"。

合并的四个真实坑（均已实测验证）：

* ``fontTools.merge.Merger`` **不接受 BytesIO**，必须传真实文件路径。
* 两个字体的 ``unitsPerEm`` 必须一致，否则 Merger 直接抛
  ``Cannot merge fonts with different unitsPerEm``。中文点阵字体常见 UPEM=256，
  而拉丁字体常见 2048，所以必须先缩放。缩放同时会正确缩放 advance/bearing。
* 冲突的字形名必须先重命名，且重命名要**同步 glyph order + glyf + hmtx + 全部 cmap
  子表 + post + CFF charset**，漏一个就会在 save 时 ``KeyError``。
* ``Merger`` 无法合并 GSUB/GPOS，合并前后的 subset 必须把布局表剥掉，
  否则产物在 Unity/FreeType 里可能加载失败。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.merge import Merger
from fontTools.subset import Options as SubsetOptions
from fontTools.subset import Subsetter
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.ttLib.scaleUpem import scale_upem

from .coverage import load_font_info

log = logging.getLogger(__name__)

_DROP_ALWAYS = [
    "DSIG", "LTSH", "PCLT", "meta",
    "GSUB", "GPOS", "GDEF", "morx", "kerx", "BASE", "JSTF", "gasp",
    "kern",
    # 以下几种表在两个字体的子集之间几乎必然不对称，而 Merger 是按
    # "表集合长度"逐表比较的，只要一边有一边没有就会抛
    # "Expected all items to be equal: [NotImplemented, 0]" / TypeError。
    # 它们对 CJK 字形显示都没有必要，统一剥掉最安全。
    "VDMX", "hdmx", "vhea", "vmtx", "VORG", "FFTM", "prop",
    "EBDT", "EBLC", "EBSC", "CBDT", "CBLC", "sbix",
    "SVG ", "COLR", "CPAL", "MATH",
]

# Merger 必须两边都有的核心表，缺一个就不能合
_REQUIRED_TABLES = {"cmap", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "name", "post", "OS/2"}


def _drop_tables(font: TTFont, tags: set[str]) -> None:
    for tag in tags:
        if tag in font:
            del font[tag]


def normalize_tables(fonts: list[TTFont], *, keep_union: bool = False) -> list[set[str]]:
    """把多个字体裁剪到**相同的表集合**。

    Merger 逐表合并时要求两边表集合一致，所以这一步是合并成功的前提。
    默认取交集（只保留共同拥有的表）；``keep_union=True`` 时保留并集并删掉
    单边独有的表。
    """
    sets = [set(f.keys()) for f in fonts]
    common = set.intersection(*sets) if sets else set()
    common = {t for t in common if t not in _DROP_ALWAYS}

    missing_required = _REQUIRED_TABLES - common
    if missing_required:
        raise MergeError(f"字体缺少合并必需的表：{sorted(missing_required)}")

    for f in fonts:
        _drop_tables(f, set(f.keys()) - common)
    return [common for _ in fonts]


class MergeError(RuntimeError):
    pass


@dataclass
class MergeReport:
    ok: bool
    out_path: Path | None = None
    added_glyphs: int = 0
    method: str = "merge"
    warnings: list[str] = field(default_factory=list)
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    upem_scaled: bool = False
    metrics_changed: dict[str, tuple[int, int]] = field(default_factory=dict)
    sources_used: list[dict[str, object]] = field(default_factory=list)
    missing_chars: str = ""

    @property
    def summary(self) -> str:
        bits = [f"method={self.method}", f"added={self.added_glyphs}"]
        bits.append(f"coverage {self.coverage_before:.3%}→{self.coverage_after:.3%}")
        if self.upem_scaled:
            bits.append("UPEM 已缩放")
        if self.metrics_changed:
            bits.append(f"度量修正 {len(self.metrics_changed)} 项")
        if self.sources_used:
            bits.append(f"用 {len(self.sources_used)} 个补充字体")
        if self.missing_chars:
            bits.append(f"仍缺 {len(self.missing_chars)} 字")
        return "  ".join(bits)


# --------------------------------------------------------------------------
# 基础操作
# --------------------------------------------------------------------------


def open_font(path: Path, face_index: int = 0) -> TTFont:
    if path.suffix.lower() in (".ttc", ".otc"):
        return TTFont(path, fontNumber=face_index)
    return TTFont(path)


def _as_ttf_path(path: Path) -> Path:
    """合并产物总是单字体 sfnt，把 .ttc/.otc 后缀纠正为 .ttf。

    否则下游（含 Pillow、Unity）会按字体集合去打开，报
    "specify a font number between 0 and N"。
    """
    if path.suffix.lower() in (".ttc", ".otc"):
        return path.with_suffix(".ttf")
    return path


def subset_font(
    font: TTFont,
    unicodes: set[int],
    *,
    retain_gids: bool = True,
    drop_layout: bool = True,
    hinting: bool = True,
) -> TTFont:
    """就地子集化。``retain_gids=True`` 保持原 glyph id，避免破坏复合字形引用。"""
    opts = SubsetOptions()
    opts.retain_gids = retain_gids
    opts.name_IDs = ["*"]
    opts.name_legacy = True
    opts.name_languages = ["*"]
    opts.glyph_names = True
    opts.notdef_outline = True
    opts.recalc_bounds = True
    opts.recalc_timestamp = False
    opts.hinting = hinting
    opts.legacy_kern = False
    opts.prune_unicode_ranges = False
    opts.prune_codepage_ranges = False
    opts.ignore_missing_unicodes = True
    opts.passthrough_tables = False
    opts.drop_tables = list(_DROP_ALWAYS)
    if drop_layout:
        opts.layout_features = []

    s = Subsetter(options=opts)
    s.populate(unicodes=sorted(unicodes))
    s.subset(font)
    return font


def rename_glyphs(font: TTFont, prefix: str) -> dict[str, str]:
    """把除 ``.notdef`` 外的所有字形改为 ``<prefix><序号>``，返回映射表。

    顺序很关键：**先把 glyph order 落定，再重建 cmap**，
    否则 fontTools 内部的反向映射缓存会指向已经改名的字形。
    """
    old_order = list(font.getGlyphOrder())
    mapping = {n: f"{prefix}{i:05X}" for i, n in enumerate(old_order) if n != ".notdef"}
    if not mapping:
        return {}

    # glyf：重建字典并修复合字组件引用
    if "glyf" in font:
        glyf = font["glyf"]
        glyphs = glyf.glyphs  # 触发懒加载
        new_glyphs = {mapping.get(name, name): rec for name, rec in glyphs.items()}
        for rec in new_glyphs.values():
            for comp in getattr(rec, "components", None) or []:
                comp.glyphName = mapping.get(comp.glyphName, comp.glyphName)
        glyf.glyphs = new_glyphs

    # 度量表
    if "hmtx" in font:
        font["hmtx"].metrics = {mapping.get(k, k): v for k, v in font["hmtx"].metrics.items()}
    if "vmtx" in font:
        try:
            font["vmtx"].metrics = {mapping.get(k, k): v for k, v in font["vmtx"].metrics.items()}
        except Exception:  # noqa: BLE001
            pass
    if "hdmx" in font and getattr(font["hdmx"], "hdmx", None):
        font["hdmx"].hdmx = {mapping.get(k, k): v for k, v in font["hdmx"].hdmx.items()}
    if "VORG" in font and getattr(font["VORG"], "VOriginRecords", None):
        font["VORG"].VOriginRecords = {
            mapping.get(k, k): v for k, v in font["VORG"].VOriginRecords.items()
        }

    # CFF / CFF2
    for tag in ("CFF ", "CFF2"):
        if tag not in font:
            continue
        try:
            top = font[tag].cff.topDictIndex[0]
            cs = top.CharStrings
            cs.glyphOrder = [mapping.get(n, n) for n in cs.keys()]
            top.charset = [mapping.get(n, n) for n in top.charset]
            if hasattr(top, "FDArray"):
                for fd in top.FDArray:
                    fd.charset = [mapping.get(n, n) for n in fd.charset]
        except Exception as exc:  # noqa: BLE001
            log.debug("CFF 字形重命名不完整：%s", exc)

    # post
    if "post" in font:
        post = font["post"]
        if getattr(post, "extraNames", None):
            post.extraNames = [mapping.get(n, n) for n in post.extraNames]
        if getattr(post, "mapping", None):
            post.mapping = {k: mapping.get(v, v) for k, v in post.mapping.items()}

    # 落定顺序
    font.setGlyphOrder([mapping.get(n, n) for n in old_order])

    # cmap：整体重建（单一 format 12 子表，支持 BMP 外字符）
    old_cmap = dict(font.getBestCmap() or {})
    new_cmap = {cp: mapping.get(n, n) for cp, n in old_cmap.items()}
    font["cmap"].tableVersion = 0
    font["cmap"].tables = []
    sub = CmapSubtable.newSubtable(12)
    sub.platformID = 3
    sub.platEncID = 10
    sub.language = 0
    sub.cmap = {cp: n for cp, n in new_cmap.items() if cp != 0}
    font["cmap"].tables.append(sub)

    # 清掉可能残留的缓存
    font.__dict__.pop("_reverseGlyphMap", None)
    font.__dict__.pop("_glyphOrder", None)
    return mapping


def _match_upem(font: TTFont, target_upem: int) -> bool:
    """把 ``font`` 的 UPEM 缩放到目标值，返回是否做了缩放。"""
    head = font.get("head")
    if head is None:
        return False
    if head.unitsPerEm == target_upem:
        return False
    try:
        scale_upem(font, target_upem)
    except Exception as exc:  # noqa: BLE001
        raise MergeError(
            f"无法把 unitsPerEm 从 {head.unitsPerEm} 缩放到 {target_upem}：{exc}"
        ) from exc
    return True


def _merge_via_files(fonts: list[TTFont], workdir: Path) -> TTFont:
    """Merger 只接受文件路径，所以先落盘。"""
    paths: list[str] = []
    for i, f in enumerate(fonts):
        p = workdir / f"merge_in_{i}.ttf"
        f.save(p)
        paths.append(str(p))
    return Merger().merge(paths)


#: 垂直度量字段的"正负号"语义。
#: ``usWinDescent`` 在规范里是**正数**（表示基线下方的高度），
#: 而 ``descent`` / ``sTypoDescender`` 是负数。缩放到别的 UPEM 时
#: 必须保持符号，否则会让字体度量出现"下沿跑到基线上方"这种荒谬值。
_METRIC_FIELDS = (
    ("hhea", "ascent", +1),
    ("hhea", "descent", -1),
    ("hhea", "lineGap", +1),
    ("OS/2", "sTypoAscender", +1),
    ("OS/2", "sTypoDescender", -1),
    ("OS/2", "sTypoLineGap", +1),
    ("OS/2", "usWinAscent", +1),
    ("OS/2", "usWinDescent", +1),
)


def _snapshot_metrics(font: TTFont) -> dict[str, dict[str, int]]:
    """在任何会改动字体的操作之前，把垂直度量抓成纯数值字典。

    必须快照，因为 Merger 会取两边度量的**最大值**，
    而我们需要的是 CJK 侧的原始值。

    快照里的数值处于**该字体自己的 ``unitsPerEm`` 坐标系**。
    调用方若要写进别的 UPEM 的字体，必须先用
    :func:`_scale_metrics_snapshot` 换算 —— 直接写会造成
    "UPEM 256 的字体带着 2048 的 ascender"，
    渲染时文字被推到画布外，游戏里表现为**文字完全不可见**，
    比缺字形（口口口）更难排查。
    """
    snap: dict[str, dict[str, int]] = {}
    hhea = font.get("hhea")
    if hhea is not None:
        snap["hhea"] = {
            a: getattr(hhea, a) for a in ("ascent", "descent", "lineGap") if hasattr(hhea, a)
        }
    os2 = font.get("OS/2")
    if os2 is not None:
        snap["OS/2"] = {
            a: getattr(os2, a)
            for a in (
                "sTypoAscender", "sTypoDescender", "sTypoLineGap",
                "usWinAscent", "usWinDescent",
            )
            if hasattr(os2, a)
        }
    return snap


def _scale_metrics_snapshot(
    snap: dict[str, dict[str, int]], factor: float
) -> dict[str, dict[str, int]]:
    """把度量快照从一个 UPEM 坐标系线性换算到另一个。

    ``factor = 目标 UPEM / 来源 UPEM``。

    **为什么要按比例而不是直接沿用原值**：我们想要的是"CJK 字体的
    行高**比例**"（例如 ascender/em ≈ 0.93），而不是它的绝对数值。
    合并后的字体用自己的 UPEM，度量必须用同一坐标系表达。
    """
    if abs(factor - 1.0) < 1e-9:
        return {t: dict(v) for t, v in snap.items()}

    out: dict[str, dict[str, int]] = {}
    for table_tag, attrs in snap.items():
        new_attrs: dict[str, int] = {}
        for attr, value in attrs.items():
            sign = next(
                (s for t, a, s in _METRIC_FIELDS if t == table_tag and a == attr), None
            )
            if sign is None:
                new_attrs[attr] = value
                continue
            scaled = int(round(abs(value) * factor))
            # 规范化符号：descent 类字段必须是负的，其余保持原符号
            new_attrs[attr] = -scaled if sign < 0 else scaled
        out[table_tag] = new_attrs
    return out


def _apply_metrics(font: TTFont, snap: dict[str, dict[str, int]]) -> dict[str, tuple[int, int]]:
    """把快照里的垂直度量写回 ``font``，返回被改动的项。"""
    changed: dict[str, tuple[int, int]] = {}
    for table_tag, attrs in snap.items():
        table = font.get(table_tag)
        if table is None:
            continue
        for attr, value in attrs.items():
            if not hasattr(table, attr):
                continue
            old = getattr(table, attr)
            if old != value:
                setattr(table, attr, value)
                changed[f"{table_tag}.{attr}"] = (old, value)
    return changed


def _copy_vertical_metrics(dst: TTFont, src: TTFont) -> dict[str, tuple[int, int]]:
    """把 ``src`` 的垂直度量覆盖到 ``dst``。

    为什么必须覆盖：``fontTools.merge.Merger`` 对 ``hhea``/``OS/2`` 的度量取
    **各字体的最大值**。拉丁字体的 ascent/descent 常比中文字体大，
    合并后行高会暴涨，游戏里对话框、列表框的行距全部错位。

    中文文本的行高应由**中文字体**主导，所以这里以 CJK 字体为准。
    """
    changed: dict[str, tuple[int, int]] = {}

    def _set(obj, attr: str, value: int) -> None:  # noqa: ANN001
        if obj is None or not hasattr(obj, attr):
            return
        old = getattr(obj, attr)
        if old != value:
            setattr(obj, attr, value)
            changed[f"{attr}"] = (old, value)

    src_hhea, dst_hhea = src.get("hhea"), dst.get("hhea")
    if src_hhea is not None and dst_hhea is not None:
        for attr in ("ascent", "descent", "lineGap"):
            _set(dst_hhea, attr, getattr(src_hhea, attr))

    src_os2, dst_os2 = src.get("OS/2"), dst.get("OS/2")
    if src_os2 is not None and dst_os2 is not None:
        for attr in (
            "sTypoAscender", "sTypoDescender", "sTypoLineGap",
            "usWinAscent", "usWinDescent",
        ):
            if hasattr(src_os2, attr) and hasattr(dst_os2, attr):
                _set(dst_os2, attr, getattr(src_os2, attr))

    return changed


def _mark_cjk_ranges(font: TTFont) -> None:
    """把 CJK 相关位写进 ``OS/2`` 的 Unicode/CodePage range。

    不设这些位，老式 GDI 引擎可能拒绝把这个字体用于中文，
    表现为"字体装上了但中文变成别的字体/方框"。
    """
    os2 = font.get("OS/2")
    if os2 is None:
        return
    try:
        # ulUnicodeRange2 bit 48 = CJK Unified Ideographs
        if hasattr(os2, "ulUnicodeRange2"):
            os2.ulUnicodeRange2 |= 1 << (48 - 32)
        # ulUnicodeRange1 bits: 0=Basic Latin, 1=Latin-1 Supplement
        if hasattr(os2, "ulUnicodeRange1"):
            os2.ulUnicodeRange1 |= 1 << 0
        # ulCodePageRange1: bit 18 = GB2312 (CP936), bit 19 = Big5, bit 20 = JIS
        if hasattr(os2, "ulCodePageRange1"):
            os2.ulCodePageRange1 |= (1 << 18) | (1 << 19)
    except Exception as exc:  # noqa: BLE001
        log.debug("OS/2 range 位设置失败：%s", exc)


def _fix_char_index_bounds(font: TTFont) -> None:
    """重算 ``usFirstCharIndex`` / ``usLastCharIndex``（含非 BMP 时取 0xFFFF）。"""
    os2 = font.get("OS/2")
    if os2 is None:
        return
    cmap = font.getBestCmap() or {}
    if not cmap:
        return
    cps = sorted(cmap.keys())
    try:
        os2.usFirstCharIndex = min(0xFFFF, cps[0])
        os2.usLastCharIndex = 0xFFFF if cps[-1] > 0xFFFF else cps[-1]
    except Exception as exc:  # noqa: BLE001
        log.debug("char index 边界重算失败：%s", exc)


def _adjust_names(font: TTFont, base_family: str, *, suffix: str = "NovaLoc") -> None:
    """给合并字体一个可区分但可识别的名字。

    保留原 family 名 —— 引擎常常按 family 名查找字体文件。
    注意：子集化产物属于 OFL 定义的 Modified Version，
    ``base_family`` 里必须已经不含保留字体名（调用方负责改名）。
    """
    if "name" not in font:
        return
    full = f"{base_family} {suffix}"
    updates = {
        1: base_family,
        2: "Regular",
        3: f"{full}:NovaLoc",
        4: full,
        6: full.replace(" ", ""),
        16: base_family,
        17: "Regular",
    }
    name_table = font["name"]
    for rec in name_table.names:
        if rec.nameID in updates:
            try:
                rec.string = updates[rec.nameID].encode("utf-16-be")
            except Exception:  # noqa: BLE001
                continue
    name_table.names = [r for r in name_table.names if r.nameID not in (18, 20, 21, 22)]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def merge_fonts(
    base_path: Path,
    cjk_path: Path,
    out_path: Path,
    required_chars: str,
    *,
    base_face_index: int = 0,
    cjk_face_index: int = 0,
    prefix: str = "nvCJK",
    extra_symbols: str = " ",
) -> MergeReport:
    """把 ``cjk_path`` 中 ``required_chars`` 需要的字形并入 ``base_path``。

    ``required_chars`` 必须是**项目实际会用到的全部字符**（来自译文 + 界面符号），
    而不是抽样 —— 抽样会漏字，漏字就是口口口。
    """
    report = MergeReport(ok=False, method="merge")
    required = set(required_chars) | set(extra_symbols)
    required_cps = {ord(c) for c in required}

    base_info = load_font_info(base_path, base_face_index)
    if base_info is None or not base_info.cmap:
        raise MergeError(f"无法读取基础字体：{base_path}")
    report.coverage_before = base_info.coverage_ratio("".join(sorted(required)))

    have = set(base_info.cmap.keys())
    needed_cps = required_cps - have
    if not needed_cps:
        # 基础字体已经全覆盖：仍然产出一份文件，保证调用方拿到的路径恒定存在。
        # 注意 TTC → 单字体 TTF，避免下游把 .ttc 当集合打开。
        report.out_path = _as_ttf_path(out_path)
        report.out_path.parent.mkdir(parents=True, exist_ok=True)
        font = open_font(base_path, base_face_index)
        font.save(report.out_path)
        font.close()
        report.ok = True
        report.coverage_after = 1.0
        report.warnings.append("基础字体已完整覆盖所需字符，未做合并（已导出为独立 TTF）")
        return report

    cjk_info = load_font_info(cjk_path, cjk_face_index)
    if cjk_info is None or not cjk_info.cmap:
        raise MergeError(f"无法读取中文字体：{cjk_path}")
    available = set(cjk_info.cmap.keys())

    still_missing = needed_cps - available
    if still_missing:
        sample = "".join(chr(c) for c in sorted(still_missing)[:60])
        report.warnings.append(f"中文字体也缺少 {len(still_missing)} 个字符：{sample}")

    to_add = needed_cps & available
    if not to_add:
        raise MergeError("中文字体没有提供任何缺失字形，无法补齐")

    base_upem = base_info.units_per_em

    with tempfile.TemporaryDirectory(prefix="novaloc-merge-") as td:
        workdir = Path(td)

        # ---- 1. 基础字体：保留原有全部码点 + 待新增码点，剥掉布局表 ----
        base = open_font(base_path, base_face_index)
        try:
            subset_font(base, have | to_add, retain_gids=True, drop_layout=True)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"基础字体子集化失败：{exc}") from exc

        # ---- 2. 中文补充字体：只留缺失字形，对齐 UPEM，重命名字形 ----
        cjk = open_font(cjk_path, cjk_face_index)
        # 垂直度量要在子集化/合并之前抓下来 —— Merger 会取两边最大值，
        # 覆盖时必须用 CJK 侧的原始值。
        cjk_metrics = _snapshot_metrics(cjk)
        try:
            subset_font(cjk, to_add | {0x20}, retain_gids=False, drop_layout=True)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"中文字体子集化失败：{exc}") from exc

        if cjk.get("head") is not None:
            report.upem_scaled = _match_upem(cjk, base_upem)

        try:
            rename_glyphs(cjk, prefix)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"字形重命名失败：{exc}") from exc

        # ---- 3. 对齐两边的表集合（Merger 的硬性要求） ----
        try:
            dropped = normalize_tables([base, cjk])
        except MergeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"表集合归一化失败：{exc}") from exc
        log.debug("合并前保留的表：%s", sorted(dropped[0]))

        # ---- 4. 合并 ----
        try:
            merged = _merge_via_files([base, cjk], workdir)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"字体合并失败：{exc}") from exc

        # ---- 5. 收尾：再剥一次布局表 ----
        try:
            final_keep = set((merged.getBestCmap() or {}).keys()) | {0x20}
            subset_font(merged, final_keep, retain_gids=True, drop_layout=True, hinting=False)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"合并后子集化失败，保留原始合并结果：{exc}")

        # ---- 6. 度量修正 + name 表 ----
        # Merger 取两边度量的最大值，会让行高暴涨；必须用 CJK 侧的值覆盖，
        # 否则游戏里对话框/列表框行距全部错位。
        try:
            changed = _apply_metrics(merged, cjk_metrics)
            if changed:
                report.metrics_changed = changed
                log.debug("垂直度量已按中文字体修正：%s", changed)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"垂直度量修正失败：{exc}")

        try:
            _mark_cjk_ranges(merged)
            _fix_char_index_bounds(merged)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"OS/2 CJK 区段位设置失败：{exc}")

        try:
            _adjust_names(merged, base_info.family or "NovaLocMerged")
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"name 表调整失败：{exc}")

        # Merger 总是产出 TTF 风格的 sfnt；若基础是 TTC，强制以 .ttf 落盘
        final_out = _as_ttf_path(out_path)
        final_out.parent.mkdir(parents=True, exist_ok=True)
        merged.save(final_out)
        out_path = final_out

    # ---- 6. 审计 ----
    after = load_font_info(out_path)
    if after is None:
        raise MergeError("合并后的字体无法读取")
    report.coverage_after = after.coverage_ratio("".join(sorted(required)))
    report.added_glyphs = max(0, after.num_glyphs - base_info.num_glyphs)
    report.out_path = out_path
    report.ok = report.coverage_after >= 0.999

    if not report.ok:
        missing = after.missing("".join(sorted(required)))
        report.warnings.append(
            f"合并后覆盖率 {report.coverage_after:.4%}，仍缺 {len(missing)} 字：{''.join(missing[:60])}"
        )

    lost = have - set(after.cmap.keys())
    if lost:
        sample = "".join(chr(c) for c in sorted(lost)[:30])
        report.warnings.append(f"原字体有 {len(lost)} 个码点在合并后丢失：{sample}")

    return report


def merge_fonts_multi(
    base_path: Path,
    sources: list[Path],
    out_path: Path,
    required_chars: str,
    *,
    base_face_index: int = 0,
    prefix: str = "nvCJK",
    extra_symbols: str = " ",
) -> MergeReport:
    """把**多个**中文字体的字形按优先级依次并入 ``base_path``。

    为什么需要多个来源：实测 SimHei 缺少 ``♪♫✓✔✕❤`` 这类符号，
    SimSun 又缺另外一批。任何单一中文字体都不可能覆盖游戏里出现的全部符号，
    所以必须按优先级回退，让每个字符取"第一个拥有它的字体"。

    ``sources`` 顺序即优先级；靠前的字体覆盖靠后的。
    """
    report = MergeReport(ok=False, method="merge_multi")
    required = set(required_chars) | set(extra_symbols)
    required_cps = {ord(c) for c in required}

    base_info = load_font_info(base_path, base_face_index)
    if base_info is None or not base_info.cmap:
        raise MergeError(f"无法读取基础字体：{base_path}")
    report.coverage_before = base_info.coverage_ratio("".join(sorted(required)))

    have = set(base_info.cmap.keys())
    needed = required_cps - have
    if not needed:
        report.out_path = _as_ttf_path(out_path)
        report.out_path.parent.mkdir(parents=True, exist_ok=True)
        f = open_font(base_path, base_face_index)
        f.save(report.out_path)
        f.close()
        report.ok = True
        report.coverage_after = 1.0
        report.warnings.append("基础字体已完整覆盖所需字符，未做合并（已导出为独立 TTF）")
        return report

    base_upem = base_info.units_per_em
    report.sources_used = []

    with tempfile.TemporaryDirectory(prefix="novaloc-merge-") as td:
        workdir = Path(td)

        base = open_font(base_path, base_face_index)
        subset_font(base, have | needed, retain_gids=True, drop_layout=True)
        fonts: list[TTFont] = [base]

        metrics_snap: dict[str, dict[str, int]] = {}
        metrics_snap_upem: int = base_upem
        remaining = set(needed)

        for idx, src in enumerate(sources):
            if not remaining:
                break
            if not src.exists():
                report.warnings.append(f"补充字体不存在，已跳过：{src}")
                continue
            info = load_font_info(src)
            if info is None or not info.cmap:
                report.warnings.append(f"补充字体无法解析，已跳过：{src}")
                continue

            takes = remaining & set(info.cmap.keys())
            if not takes:
                continue

            try:
                extra = open_font(src)
                if idx == 0 or not metrics_snap:
                    # 快照发生在缩放**之前**，所以必须记下它当时所处的
                    # UPEM 坐标系，稍后按比例换算到合并字体的 UPEM。
                    metrics_snap = _snapshot_metrics(extra)
                    extra_head = extra.get("head")
                    metrics_snap_upem = (
                        extra_head.unitsPerEm if extra_head is not None else base_upem
                    )
                subset_font(extra, takes | {0x20}, retain_gids=False, drop_layout=True)
                scaled = False
                if extra.get("head") is not None and extra["head"].unitsPerEm != base_upem:
                    scaled = _match_upem(extra, base_upem)
                rename_glyphs(extra, f"{prefix}{idx}_")
            except Exception as exc:  # noqa: BLE001
                report.warnings.append(f"处理 {src.name} 失败，已跳过：{exc}")
                continue

            fonts.append(extra)
            remaining -= takes
            report.sources_used.append(
                {"font": str(src), "glyphs": len(takes), "upem_scaled": scaled}
            )

        if len(fonts) == 1:
            raise MergeError("没有任何补充字体提供缺失字形，无法补齐")

        if remaining:
            sample = "".join(chr(c) for c in sorted(remaining)[:60])
            report.warnings.append(f"所有补充字体合计仍缺 {len(remaining)} 个字符：{sample}")
            report.missing_chars = "".join(chr(c) for c in sorted(remaining))

        # 表集合必须全部一致，否则 Merger 会因"单边独有的表"报错
        try:
            normalize_tables(fonts)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"表集合归一化失败：{exc}") from exc

        try:
            merged = _merge_via_files(fonts, workdir)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"多字体合并失败：{exc}") from exc

        try:
            final_keep = set((merged.getBestCmap() or {}).keys()) | {0x20}
            subset_font(merged, final_keep, retain_gids=True, drop_layout=True, hinting=False)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"合并后子集化失败，保留原始合并结果：{exc}")

        if metrics_snap:
            try:
                # 关键：快照是在补充字体**自己的 UPEM** 下抓的，
                # 而合并且字体用的是 base_upem。不换算就写回去会得到
                # "UPEM 256 却带 ascender 2210" 的字体 —— 渲染时文字被
                # 推到画布外，游戏里表现为文字完全不可见（比口口口更难查）。
                scaled_snap = _scale_metrics_snapshot(
                    metrics_snap, base_upem / float(metrics_snap_upem or base_upem)
                )
                changed = _apply_metrics(merged, scaled_snap)
                if changed:
                    report.metrics_changed = changed
            except Exception as exc:  # noqa: BLE001
                report.warnings.append(f"垂直度量修正失败：{exc}")

        try:
            _mark_cjk_ranges(merged)
            _fix_char_index_bounds(merged)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"OS/2 CJK 区段位设置失败：{exc}")

        try:
            _adjust_names(merged, base_info.family or "NovaLocMerged")
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"name 表调整失败：{exc}")

        final_out = _as_ttf_path(out_path)
        final_out.parent.mkdir(parents=True, exist_ok=True)
        merged.save(final_out)
        out_path = final_out

    after = load_font_info(out_path)
    if after is None:
        raise MergeError("合并后的字体无法读取")
    report.coverage_after = after.coverage_ratio("".join(sorted(required)))
    report.added_glyphs = max(0, after.num_glyphs - base_info.num_glyphs)
    report.out_path = out_path
    report.ok = report.coverage_after >= 0.999 and not report.missing_chars

    lost = have - set(after.cmap.keys())
    if lost:
        sample = "".join(chr(c) for c in sorted(lost)[:30])
        report.warnings.append(f"原字体有 {len(lost)} 个码点在合并后丢失：{sample}")

    return report


def plan_sources(
    sources: list[Path], required_chars: str
) -> tuple[list[Path], dict[str, list[str]]]:
    """按优先级算出每个补充字体各自负责哪些字符（用于预览与日志）。

    返回 ``(被采用的字体顺序, {字体路径: [负责的字符]})``。
    """
    remaining = {ord(c) for c in required_chars}
    ordered: list[Path] = []
    assign: dict[str, list[str]] = {}
    for src in sources:
        info = load_font_info(src)
        if info is None or not info.cmap:
            continue
        takes = remaining & set(info.cmap.keys())
        if not takes:
            continue
        ordered.append(src)
        assign[str(src)] = [chr(c) for c in sorted(takes)]
        remaining -= takes
        if not remaining:
            break
    return ordered, assign


def replace_with_font(
    src_font: Path | None,
    cjk_font: Path,
    out_path: Path,
    required_chars: str,
) -> MergeReport:
    """直接用中文字体替换。"""
    report = MergeReport(ok=False, method="replace")
    if src_font is not None:
        info_before = load_font_info(src_font)
        report.coverage_before = info_before.coverage_ratio(required_chars) if info_before else 0.0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cjk_font, out_path)
    after = load_font_info(out_path)
    if after is None:
        raise MergeError("替换后的字体无法读取")
    report.coverage_after = after.coverage_ratio(required_chars)
    report.out_path = out_path
    report.ok = report.coverage_after >= 0.999
    if not report.ok:
        report.warnings.append(f"替换字体覆盖不足：{report.coverage_after:.4%}")
    return report


def subset_font_for_chars(
    src: Path,
    out: Path,
    chars: str,
    *,
    face_index: int = 0,
    keep_symbols: str = " 0123456789.,:;!?'\"()[]{}%+-–—_/@#&*=<>&·…、。！？：；「」『』（）《》【】—～",
) -> Path:
    """把字体裁剪到只含 ``chars``（外加基础符号），用于减小注入体积。"""
    font = open_font(src, face_index)
    cps = {ord(c) for c in set(chars) | set(keep_symbols)} | {0x20, 0x09, 0x0A, 0x0D}
    subset_font(font, cps, retain_gids=False, drop_layout=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    font.save(out)
    return out


def font_stats(path: Path, required_chars: str = "") -> dict[str, object]:
    info = load_font_info(path)
    if info is None:
        return {"ok": False, "path": str(path)}
    return {
        "ok": True,
        "path": str(path),
        "family": info.family,
        "subfamily": info.subfamily,
        "num_glyphs": info.num_glyphs,
        "cmap_size": len(info.cmap or {}),
        "is_cff": info.is_cff,
        "units_per_em": info.units_per_em,
        "cjk_capable": info.is_cjk_capable,
        "coverage": info.coverage_ratio(required_chars) if required_chars else None,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
