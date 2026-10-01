"""接触印样（``review.py``）的测试。

## 这个模块要守住的三件事

1. **该报的必须报，不许静默少图**。印样少一张而没人知道，
   比不生成印样更危险 —— 用户会以为"待复核的就这几张"。
   所以读不出来的图必须出现在 ``unreadable`` 里。
2. **几何信息缺失要如实说明**。旧工作区（`localize.json` 没存坐标）
   画不出框，必须明确告诉用户"是数据没有"，而不是画一张没有框的图
   让人以为检测出了 0 个文字块。
3. **不能因为缺产物就丢掉这一格**。没被改动过的图不会写进 ``out/``，
   这时"产物就是原图"，不是"缺产物"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.images.io import imwrite_bgr  # noqa: E402
from novaloc.review import SheetResult, contact_sheet  # noqa: E402


class FakeWS:
    """够用的 Workspace 替身：只用到 ``read_json`` / ``source_dir`` / ``p``。"""

    def __init__(self, root: Path, records: list[dict]) -> None:
        self.root = root
        self.source_dir = root / "src"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(parents=True, exist_ok=True)
        (root / "images" / "localize.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )

    def read_json(self, rel: str, default=None):  # noqa: ANN001, ANN201
        p = self.root / rel
        if not p.is_file():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


def _img(w: int = 120, h: int = 60, color: tuple[int, int, int] = (30, 90, 200)) -> np.ndarray:
    """造一张有内容的图（纯色也要有变化，否则"差异检测"无意义）。"""
    a = np.full((h, w, 3), color, dtype=np.uint8)
    a[: h // 3, :] = (240, 240, 240)
    return a


def _record(
    uid: str,
    path: str,
    *,
    needs_review: int = 1,
    changed: bool = True,
    blocks: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    return {
        "uid": uid,
        "path": path,
        "ok": True,
        "translated": len(blocks or []),
        "needs_review": needs_review,
        "changed": changed,
        "warnings": warnings or [],
        "blocks": blocks or [],
    }


def _block(bid: str, box, **kw) -> dict:  # noqa: ANN001, ANN003
    d = {
        "id": bid,
        "source": "Start",
        "target": "开始",
        "ok": True,
        "overflow": False,
        "too_small": False,
        "warnings": [],
        "box": list(box),
    }
    d.update(kw)
    return d


def _make(tmp_path: Path, records: list[dict], *, with_out: bool = True) -> FakeWS:
    ws = FakeWS(tmp_path, records)
    for r in records:
        src = ws.source_dir / r["path"]
        src.parent.mkdir(parents=True, exist_ok=True)
        imwrite_bgr(src, _img())
        if with_out and r.get("changed"):
            dst = ws.p("out") / r["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 产物用另一种颜色，便于确认"对照"两半确实不同
            imwrite_bgr(dst, _img(color=(200, 60, 30)))
    return ws


# ---------------------------------------------------------------------------
# 一、基本产出
# ---------------------------------------------------------------------------


def test_writes_a_sheet(tmp_path: Path) -> None:
    ws = _make(tmp_path, [_record("a", "img/a.png", blocks=[_block("a#0", (5, 5, 40, 20))])])
    res = contact_sheet(ws, per_sheet=4, cols=2, cell=120)
    assert res.ok, res.summary()
    assert res.sheets == 1
    assert res.out_paths and res.out_paths[0].is_file()
    assert res.items == 1


def test_only_flagged_by_default(tmp_path: Path) -> None:
    """默认只拼 ``needs_review`` 非 0 的图。"""
    ws = _make(
        tmp_path,
        [
            _record("a", "img/a.png", needs_review=1),
            _record("b", "img/b.png", needs_review=0),
        ],
    )
    res = contact_sheet(ws, cell=100)
    assert res.items == 1, "needs_review=0 的图不该被拼进来"


def test_all_images_flag(tmp_path: Path) -> None:
    ws = _make(
        tmp_path,
        [
            _record("a", "img/a.png", needs_review=1),
            _record("b", "img/b.png", needs_review=0),
        ],
    )
    res = contact_sheet(ws, only_flagged=False, cell=100)
    assert res.items == 2


def test_no_records_is_not_an_error(tmp_path: Path) -> None:
    """一张都没有时返回空结果，**不报错**（这不是失败）。"""
    ws = FakeWS(tmp_path, [])
    res = contact_sheet(ws)
    assert res.items == 0
    assert res.out_paths == []
    assert not res.ok  # ok 要求有产物；但没抛异常


def test_limit(tmp_path: Path) -> None:
    recs = [_record(f"u{i}", f"img/{i}.png") for i in range(6)]
    ws = _make(tmp_path, recs)
    res = contact_sheet(ws, limit=2, cell=100)
    assert res.items == 2


def test_multiple_sheets(tmp_path: Path) -> None:
    recs = [_record(f"u{i}", f"img/{i}.png") for i in range(5)]
    ws = _make(tmp_path, recs)
    res = contact_sheet(ws, per_sheet=2, cols=2, cell=100)
    assert res.sheets == 3, f"5 张按每张 2 格应出 3 张印样，实际 {res.sheets}"
    assert all(p.is_file() for p in res.out_paths)


# ---------------------------------------------------------------------------
# 二、★ 如实报告：不许静默少图
# ---------------------------------------------------------------------------


def test_unreadable_source_is_reported_not_skipped(tmp_path: Path) -> None:
    """★ 原图丢了必须报出来，而且要**阻止 ok**。

    静默少一张是最坏的结果：用户看印样会以为"待复核的就这些"。
    """
    ws = _make(tmp_path, [_record("a", "img/a.png"), _record("b", "img/b.png")])
    (ws.source_dir / "img" / "a.png").unlink()
    res = contact_sheet(ws, cell=100)
    assert res.unreadable, "读不出来的图必须出现在 unreadable 里"
    assert not res.ok, "有读不出来的图时 ok 必须为假"


def test_missing_out_falls_back_to_source(tmp_path: Path) -> None:
    """★ 产物不存在 ≠ 缺这一格。

    `images_localize` 只把**改动过**的图写进 `out/`。没改动的图
    产物就是原图，必须照样拼进来，否则审校会漏掉"这张没动"。
    """
    ws = _make(tmp_path, [_record("a", "img/a.png", changed=False)], with_out=False)
    res = contact_sheet(ws, cell=100)
    assert res.items == 1
    assert res.sheets == 1, "没产物的图也必须出现在印样里"
    assert res.ok, res.summary()


def test_missing_geometry_is_counted(tmp_path: Path) -> None:
    """★ 旧工作区没有坐标时要**数出来**并给出可执行指引。

    画不出框时用户必须知道是"数据没有"，而不是"这张图确实没文字"。
    """
    ws = _make(
        tmp_path,
        [_record("a", "img/a.png", blocks=[{"id": "a#0", "source": "S", "target": "T"}])],
    )
    res = contact_sheet(ws, cell=100)
    assert res.missing_geometry == 1, "没有 box/quad 的记录必须被计数"
    assert "重跑" in res.summary()


def test_geometry_present_is_not_counted(tmp_path: Path) -> None:
    ws = _make(tmp_path, [_record("a", "img/a.png", blocks=[_block("a#0", (5, 5, 40, 20))])])
    res = contact_sheet(ws, cell=100)
    assert res.missing_geometry == 0
    assert res.drawn_boxes == 1


# ---------------------------------------------------------------------------
# 三、绘制
# ---------------------------------------------------------------------------


def test_boxes_are_drawn(tmp_path: Path) -> None:
    """画了框的产物应当和没画框的不同（真的画上去了）。"""
    recs = [_record("a", "img/a.png", blocks=[_block("a#0", (10, 10, 90, 40))])]
    ws = _make(tmp_path, recs)
    with_boxes = contact_sheet(ws, cell=160, draw_boxes=True, before_after=False)
    a = with_boxes.out_paths[0].read_bytes()
    ws2 = _make(tmp_path / "nobox", recs)
    without = contact_sheet(ws2, cell=160, draw_boxes=False, before_after=False)
    b = without.out_paths[0].read_bytes()
    assert a != b, "开关 draw_boxes 必须真的改变产物"


def test_quad_geometry_is_used(tmp_path: Path) -> None:
    """有 quad（斜排文字）时用四边形，而不是退回 box。"""
    blk = _block("a#0", (10, 10, 90, 40))
    blk.pop("box")
    blk["quad"] = [[12, 14], [88, 10], [92, 44], [10, 48]]
    ws = _make(tmp_path, [_record("a", "img/a.png", blocks=[blk])])
    res = contact_sheet(ws, cell=140)
    assert res.drawn_boxes == 1
    assert res.missing_geometry == 0


def test_after_only_mode(tmp_path: Path) -> None:
    ws = _make(tmp_path, [_record("a", "img/a.png")])
    res = contact_sheet(ws, before_after=False, cell=120)
    assert res.sheets == 1


def test_no_records_geometry_not_blamed(tmp_path: Path) -> None:
    """没有记录时不该报"缺几何信息"。"""
    ws = FakeWS(tmp_path, [])
    res = contact_sheet(ws)
    assert res.missing_geometry == 0


# ---------------------------------------------------------------------------
# 四、边界
# ---------------------------------------------------------------------------


def test_tiny_cell_does_not_crash(tmp_path: Path) -> None:
    """单元格极小时不能崩（缩放到 1x1 也要能出图）。"""
    ws = _make(tmp_path, [_record("a", "img/a.png", blocks=[_block("a#0", (1, 1, 5, 5))])])
    res = contact_sheet(ws, cell=40)
    assert res.sheets == 1


def test_cols_clamped(tmp_path: Path) -> None:
    """列数被钳到合法范围（传 0 / 99 都不能崩）。"""
    recs = [_record(f"u{i}", f"img/{i}.png") for i in range(3)]
    ws = _make(tmp_path, recs)
    assert contact_sheet(ws, cols=0, cell=80).sheets >= 1
    assert contact_sheet(ws, cols=99, cell=80).sheets >= 1


def test_bad_rel_path_is_skipped(tmp_path: Path) -> None:
    ws = _make(tmp_path, [_record("a", "")])
    res = contact_sheet(ws)
    assert res.items == 0, "没有 path 的记录没法拼，跳过即可"


def test_summary_mentions_unreadable(tmp_path: Path) -> None:
    r = SheetResult(unreadable=["x.png"])
    assert "读不出来" in r.summary()


@pytest.mark.parametrize("flag", [True, False])
def test_warnings_shown(tmp_path: Path, flag: bool) -> None:
    ws = _make(tmp_path, [_record("a", "img/a.png", warnings=["跳过 2 个疑似幻觉文字块"])])
    res = contact_sheet(ws, cell=120, before_after=flag)
    assert res.sheets == 1
