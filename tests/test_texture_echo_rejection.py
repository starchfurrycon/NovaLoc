"""贴图译文的两类"不能画上去"的坏输出。

真实贴图数据（BeyondPortal，173 个文字块）里出现过：

* 模型回"我读不出来"：``'+222?22?'`` → ``'已识别文字，无法确定具体含义'``，
  这句话被**原样重绘进贴图**，玩家会在游戏里读到它；
* 模型把原文抄一遍再加点料：``'[o]'`` → ``'[o]中文译文'``。

第二类特别阴：它比第一类长、是中文、长度也"合理"，
非空检查、长度检查、甚至"含不含汉字"的检查全都放行。

## 模型会用**加壳**绕开判据

`'[o]'` → `'[o]中文译文'` 多出来的是**汉字**，而最初的判据只查
ASCII 字母数字，所以没抓到。后来模型还变本加厉写了
`'" [o] 中文译文 "'`（引号 + 空格）、`'(o) 中文意思'`。

所以最终判据是**只比实义字符**：把标点、空白、括号全丢掉，
只留下字母/数字/汉字再比。这一步把所有包装花样一并解决，
而且不需要为 `'....'` → `'……'` 特判放行 ——
那两者实义字符都是空，判据自然不触发。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.service import TextureTranslator, _content  # noqa: E402


def _t() -> TextureTranslator:
    return TextureTranslator(Context(config=Config(), events=EventBus()))


# ----------------------------------------------------------------------
# 一、元话（模型在回答"我读不出来"）
# ----------------------------------------------------------------------

META_ANSWERS = [
    ("+222?22?", "已识别文字，无法确定具体含义"),
    ("xxx", "无法识别"),
    ("xxx", "抱歉，我无法翻译这段文字"),
    ("xxx", "作为AI，我不能处理这个请求"),
    ("xxx", "unable to read the text"),
    ("xxx", "there is no text in this image"),
]


@pytest.mark.parametrize("src,tgt", META_ANSWERS)
def test_meta_answers_are_rejected(src: str, tgt: str) -> None:
    t = _t()
    assert not t._translation_is_usable(src, tgt), f"元话被当成译文：{tgt!r}"


# ----------------------------------------------------------------------
# 二、原文回声 + 料（含**加壳**的各种写法）
# ----------------------------------------------------------------------

ECHOES = [
    ("[o]", "[o]中文译文"),
    ("[o]", "[o] 中文译文"),
    ("[o]", "[o]翻译"),
    ("[o]", "[o]XX"),
    # 加壳绕开判据 —— 这三条是"只比实义字符"要解决的
    ("[o]", '" [o] 中文译文 "'),
    ("[o]", "(o) 中文意思"),
    ("[o]", "《o》译文"),
    ("22zzzz²", "22zzzz² 译文"),
    ("GAME OVER", "GAME OVER 游戏结束"),
]


@pytest.mark.parametrize("src,tgt", ECHOES)
def test_source_echo_with_junk_is_rejected(src: str, tgt: str) -> None:
    t = _t()
    assert not t._translation_is_usable(src, tgt), f"原文回声被当成译文：{tgt!r}"


# ----------------------------------------------------------------------
# 三、正常情况**必须放行**（这些是真实数据里的好译文）
# ----------------------------------------------------------------------

GOOD = [
    ("GAME OVER", "游戏结束"),
    ("ABSoRB", "吸收"),
    ("Weapon", "武器"),
    ("Optimize", "优化"),
    ("Fanny pack", "胸包"),
    ("nEn ORORERPERTO TO TE", "欢迎来到奥罗瑞普托"),
    ("LUK", "幸运"),
    ("HP", "生命值"),
    ("M.Defense", "魔防"),
    ("CRIQICAL", "CRITICAL"),
]


@pytest.mark.parametrize("src,tgt", GOOD)
def test_good_translations_are_kept(src: str, tgt: str) -> None:
    t = _t()
    assert t._translation_is_usable(src, tgt), f"正常译文被误杀：{src!r} → {tgt!r}"


def test_identical_target_is_allowed() -> None:
    """原文 == 译文是**允许**的：专有术语本就该保留。

    注意这里说的是"没变长"，不是"内容相同" ——
    `'AGI' → 'AGI'` 走不到回声判据（没多出东西）。
    这类块另有 `looks_untranslated` 之类的检查去管，
    不是本函数的职责。
    """
    t = _t()
    assert t._translation_is_usable("[o]", "[o]")
    assert t._translation_is_usable("AGI", "AGI")


def test_punctuation_style_change_is_allowed() -> None:
    """`'....'` → `'……'`：英文省略号译成中文省略号，是**真译文**。

    第一版判据只看"变长了就拒"，把这条误杀了。
    现在靠"实义字符都是空"自然放行，不需要特判。
    """
    t = _t()
    assert t._translation_is_usable("....", "……")
    assert _content("....") == ""
    assert _content("……") == ""


def test_replaced_brackets_are_allowed() -> None:
    """`'[o]'` → `'[哦]'` 是把里面的字符翻译了，不是回声。"""
    t = _t()
    assert t._translation_is_usable("[o]", "[哦]")


# ----------------------------------------------------------------------
# 四、`_content` 本身
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expect",
    [
        ("[o]", "o"),
        ('" [o] 中文译文 "', "o中文译文"),
        ("(o) 中文意思", "o中文意思"),
        ("《o》译文", "o译文"),
        ("....", ""),
        ("……", ""),
        ("22zzzz²", "22zzzz2"),  # NFKC 把上标 ² 变成 2
        ("", ""),
    ],
)
def test_content_keeps_only_meaningful_chars(text: str, expect: str) -> None:
    assert _content(text) == expect


def test_rejection_reasons_are_counted() -> None:
    """被丢弃的原因要计数，报告里能看到"丢了多少、为什么"。"""
    t = _t()
    t._translation_is_usable("[o]", "[o]中文译文")
    t._translation_is_usable("x", "无法识别")
    reasons = t._rejections()
    assert reasons.get("source_echo_with_junk"), reasons
    assert reasons.get("meta_answer"), reasons


# ----------------------------------------------------------------------
# 五、模型跑到别的文字系统（文本路径已有，贴图路径**更**需要）
# ----------------------------------------------------------------------

#: 真实跑偏记录（文本侧实测到的同一批）
DRIFT_TARGETS = [
    "苏 กี้",
    "我现在就想让你 دخول我!!!",
    "我们ค่อยๆ ก็ได้",
    "Entonces, придется тебе подняться.",
    "而且你竟然饶了它们 ജീവ",
]


@pytest.mark.parametrize("target", DRIFT_TARGETS)
def test_drift_is_never_drawn_onto_a_texture(target: str) -> None:
    """**核心**：混进别的文字系统的译文绝不能画到贴图上。

    贴图路径比文本路径**更**不能放过这类错误：
    文本译文坏了可以在下一轮重译；贴图译文坏了是**直接烘焙进像素**的，
    玩家会在游戏画面上看到夹着泰文的乱码，
    而这个错误被写进 PNG 之后**再也不会被追问**。
    """
    t = _t()
    assert t._translation_is_usable("Suki", target) is False, (
        f"跑偏译文被放行、会画到贴图上：{target!r}"
    )


def test_drift_rejection_is_counted_under_its_own_reason() -> None:
    """原因要单独计数 —— 和"原文回声"混在一起就查不出是哪个病。"""
    t = _t()
    t._translation_is_usable("Suki", "苏 กี้")
    assert t._rejections().get("foreign_script"), t._rejections()


@pytest.mark.parametrize(
    "source,target",
    [
        ("ATK", "攻击力"),
        ("Now Loading...", "载入中……"),
        ("Damage 3", "伤害 3"),
        # 单个希腊字母是正常的（数学/单位符号），不能误杀
        ("Damage 3\u03c0", "伤害 3\u03c0"),
        # 属性缩写保留是允许的
        ("AGI", "AGI"),
    ],
)
def test_normal_texture_translations_still_pass(source: str, target: str) -> None:
    """加守卫不能把正常译文一起挡掉（`π` 那类单字符最容易被误杀）。"""
    t = _t()
    assert t._translation_is_usable(source, target) is True, (
        f"正常译文被误杀：{source!r} → {target!r}"
    )
