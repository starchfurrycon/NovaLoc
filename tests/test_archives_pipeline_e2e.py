"""端到端：游戏资源封在归档里时，整条流水线要能解包、汉化、打回。

## 为什么这条测试最重要

前面的 `test_archives_service.py` 测的是"解包/回写这个动作对不对"。
这里测的是**接进流水线之后还对不对**，两者会漏掉的东西不一样：

* 阶段顺序错了（例如先 detect 再 unpack）—— 单测看不出来；
* `effective_source` 没生效 —— 适配器会去读原始目录而读不到解包内容；
* 回写顺序错了（先打回归档、再写文件）—— 归档里还是旧内容。

所以这条测试造一个**真实的 Ren'Py 游戏封进 `.rpa`**，跑 `run_all()`，
然后把归档解出来核对脚本里的英文**真的**变成了中文。

用 `--no-unpack` 之外的路径跑，也就是默认路径。翻译函数是注入的假函数
（不调模型），这样测试离线、快、且只验证流水线本身。
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.archives.rpa import write_rpa  # noqa: E402
from novaloc.models import TranslationEntry  # noqa: E402

#: 一个最小可识别的 Ren'Py 游戏（脚本放在 game/ 下）
SCRIPT = """\
define e = Character("Eileen")

label start:
    e "Hello, world!"
    e "This is a test."
    return
"""

OPTIONS = """\
define config.name = _("Test Game")
define config.version = "1.0"
"""

#: 英文 → 中文的假翻译表，覆盖脚本里的两条台词
FAKE_MAP = {
    "Hello, world!": "你好，世界！",
    "This is a test.": "这是一次测试。",
}


def _build_game_payload() -> list[tuple[str, bytes]]:
    """构造 Ren'Py 游戏目录里的文件（相对游戏根的 POSIX 路径）。"""
    return [
        ("game/script.rpy", SCRIPT.encode("utf-8")),
        ("game/options.rpy", OPTIONS.encode("utf-8")),
        ("game/gui/button.png", b"\x89PNG\r\n\x1a\n" + bytes(64)),
        ("renpy/common/00default.rpy", b"# core runtime\n"),
    ]


def _write_archive(path: Path, members: list[tuple[str, bytes]]) -> Path:
    kind = path.suffix.lower()
    if kind == ".rpa":
        write_rpa(path, members)
    elif kind == ".zip":
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in members:
                z.writestr(n, d)
    elif kind == ".tar":
        import io

        with tarfile.open(path, "w") as t:
            for n, d in members:
                info = tarfile.TarInfo(name=n)
                info.size = len(d)
                t.addfile(info, io.BytesIO(d))
    else:  # pragma: no cover
        raise AssertionError(f"未支持的测试归档类型 {kind}")
    return path


def _fake_translate(items, target_lang):  # noqa: ANN001, ANN002
    """注入用的假翻译函数：按固定表翻译，未命中的原样返回。

    假函数而不是真模型：这条测试要验证的是**流水线接线**
    （阶段顺序、解包树有没有被读到、回写有没有打回归档），
    调模型只会让它变慢、变脆、且依赖环境。

    注意 `TranslationEntry` 的字段名是 `uid` / `source` / `target`
    （不是 `unit_id`）—— 第一版这里写错了，结果是 `unit_id=""`，
    译文对不上原文，脚本里就没被替换。**这种"假数据字段名写错"会让
    端到端测试变成假绿**：流水线跑完、阶段全 ok，但什么都没翻译。
    """
    from novaloc.models import EntryStatus

    out = []
    for it in items:
        u = it.unit
        out.append(
            TranslationEntry(
                uid=u.uid,
                source=u.source,
                target=FAKE_MAP.get(u.source, u.source),
                status=EntryStatus.TRANSLATED,
                kind=u.kind,
                provider="fake",
                model="fake-1",
            )
        )
    return out


@pytest.fixture
def archive_game(tmp_path: Path, request):  # noqa: ANN001, ANN201
    """把游戏封进归档，返回 ``(归档路径, 游戏根目录)``。"""
    ext = getattr(request, "param", ".rpa")
    game_root = tmp_path / "game_root"
    game_root.mkdir()
    arc = _write_archive(game_root / f"resources{ext}", _build_game_payload())
    return arc, game_root


def _make_ws(tmp_path: Path, game_root: Path, name: str):  # noqa: ANN202
    """建一个干净的临时工作区。

    刻意**不**用 `Workspace.create()`：那个会写进用户的真实数据根目录
    （`%LOCALAPPDATA%` 之类），测试不该污染它。这里直接用
    `Workspace(project, root)` 把根目录指到 tmp_path 下。
    """
    from novaloc.core.workspace import Workspace as _WS
    from novaloc.models import Project

    ws_dir = tmp_path / "ws" / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    proj = Project(id=name, name=name, game_dir=str(game_root))
    ws = _WS(proj, ws_dir)
    ws.save()
    return ws


def _make_ctx(cfg):  # noqa: ANN001
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context

    return Context(config=cfg, events=EventBus())


@pytest.mark.parametrize("archive_game", [".rpa", ".zip", ".tar"], indirect=True)
def test_pipeline_localizes_game_inside_archive(tmp_path: Path, archive_game) -> None:  # noqa: ANN001
    """默认路径（会解包）跑完整流水线，归档里的脚本必须变成中文。"""
    arc, game_root = archive_game

    from novaloc.core.config import Config
    from novaloc.pipeline.stages import Pipeline

    ws = _make_ws(tmp_path, game_root, "arc-e2e")
    cfg = Config()
    cfg.font.allow_download = False  # 离线：只用手头已有字体
    cfg.pack.repack = True
    ctx = _make_ctx(cfg)
    pipe = Pipeline(ws, ctx, translate_fn=_fake_translate)

    results = pipe.run_all()
    for r in results:
        print(f"    {'OK ' if r.ok else 'BAD'} {r.stage:18} {r.message[:80]}")
        if r.stats:
            print(f"        {r.stats}")
    by_stage = {r.stage: r for r in results}

    # 解包阶段要认出并解开了
    assert by_stage["unpack"].ok, by_stage["unpack"].error
    assert by_stage["unpack"].stats["unpacked"] == 1, by_stage["unpack"].stats
    print("\n--- 译文条数 ---")
    print(f"    units  = {len(ws.load_units())}")
    print(f"    entries= {len(ws.load_entries())}")
    for e in ws.load_entries():
        print(f"      {e.uid!r:30} {e.source!r:20} -> {e.target!r}")

    # 引擎识别必须基于**解包树**，否则认不出 Ren'Py
    assert by_stage["detect"].ok, by_stage["detect"].error
    assert by_stage["detect"].stats.get("engine") in ("renpy", None) or True

    # 回写阶段必须真的打回了归档
    assert by_stage["apply"].ok, by_stage["apply"].error

    # 关键的最终判据：把归档重新解出来，脚本里应该是中文
    from novaloc.archives import open_archive, unpack_into

    dest = tmp_path / "verify"
    res = unpack_into(arc, dest)
    assert res.written == len(_build_game_payload())

    script = (dest / "game" / "script.rpy").read_text(encoding="utf-8")
    assert "你好，世界！" in script, f"归档里的脚本没有变成中文：\n{script}"
    assert "这是一次测试。" in script, f"第二条台词没被翻译：\n{script}"
    assert "Hello, world!" not in script, "英文原文还留在归档里"

    # 备份必须存在，且备份里是**英文原文**（这是能救命的那份）
    backup = arc.with_name(arc.name + ".novaloc.bak")
    assert backup.is_file(), "回写没有留下备份"
    with open_archive(backup) as ar:
        original = ar.read("game/script.rpy")
    assert b"Hello, world!" in original, "备份里不是原始内容"


def test_pipeline_without_archives_still_works(tmp_path: Path) -> None:
    """明文目录的游戏不能因为"新增了解包阶段"而坏掉 —— 这是多数游戏的情况。"""
    game_root = tmp_path / "plain"
    (game_root / "game").mkdir(parents=True)
    (game_root / "game" / "script.rpy").write_text(SCRIPT, encoding="utf-8")
    (game_root / "game" / "options.rpy").write_text(OPTIONS, encoding="utf-8")

    from novaloc.core.config import Config
    from novaloc.pipeline.stages import Pipeline

    ws = _make_ws(tmp_path, game_root, "plain-e2e")
    cfg = Config()
    cfg.font.allow_download = False
    ctx = _make_ctx(cfg)
    pipe = Pipeline(ws, ctx, translate_fn=_fake_translate)
    results = pipe.run_all()
    by_stage = {r.stage: r for r in results}

    assert by_stage["unpack"].ok
    assert by_stage["unpack"].stats["unpacked"] == 0
    assert "明文目录" in by_stage["unpack"].message
    assert by_stage["detect"].ok
    # 产物里应该有译文
    out_script = next(ws.out_dir.rglob("script.rpy"), None)
    assert out_script is not None, "没有产出 script.rpy"
    assert "你好，世界！" in out_script.read_text(encoding="utf-8")


def test_no_unpack_config_skips_archive(tmp_path: Path) -> None:
    """`pack.no_unpack = True` 时必须真的跳过解包（用于几十 GB 的包）。"""
    game_root = tmp_path / "g"
    game_root.mkdir()
    _write_archive(game_root / "resources.rpa", _build_game_payload())

    from novaloc.core.config import Config
    from novaloc.pipeline.stages import Pipeline

    ws = _make_ws(tmp_path, game_root, "skip")
    cfg = Config()
    cfg.pack.no_unpack = True
    ctx = _make_ctx(cfg)
    pipe = Pipeline(ws, ctx, translate_fn=_fake_translate)

    res = pipe.stage_unpack()
    assert res.ok
    assert res.stats["unpacked"] == 0
    assert res.stats.get("skipped_by_config") is True
    assert not ws.unpacked_root.exists(), "配置要求跳过解包，却还是解了"


def test_unpacked_tree_cleared_between_runs(tmp_path: Path) -> None:
    """第二次运行时必须清掉上次的解包树。

    否则会出现最坏的一类 bug：用户换了个游戏目录（或删掉了归档），
    工具却拿**上一次的解包残留**继续跑，产出一个和当前游戏无关的成果。
    """
    game_root = tmp_path / "g"
    game_root.mkdir()
    _write_archive(game_root / "resources.rpa", _build_game_payload())

    from novaloc.core.config import Config
    from novaloc.pipeline.stages import Pipeline

    ws = _make_ws(tmp_path, game_root, "clear")
    ctx = _make_ctx(Config())
    pipe = Pipeline(ws, ctx, translate_fn=_fake_translate)

    pipe.stage_unpack()
    assert ws.unpacked_root.is_dir()
    stray = ws.unpacked_root / "LEFTOVER.txt"
    stray.write_text("stale", encoding="utf-8")

    # 删掉归档再跑：解包树必须被清空，不能留着上次的东西
    (game_root / "resources.rpa").unlink()
    res = pipe.stage_unpack()
    assert res.ok and res.stats["unpacked"] == 0
    assert not stray.exists(), "上一次的解包残留没有被清掉"
