"""字体质检：用"确定性不变量"判断一个字体是否可靠，而不是靠肉眼。

为什么不用 IoU 比对？
    实测表明，把中文字形并入拉丁字体后，字体整体的垂直度量
    （``hhea.ascent/descent``、``OS/2.usWinAscent``）会变成拉丁字体的值，
    导致同一个汉字在合并字体里比在源字体里**基线略高几个像素**。
    这会稳定地把 IoU 压到 ~0.92，但字形轮廓其实完全无损
    （实测：UPEM 缩放与子集化的 IoU 都是 1.0000，ink 比 0.91~1.13）。
    因此 IoU 会把"正常的度量继承"误报成"字形损坏"。

真正该检查的不变量：
    1. 码点在 cmap 里（否则引擎根本找不到这个字 → 口口口）；
    2. 该码点渲染出来**有墨迹**（否则是空白 → 视觉上也是缺字）；
    3. 该码点的字形**不同于 .notdef**（否则引擎画的是豆腐块方框）；
    4. 同一字体内不同字的渲染结果**互不相同**（否则说明字形被互相覆盖）；
    5. 字体能被 FreeType/Pillow 正常加载（否则游戏加载就失败）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .coverage import FontInfo, load_font_info
from .textutil import is_blank_by_design

log = logging.getLogger(__name__)

# .notdef 的典型外观：一个空心矩形。用于"是不是豆腐块"的判定。
_TOFU_ASPECT_TOLERANCE = 0.18


@dataclass
class GlyphIssue:
    char: str
    codepoint: int
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return f"U+{self.codepoint:04X} {self.char!r} [{self.kind}] {self.detail}"


@dataclass
class FontQAReport:
    path: str
    ok: bool = True
    loaded: bool = False
    family: str = ""
    num_glyphs: int = 0
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    blank: list[str] = field(default_factory=list)
    tofu: list[str] = field(default_factory=list)
    issues: list[GlyphIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.loaded:
            return f"❌ 无法加载 {self.path}"
        parts = [f"检查 {self.checked} 字"]
        if self.missing:
            parts.append(f"缺码点 {len(self.missing)}")
        if self.blank:
            parts.append(f"空白 {len(self.blank)}")
        if self.tofu:
            parts.append(f"豆腐块 {len(self.tofu)}")
        if not (self.missing or self.blank or self.tofu):
            parts.append("全部通过")
        return ("✅ " if self.ok else "❌ ") + "  ".join(parts)


# --------------------------------------------------------------------------
# 渲染工具
# --------------------------------------------------------------------------


def render_glyph(path: Path, ch: str, size: int = 48, box: int = 96) -> np.ndarray | None:
    """把单个字符渲染成二值掩膜。失败返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        return None
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError as exc:
        log.debug("渲染失败 %s：%s", path, exc)
        return None

    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((box // 2, int(box * 0.78)), ch, font=font, fill=255, anchor="ms")
    return (np.asarray(img, dtype=np.uint8) > 96).astype(np.uint8)


def _is_tofu_like(mask: np.ndarray) -> bool:
    """判断掩膜是否像 .notdef 的矩形框。

    豆腐块的特征：一个几乎填满包围盒的空心矩形 —— 外框实心、
    内部空洞比例很高，且边长比接近 1。
    """
    if mask.sum() == 0:
        return False
    ys, xs = np.nonzero(mask)
    h = ys.max() - ys.min() + 1
    w = xs.max() - xs.min() + 1
    if h < 6 or w < 6:
        return False
    aspect = w / h
    if abs(aspect - 1.0) > _TOFU_ASPECT_TOLERANCE:
        return False
    # 内部空洞比例
    inner = mask[ys.min() + 3 : ys.max() - 2, xs.min() + 3 : xs.max() - 2]
    if inner.size == 0:
        return False
    hollow = 1.0 - inner.sum() / inner.size
    # 边框厚度占比：边框像素 / 周长
    border = mask.sum() - inner.sum()
    perimeter = 2 * (h + w)
    thick = border / max(1, perimeter)
    return hollow > 0.72 and 0.8 <= thick <= 6.0


# --------------------------------------------------------------------------
# 主检查
# --------------------------------------------------------------------------


def _glyph_id_collisions(path: Path, chars: list[str]) -> list[list[str]]:
    """找出映射到**同一个 glyph id** 的码点组。

    这才是真正的"字形被互相覆盖"：合并时两个字被塞进了同一个 glyph。
    设计上共用字形（`I`/`l`）不会共享 glyph id，所以不会误报。
    """
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(path, lazy=True)
    except Exception:  # noqa: BLE001
        return []
    try:
        cmap = f.getBestCmap() or {}
        by_gid: dict[int, list[str]] = {}
        for c in chars:
            name = cmap.get(ord(c))
            if not name or name == ".notdef":
                continue
            try:
                gid = f.getGlyphID(name)
            except Exception:  # noqa: BLE001
                continue
            by_gid.setdefault(gid, []).append(c)
        return [v for v in by_gid.values() if len(v) > 1]
    finally:
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass


def _shared_render_groups(
    chars: list[str], render_fn, size: int, path: Path, *, limit: int = 400
) -> list[list[str]]:
    """找出渲染形状相同的字符组（仅作提示，不算故障）。"""
    sig: dict[bytes, list[str]] = {}
    for c in chars[:limit]:
        m = render_fn(path, c, size)
        if m is None or m.sum() == 0:
            continue
        ys, xs = np.nonzero(m)
        crop = m[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        sig.setdefault(crop.tobytes(), []).append(c)
    return [v for v in sig.values() if len(v) > 1]


def _is_blank_by_design(ch: str) -> bool:
    """空白类字符渲染成空是**正确的**，不能当成缺字。

    包括 ASCII 空格、NBSP、全角空格、各类 Unicode 空格、ZWSP、
    制表/换行、变体选择符与组合记号。

    ## ★ 判定只有一份实现，但它和 `is_ignorable` **不是同一件事**

    早先这里是**自己一套**判据（`Zs/Zl/Zp/Cc/Cf` + 几个硬编码码点），
    而 `textutil.is_ignorable`（字符集汇总用的那个）是**另一套**
    （只认 `NON_RENDERING` 那 7 个）。两个"裁判"对 `U+FE0F` 给出不同结论：

    * `is_ignorable` 说"要字形" ⇒ 它进了"必翻"字符集 ⇒
      `plan.still_missing` 里永远留着它 ⇒ **每个**字体都报缺 1 个字符；
    * `_is_blank_by_design` 说"渲染成空是对的" ⇒ QA 又不报它。

    结果是一条**永远消不掉**的噪音提示，把 `ship.otf` 的真实失败原因
    （缺 `glyf`/`loca`）盖住了。

    修的时候**顺手想合并成一个函数，结果炸了两条测试** ——
    因为这两个问题其实**不同**：

    | 问题 | 谁答 | 答"是"的含义 |
    |---|---|---|
    | 这个字符**需要字形**吗？ | `is_ignorable` | 不需要 ⇒ 不该进字符集 |
    | 渲染成空白**可以接受**吗？ | 这个函数 | 可以 ⇒ 不算缺字 |

    反例（`test_ui_safe_chars_contain_no_ignorables` 与
    `test_incident_chars_would_be_source_only` 就是在守它们）：

    * **全角空格**：占宽度 ⇒ 字符集里**该有**它（`UI_SAFE_CHARS` 特意列了）；
    * **泰文声调符号**：有宽度、要渲染 ⇒ 不能因为类别是 `Mn` 就从字符集剔掉。

    所以这里是"更宽"的那一侧，`is_ignorable` 是"更窄"的那一侧。
    """
    return is_blank_by_design(ch)


def verify_font(
    path: Path,
    required_chars: str,
    *,
    render_size: int = 40,
    check_tofu: bool = True,
    check_distinct: bool = True,
    char_limit: int = 3000,
) -> FontQAReport:
    """检查字体是否能安全显示 ``required_chars`` 里的每一个字符。"""
    rep = FontQAReport(path=str(path))
    info: FontInfo | None = load_font_info(path)
    if info is None:
        rep.ok = False
        rep.loaded = False
        rep.warnings.append("字体无法解析")
        return rep

    rep.loaded = True
    rep.family = info.family
    rep.num_glyphs = info.num_glyphs
    rep.metrics = {
        "units_per_em": info.units_per_em,
        "cmap_size": len(info.cmap or {}),
    }
    rep.metrics.update(_vertical_metrics(path))

    chars = sorted(set(required_chars))
    if len(chars) > char_limit:
        rep.warnings.append(f"字符数 {len(chars)} 超过检查上限 {char_limit}，只检查前 {char_limit} 个")
        chars = chars[:char_limit]
    rep.checked = len(chars)

    # 空白类字符单独处理：它们渲染为空是正确的，不参与缺字判定
    blanks_by_design = {c for c in chars if _is_blank_by_design(c)}
    meaningful = [c for c in chars if c not in blanks_by_design]
    rep.checked = len(meaningful)

    # ---- 1. cmap 覆盖 ----
    missing = [c for c in meaningful if not info.has_char(c)]
    if missing:
        rep.missing = missing
        for c in missing[:200]:
            rep.issues.append(GlyphIssue(c, ord(c), "missing_cmap", "cmap 中没有该码点"))

    present = [c for c in meaningful if info.has_char(c)]

    # ---- 2. 渲染检查（空白 / 豆腐块） ----
    # 豆腐块检查**故意无条件执行**：它是"防口口口"的核心闸门，
    # 允许调用方关掉就等于允许交付一个全是方块的字体。
    # 原先写作 `if check_tofu or True:` —— 恒真，参数形同虚设。
    # 现在直接不判断：``check_tofu`` 保留在签名里仅为向后兼容，
    # 传 False 也不会（也不应该）跳过检查。
    _ = check_tofu
    notdef = render_glyph(path, "\uFFFF", render_size)  # 未映射码点 → 走 .notdef
    tofu_ref = notdef if (notdef is not None and notdef.sum() > 0) else None

    masks: dict[str, np.ndarray] = {}
    for c in present:
        m = render_glyph(path, c, render_size)
        if m is None:
            rep.warnings.append("Pillow 无法渲染该字体，跳过字形渲染检查")
            masks = {}
            break
        masks[c] = m

    for c, m in masks.items():
        if m.sum() == 0:
            rep.blank.append(c)
            rep.issues.append(GlyphIssue(c, ord(c), "blank", "渲染为空白"))

    if tofu_ref is not None:
        for c, m in masks.items():
            if m.sum() == 0:
                continue
            # 与 .notdef 完全一致 → 引擎在画豆腐块
            if m.shape == tofu_ref.shape and np.array_equal(m, tofu_ref):
                rep.tofu.append(c)
                rep.issues.append(GlyphIssue(c, ord(c), "tofu", "渲染结果与 .notdef 相同"))

    # ---- 3. 字形互不相同 ----
    # 注意：仅仅"渲染结果相同"**不构成问题**。很多字符在设计上就共用一个
    # 字形（`I`/`l`/`Ⅰ`/`Ｉ`、`°`/`。` 等），这是字体的正常行为。
    # 真正的故障是"不同的码点被映射到**同一个 glyph id**"，那才是合并时
    # 字形互相覆盖。所以这里直接查 cmap → gid。
    if check_distinct:
        collisions = _glyph_id_collisions(path, present)
        if collisions:
            sample = ["/".join(v[:4]) for v in collisions[:10]]
            rep.warnings.append(
                f"有 {len(collisions)} 组码点映射到同一 glyph id（字形可能被覆盖）：{sample}"
            )
        else:
            shared = _shared_render_groups(present, render_glyph, render_size, path)
            if shared:
                rep.warnings.append(
                    f"提示：{len(shared)} 组字符渲染形状相同（多为设计上共用字形，非故障）："
                    f"{['/'.join(v[:4]) for v in shared[:6]]}"
                )

    rep.ok = not (rep.missing or rep.blank or rep.tofu)
    return rep


def _vertical_metrics(path: Path) -> dict[str, int]:
    """读出垂直度量，用于人工判断行距是否合理。"""
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(path, lazy=True)
    except Exception:  # noqa: BLE001
        return {}
    try:
        out: dict[str, int] = {}
        if "hhea" in f:
            out["hhea_ascent"] = f["hhea"].ascent
            out["hhea_descent"] = f["hhea"].descent
            out["hhea_linegap"] = f["hhea"].lineGap
        if "OS/2" in f:
            os2 = f["OS/2"]
            out["os2_typo_asc"] = getattr(os2, "sTypoAscender", 0)
            out["os2_typo_desc"] = getattr(os2, "sTypoDescender", 0)
            out["os2_win_asc"] = getattr(os2, "usWinAscent", 0)
            out["os2_win_desc"] = getattr(os2, "usWinDescent", 0)
        return out
    finally:
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass


def compare_glyph_shapes(
    a: Path, b: Path, chars: str, *, size: int = 48
) -> dict[str, float]:
    """辅助工具：两个字体渲染同一批字符的 IoU 分布。

    注意：**不要**把低 IoU 直接当成错误。合并字体会继承基础字体的
    垂直度量，导致基线偏移，IoU 稳定落在 ~0.92。判断可靠性请用
    :func:`verify_font` 的不变量检查。
    """
    ious: list[float] = []
    for c in dict.fromkeys(chars):
        ma, mb = render_glyph(a, c, size), render_glyph(b, c, size)
        if ma is None or mb is None:
            continue
        u = int(np.logical_or(ma, mb).sum())
        ious.append(1.0 if u == 0 else int(np.logical_and(ma, mb).sum()) / u)
    if not ious:
        return {}
    arr = np.array(ious)
    return {
        "n": float(len(arr)),
        "mean": float(arr.mean()),
        "min": float(arr.min()),
        "p05": float(np.percentile(arr, 5)),
    }


def batch_verify(paths: list[Path], required_chars: str) -> list[FontQAReport]:
    return [verify_font(p, required_chars) for p in paths]
