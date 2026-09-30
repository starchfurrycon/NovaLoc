"""字体发现与字形覆盖审计。

这是"不出现口口口"的第一道防线：在把任何中文写回游戏之前，
必须确认目标字体的 cmap 里真的有这些字。

审计用的字符集来自**项目实际要写回的全部中文文本**（见 :mod:`novaloc.fonts.charset`），
而不是"随便抽几个字看看" —— 只有这样才能保证不漏。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont, TTLibError

from ..models import FontCoverage
from . import catalog
from .textutil import is_ignorable

log = logging.getLogger(__name__)

# 必需的基础符号：即使文本本身没用到，也要求字体具备
ESSENTIAL_SYMBOLS = "0123456789.,:;!?'\"()[]{}%+-–—_/@#&*=<>&·…、。！？：；「」『』（）《》【】—～"

# GB2312 一级汉字的前 200 个，用于快速判断"这字体是不是中文可用"
_GB2312_LEVEL1_SAMPLE = (
    "啊阿埃挨哎唉哀皑癌蔼矮艾碍爱隘鞍氨安俺按暗岸胺案肮昂盎凹敖熬翱袄傲奥懊澳芭"
    "捌扒叭吧笆八疤巴拔跋靶把耙坝霸罢爸白柏百摆佰败拜稗斑班搬扳般颁板版扮拌伴瓣"
    "半办绊邦帮梆榜膀绑棒磅蚌镑傍谤苞胞包褒剥"
)

_FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}


def _iter_ttc_fonts(path: Path):
    """TTC/OTC 是字体集合，需要逐个 face 处理。"""
    suffixes = path.suffix.lower()
    if suffixes in (".ttc", ".otc"):
        try:
            n = TTFont(path, fontNumber=0, lazy=True)
            num = n.reader.numFonts if hasattr(n.reader, "numFonts") else 1
            n.close()
        except Exception:  # noqa: BLE001
            num = 1
        for i in range(num):
            try:
                yield TTFont(path, fontNumber=i, lazy=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("TTC face %d 读取失败：%s", i, exc)
    else:
        yield TTFont(path, lazy=True)


@dataclass
class FontInfo:
    path: Path
    face_index: int = 0

    family: str = ""
    subfamily: str = ""
    full_name: str = ""
    version: str = ""
    num_glyphs: int = 0
    is_cff: bool = False
    units_per_em: int = 1000
    cmap: dict[int, str] | None = None
    """码点 -> 字形名。"""

    @property
    def id(self) -> str:
        return f"{self.path.name}#{self.face_index}" if self.face_index else self.path.name

    @property
    def is_cjk_capable(self) -> bool:
        if not self.cmap:
            return False
        hits = sum(1 for ch in _GB2312_LEVEL1_SAMPLE if ord(ch) in self.cmap)
        return hits >= len(_GB2312_LEVEL1_SAMPLE) * 0.9

    def has_char(self, ch: str) -> bool:
        return bool(self.cmap) and ord(ch) in self.cmap

    def missing(self, chars: str) -> list[str]:
        """列出 ``chars`` 里这个字体没有字形的字符。

        **不渲染的字符（``\\n`` / ``\\t`` / 零宽）不算缺字** ——
        没有任何字体有它们的字形。以前这里不过滤，于是译文里的换行
        会被报成"缺 1 个字符"，覆盖率停在 99.93%，触发"缺字就硬失败"
        的契约、让整个字体阶段中止。真实游戏上就是"字体适配永远失败"。
        """
        if not self.cmap:
            return sorted({c for c in chars if not is_ignorable(c)})
        seen: dict[str, None] = {}
        for ch in chars:
            if is_ignorable(ch):
                continue
            if ord(ch) not in self.cmap:
                seen.setdefault(ch, None)
        return sorted(seen)

    def coverage_ratio(self, chars: str) -> float:
        """覆盖率 = 有字形的字符数 / **需要字形的**字符数。

        ⚠️ 分子与分母必须用**同一套**过滤。
        以前 :meth:`missing` 和这里的 ``set(chars)`` 各算各的：
        ``missing`` 把 ``\\n`` 算进"缺"（因为 cmap 里没有），
        分母却把 ``\\n`` 算进"总数"，两边同时对不上，
        于是页面上/报告里显示的覆盖率**偏低**，而且和
        `merge.py` 算出来的 ``coverage_after`` 不是同一个数。
        """
        if not chars:
            return 1.0
        uniq = {c for c in set(chars) if not is_ignorable(c)}
        if not uniq:
            return 1.0
        miss = len(self.missing(chars))
        return 1.0 - miss / len(uniq)


def _name_of(font: TTFont, name_id: int) -> str:
    try:
        for rec in font["name"].names:
            if rec.nameID == name_id:
                # 早先这里套了 ``for enc in ("utf-16-be","utf-8","latin-1")``，
                # 但 ``rec.toUnicode()`` **不接受编码参数**，三次尝试做的事
                # 完全一样 —— 成功则第一次就返回，失败则白失败三次。
                # 改为尝试一次，失败再退回原始字节的宽松解码。
                try:
                    v = rec.toUnicode()
                except Exception:  # noqa: BLE001
                    try:
                        v = str(rec.string, "utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        continue
                if v and v.strip():
                    return v.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def load_font_info(path: Path, face_index: int = 0) -> FontInfo | None:
    """读取字体元信息与 cmap。失败返回 None（坏字体不该中断流水线）。"""
    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            font = TTFont(path, fontNumber=face_index, lazy=True)
        else:
            font = TTFont(path, lazy=True)
    except (TTLibError, OSError, ValueError) as exc:
        log.warning("无法读取字体 %s：%s", path, exc)
        return None

    try:
        cmap: dict[int, str] = {}
        try:
            best = font.getBestCmap()
            if best:
                cmap = best
        except Exception:  # noqa: BLE001
            for table in font["cmap"].tables:  # type: ignore[union-attr]
                try:
                    cmap.update(table.cmap)
                except Exception:  # noqa: BLE001
                    continue

        head = font.get("head")
        return FontInfo(
            path=path,
            face_index=face_index,
            family=_name_of(font, 1) or _name_of(font, 16),
            subfamily=_name_of(font, 2) or _name_of(font, 17),
            full_name=_name_of(font, 4),
            version=_name_of(font, 5),
            num_glyphs=font["maxp"].numGlyphs if "maxp" in font else 0,
            is_cff="CFF " in font or "CFF2" in font,
            units_per_em=getattr(head, "unitsPerEm", 1000) if head else 1000,
            cmap=cmap,
        )
    finally:
        try:
            font.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 系统字体发现
# --------------------------------------------------------------------------

_WINDOWS_FONT_DIRS = [
    Path(r"C:\Windows\Fonts"),
    Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
]


def system_font_dirs() -> list[Path]:
    if sys.platform == "win32":
        return [d for d in _WINDOWS_FONT_DIRS if d.is_dir()]
    out = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
    home = Path.home()
    out += [home / ".fonts", home / ".local" / "share" / "fonts"]
    return [d for d in out if d.is_dir()]


def scan_font_files(dirs: list[Path] | None = None, *, limit: int = 4000) -> list[Path]:
    """扫描字体文件（不解析，只找文件）。"""
    out: list[Path] = []
    for d in dirs or system_font_dirs():
        try:
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in _FONT_EXTS:
                    out.append(p)
                    if len(out) >= limit:
                        return out
        except OSError:
            continue
    return out


def find_font_by_family(family: str, dirs: list[Path] | None = None) -> Path | None:
    """在系统里找指定家族名的字体文件。"""
    want = family.strip().lower()
    for p in scan_font_files(dirs):
        stem = p.stem.lower()
        if want.replace(" ", "") in stem.replace(" ", ""):
            return p
    # 退一步：解析 name 表（慢，只在需要时做）
    for p in scan_font_files(dirs)[:600]:
        info = load_font_info(p)
        if info and info.family.strip().lower() == want:
            return p
    return None


# --------------------------------------------------------------------------
# 覆盖审计
# --------------------------------------------------------------------------


def audit_font(
    path: Path,
    required_chars: str,
    *,
    font_id: str | None = None,
    face_index: int = 0,
    cjk_check: bool = True,
) -> FontCoverage | None:
    """审计单个字体对 ``required_chars`` 的覆盖情况。"""
    info = load_font_info(path, face_index)
    if info is None:
        return None

    missing = info.missing(required_chars) if cjk_check else []
    return FontCoverage(
        font_id=font_id or info.id,
        path=str(path),
        family=info.family,
        style=info.subfamily,
        num_glyphs=info.num_glyphs,
        cmap_size=len(info.cmap or {}),
        covers_cjk=info.is_cjk_capable,
        missing=missing,
    )


def audit_many(
    paths: list[Path],
    required_chars: str,
    *,
    only_cjk: bool = True,
) -> list[FontCoverage]:
    out: list[FontCoverage] = []
    for p in paths:
        cov = audit_font(p, required_chars)
        if cov is None:
            continue
        if only_cjk and not cov.covers_cjk:
            continue
        out.append(cov)
    out.sort(key=lambda c: (c.missing_count, -c.cmap_size))
    return out


@dataclass
class CoverageVerdict:
    ok: bool
    font: str
    missing: list[str]
    ratio: float
    reason: str = ""


def verdict(coverage: FontCoverage, required_chars: str, threshold: float = 0.999) -> CoverageVerdict:
    ratio = coverage.coverage_ratio(required_chars) if required_chars else 1.0
    missing = coverage.missing
    if not missing:
        return CoverageVerdict(True, coverage.font_id, [], 1.0, "")
    if ratio >= threshold:
        return CoverageVerdict(
            True,
            coverage.font_id,
            missing,
            ratio,
            f"覆盖率 {ratio:.4%} 达到阈值 {threshold:.2%}，缺失 {len(missing)} 个字（多为生僻字）",
        )
    return CoverageVerdict(
        False,
        coverage.font_id,
        missing,
        ratio,
        f"覆盖率仅 {ratio:.4%}，缺失 {len(missing)} 个字符，注入后会显示为口口口，已拒绝写入",
    )


def suggest_font_for_char(char: str, candidates: list[Path]) -> Path | None:
    """为某个缺失字符找替补字体。"""
    for p in candidates:
        info = load_font_info(p)
        if info and info.has_char(char):
            return p
    return None


def guess_style_from_name(family: str, subfamily: str = "") -> dict[str, bool]:
    """从字体名猜它是不是粗体/斜体/衬线 —— 用于给中文字体挑最接近的字重。"""
    s = f"{family} {subfamily}".lower()
    return {
        "bold": any(k in s for k in ("bold", "black", "heavy", "extrabold", "semibold", "demibold", "粗", "黑")),
        "italic": any(k in s for k in ("italic", "oblique", "倾斜", "斜")),
        "serif": any(k in s for k in ("serif", "song", "ming", "宋", "明", "times", "georgia", "garamond")),
        "mono": any(k in s for k in ("mono", "consol", "courier", "等宽")),
        "light": any(k in s for k in ("light", "thin", "extralight", "细")),
        "handwritten": any(k in s for k in ("hand", "script", "comic", "kai", "楷", "文楷")),
        "pixel": any(k in s for k in ("pixel", "bitmap", "dots", "像素", "点阵")),
    }


def match_chinese_font(style: dict[str, bool], cfg) -> str:  # noqa: ANN001
    """按原字体的风格特征挑一个最合适的中文字体 id。"""
    if style.get("pixel") and cfg.font.pixel_font:
        return cfg.font.pixel_font
    if style.get("handwritten"):
        return "lxgw-wenkai-screen"
    if style.get("serif"):
        return "source-han-serif-sc"
    if style.get("bold"):
        return "source-han-sans-sc"
    if style.get("mono"):
        return "sarasa-gothic-sc"
    return "source-han-sans-sc"


_WEIGHT_ORDER = ["ExtraLight", "Light", "Normal", "Regular", "Medium", "SemiBold", "Bold", "Heavy", "Black"]


def pick_weight(style: dict[str, bool]) -> str:
    if style.get("bold"):
        return "Bold"
    if style.get("light"):
        return "Light"
    return "Regular"


def non_redistributable_warning(path: Path) -> str | None:
    """如果用户选了商用字体，给一句提醒（不阻止使用，只提醒别打包）。"""
    info = load_font_info(path)
    if info is None:
        return None
    fam = f"{info.family} {info.subfamily}".lower()
    for hint in catalog.NON_REDISTRIBUTABLE_HINTS:
        if hint.lower() in fam:
            return (
                f"字体「{info.family}」属于商业授权字体，可以用于本地汉化，"
                "但**不要**把它打包进你分发的汉化补丁。"
            )
    return None
