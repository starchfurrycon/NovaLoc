r"""接触印样：把审校对象拼成一张大图，一眼看完。

## 为什么需要它

贴图汉化里"需要人工看一眼"的图占比不高但绝对数不小
（DemonsRoots 实测 1,141 张里 **61 张**有 `needs_review`）。逐张打开看
非常低效 —— 而这正是本项目里**唯一必须靠人眼**的环节：
机器已经尽力（检测、翻译、inpaint、重绘都做完并自检过），
剩下的判断是"看着别扭吗"，只有人能下。

所以这个模块**不增加任何能力**，只把"呈现"这一环补上：
把 N 张图缩放到统一单元格、拼成一张网格大图、每格标上编号与文件名。

## 两种排法

* ``before_after``（默认两列）—— 左原图右产物，**最有用**：
  判断"翻得对不对、有没有残留外文"必须两图对照；
* ``after_only``（单列）—— 图很多时先快速扫一遍产物。

## 为什么"带框"要用落盘的几何信息

图上要画出文字框，就必须知道"哪块文字在哪"。`annotate.py` 需要
``block.quad`` / ``block.box`` —— 这些以前**没有落盘**
（见 `pipeline/stages.py` 里那段注释），所以本模块会**如实报告**
"这批记录没有几何信息，无法画框"，而不是画一张没有框的图
让人以为检测出了 0 个文字块。

## 用法

::

    from novaloc.review import contact_sheet
    contact_sheet(ws, out_dir=ws.p("images", "analyzed"))

命令行::

    novaloc review <workspace-id>
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .images.annotate import _color_for  # noqa: PLC2701
from .images.io import imread_bgr, imwrite_bgr

log = logging.getLogger(__name__)

#: 单个单元格的最大边长（像素）。400 是折中：
#: 再小就看不清贴图上的小字（这个模块的目的就是看小字），
#: 再大则一张印样装不下几张图、失去"一眼看完"的意义。
CELL_MAX = 400

#: 网格最多几列（两列排法时这是**图组**数）
MAX_COLS = 4

#: 标签条高度（放编号 + 文件名 + 状态）
LABEL_H = 34


@dataclass
class SheetItem:
    """印样里的一格。"""

    uid: str
    path: str
    caption: str = ""
    status: str = ""
    before: np.ndarray | None = None
    after: np.ndarray | None = None
    blocks: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    origin: Path | None = None
    """原图的真实路径（读不出来时用来报告到底缺哪个文件）。"""


@dataclass
class SheetResult:
    """一次拼版的产物与统计。"""

    out_paths: list[Path] = field(default_factory=list)
    sheets: int = 0
    items: int = 0
    drawn_boxes: int = 0
    missing_geometry: int = 0
    """有多少条记录**没有**几何信息（画不了框）。0 才是正常的。"""
    unreadable: list[str] = field(default_factory=list)
    """读不出来的原图/产物路径（如实报告，不静默跳过）。"""

    @property
    def ok(self) -> bool:
        return bool(self.out_paths) and not self.unreadable

    def summary(self) -> str:
        parts = [
            f"{self.items} 张 / {self.sheets} 张印样",
            f"画出文字框 {self.drawn_boxes} 个",
        ]
        if self.missing_geometry:
            parts.append(
                f"⚠ {self.missing_geometry} 条记录没有几何信息（画不了框）"
                " —— 这些工作区是旧版流水线跑的，重跑 `images_localize` 即可"
            )
        if self.unreadable:
            parts.append(f"⚠ {len(self.unreadable)} 张读不出来：{self.unreadable[:3]}")
        return "，".join(parts)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _fit(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """等比缩放并居中放进 ``w × h`` 的格子（空白处填深灰）。"""
    canvas = np.full((h, w, 3), 28, dtype=np.uint8)
    if img is None or img.size == 0:
        return canvas
    ih, iw = img.shape[:2]
    if ih <= 0 or iw <= 0:
        return canvas
    scale = min(w / iw, h / ih)
    # 不放大：小图放大只会变糊，看不清原始笔画
    if scale > 1.0:
        scale = 1.0
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_NEAREST
    resized = cv2.resize(img, (nw, nh), interpolation=interp)
    ox, oy = (w - nw) // 2, (h - nh) // 2
    canvas[oy : oy + nh, ox : ox + nw] = resized[:, :, :3]
    return canvas


def _to_bgr(img: np.ndarray | None) -> np.ndarray | None:
    """合成到深色底上，避免透明区域看起来是黑洞。"""
    if img is None or img.size == 0:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        rgb = img[:, :, :3].astype(np.float32)
        bg = np.full_like(rgb, 28.0)
        return (rgb * alpha + bg * (1 - alpha)).astype(np.uint8)
    return img[:, :, :3]


def _put_text(
    arr: np.ndarray,
    text: str,
    org: tuple[int, int],
    *,
    scale: float = 0.42,
    color: tuple[int, int, int] = (235, 235, 235),
    thick: int = 1,
) -> None:
    """写一行文字。

    用 ``cv2.putText``（Hershey 矢量字体）而不是 PIL + TTF：
    **它不依赖任何字体文件**（本仓库一个字体文件都不带），
    而且这里要写的全是 ASCII（编号、文件名、状态、计数）。
    非 ASCII 会被替换成 ``?`` —— 比画不出来强，且不引入依赖。
    """
    safe = text.encode("ascii", "replace").decode("ascii")
    cv2.putText(arr, safe, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def _cell(
    item: SheetItem,
    *,
    before_after: bool,
    cell: int,
) -> np.ndarray:
    """拼出一格：可选"原图 | 产物"两联，下面一条标签。"""
    inner = cell - LABEL_H
    if before_after:
        # 中间留 6 px 分隔线，让"左原右新"一眼可辨
        half = (inner - 6) // 2
        left = _fit(item.before, half, inner)
        right = _fit(item.after, half, inner)
        body = np.full((inner, inner, 3), 28, dtype=np.uint8)
        body[:, :half] = left
        body[:, half + 6 : half + 6 + half] = right
        # 顶头写 "原 / 译"
        _put_text(body, "BEFORE", (3, 13), scale=0.36, color=(150, 200, 255))
        _put_text(body, "AFTER", (half + 9, 13), scale=0.36, color=(150, 255, 180))
    else:
        body = _fit(item.after, inner, inner)

    out = np.full((cell, cell, 3), 20, dtype=np.uint8)
    out[:inner, :inner] = body

    # ---- 标签条 ----
    label = np.full((LABEL_H, cell, 3), 40, dtype=np.uint8)
    _put_text(label, item.caption[:64], (4, 13), scale=0.40)
    status = item.status or ""
    if item.warnings:
        status += f" · {len(item.warnings)} 警告"
    if status:
        _put_text(label, status[:64], (4, 27), scale=0.36, color=(120, 210, 255))
    out[inner:, :] = label
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _load_items(
    ws: Any,
    *,
    only_flagged: bool,
    limit: int | None,
    draw_boxes: bool,
    result: SheetResult,
) -> list[SheetItem]:
    """从工作区读 `localize.json` 并准备好每一格。"""
    records = ws.read_json("images/localize.json", []) or []
    items: list[SheetItem] = []
    for rec in records:
        if only_flagged and not rec.get("needs_review"):
            continue
        rel = rec.get("path") or ""
        if not rel:
            continue
        blocks = rec.get("blocks") or []
        # 几何信息缺失要**数出来**：画不出框时用户必须知道是数据没有，
        # 而不是"这张图确实没文字"。
        #
        # ▲ 判据是"box **或** quad" —— 只认 box 会把斜排文字（只有 quad）
        #   误报成缺几何信息。这两种都是合法坐标（见 models.ImageTextBlock）。
        if draw_boxes and blocks and not any(b.get("box") or b.get("quad") for b in blocks):
            result.missing_geometry += 1

        item = SheetItem(
            uid=str(rec.get("uid") or rel),
            path=rel,
            caption=Path(rel).name,
            status=(
                ("已汉化" if rec.get("changed") else "未改动")
                + (f" · 译 {rec.get('translated')}" if rec.get("translated") else "")
            ),
            blocks=blocks,
            warnings=list(rec.get("warnings") or []),
        )
        items.append(item)
        if limit and len(items) >= limit:
            break
    return items


def _load_pixels(ws: Any, item: SheetItem, result: SheetResult) -> None:
    """读原图与产物像素；读不出来就如实记下。"""
    src = ws.source_dir / item.path
    item.origin = src
    item.before = imread_bgr(src)
    if item.before is None:
        result.unreadable.append(str(src))

    # 产物在 out/ 下（`images_localize` 写的就是这里）。
    # 注意路径里可能带加密扩展名（`.rpgmvp`），`imread_bgr` 会处理。
    dst = ws.p("out") / item.path
    if dst.is_file():
        item.after = imread_bgr(dst)
    else:
        # 没被改动过的图不会写进 out/ —— 这时"产物"就是原图，
        # 而不是"缺产物"。用原图当产物，印样依然能看出"这张没动"。
        item.after = item.before

    if item.after is None and item.before is not None:
        item.after = item.before


def _annotate(item: SheetItem) -> np.ndarray | None:
    """把文字框画到产物图上（有几何信息才画）。"""
    bgr = _to_bgr(item.after)
    if bgr is None:
        return None
    arr = np.ascontiguousarray(bgr.copy())
    h, w = arr.shape[:2]
    n = 0
    for b in item.blocks:
        quad = b.get("quad")
        if quad and len(quad) == 4:
            pts = [(float(x), float(y)) for x, y in quad]
        elif b.get("box"):
            x0, y0, x1, y1 = (float(v) for v in b["box"])
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        else:
            continue
        color = _status_color(b)
        _draw_quad(arr, pts, color, h, w)
        n += 1
    return arr if n else bgr


def _status_color(b: dict[str, Any]) -> tuple[int, int, int]:
    """按块状态取颜色（与 `annotate.py` 同一套语义）。"""

    class _B:  # 只为复用 `_color_for` 的属性读取逻辑
        status = "translated"
        warnings = b.get("warnings") or []

    bb = _B()
    if not b.get("ok"):
        bb.status = "failed"
    elif b.get("overflow") or b.get("too_small"):
        bb.warnings = list(bb.warnings) + ["x"]
    return _color_for(bb)


def _draw_quad(
    arr: np.ndarray, pts: list[tuple[float, float]], color: tuple[int, int, int], h: int, w: int
) -> None:
    """画**四个角标**而不是整框 —— 不遮挡底下的文字。"""
    ivals = [(int(round(x)), int(round(y))) for x, y in pts]
    for i in range(4):
        for a, b in ((ivals[i - 1], ivals[i]), (ivals[i], ivals[(i + 1) % 4])):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length <= 0:
                continue
            seg = min(length, max(length * 0.25, 4.0))
            k = seg / length
            e = (int(round(a[0] + dx * k)), int(round(a[1] + dy * k)))
            cv2.line(arr, a, e, color, 2, cv2.LINE_AA)
    # 左上角一个小实心块，方便在缩略图上定位
    x0 = max(0, min(p[0] for p in ivals) - 4)
    y0 = max(0, min(p[1] for p in ivals) - 4)
    cv2.rectangle(arr, (x0, y0), (min(w - 1, x0 + 6), min(h - 1, y0 + 6)), color, -1)


def contact_sheet(
    ws: Any,
    *,
    out_dir: Path | None = None,
    only_flagged: bool = True,
    limit: int | None = None,
    per_sheet: int = 24,
    cols: int = MAX_COLS,
    cell: int = CELL_MAX,
    before_after: bool = True,
    draw_boxes: bool = True,
    prefix: str = "contact_sheet",
) -> SheetResult:
    """把一个工作区里需要审校的贴图拼成接触印样。

    Args:
        ws: 工作区（`novaloc.core.workspace.Workspace`）。只用到
            `read_json` / `source_dir` / `p`，便于测试替身。
        out_dir: 输出目录。默认 ``<workspace>/images/analyzed``。
        only_flagged: 只拼 ``needs_review`` 非 0 的图（默认）。
            置 False 则把**所有**处理过的图都拼进来。
        limit: 最多拼多少张（None = 全部）。
        per_sheet: 每张印样放几格。
        cols: 网格列数。
        cell: 单元格边长（像素）。
        before_after: True = 左原图右产物；False = 只放产物。
        draw_boxes: 是否把文字框画到产物上。
        prefix: 输出文件名前缀。

    Returns:
        :class:`SheetResult`。**读不出来的图会出现在 `unreadable` 里**，
        不会静默少几张 —— 印样少一张而没人知道，比不生成更危险。
    """
    result = SheetResult()
    items = _load_items(
        ws, only_flagged=only_flagged, limit=limit, draw_boxes=draw_boxes, result=result
    )
    result.items = len(items)
    if not items:
        log.info("没有需要审校的贴图（only_flagged=%s）", only_flagged)
        return result

    cols = max(1, min(cols, MAX_COLS))
    out_dir = Path(out_dir) if out_dir is not None else ws.p("images", "analyzed")
    out_dir.mkdir(parents=True, exist_ok=True)

    per_sheet = max(1, per_sheet)
    for start in range(0, len(items), per_sheet):
        chunk = items[start : start + per_sheet]
        # 最后一张不足一整格时不要把网格拉扁 —— 按实际格数算列宽
        n_cols = min(cols, len(chunk))
        n_rows = math.ceil(len(chunk) / n_cols)

        canvas = np.full((n_rows * cell, n_cols * cell, 3), 16, dtype=np.uint8)
        for i, item in enumerate(chunk):
            _load_pixels(ws, item, result)
            if item.before is None and item.after is None:
                continue
            shown = item
            if draw_boxes and item.blocks:
                annotated = _annotate(item)
                if annotated is not None:
                    # 只替换"产物"那张，原图保持原样（对照才有意义）
                    shown = SheetItem(
                        uid=item.uid,
                        path=item.path,
                        caption=item.caption,
                        status=item.status,
                        before=item.before,
                        after=annotated,
                        blocks=item.blocks,
                        warnings=item.warnings,
                        origin=item.origin,
                    )
                    result.drawn_boxes += sum(
                        1 for b in item.blocks if b.get("box") or b.get("quad")
                    )
            r, c = divmod(i, n_cols)
            canvas[r * cell : (r + 1) * cell, c * cell : (c + 1) * cell] = _cell(
                shown, before_after=before_after, cell=cell
            )

        idx = start // per_sheet + 1
        name = f"{prefix}_{idx:02d}.png" if len(items) > per_sheet else f"{prefix}.png"
        path = out_dir / name
        if imwrite_bgr(path, canvas):
            result.out_paths.append(path)
            result.sheets += 1
            log.info("接触印样已写出：%s（%d 格）", path, len(chunk))
        else:
            log.warning("接触印样写出失败：%s", path)

    return result


__all__ = ["CELL_MAX", "SheetItem", "SheetResult", "contact_sheet"]
