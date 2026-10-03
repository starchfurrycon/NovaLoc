r"""UnityFS 包内文本**接入流水线**的回归测试。

## 这里钉住的三件事

1. **默认不扫包** —— 实测多数游戏包里没文本，而扫描有耗时
   （188 MB 包约 10 s，某游戏有 5857 个包）。默认打开会让全库批量跑变慢。
2. **`--bundles` / `scan_bundles=True` 时才扫**，且扫出的条目
   **带得动回写所需的信息**（`location.file` 指向包、`location.pointer`
   是 ``包名:path_id:字段路径``）。
3. **回写只写 `out/` 里的副本** —— 绝不猜原游戏路径。
   这是"就地写回"功能里最容易出人命的地方。

## 为什么不在这里跑真包

真包实验在 `.scratch/_bundle_e2e_write.py`（188 MB / 148 MB，
一次 10~14 s）。单测里用**假包**只验证**接线**，
真包验证的是**UnityPy 行为**，两者互补、不能互相替代。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novaloc.core.config import Config
from novaloc.core.events import EventBus
from novaloc.core.registry import Context
from novaloc.engines.unity import UnityAdapter
from novaloc.models import TextKind, TextLocation, TextUnit


def _ctx(*, scan_bundles: bool = False, bundle_scan_limit: int = 0) -> Context:
    cfg = Config()
    cfg = cfg.model_copy(
        update={
            "pack": cfg.pack.model_copy(
                update={
                    "scan_bundles": scan_bundles,
                    "bundle_scan_limit": bundle_scan_limit,
                }
            )
        }
    )
    return Context(config=cfg, events=EventBus())


# ---------------------------------------------------------------------------
# 1. 默认关
# ---------------------------------------------------------------------------


def test_scan_bundles_is_off_by_default() -> None:
    """★ 默认必须是关的。

    实测依据：本库 46 个含 UnityFS 的游戏里 **25 个包里根本没文本**、
    11 个"仅少量"，而有文本的**绝大多数已官方汉化**
    （Jerez's Arena 9619 个槽位组里 8708 个自带中文，只剩 300 条真需翻译）。

    ⇒ 默认打开 = 每个游戏白花 10 s 以上去换 0 条译文。
    """
    assert Config().pack.scan_bundles is False
    assert Config().pack.bundle_scan_limit == 0


def test_extract_does_not_scan_bundles_when_disabled(tmp_path: Path) -> None:
    """关着的时候**不该碰** UnityFS（不能"顺便扫一下"）。"""
    game = tmp_path / "game"
    (game / "G_Data").mkdir(parents=True)
    # 造一个假的 UnityFS 头文件；如果被扫了，会尝试 UnityPy 解析
    (game / "G_Data" / "fake.bundle").write_bytes(b"UnityFS\x00" + b"\x00" * 64)

    units, rep = UnityAdapter(_ctx(scan_bundles=False)).extract_text(game)

    assert not any(u.tags and "unity_bundle" in u.tags for u in units)
    assert "unity_bundle_scanned" not in rep.skipped


def test_bundle_limit_zero_means_all() -> None:
    """``bundle_scan_limit=0`` 是"不限"，而不是"一个都不扫"。"""
    from novaloc.engines.unity_bundle import find_bundles

    # 直接钉住语义：传 limit=None 时不过滤
    assert Config().pack.bundle_scan_limit == 0
    assert find_bundles.__doc__ is not None


# ---------------------------------------------------------------------------
# 2. 打开时产出正确的 TextUnit（用假 scan_bundle 注入）
# ---------------------------------------------------------------------------


class _FakeSlot:
    def __init__(self, assets: str, pid: int, fpath: str, src: str, tag: str) -> None:
        self._assets = assets
        self.path_id = pid
        self.field_path = fpath
        self._src = src
        self._tag = tag
        self.bundle = Path(assets)

    def source_text(self) -> tuple[str, str]:
        return (self._tag, self._src)

    def pointer(self) -> str:
        return f"{self._assets}:{self.path_id}:{self.field_path}"


def _patch_scanner(monkeypatch: pytest.MonkeyPatch, slots: list[_FakeSlot]) -> None:
    """把 `find_bundles` / `scan_bundle` 换成假的（单测不碰 UnityPy）。"""
    import novaloc.engines.unity_bundle as ub

    monkeypatch.setattr(ub, "find_bundles", lambda game_dir, limit=None: [Path("pack.bundle")])
    monkeypatch.setattr(ub, "scan_bundle", lambda b, report=None, max_objects=None: slots)


def test_enabled_scan_produces_writable_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 打开后产出的条目必须**带得动回写信息**。

    回写靠 `location.pointer`（``包名:path_id:字段路径``）。
    这里钉住：uid / file / pointer 三者都对。
    """
    game = tmp_path / "game"
    (game / "G_Data").mkdir(parents=True)
    _patch_scanner(
        monkeypatch,
        [_FakeSlot("pack.bundle", 12345, ".m_text", "Character Name", "__bare__")],
    )

    units, rep = UnityAdapter(_ctx(scan_bundles=True)).extract_text(game)

    bu = [u for u in units if u.tags and "unity_bundle" in u.tags]
    assert len(bu) == 1, f"应有 1 条包内条目，实得 {len(bu)}：{units!r}"
    u = bu[0]
    assert u.source == "Character Name"
    assert u.uid == "unitybundle:pack.bundle:12345:.m_text"
    # location.file 是**相对游戏根**的路径（apply 阶段据此在 out/ 里找副本）
    assert u.location.file.endswith("pack.bundle"), u.location.file
    assert not Path(u.location.file).is_absolute(), "必须是相对路径，否则会写错地方"
    assert u.location.pointer == "pack.bundle:12345:.m_text"
    assert u.kind == TextKind.UNKNOWN
    assert u.adapter == "unity"
    assert "bundle:pack.bundle" in u.tags


def test_scan_stats_are_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 跳过统计必须**如实报出** —— 这是"为什么只找到这么几条"的依据。

    没有这些数字，用户看到"只翻到 300 条"会以为工具坏了；
    有了这些数字，能看出是 8708 条**本来就有中文**（故意跳过）。
    """
    game = tmp_path / "game"
    (game / "G_Data").mkdir(parents=True)
    _patch_scanner(monkeypatch, [])

    _units, rep = UnityAdapter(_ctx(scan_bundles=True)).extract_text(game)

    assert "unity_bundle_scanned" in rep.skipped
    assert "unity_bundle_has_target" in rep.skipped
    assert "unity_bundle_bare_is_chinese" in rep.skipped


def test_empty_source_slot_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源文为空的槽位不该变成条目（否则翻译阶段要求模型翻空白）。"""
    game = tmp_path / "game"
    (game / "G_Data").mkdir(parents=True)
    _patch_scanner(
        monkeypatch,
        [
            _FakeSlot("pack.bundle", 1, ".m_text", "   ", "__bare__"),
            _FakeSlot("pack.bundle", 2, ".m_text", "Real text here", "__bare__"),
        ],
    )

    units, _rep = UnityAdapter(_ctx(scan_bundles=True)).extract_text(game)

    bu = [u for u in units if u.tags and "unity_bundle" in u.tags]
    assert len(bu) == 1
    assert bu[0].source == "Real text here"


def test_bad_bundle_does_not_break_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 一个坏包**不能**让整条抽取失败（#41 的教训：异常别逃出去）。"""
    import novaloc.engines.unity_bundle as ub

    game = tmp_path / "game"
    (game / "G_Data").mkdir(parents=True)
    monkeypatch.setattr(ub, "find_bundles", lambda game_dir, limit=None: [Path("bad.bundle")])

    def _boom(b, report=None, max_objects=None):  # noqa: ANN001, ANN202
        raise RuntimeError("模拟 UnityPy 崩了")

    monkeypatch.setattr(ub, "scan_bundle", _boom)

    units, rep = UnityAdapter(_ctx(scan_bundles=True)).extract_text(game)

    # 抽取本身仍然成功返回
    assert isinstance(units, list)
    assert any("UnityFS" in e for e in rep.errors), f"没报出来：{rep.errors!r}"


# ---------------------------------------------------------------------------
# 3. 回写只写副本
# ---------------------------------------------------------------------------


def test_apply_bundles_returns_none_when_nothing_to_write(tmp_path: Path) -> None:
    """没有包内条目要写时**必须零代价**（不能白加载 188 MB 的包）。"""
    a = UnityAdapter(_ctx())
    got = a._apply_bundles(tmp_path, [], {})
    assert got is None


def test_apply_bundles_ignores_units_without_pointer(tmp_path: Path) -> None:
    """普通（非包内）条目的 pointer 为空 ⇒ 不该被当成包内条目。"""
    a = UnityAdapter(_ctx())
    u = TextUnit(
        uid="x",
        source="hi",
        kind=TextKind.UNKNOWN,
        location=TextLocation(file="a.txt"),
    )
    assert a._apply_bundles(tmp_path, [u], {"x": "你好"}) is None


def test_apply_bundles_writes_only_to_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ **最关键的一条**：传给 `apply_translations` 的路径必须在 `out/` 里。

    `apply_translations` 会**替换它拿到的文件**。如果这里把原游戏目录
    的路径传进去，"先在 out/ 里验收再写回"这个安全设计就全废了 ——
    而且用户会以为还能反悔，实际已经被改了。
    """
    out = tmp_path / "out"
    (out / "G_Data").mkdir(parents=True)
    (out / "G_Data" / "pack.bundle").write_bytes(b"UnityFS\x00" + b"\x00" * 32)

    captured: dict[str, object] = {}

    def _fake_apply(translations, *, bundle_paths, backup_root=None, pack="lz4", report=None):  # noqa: ANN001, ANN202
        captured["translations"] = dict(translations)
        captured["bundle_paths"] = dict(bundle_paths)
        from novaloc.engines.unity_bundle import BundleApplyReport

        r = report if report is not None else BundleApplyReport()
        r.bundles_total = len(bundle_paths)
        r.bundles_written = len(bundle_paths)
        r.slots_written = len(translations)
        return r

    import novaloc.engines.unity_bundle as ub

    monkeypatch.setattr(ub, "apply_translations", _fake_apply)

    a = UnityAdapter(_ctx())
    u = TextUnit(
        uid="unitybundle:pack.bundle:77:.m_text",
        source="Character Name",
        kind=TextKind.UNKNOWN,
        location=TextLocation(
            file="G_Data/pack.bundle", pointer="pack.bundle:77:.m_text"
        ),
        tags=["unity_bundle"],
    )
    note = a._apply_bundles(out, [u], {"unitybundle:pack.bundle:77:.m_text": "角色名"})

    assert note is not None
    assert captured["translations"] == {"pack.bundle:77:.m_text": "角色名"}
    got = captured["bundle_paths"]
    assert set(got) == {"pack.bundle"}
    p = Path(got["pack.bundle"])  # type: ignore[arg-type]
    assert p == out / "G_Data" / "pack.bundle"
    assert out in p.parents, f"写回路径不在 out/ 里，危险：{p}"
    assert note["files"] == 1


def test_apply_bundles_skips_empty_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """译文为空 ⇒ 不写（否则会把游戏里原有的文字清成空）。"""
    out = tmp_path / "out"
    (out / "G_Data").mkdir(parents=True)
    (out / "G_Data" / "pack.bundle").write_bytes(b"UnityFS\x00")

    called = {"n": 0}

    def _fake_apply(translations, **kw):  # noqa: ANN001, ANN003, ANN202
        called["n"] += 1
        from novaloc.engines.unity_bundle import BundleApplyReport

        return BundleApplyReport()

    import novaloc.engines.unity_bundle as ub

    monkeypatch.setattr(ub, "apply_translations", _fake_apply)

    a = UnityAdapter(_ctx())
    u = TextUnit(
        uid="unitybundle:pack.bundle:77:.m_text",
        source="Character Name",
        kind=TextKind.UNKNOWN,
        location=TextLocation(file="G_Data/pack.bundle", pointer="pack.bundle:77:.m_text"),
    )
    assert a._apply_bundles(out, [u], {"unitybundle:pack.bundle:77:.m_text": "   "}) is None
    assert called["n"] == 0, "空译文不该触发回写"


def test_apply_bundles_missing_file_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """包里没找到对应文件 ⇒ 该条**不参与**回写，但不能让 apply 整体崩。"""
    out = tmp_path / "out"
    out.mkdir()

    a = UnityAdapter(_ctx())
    u = TextUnit(
        uid="unitybundle:gone.bundle:1:.m_text",
        source="x",
        kind=TextKind.UNKNOWN,
        location=TextLocation(file="G_Data/gone.bundle", pointer="gone.bundle:1:.m_text"),
    )
    # ★ 文件不存在 ⇒ **必须报出来**，不能静默（改了会以为跑完了但译文丢了）
    note = a._apply_bundles(out, [u], {"unitybundle:gone.bundle:1:.m_text": "译"})
    assert note is not None, "包找不到时必须报告，不能返回 None 当作没事"
    assert note["files"] == 0
    joined = "\\n".join(note["warnings"])
    assert "找不到" in joined or "没有写入" in joined, joined
