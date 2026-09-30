"""字体接线的回归测试：把补好的字体真正接到引擎上。

背景（ROADMAP #3）：``unity.py`` 与 ``loose.py`` 没有覆写 ``wire_fonts()``。
后果是**最容易被忽略、又最难自查**的那种失败：字体文件确实进了输出目录，
但引擎不会去读它，用户看到"文件换了但游戏里还是口口口"。

本测试锁住三件事：

1. 两个适配器都**必须**覆写 ``wire_fonts()``（基类返回空 = 静默不接线）；
2. 字体确实被复制到了**引擎会去读的位置**（Unity 是 StreamingAssets）；
3. 说明里必须**如实**包含"这一步不足以生效"的边界 ——
   Unity 的 TMP 图集是预烘焙位图，只换 TTF 无效。宁可明确告知，
   也不能产出一个看着成功、实际没用的结果。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines.base import EngineAdapter  # noqa: E402
from novaloc.engines.loose import LooseFilesAdapter  # noqa: E402
from novaloc.engines.unity import UnityAdapter  # noqa: E402

ADAPTERS = [UnityAdapter, LooseFilesAdapter]


def _ctx() -> Context:
    return Context(config=Config(), events=EventBus())


@pytest.mark.parametrize("cls", ADAPTERS, ids=[c.id for c in ADAPTERS])
def test_adapter_overrides_wire_fonts(cls: type[EngineAdapter]) -> None:
    """必须覆写 wire_fonts —— 基类实现返回空列表（= 什么都不做）。"""
    assert "wire_fonts" in cls.__dict__, (
        f"{cls.__name__} 没有覆写 wire_fonts()，"
        "字体文件会被放进输出目录但引擎不会去读"
    )


@pytest.mark.parametrize("cls", ADAPTERS, ids=[c.id for c in ADAPTERS])
def test_empty_installed_is_noop(cls: type[EngineAdapter], tmp_path: Path) -> None:
    """没有字体要接线时不应报错、不应产生说明。"""
    a = cls(_ctx())
    assert a.wire_fonts(tmp_path, {}) == []


@pytest.mark.parametrize("cls", ADAPTERS, ids=[c.id for c in ADAPTERS])
def test_missing_source_reported_not_silent(cls: type[EngineAdapter], tmp_path: Path) -> None:
    """源字体不存在时必须明确报出，而不是静默当成功。"""
    out = tmp_path / "out"
    out.mkdir()
    a = cls(_ctx())
    notes = a.wire_fonts(out, {"fonts/orig.ttf": str(tmp_path / "does_not_exist.ttf")})
    joined = "\n".join(notes)
    assert "找不到" in joined, f"缺文件却没报出来：{notes!r}"


def _fake_font(tmp_path: Path, name: str = "novaloc_cjk.ttf") -> Path:
    """造一个假的字体文件（只需要存在且能被复制）。"""
    p = tmp_path / name
    p.write_bytes(b"\x00\x01\x00\x00" + b"FAKEFONT" * 8)
    return p


def test_unity_puts_font_in_streaming_assets(tmp_path: Path) -> None:
    """Unity：字体必须落到 ``*_Data/StreamingAssets`` 下才可能被运行时读到。"""
    game = tmp_path / "out"
    (game / "Game_Data" / "StreamingAssets").mkdir(parents=True)
    font = _fake_font(tmp_path)

    a = UnityAdapter(_ctx())
    notes = a.wire_fonts(game, {"fonts/orig.ttf": str(font)})

    dst = game / "Game_Data" / "StreamingAssets" / "_novaloc_fonts" / font.name
    assert dst.is_file(), f"字体没进 StreamingAssets：{notes!r}"


def test_unity_warns_about_tmp_atlas(tmp_path: Path) -> None:
    """检测到 TMP 资源时必须说明"只换 TTF 无效，要重烘焙图集"。"""
    game = tmp_path / "out"
    sa = game / "Game_Data" / "StreamingAssets"
    sa.mkdir(parents=True)
    # 造一个像 TMP 字体资源的 .asset
    (game / "Game_Data" / "NotoSans SDF.asset").write_bytes(
        b"%YAML 1.1\nMonoBehaviour:\n  m_AtlasTextures:\n  - {fileID: 2800000}\n"
        b"  m_FaceInfo:\n    m_FamilyName: Noto Sans\n"
    )
    font = _fake_font(tmp_path)

    notes = UnityAdapter(_ctx()).wire_fonts(game, {"fonts/orig.ttf": str(font)})
    joined = "\n".join(notes)
    assert "TextMeshPro" in joined, f"没识别到 TMP 资源：{notes!r}"
    assert "重新烘焙" in joined or "重烘焙" in joined, f"没说清需要重烘焙：{notes!r}"
    assert "Font Asset Creator" in joined, f"没给出可操作步骤：{notes!r}"


def test_unity_without_tmp_says_so(tmp_path: Path) -> None:
    """没有 TMP 资源时不能说"已经搞定"，要说明还需人工处理。"""
    game = tmp_path / "out"
    (game / "Game_Data").mkdir(parents=True)
    font = _fake_font(tmp_path)

    notes = UnityAdapter(_ctx()).wire_fonts(game, {"fonts/orig.ttf": str(font)})
    joined = "\n".join(notes)
    assert "口口口" in joined or "AssetBundle" in joined, f"没说明后续怎么办：{notes!r}"


def test_unity_mentions_dynamic_font_escape_hatch(tmp_path: Path) -> None:
    """动态 TMP 字体资源是真实存在的例外，必须提到（否则会误导用户白做工）。"""
    game = tmp_path / "out"
    (game / "Game_Data").mkdir(parents=True)
    font = _fake_font(tmp_path)
    notes = UnityAdapter(_ctx()).wire_fonts(game, {"fonts/orig.ttf": str(font)})
    assert "动态" in "\n".join(notes), f"没提到动态字体的可能：{notes!r}"


def test_loose_replaces_same_named_font_in_place(tmp_path: Path) -> None:
    """散装：输出目录里已有同名字体时应就地替换（这是最可能生效的做法）。"""
    out = tmp_path / "out"
    (out / "fonts").mkdir(parents=True)
    existing = out / "fonts" / "game.ttf"
    existing.write_bytes(b"OLDFONT")

    newfont = tmp_path / "game.ttf"
    newfont.write_bytes(b"NEWFONT-with-CJK")

    notes = LooseFilesAdapter(_ctx()).wire_fonts(out, {"fonts/game.ttf": str(newfont)})
    assert existing.read_bytes() == b"NEWFONT-with-CJK", f"没就地替换：{notes!r}"
    assert any("替换" in n for n in notes), f"没报告替换动作：{notes!r}"


def test_loose_states_its_limits(tmp_path: Path) -> None:
    """散装模式必须说明它无法自动改字体指向。"""
    out = tmp_path / "out"
    out.mkdir()
    font = _fake_font(tmp_path)
    notes = LooseFilesAdapter(_ctx()).wire_fonts(out, {"fonts/orig.ttf": str(font)})
    joined = "\n".join(notes)
    assert "口口口" in joined, f"没说明不生效时怎么办：{notes!r}"
    assert (out / "_fonts" / font.name).is_file(), f"没集中放到 _fonts/：{notes!r}"


def test_loose_does_not_touch_unrelated_fonts(tmp_path: Path) -> None:
    """只有同名字体才替换，不能顺手把别的字体也覆盖了。"""
    out = tmp_path / "out"
    (out / "fonts").mkdir(parents=True)
    other = out / "fonts" / "unrelated.ttf"
    other.write_bytes(b"KEEPME")

    font = _fake_font(tmp_path, "novaloc_cjk.ttf")
    LooseFilesAdapter(_ctx()).wire_fonts(out, {"fonts/orig.ttf": str(font)})
    assert other.read_bytes() == b"KEEPME", "误改了不相关的字体文件"
