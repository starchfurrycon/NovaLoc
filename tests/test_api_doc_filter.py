r"""★ 排除**引擎自带的 API 文档 XML** —— 本轮最大的一处抽取噪声。

## 实测问题

`Arena Story` 一个游戏抽出 **83,585 条**"文本"，其中 **13,889 条**是
Unity 引擎随包发布的 API 文档注释：

    <name>UnityEngine.AccessibilityModule</name>
    <para>A class containing methods to assist with accessibility for
          users with different visual abilities.
    <param name="palette">An array of colors to populate with a palette.

**玩家永远看不到这些**，翻了毫无意义；而且它们**天生带 XML 标签**
⇒ 占位符守卫必然判"丢记号" ⇒ 变成成片的**假失败**，
每轮 backlog 都要重试一遍（这是"失败 24,049 条"的主要来源）。

## 修法：两道判据

1. **路径形态** `*_Data/Managed/UnityEngine*.xml` —— Unity 文档的固定位置；
2. **内容标签** `<member>`/`<summary>`/`<param>`/`<typeparam>`/`<returns>`…

判据 1 不能省：实测 `Arena Story` 里有几个文档**已被旧版本翻译写坏**，
标签被译文替换掉了（`'中文译文 已完成<doc> ...'`），判据 2 对它们漏判。

## 本文件测什么

* 两类判据各自生效；
* **不误伤游戏自己的数据**（`<Set Sts Data>` 这类自研标签必须保留）——
  这条最重要：漏抽的代价是玩家看到没翻的文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.unity import _looks_like_api_doc  # noqa: E402


def _doc(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "Game_Data" / "Managed"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 判据一：路径形态
# ----------------------------------------------------------------------
def test_managed_unityengine_xml_by_path(tmp_path: Path) -> None:
    """`*_Data/Managed/UnityEngine*.xml` ⇒ 文档（哪怕内容已被改坏）。"""
    p = _doc(tmp_path, "UnityEngine.VideoModule.xml", "中文译文 已完成 doc")
    assert _looks_like_api_doc(p) is True


def test_managed_unityengine_bare_xml(tmp_path: Path) -> None:
    """`UnityEngine.xml`（没有模块后缀）也算。"""
    p = _doc(tmp_path, "UnityEngine.xml", "whatever")
    assert _looks_like_api_doc(p) is True


def test_path_rule_is_case_insensitive(tmp_path: Path) -> None:
    """大小写无关（`managed` / `Managed` 都出现过）。"""
    d = tmp_path / "G_Data" / "MANAGED"
    d.mkdir(parents=True)
    p = d / "unityengine.CoreModule.XML"
    p.write_text("x", encoding="utf-8")
    assert _looks_like_api_doc(p) is True


# ----------------------------------------------------------------------
# 判据二：内容标签（文档被挪了位置时兜住）
# ----------------------------------------------------------------------
def test_content_rule_when_not_in_managed(tmp_path: Path) -> None:
    """文档不在 `Managed/` 下时，靠内容标签认出来。"""
    d = tmp_path / "Game_Data" / "Documentation"
    d.mkdir(parents=True)
    p = d / "SomeApi.xml"
    p.write_text(
        '<?xml version="1.0"?>\n<doc>\n<members>\n'
        '<member name="T:Foo"><summary>Does a thing.</summary></member>\n'
        "</members>\n</doc>",
        encoding="utf-8",
    )
    assert _looks_like_api_doc(p) is True


# ----------------------------------------------------------------------
# ★ 不误伤：游戏自己的 XML 数据必须保留
# ----------------------------------------------------------------------
def test_game_own_xml_is_kept(tmp_path: Path) -> None:
    r"""★ 游戏自己的数据用自研标签 ⇒ **不能**当成文档排掉。

    实测 `072 Project` 用 `<Set Sts Data>`：

        <Set Sts Data>
        skill:12
        Cost sp: 2
        </Set Sts Data>

    这是**该处理**的（虽然是插件 DSL，但至少不是引擎文档）。
    漏抽它不会让玩家"看到没翻的引擎文档"，而误排游戏数据会让
    玩家看到没翻的内容 —— 后者严重得多。
    """
    d = tmp_path / "Game_Data" / "StreamingAssets"
    d.mkdir(parents=True)
    p = d / "game_data.xml"
    p.write_text(
        '<?xml version="1.0"?>\n<skills>\n'
        "<Set Sts Data>\nskill:12\nCost sp: 2\n</Set Sts Data>\n"
        "</skills>",
        encoding="utf-8",
    )
    assert _looks_like_api_doc(p) is False


def test_plain_game_xml_without_doc_tags(tmp_path: Path) -> None:
    """普通游戏 XML（没有文档标签）也要保留。"""
    d = tmp_path / "Game_Data" / "StreamingAssets"
    d.mkdir(parents=True)
    p = d / "items.xml"
    p.write_text("<items><item id='1'>Potion</item></items>", encoding="utf-8")
    assert _looks_like_api_doc(p) is False


def test_unreadable_file_is_not_a_doc(tmp_path: Path) -> None:
    """读不了 ⇒ 返回 False（宁可多抽，不可漏抽）。"""
    d = tmp_path / "Game_Data" / "Managed"
    d.mkdir(parents=True)
    p = d / "SomeOther.xml"  # 不在 Managed 下叫 UnityEngine，且内容读不到
    p.mkdir()
    assert _looks_like_api_doc(p) is False


def test_non_unityengine_name_in_managed_is_not_doc_by_path(tmp_path: Path) -> None:
    """`Managed/` 下但文件名不是 `UnityEngine*` ⇒ 路径判据不认。

    这可能是游戏自己的程序集文档，不确定时**不排除**。
    """
    d = tmp_path / "Game_Data" / "Managed"
    d.mkdir(parents=True)
    p = d / "Assembly-CSharp.xml"
    p.write_text("<items><item>Potion</item></items>", encoding="utf-8")
    assert _looks_like_api_doc(p) is False
