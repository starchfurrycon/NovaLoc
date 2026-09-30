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


def test_error_parser_takes_the_tail_not_the_prefix() -> None:
    """**从右端切**，不要按"是不是字母"筛。

    第一版写成"冒号后、去掉空白和 ASCII"，结果把前缀里的
    `font`/`is` 之类字母也当成"字符"留下了。
    更糟的是外来文字（阿拉伯、天城）**也是 `isalpha()` 为真**，
    按"是不是字母"根本分不出来。
    """
    from novaloc.pipeline.stages import Pipeline

    got = Pipeline._chars_from_patch_error(INCIDENT_ERROR)
    assert got == set(INCIDENT_CHARS), f"多出 {sorted(got - set(INCIDENT_CHARS))}"

    # 没有冒号 → 空集（调用方据此不做重试，宁可硬失败也不瞎剔）
    assert Pipeline._chars_from_patch_error("没有冒号的报错") == set()
    assert Pipeline._chars_from_patch_error(None) == set()
    assert Pipeline._chars_from_patch_error("") == set()


# ----------------------------------------------------------------------
# 三、「影响多少条文本」——剔除判据
# ----------------------------------------------------------------------

def _pipeline():
    from novaloc.core.config import Config
    from novaloc.core.events import EventBus
    from novaloc.core.registry import Context
    from novaloc.core.workspace import Workspace
    from novaloc.models import Project
    from novaloc.pipeline.stages import Pipeline

    ws = Workspace(Project(name="t", game_dir="."), Path("."))
    return Pipeline(ws, Context(config=Config(), events=EventBus()))


def test_char_entry_counts_counts_texts_not_occurrences() -> None:
    """**核心口径**：数"出现在多少条里"，不是"出现多少次"。

    用"次数"会被长句里的重复字符骗过：
    `'的的的的的'` 是 1 条、5 次。而判据要的是"影响多少条文本"。
    """
    from novaloc.pipeline.stages import Pipeline

    c = Pipeline._char_entry_counts(["的的的的的", "你好"])
    assert c["的"] == 1, "同一条里出现 5 次，也只算 1 条"
    assert c["你"] == 1
    # 跨组累加
    c2 = Pipeline._char_entry_counts(["的"], ["的"])
    assert c2["的"] == 2, "两条各出现一次 → 2 条"


def test_common_chars_look_frequent_and_drift_chars_look_rare() -> None:
    """真实数据的形状：正常字符几千条，跑偏字符几条。"""
    from novaloc.pipeline.stages import Pipeline

    translated = ["穿过传送门"] * 500 + ["我们ค่อยๆ ก็ได้"]
    counts = Pipeline._char_entry_counts(translated)
    assert counts["传"] == 500, "正常字符应当高频"
    assert counts["ค"] == 1, "跑偏字符应当罕见"
    pl = _pipeline()
    limit = pl._droppable_char_limit(len(translated))
    assert counts["ค"] <= limit, "罕见字符应当允许剔除"
    assert counts["传"] > limit, "高频字符绝不能剔除"


def test_droppable_limit_scales_with_project_size() -> None:
    """绝对阈值不够用，必须带比例项。

    实测：`'กี้'`（泰文）出现在 **7 条**里 —— 绝对阈值 3 挡不住，
    于是"剔了 18 个还剩 3 个"、字体合并**还是失败**。
    而 7 条相对于 2 万条只占 0.03%。
    """
    pl = _pipeline()
    assert pl._droppable_char_limit(10) == 3, "小项目用绝对下限"
    assert pl._droppable_char_limit(1000) >= 3
    assert pl._droppable_char_limit(30000) > 7, (
        "2 万条规模下，影响 7 条的字符必须允许剔除（真实事故就是这样）"
    )
    # 但也不能大到把真需求剔掉：0.5% 是保守上限
    assert pl._droppable_char_limit(30000) < 30000 * 0.01


def test_limit_never_reaches_real_content() -> None:
    """真正需要的字符（出现在 90%+ 条里）永远碰不到剔除线。"""
    from novaloc.pipeline.stages import Pipeline

    n = 30000
    translated = ["的生命值"] * n
    counts = Pipeline._char_entry_counts(translated)
    limit = _pipeline()._droppable_char_limit(n)
    for ch in "的生命值":
        assert counts[ch] > limit, f"{ch!r} 竟然落在可剔范围里"
