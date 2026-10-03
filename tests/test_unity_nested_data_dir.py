r"""★ 嵌套的 Unity 工程：**检测、抽取、回写**三处必须都看到同一批目录。

## 背景（实测）

库里 7 个 Unity 游戏被判成 `unknown`，而它们的 Unity 特征文件**全都在**：

    Dusk City Uncensored\Dusk City_Data\resources.assets
    ^^^^^^^^^^^^^^^^^^^^ ← 多套了一层包装目录

`detect` 用 `game_dir.iterdir()`（**只扫顶层**）⇒ conf 0.0 ⇒ 引擎 `unknown`
⇒ **连抽取都不跑**。这类"解包后又套一层"在搬运/自制发布里很常见。

## 判据

1. 嵌套的 `*_Data` 必须被**检测**到；
2. 嵌套的 `*_Data` 下的明文文本必须被**抽取**到（这是检测的**目的** ——
   只修检测会让状态变成"认出了引擎、却没抽到任何文本"，
   比原来更难排查）；
3. 抽取出的 `location.file` 必须是**相对游戏根**的路径，
   这样回写能按相同相对路径写进 `out/`；
4. **顶层布局不能被弄坏**（回归）；
5. 遍历必须**有限**（不能为了找 `_Data` 把 4 GB 的资源全走一遍）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novaloc.engines.unity import (
    _UNITY_DATA_MAX_DEPTH,
    UnityAdapter,
    _find_unity_dirs,
)


@pytest.fixture(autouse=True)
def _isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """★ 数据根必须**按测试**隔离，绝不能污染进程级环境。

    ## 我在这里踩过一个真坑（全量套件里 9 条 image/OCR 用例挂掉）

    第一版是**模块级**：

        os.environ.setdefault("NOVALOC_DATA_ROOT", r"D:\\NovaLoc\\_unity_nested_test")

    `os.environ` 是**进程全局**的，而 pytest 整个套件跑在同一个进程里。
    于是这条 `setdefault` 之后的所有测试都以为数据根是那个临时目录：

        E  处理失败：缺少 2 个离线模型：PP-OCRv6_det_medium.onnx、…；
           请放到 D:\\NovaLoc\\_unity_nested_test\\models\\rapidocr

    ⇒ `test_image_alpha_preserved.py` / `test_ocr_cache.py` 共 **9 条**
      在**全量套件**里必挂，单独跑却全绿（因为那时没人设过这个变量）。

    ⚠️ 这正是一个"**测试之间互相污染**"的典型：单跑绿、全跑红，
       而失败信息指向的是 OCR 模型缺失 —— 与 Unity 毫无关系，
       极难从失败信息反推到真凶。⇒ 只能用 `monkeypatch`（自动复原）。
    """
    monkeypatch.setenv("NOVALOC_DATA_ROOT", str(tmp_path_factory.mktemp("data")))


def _ctx(tmp_path: Path) -> SimpleNamespace:
    """最小 Context 替身。

    ⚠️ `ctx.config` 不能是 `None`：`extract_text` 会读
    `self.cfg.pack.scan_bundles`（UnityFS 包内文本开关），
    传 `None` 会 `AttributeError: 'NoneType' object has no attribute 'pack'`。
    第一版就是 `config=None`，4 个抽取用例全挂在同一个地方。
    """
    from novaloc.core.config import get_config

    return SimpleNamespace(
        config=get_config(), events=None, logger=None, root=tmp_path, workspace=None
    )


def _make_unity(game: Path, *, depth: int, name: str = "MyGame") -> Path:
    """造一个最小 Unity 工程，`*_Data` 位于 `depth` 层包装目录之下。"""
    if depth == 0:
        dd = game / f"{name}_Data"
    else:
        wrap = game
        for i in range(depth):
            wrap = wrap / f"wrap{i}"
        dd = wrap / f"{name}_Data"
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "globalgamemanagers").write_bytes(b"\x00" * 32 + b"2021.3.16f1")
    (dd / "resources.assets").write_bytes(b"\x00" * 16)
    (dd / "Managed").mkdir(exist_ok=True)
    (dd / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ")
    # 明文文本（Unity 适配器会抽这个）
    (dd / "StreamingAssets").mkdir(exist_ok=True)
    (dd / "StreamingAssets" / "strings.json").write_text(
        '{"greeting": "Hello there, friend."}', encoding="utf-8"
    )
    return dd


# ---------------------------------------------------------------------------
# 1) `_find_unity_dirs` 本身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_finds_data_dir_at_various_depths(tmp_path: Path, depth: int) -> None:
    """0~3 层包装都要能找到（4 层是上限的分界，见下一个用例）。"""
    game = tmp_path / "g"
    game.mkdir()
    dd = _make_unity(game, depth=depth)
    found = _find_unity_dirs(game)
    assert dd in found, f"depth={depth} 时应当找到 {dd}，实际 {found}"


def test_respects_max_depth(tmp_path: Path) -> None:
    """★ 超过上限就找不到 —— 这是**故意的**，避免全盘遍历。

    同时它必须**不报错**（只记 debug 日志），因为"找不到"是合法结果。
    """
    game = tmp_path / "g"
    game.mkdir()
    deep = _UNITY_DATA_MAX_DEPTH + 2
    dd = _make_unity(game, depth=deep)
    found = _find_unity_dirs(game)
    assert dd not in found, f"超过 {_UNITY_DATA_MAX_DEPTH} 层不该被找到"


def test_skips_heavy_dirs(tmp_path: Path) -> None:
    """不该下潜进 `backup`/`node_modules` 这类目录。"""
    game = tmp_path / "g"
    game.mkdir()
    dd = _make_unity(game / "backup", depth=0)
    found = _find_unity_dirs(game)
    assert dd not in found, "不该进 backup/ 找"


def test_top_level_still_works(tmp_path: Path) -> None:
    """★ 回归：顶层布局（绝大多数游戏）不能被改坏。"""
    game = tmp_path / "g"
    game.mkdir()
    dd = _make_unity(game, depth=0)
    assert dd in _find_unity_dirs(game)


# ---------------------------------------------------------------------------
# 2) detect：嵌套的要认出来
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_detect_recognises_nested_unity(tmp_path: Path, depth: int) -> None:
    """★★ 本次修复的核心：嵌套的 Unity 工程必须被判成 `unity`。"""
    game = tmp_path / "g"
    game.mkdir()
    _make_unity(game, depth=depth)
    info = UnityAdapter(_ctx(tmp_path)).detect(game)
    assert info.engine_id == "unity"
    assert info.confidence > 0.5, f"depth={depth} 置信度过低：{info.confidence}"
    assert info.confidence > 0, "嵌套时也不能是 0（那等于放弃）"


def test_detect_reports_nesting_in_evidence(tmp_path: Path) -> None:
    """★ 证据里要能看出"嵌套在哪" —— 否则排查时看不出为什么以前判不出来。"""
    game = tmp_path / "g"
    game.mkdir()
    _make_unity(game, depth=1)
    info = UnityAdapter(_ctx(tmp_path)).detect(game)
    joined = " ".join(info.evidence)
    assert "嵌套" in joined, f"应当说明是嵌套的：{info.evidence}"


def test_detect_without_data_dir_returns_unknown(tmp_path: Path) -> None:
    """没有 `*_Data` 就**不该**硬判成 unity（反向保护）。"""
    game = tmp_path / "g"
    game.mkdir()
    (game / "readme.txt").write_text("nothing here", encoding="utf-8")
    info = UnityAdapter(_ctx(tmp_path)).detect(game)
    assert info.confidence == 0.0, f"不该有置信度：{info.confidence}"


# ---------------------------------------------------------------------------
# 3) extract：嵌套的目录里必须**真的抽出文本**
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_extract_finds_text_in_nested_data_dir(tmp_path: Path, depth: int) -> None:
    """★★ 只修检测是不够的：抽取也必须看到同一个目录。

    否则状态会变成"引擎认出来了、却一条文本都没抽到" ——
    那比原来（判成 unknown）**更难排查**，因为状态字段显示成功了。
    """
    game = tmp_path / "g"
    game.mkdir()
    _make_unity(game, depth=depth)
    units, _rep = UnityAdapter(_ctx(tmp_path)).extract_text(game)
    srcs = [u.source for u in units]
    assert any("Hello there" in s for s in srcs), (
        f"depth={depth} 应当抽出明文文本，实际 {srcs}"
    )
    # 3) location.file 必须**相对游戏根**
    for u in units:
        rel = u.location.file.replace("\\", "/")
        assert not rel.startswith("/"), f"不该是绝对路径：{rel}"
        assert str(tmp_path) not in rel, f"不该含外层绝对路径：{rel}"
        # 相对游戏根 ⇒ 拼回去必须真实存在
        assert (game / rel).exists(), f"{rel} 相对游戏根拼不回去"


def test_extract_relative_path_is_writeback_compatible(tmp_path: Path) -> None:
    """★ 用 `location.file` 拼 `out_dir` 必须落在与源相同的相对位置。

    这是回写能工作的**前提**：`apply` 拿 `out_dir / location.file` 定位产物。
    """
    game = tmp_path / "g"
    game.mkdir()
    _make_unity(game, depth=1)
    units, _rep = UnityAdapter(_ctx(tmp_path)).extract_text(game)
    assert units, "应当抽到文本"
    rel = units[0].location.file.replace("\\", "/")
    # 关键：相对路径里**必须含**那层包装目录，这样 out/ 结构才与源一致
    assert "wrap0/" in rel, f"相对路径应当保留包装层：{rel}"
    assert rel.endswith("strings.json"), f"意外路径：{rel}"
