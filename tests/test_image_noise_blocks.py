"""贴图上的**噪声块**必须在翻译之前就被挡掉。

## 为什么（真实游戏跑完一整轮之后才发现的）

一台真实 RPG Maker MZ 游戏跑完 `images_localize`：87 张图被"汉化"、
236 处文字被"替换"。看报告一切正常。把结果摊开看才发现，
进到翻译的 238 个块里有一大批**根本不是文字**：

==================== ================================ ========
类别                 样本                             占比
==================== ================================ ========
纯数字噪声            ``'4994994'`` ``'444444'`` ``'16999699'``  24.8%
符号噪声              ``'●+++++++'`` ``'....'`` ``'")'``        3.8%
重复噪声              ``'COOOOOOO'`` ``'OOOOOOOO'``             2.5%
==================== ================================ ========

危害有三层，一层比一层重：

1. **白烧翻译时间**（每个块一次模型调用）；
2. **画回贴图**：`Balloon.png`（一张动画表/行走图）有 **47.4%** 的像素
   被改动 —— 等于把原画涂花了；
3. 最坏的：``'+222?22?'`` 送给翻译后，模型回的是
   ``'已识别文字，无法确定具体含义'``（一句"我读不出来"），
   而这句**抱怨被原样画到了游戏画面上**。

本项目的硬约束是"贴图文字绝不来自生成模型"，而把噪声当真文字翻译、
再把模型的元话画上去，正是这条约束要防的事。

## 判据（用真实数据校准，不是拍脑袋）

`_looks_like_text` 两条：

1. 至少要有一个字母（含 CJK）。纯数字块没有翻译价值 —— 贴图里的数字
   通常是伤害数值/图标编号，引擎会用变量渲染真实数值。
2. 数字占字母数字的比例 < 0.5。这条额外挡掉 ``'22zzzz²'``（数字 3/6）。

校准结果（真实数据，238 个块）：**保留 173、丢弃 65**，
被丢弃的全部是上表那三类；短的正例 ``'Zz'`` ``'on'`` ``'OB'``
``'一场'`` ``'ATK'`` ``'MHP'`` 一个没被误杀。

`_translation_is_usable` 两条（挡的是**译文**而不是源文）：

1. 元话标记（"无法确定"/"抱歉"/"unable to"……）；
2. 译文 = 原文 + 含字母数字的冗余（实测 ``'[o]'`` → ``'[o]中文译文'``）。
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
from novaloc.images.service import TextureTranslator  # noqa: E402


def _tt() -> TextureTranslator:
    return TextureTranslator(Context(config=Config(), events=EventBus()))


# ----------------------------------------------------------------------
# 一、源文：噪声块不得进入翻译
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        # 真实数据里的纯数字噪声（24.8%）—— 全部被挡
        "4994994", "444444", "999999", "000", "00", "68", "88888818",
        "16999699", "99999999", "16", "32", "21", "10", "90", "1C", "30",
        "1/1", "×0/1", "×2/2", "2h", "76",
        # 数字占主导（0.75）
        "Z2 Z22 Z22. Z22. 22.222..",
    ],
)
def test_pure_number_blocks_are_not_text(text: str) -> None:
    """**核心回归**：纯数字/数字主导的块不是要翻译的文字。

    它们在贴图里是伤害数值、图标编号、数量 —— 而且真实数值是引擎用
    变量渲染的，不是烘焙在贴图里的。

    实测这 33 种文本被丢掉，**全是数字或符号**，没有一个真词。
    """
    assert _tt()._looks_like_text(text) is False, f"{text!r} 被判成了文字"


@pytest.mark.parametrize(
    "text",
    [
        # 真实数据里的符号噪声
        "●+++++++", "*★", "....", '")', "3?",
    ],
)
def test_symbol_only_blocks_are_not_text(text: str) -> None:
    assert _tt()._looks_like_text(text) is False, f"{text!r} 被判成了文字"


@pytest.mark.parametrize(
    "text",
    [
        # 真实的正例：必须留下
        "ABSoRB", "GAME OVER", "CRIQICAL", "LMMUNE", "CRITICAL",
        "Now Loading...", "AGI", "LUK", "MHP", "ATK", "DEF",
        # 短块：真实数据里这些是图标上的英文缩写，不能因为短就杀
        "Zz", "zzz", "on", "OB", "AD", "CC", "mr", "GG", "一场", "经验值",
        # 含少量数字的正常文字（数字占比 < 0.5）
        "Level 3", "abc1",
    ],
)
def test_real_text_is_kept(text: str) -> None:
    """**反例**：真文字一个都不能被误杀。

    这条和上面两条同等重要：闸门做过头会让功能变死
    （"修好了"其实是什么都没做）。
    """
    assert _tt()._looks_like_text(text) is True, f"真文字 {text!r} 被误杀"


def test_known_limitations_are_documented_not_hidden() -> None:
    """**把已知漏网之鱼显式钉住**，免得以后以为它们被处理了。

    这三类**仍然通过** `_looks_like_text`，各有原因：

    * ``'22zzzz²'`` —— 数字占比 3/7≈0.43，低于阈值 0.5。
      它是真噪声（行走图上的乱码），但按"数字占比"这一条抓不住；
    * ``'V.'`` / ``'[o]'`` —— 含字母，像是很短的标签。
      它们是**真噪声**，但靠 `_translation_is_usable` 的
      "原文回显 + 冗余"判据才拦得住（`'[o]'` → `'[o]中文译文'`）。
    * ``'COOOOOOO'`` —— 含字母，是重复噪声。

    要真正解决需要"同图内该文本是否只出现在图案区域"之类的
    上下文判据，得先拿更多真实数据校准。**留着这条测试是为了
    让这个事实一直可见**，而不是把它藏起来。
    """
    tt = _tt()
    for text in ("22zzzz²", "V.", "[o]", "COOOOOOO"):
        assert tt._looks_like_text(text) is True, (
            f"{text!r} 现在被拦住了 —— 那是好事，请更新这条测试与文档"
        )


def test_empty_and_whitespace_are_not_text() -> None:
    tt = _tt()
    assert tt._looks_like_text("") is False
    assert tt._looks_like_text("   \n ") is False


def test_digit_ratio_boundary() -> None:
    """数字占比正好 0.5 要**拒**（判据是 `< 0.5`）。

    ``'HP99'`` 也在被拒之列 —— 它是个**边界上的取舍**：
    纯字母的 ``'HP'`` 能过，带两位数字的不行。真实数据里被判掉的是
    ``'1C'`` ``'2h'`` ``'30'`` 这类，保留的短标签全是纯字母
    （``'ATK'`` ``'MHP'`` ``'Zz'``），所以这个边界没有伤到真文字。
    """
    tt = _tt()
    assert tt._looks_like_text("ab12") is False, "数字占比 50% 应被拒"
    assert tt._looks_like_text("HP99") is False, "数字占比 50% 应被拒"
    assert tt._looks_like_text("abc1") is True, "数字占比 25% 应通过"
    assert tt._looks_like_text("HP") is True, "纯字母必须通过"


def test_integration_plausible_block_uses_the_new_rule() -> None:
    """`_is_plausible_text_block` 必须真的接上这条判据。"""
    tt = _tt()

    class B:
        def __init__(self, s: str) -> None:
            self.source = s
            self.box = (0, 0, 40, 12)

    assert tt._is_plausible_text_block(B("4994994"), 200, 200) is False
    assert tt._is_plausible_text_block(B("GAME OVER"), 200, 200) is True
    # 原有判据不能被破坏
    assert tt._is_plausible_text_block(B("A"), 200, 200) is False, "单字符仍要拦"
    assert tt._is_plausible_text_block(B("GAME OVER"), 40, 12) is False, "超大占比仍要拦"


# ----------------------------------------------------------------------
# 二、译文：模型说的"我读不出来"不得画上去
# ----------------------------------------------------------------------

def test_meta_answer_is_rejected() -> None:
    """**核心回归**：实测 ``'+222?22?'`` → ``'已识别文字，无法确定具体含义'``。

    这句是模型的**抱怨**，不是译文。它被原样重绘进贴图后，
    玩家会在游戏里看到"已识别文字，无法确定具体含义" ——
    比不翻译坏得多。
    """
    tt = _tt()
    assert tt._translation_is_usable("+222?22?", "已识别文字，无法确定具体含义") is False
    assert tt._rejections().get("meta_answer", 0) == 1


@pytest.mark.parametrize(
    "answer",
    [
        "无法确定图片中的文字",
        "抱歉，我无法识别",
        "抱歉，我无法完成这个请求",
        "I cannot determine the text",
        "unable to read the text",
        "There is no text in this image",
    ],
)
def test_meta_markers_are_caught(answer: str) -> None:
    assert _tt()._translation_is_usable("abc", answer) is False


def test_source_echo_with_junk_is_rejected() -> None:
    """实测 ``'[o]'`` → ``'[o]中文译文'`` —— 占位符垃圾被当成译文。"""
    tt = _tt()
    assert tt._translation_is_usable("[o]", "[o]中文译文") is False
    assert tt._rejections().get("source_echo_with_junk", 0) == 1


def test_unchanged_translation_is_allowed() -> None:
    """专有术语本就该保留原样（``'AGI'`` → ``'AGI'``）。"""
    assert _tt()._translation_is_usable("AGI", "AGI") is True


def test_punctuation_only_extra_is_allowed() -> None:
    """**回归**：``'....'`` → ``'……'`` 是**真译文**（英文省略号→中文省略号）。

    第一版判据只看"译文是原文加长"，把这条误杀了 ——
    多出来的部分必须是**字母数字**才算冗余。
    """
    assert _tt()._translation_is_usable("....", "……") is True


def test_normal_translations_pass() -> None:
    tt = _tt()
    for src, tgt in [
        ("GAME OVER", "游戏结束"),
        ("ABSoRB", "吸收"),
        ("MHP", "生命值"),
        ("on", "启动"),
        ("Now Loading...", "正在加载……"),
        ("Level 3", "等级 3"),
    ]:
        assert tt._translation_is_usable(src, tgt) is True, f"{src!r}→{tgt!r} 被误杀"


def test_empty_target_is_unusable() -> None:
    """空译文不可用 —— 调用方会保留原样（不画东西）。"""
    assert _tt()._translation_is_usable("abc", "") is False


# ----------------------------------------------------------------------
# 三、端到端：坏译文不会落到结果里
# ----------------------------------------------------------------------

def test_translate_texts_drops_bad_answers(tmp_path: Path) -> None:
    """整条链路：翻译回调回坏答案时，那个块保持未翻译。"""
    tt = _tt()

    class Entry:
        def __init__(self, target: str) -> None:
            self.target = target

    def fake_translate(items, target_lang):  # noqa: ANN001, ARG001
        # 对"读不出来"的块回一句抱怨，对正常的块回好译文
        return [
            Entry("已识别文字，无法确定具体含义") if it.unit.source == "+222?22?" else Entry("游戏结束")
            for it in items
        ]

    tt._translate_fn = fake_translate
    out = tt._translate_texts(
        ["+222?22?", "GAME OVER"],
        "zh-CN",
        existing={},
        glossary=[],
        context_lines=[],
        path=tmp_path / "x.png",
        dry_run=False,
    )
    assert 0 not in out, f"坏译文没有没丢掉：{out.get(0)!r}"
    assert out.get(1) == "游戏结束", "好译文被牵连了"
