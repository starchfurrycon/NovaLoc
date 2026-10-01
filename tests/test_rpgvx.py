"""RPG Maker VX Ace / VX / XP 适配器的端到端测试。

## 为什么用合成 fixture

本机上已经**没有**真实的 VX Ace/XP 游戏了（原先那批已经删掉），
所以这里用 :mod:`novaloc.engines.rubymarshal` 自己造出**真的**
``.rxdata``/``.rvdata`` 二进制 —— 不是 mock，适配器面对的就是它
在真实游戏里会读到的那串字节。

造数据用的是我们自己写的 ``dumps``，这里有个隐含风险：
**如果编解码器错得"自洽"，往返测试就会把错误当成正确**。
所以解码器另有一组按官方文档原始字节向量写的测试
（``tests/test_rubymarshal.py``），那组才是真正的锚点。

## fixture 刻意还原的真实结构

* ``Actors`` 是**数组**、下标 0 是 ``None``、元素是 ``RPG::Actor`` 对象。
* 地图是对象，``@events`` 是 **Hash**（键是事件 ID 整数），
  每个事件有 ``@pages`` 数组，页面里有 ``@list`` 指令数组。
* ``401``（显示文字）被拆成多条 —— 还原"一句话被切成几行"的真实现象。
* ``System`` 里有 ``@terms`` 嵌套对象。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines.rpgvx import RpgVxAdapter, set_pointer  # noqa: E402
from novaloc.engines.rubymarshal import (  # noqa: E402
    MAGIC_48,
    MAGIC_49,
    RValue,
    _Sym,
    dumps,
    loads,
)


def S(name: str) -> _Sym:
    return _Sym(name)


def ctx() -> Context:
    return Context(config=Config(), events=EventBus())


def _obj(cls: str, **ivars: object) -> RValue:
    return RValue(cls, {S(k.lstrip("@")): v for k, v in ivars.items()})


# ----------------------------------------------------------------------
# fixture：一个最小的 VX Ace 游戏
# ----------------------------------------------------------------------


def _actors_data() -> list[object]:
    return [
        None,
        _obj(
            "Actor",
            name="アルド",
            nickname="勇者",
            description="王国の若き剣士。\n正義感が強い。",
            note="<PassiveSkill:5>",
        ),
        _obj("Actor", name="リナ", nickname="", description="", note=""),
    ]


def _system_data() -> RValue:
    terms = _obj(
        "Terms",
        basic=["レベル", "体力", "魔力"],
        commands=["戦う", "逃げる"],
        params=["最大HP", "攻撃力"],
        messages=["は戦闘不能になった！"],
        skill_types=["特技"],
        weapon_types=["剣"],
        armor_types=["盾"],
    )
    return _obj(
        "System",
        game_title="デモンの根",
        currency_unit="G",
        terms=terms,
        party_members=[1],
        elements=["物理", "炎"],
    )


def _map_data() -> RValue:
    """地图：两个事件，事件 1 有两页。"""

    def cmd(code: int, *params: object) -> RValue:
        return _obj("RPG::EventCommand", code=code, indent=0, parameters=list(params))

    page1 = _obj(
        "RPG::EventPage",
        list=[
            cmd(101, "Face1", 0),
            cmd(401, "A magic device displays footage of the"),
            cmd(401, "suffering of the slaves."),
            cmd(102, ["Yes", "No"], 0),
            cmd(355, "puts 'this is ruby script'"),
            cmd(108, "# developer note, do not translate"),
            cmd(401, "セーブはできません。"),
        ],
    )
    page2 = _obj("RPG::EventPage", list=[cmd(401, "Second page text")])
    return _obj(
        "RPG::Map",
        events={
            1: _obj("RPG::Event", id=1, name="EV001", pages=[page1, page2]),
            2: _obj("RPG::Event", id=2, name="", pages=[]),
        },
    )


def _mapinfos_data() -> list[object]:
    return [
        None,
        _obj("RPG::MapInfo", id=1, name="王都ボヘロス", order=1, parent_id=0),
    ]


def make_game(root: Path, *, magic: bytes = MAGIC_49, suffix: str = ".rxdata") -> Path:
    """造一个最小 VX Ace（或指定 magic/后缀）游戏目录。"""
    d = root / "Data"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"Actors{suffix}").write_bytes(dumps(_actors_data(), magic=magic))
    (d / f"System{suffix}").write_bytes(dumps(_system_data(), magic=magic))
    (d / f"Map001{suffix}").write_bytes(dumps(_map_data(), magic=magic))
    (d / f"MapInfos{suffix}").write_bytes(dumps(_mapinfos_data(), magic=magic))
    # 必须存在的"跳过"文件，验证脚本文件确实被跳过
    (d / f"Scripts{suffix}").write_bytes(dumps(["# ruby source", "def f; end"], magic=magic))
    (root / "Game.ini").write_text("[Game]\nTitle=Demo\n", encoding="utf-8")
    return root


# ----------------------------------------------------------------------
# 检测
# ----------------------------------------------------------------------


def test_detects_vx_ace_from_rxdata_and_magic(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    info = RpgVxAdapter(ctx()).detect(root)
    assert info.ok
    assert info.engine_id == "rpgvx"
    assert info.version == "VX Ace", info.evidence
    assert info.confidence > 0.6
    assert any("rxdata" in e for e in info.evidence)


def test_detects_xp_from_rvdata(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game", magic=MAGIC_48, suffix=".rvdata")
    info = RpgVxAdapter(ctx()).detect(root)
    assert info.ok
    assert info.version == "XP", info.evidence


def test_detect_confidence_is_zero_without_data_dir(tmp_path: Path) -> None:
    (tmp_path / "nothing").mkdir()
    info = RpgVxAdapter(ctx()).detect(tmp_path / "nothing")
    assert not info.ok
    assert info.confidence == 0.0


def test_runtime_dll_is_evidence(tmp_path: Path) -> None:
    """VX 也写 .rvdata，靠 RGSS202E.dll 与 XP 区分。"""
    root = make_game(tmp_path / "game", magic=MAGIC_48, suffix=".rvdata")
    (root / "RGSS202E.dll").write_bytes(b"MZ fake")
    info = RpgVxAdapter(ctx()).detect(root)
    assert info.version == "VX", info.evidence
    assert any("RGSS202E" in e for e in info.evidence)


# ----------------------------------------------------------------------
# 抽取
# ----------------------------------------------------------------------


def test_extracts_database_fields(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, rep = RpgVxAdapter(ctx()).extract_text(root)
    assert not rep.errors, rep.errors
    by_uid = {u.uid: u for u in units}
    # 角色名 / 昵称 / 描述
    assert "Data/Actors.rxdata:/1/@name" in by_uid
    assert by_uid["Data/Actors.rxdata:/1/@name"].source == "アルド"
    assert "Data/Actors.rxdata:/1/@nickname" in by_uid
    assert "Data/Actors.rxdata:/1/@description" in by_uid
    # 空昵称/空描述不入库
    assert "Data/Actors.rxdata:/2/@nickname" not in by_uid
    assert "Data/Actors.rxdata:/2/@description" not in by_uid


def test_skips_scripts_file(tmp_path: Path) -> None:
    """Scripts 里是 Ruby 源码，绝不能翻 —— 翻了游戏直接崩。"""
    root = make_game(tmp_path / "game")
    units, rep = RpgVxAdapter(ctx()).extract_text(root)
    assert not any("Scripts" in u.location.file for u in units)
    assert "Scripts.rxdata" in rep.skipped


def test_extracts_system_title_and_terms(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    src = {u.location.pointer: u.source for u in units if u.location.file.endswith("System.rxdata")}
    assert src["/@game_title"] == "デモンの根"
    # @terms 里的数组逐项入库
    assert src["/@terms/@basic/0"] == "レベル"
    assert src["/@terms/@commands/1"] == "逃げる"
    assert src["/@terms/@skill_types/0"] == "特技"
    # @elements 不在白名单里，不翻
    assert not any("@elements" in p for p in src)


def test_extracts_mapinfos_name(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    names = [u for u in units if u.location.file.endswith("MapInfos.rxdata")]
    assert len(names) == 1
    assert names[0].source == "王都ボヘロス"
    assert names[0].location.pointer == "/1/@name"


def test_consecutive_401_are_merged_into_one_unit(tmp_path: Path) -> None:
    r"""★ 连续 401 必须合成一条，否则模型只看到半句话。

    MV 那边实测 48% 的 401 组是"一句话被按显示宽度切开"的；
    VX 系是同一个机制、同一个现象。这里断言两件事：
    源文是**完整两句**（用 ``\n`` 连接），以及 ``siblings`` 给出其余槽位。
    """
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    merged = [
        u
        for u in units
        if u.location.pointer.endswith("/@list/1/@parameters/0")
        and "magic device" in u.source
    ]
    assert len(merged) == 1, "连续 401 没有被合成一条"
    u = merged[0]
    assert u.source == (
        "A magic device displays footage of the\nsuffering of the slaves."
    ), u.source
    assert u.location.siblings == [
        "/@events/@1/@pages/0/@list/2/@parameters/0"
    ], u.location.siblings
    assert u.kind.value == "dialogue"


def test_401_group_is_broken_by_non_401(tmp_path: Path) -> None:
    """401 组被 102/355 打断后各自独立，不会越过它们合并。"""
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    tail = [u for u in units if "セーブはできません" in u.source]
    assert len(tail) == 1
    assert tail[0].location.siblings == [], "单条 401 不该有 siblings"


def test_skips_script_and_comment_commands(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    joined = "\n".join(u.source for u in units)
    assert "puts 'this is ruby script'" not in joined
    assert "developer note" not in joined
    # 101（脸图）的参数是图片名，不该入库
    assert "Face1" not in joined


def test_choices_are_extracted_per_option(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    opts = {u.source: u for u in units if u.source in ("Yes", "No")}
    assert set(opts) == {"Yes", "No"}
    assert all(u.kind.value == "ui_label" for u in opts.values())
    assert opts["Yes"].location.pointer.endswith("/3/@parameters/0/0"), opts["Yes"].location.pointer


def test_pages_are_both_visited(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    assert any("Second page text" in u.source for u in units)


def test_empty_event_name_is_not_a_unit(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    assert not any(u.source.strip() == "" for u in units)


def test_cp932_units_are_marked_cp932(tmp_path: Path) -> None:
    """4.8 的字符串是 CP932，TextLocation.encoding 必须如实记录。"""
    root = make_game(tmp_path / "game", magic=MAGIC_48, suffix=".rvdata")
    units, _ = RpgVxAdapter(ctx()).extract_text(root)
    encs = {u.location.encoding for u in units}
    assert encs == {"cp932"}, encs


# ----------------------------------------------------------------------
# 回写
# ----------------------------------------------------------------------


def test_apply_writes_translations_and_preserves_bytes(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    ad = RpgVxAdapter(ctx())
    units, _ = ad.extract_text(root)
    tr = {u.uid: f"ZH:{u.source}" for u in units}
    out = tmp_path / "out"
    res = ad.apply(root, out, units, tr)
    assert res.ok, res.warnings
    assert res.files_written >= 4

    # 读回来核对
    obj = loads((out / "Data" / "Actors.rxdata").read_bytes())
    assert obj[1].ivars[S("name")] == "ZH:アルド"
    # 没给的字段保持原样
    assert obj[1].ivars[S("nickname")] == "ZH:勇者"

    # 401 组按行拆回两个槽位
    m = loads((out / "Data" / "Map001.rxdata").read_bytes())
    ev = m.ivars[S("events")][1]
    lst = ev.ivars[S("pages")][0].ivars[S("list")]
    assert lst[1].ivars[S("parameters")][0] == "ZH:A magic device displays footage of the"
    assert lst[2].ivars[S("parameters")][0] == "suffering of the slaves."


def test_apply_preserves_magic_48(tmp_path: Path) -> None:
    """▲ 4.8 文件必须还用 4.8 写回去，否则字符串编码会错。"""
    root = make_game(tmp_path / "game", magic=MAGIC_48, suffix=".rvdata")
    ad = RpgVxAdapter(ctx())
    units, _ = ad.extract_text(root)
    tr = {u.uid: "中文测试" for u in units}
    out = tmp_path / "out"
    res = ad.apply(root, out, units, tr)
    assert res.ok
    for f in (out / "Data").glob("*.rvdata"):
        assert f.read_bytes()[:2] == MAGIC_48, f"{f.name} 的 magic 变了"
    # 中文在 CP932 里没有对应，写出去会变成 '?' 或能编码的近似 ——
    # 关键是**读回来不能抛异常**，且结构还在
    obj = loads((out / "Data" / "Actors.rvdata").read_bytes())
    assert obj[1].ivars[S("name")] != "アルド"


def test_apply_reports_missing_source_file(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    ad = RpgVxAdapter(ctx())
    units, _ = ad.extract_text(root)
    bad = units[0].model_copy(deep=True)
    bad.location.file = "Data/DoesNotExist.rxdata"
    res = ad.apply(root, tmp_path / "out", [bad], {bad.uid: "x"})
    assert any("不存在" in w for w in res.warnings)


def test_apply_copies_untouched_data_files(tmp_path: Path) -> None:
    """没改过的数据文件也要拷过去，否则 out/ 不是一份完整游戏。"""
    root = make_game(tmp_path / "game")
    ad = RpgVxAdapter(ctx())
    units, _ = ad.extract_text(root)
    tr = {u.uid: "改" for u in units}
    out = tmp_path / "out"
    ad.apply(root, out, units, tr)
    assert (out / "Data" / "Scripts.rxdata").is_file()
    assert loads((out / "Data" / "Scripts.rxdata").read_bytes()) == [
        "# ruby source",
        "def f; end",
    ]


def test_apply_with_no_translations_is_ok(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    ad = RpgVxAdapter(ctx())
    units, _ = ad.extract_text(root)
    res = ad.apply(root, tmp_path / "out", units, {})
    assert res.ok
    assert any("没有需要回写" in w for w in res.warnings)


# ----------------------------------------------------------------------
# 指针
# ----------------------------------------------------------------------


def test_set_pointer_on_nested_object() -> None:
    data = _map_data()
    assert set_pointer(data, "/@events/@1/@pages/0/@list/1/@parameters/0", "NEW")
    lst = data.ivars[S("events")][1].ivars[S("pages")][0].ivars[S("list")]
    assert lst[1].ivars[S("parameters")][0] == "NEW"


def test_set_pointer_returns_false_on_bad_path() -> None:
    data = _actors_data()
    assert not set_pointer(data, "/1/@nope", "x")
    assert not set_pointer(data, "/99/@name", "x")
    assert not set_pointer(data, "no-leading-slash", "x")
    # 失败时绝不能"就近"改到别的地方
    assert data[1].ivars[S("name")] == "アルド"


def test_set_pointer_on_hash_with_integer_keys() -> None:
    data = _map_data()
    assert set_pointer(data, "/@events/2/@name", "改过了")
    assert data.ivars[S("events")][2].ivars[S("name")] == "改过了"


# ----------------------------------------------------------------------
# 字体 / 贴图
# ----------------------------------------------------------------------


def test_discover_fonts_lists_fonts_dir(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    (root / "Fonts").mkdir()
    (root / "Fonts" / "VL-Gothic-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")
    fonts = RpgVxAdapter(ctx()).discover_fonts(root)
    assert [f.font_id for f in fonts] == ["Fonts/VL-Gothic-Regular.ttf"]


def test_wire_fonts_copies_and_explains(tmp_path: Path) -> None:
    """wire_fonts 返回的是**说明**，并且必须讲清"还差改脚本这一步"。"""
    out = tmp_path / "out"
    (out / "Fonts").mkdir(parents=True)
    src = tmp_path / "SourceHanSans.ttf"
    src.write_bytes(b"\x00\x01\x00\x00fake")
    notes = RpgVxAdapter(ctx()).wire_fonts(out, {"VL Gothic": str(src)})
    assert (out / "Fonts" / "SourceHanSans.ttf").is_file()
    joined = "\n".join(notes)
    assert "SourceHanSans.ttf" in joined
    assert "Scripts.rxdata" in joined, "必须说明字体名由脚本决定"


def test_extract_images_finds_graphics(tmp_path: Path) -> None:
    root = make_game(tmp_path / "game")
    g = root / "Graphics" / "System"
    g.mkdir(parents=True)
    (g / "Window.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "Graphics" / "note.txt").write_text("x", encoding="utf-8")
    assets, rep = RpgVxAdapter(ctx()).extract_images(root)
    assert [a.path for a in assets] == ["Graphics/System/Window.png"]
    assert assets[0].kind == "texture"


# ----------------------------------------------------------------------
# 与其它适配器共存
# ----------------------------------------------------------------------


def test_does_not_claim_mv_games(tmp_path: Path) -> None:
    """纯 MV 布局（data/*.json、没有 .rxdata）不该被 VX 适配器认领。"""
    root = tmp_path / "mv"
    d = root / "data"
    d.mkdir(parents=True)
    (d / "System.json").write_text("{}", encoding="utf-8")
    info = RpgVxAdapter(ctx()).detect(root)
    assert not info.ok
