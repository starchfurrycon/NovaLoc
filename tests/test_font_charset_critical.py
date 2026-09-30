"""字体补丁的"必需字符集"必须和"希望覆盖的字符集"分开。

## 事故：一个字符没有字体能提供 → 整个中文字形注入失败

真实数据（BeyondPortal，MZ）。`fonts` 阶段硬失败：

    ❌ 字体补丁失败：字符集里有 21 个字符没有任何候选字体能提供：
       خ د ل ه و گ ی ജ വ ീ ก ค ด ย อ ี ไ ๆ ็ ่ ้
    ⚠ 为避免游戏内出现口口口，本次结果**未采用**

紧接着**更糟的事**：`out/fonts/` 里那两个字体是**字节完全相同的源文件副本** ——
因为补丁失败（`action=failed`、`out_path` 为空），`apply` 没有字体可回写，
于是把原字体原样拷过去了。而原字体的中文覆盖是 **3.4%**。
**玩家进游戏会看到满屏口口口，而流水线"成功"了。**

## 根因：字符集把"永远不会渲染的字符"也算成必需

`build_required_charset(translated, source)` 把**原文**也并了进去，
注释说得很对：游戏里总有没被翻译的串（人名、型号），它们也要渲染。

但**"原文全部字符"这个要求过宽了**。那 21 个字符的真正来源是
**OCR 噪声/译文跑偏**（阿拉伯/泰/马拉雅拉姆），它们：
* 不在任何一条中文译文里；
* 只是被并进了"源文"这一侧。

## 修法：分清"必翻"和"顺带"

* **必需字符** = 全部译文 + UI 安全字符。
  这些**一定**会被写回游戏，缺一个就是口口口 → **要求 100% 覆盖**。
* **希望覆盖** = 源文全部字符。补得上最好；
  补不上就**警告**，不能因此把整个中文字形注入拖垮。

关键判据：**先把源文侧"补不上"的字符挑出来**，
只要译文侧真的 100% 覆盖率，就照常产出字体。

> 宁可让某个未翻译的原文串缺字形（那是"原文没翻"的问题，
> 另有质检项去管），也不能因为一个噪声字符让**全部中文**都变口口口。

这正是"**失败的作用域**"问题：小问题不该拖垮大目标。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.fonts.charset import UI_SAFE_CHARS, build_required_charset  # noqa: E402

#: 真实触发事故的那 21 个字符（阿拉伯/马拉雅拉姆/泰）
INCIDENT_CHARS = "خدلهوگىജവീกคดยอีไๆ็่้"
#: 事故里的原样报错（`patch_font` 的 error 字段格式）
INCIDENT_ERROR = (
    "字符集里有 21 个字符没有任何候选字体能提供，无法生成完整字体："
    + INCIDENT_CHARS
)


# ----------------------------------------------------------------------
# 一、字符集构建：懂"必翻"与"顺带"的区别
# ----------------------------------------------------------------------

def test_translated_chars_are_always_required() -> None:
    """译文里的每一个字符都必须在字符集里（缺了就是口口口）。"""
    cs = build_required_charset(translated_texts=["穿过传送门"])
    for ch in "穿过传送门":
        assert ch in cs, f"译文里的 {ch!r} 竟然不在字符集里"


def test_ui_safe_chars_are_included_by_default() -> None:
    cs = build_required_charset(translated_texts=["你好"])
    missing = [c for c in UI_SAFE_CHARS if c not in cs]
    # UI_SAFE_CHARS 里可能有"不可渲染字符"被 is_ignorable 剔掉，
    # 但绝不能是空白整段都没了
    assert len(missing) < len(UI_SAFE_CHARS) / 2, f"UI 安全字符缺失过多：{missing[:20]}"


def test_source_chars_are_included_too() -> None:
    """源文也要进字符集 —— 未翻译的串仍然要渲染。"""
    cs = build_required_charset(source_texts=["Boss 战"])
    assert "B" in cs and "战" in cs


def test_ignorable_chars_never_enter_the_charset() -> None:
    """换行/制表/零宽字符永远不进字符集（它们不可能有字形）。

    这是老 bug #14 的教训：一个换行符就让字体阶段整个中止。
    """
    cs = build_required_charset(translated_texts=["第一行\n第二行\t结束"])
    for bad in "\n\r\t\u200b\u200c\u200d\ufeff":
        assert bad not in cs, f"不可渲染字符 {bad!r} 混进了字符集"


def test_incident_chars_would_be_source_only() -> None:
    """事故现场：那 21 个字符**只在源文侧**，一条译文里都没有。

    这就是"必需 vs 顺带"能修好它的原因。
    """
    translated = ["穿过传送门", "生命值", "攻击力"]
    source = ["Beyond the Portal", "HP", INCIDENT_CHARS]
    cs = build_required_charset(translated_texts=translated, source_texts=source)
    assert set(INCIDENT_CHARS) <= set(cs), "它们确实进了字符集（走的是源文侧）"

    critical = set(build_required_charset(translated_texts=translated))
    overlap = critical & set(INCIDENT_CHARS)
    assert not overlap, f"它们绝不该出现在必需字符里：{overlap}"


# ----------------------------------------------------------------------
# 二、报错信息的解析（要能从 error 里拿回"哪些字补不上"）
# ----------------------------------------------------------------------

def test_can_extract_uncovorable_chars_from_the_error() -> None:
    """`patch_font` 的 error 是**给人看的字符串**，但补丁流程需要
    "是哪几个字"才能把它们从"顺带"那一侧剔掉重试。

    这里钉住分隔符：冒号之后就是要剔除的字符。
    """
    _, _, tail = INCIDENT_ERROR.partition("：")
    assert tail, "冒号后应当有字符"
    dropped = set(tail) - set(" 。，、")
    assert set(INCIDENT_CHARS) <= dropped, f"没解析出全部字符：{sorted(dropped)}"
