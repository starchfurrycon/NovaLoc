"""句子数守卫：抓"翻译时丢掉整句"。

## 这个判据的定位（**故意只报不拦**）

实测 5,224 条已判成功的译文里，按句子数比对有 **480 条（9.2%）** 的译文
句数少于源文，丢的常常是玩法规则（`Add 4 cards... Then discard 2.` →
`加入 4 张牌。`）。旧的长度比判据抓不住，因为英→中的正常长度比只有约
0.35，阈值必须放得很低，一低就漏掉"丢一个从句"。

**但它不能作为致命判据**：人工抽查开火样本发现大部分是假阳性，全是
"语气词被合并"这类风格差异，内容其实完整。所以它只产生 warning。
下面的测试同时守住"该开火时开火"和"**不该开火时绝不开火**"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.translate.guards import (  # noqa: E402
    _terminates_sentence,
    check_sentence_drop,
    count_sentences,
    guard,
)

# ---------------------------------------------------------------------------
# _terminates_sentence —— 这套判据的核心：区分"省略号"与"加强语气"
# ---------------------------------------------------------------------------


def test_ellipsis_does_not_terminate() -> None:
    """``...`` 是语气、不是句子边界。这是假阳性的最大来源。"""
    assert not _terminates_sentence("...")
    assert not _terminates_sentence("..")
    assert not _terminates_sentence("... ")


def test_single_dot_terminates() -> None:
    assert _terminates_sentence(".")
    assert _terminates_sentence(". ")


def test_emphasis_terminates() -> None:
    """``?!`` / ``!?`` / ``???`` 是**加强语气**，确实收句 —— 与省略号不同。"""
    assert _terminates_sentence("!")
    assert _terminates_sentence("?")
    assert _terminates_sentence("?!")
    assert _terminates_sentence("!?")
    assert _terminates_sentence("???")
    assert _terminates_sentence(";")


def test_empty_run_does_not_terminate() -> None:
    assert not _terminates_sentence("")
    assert not _terminates_sentence("   ")


# ---------------------------------------------------------------------------
# count_sentences
# ---------------------------------------------------------------------------


def test_counts_latin_sentences() -> None:
    assert count_sentences("One. Two. Three.", latin=True) == 3
    assert count_sentences("Only one", latin=True) == 1


def test_abbreviations_do_not_split_sentences() -> None:
    """``Mr. Smith`` 是一个句子，不是两个。

    不做缩写保护会让源句数虚高，从而把大量**正常**译文误判成"缺句"。
    """
    assert count_sentences("Mr. Smith went home.", latin=True) == 1
    assert count_sentences("Use it vs. the boss.", latin=True) == 1
    assert count_sentences("e.g. this one.", latin=True) == 1
    assert count_sentences("See J. Doe now.", latin=True) == 1
    assert count_sentences("approx. 5 items", latin=True) == 1


def test_ellipsis_is_not_a_sentence_boundary() -> None:
    """★ 实测踩出来的最大假阳性来源。

    ``「Sigh... Whatever, let's go...」`` 若按 ``...`` 切成 2 句，
    而中文译文只用 ``…``，就会把一条**内容完整**的译文判成缺句。
    """
    assert count_sentences("Sigh... Whatever, let's go...", latin=True) == 1
    assert count_sentences("Wait... what?", latin=True) == 1


def test_emphasis_still_splits() -> None:
    """省略号不收句，但 ``?!`` / ``!`` 收句 —— 两者必须区分开。"""
    assert count_sentences("What?! No way!", latin=True) == 2
    assert count_sentences("Really...? Yes.", latin=True) == 2


def test_counts_cjk_sentences() -> None:
    assert count_sentences("第一句。第二句。", latin=False) == 2
    assert count_sentences("只有一句", latin=False) == 1
    # ``……`` 是语气，不收句
    assert count_sentences("哎呀……差点就完了～", latin=False) == 1


# ---------------------------------------------------------------------------
# check_sentence_drop —— 该开火
# ---------------------------------------------------------------------------


def test_fires_when_a_sentence_is_dropped() -> None:
    src = "Add 4 cards from the deck to hand. Then, randomly discard 2 cards."
    tgt = "加入 4 张牌。"
    warnings = check_sentence_drop(src, tgt)
    assert any(w.startswith("sentence_drop") for w in warnings)


def test_fires_on_three_to_one() -> None:
    src = "Please set the value. It affects the ending. Read carefully."
    assert check_sentence_drop(src, "请设置该值。") != []


# ---------------------------------------------------------------------------
# check_sentence_drop —— 不该开火（假阳性防护）
# ---------------------------------------------------------------------------


def test_quiet_when_all_sentences_present() -> None:
    src = "Guaranteed escape from battle. Cannot be used on Area Bosses."
    tgt = "保证在战斗中脱离。不能对区域首领使用。"
    assert check_sentence_drop(src, tgt) == []


def test_quiet_on_single_sentence_source() -> None:
    """单句源文**根本不参与**判据，所以不会因为"中文没句号"而误报。"""
    assert check_sentence_drop("Just one sentence here", "就一句话在这里") == []


def test_quiet_on_interjection_collapse() -> None:
    """语气词合并属于**风格差异**，内容完整 —— 判据必须闭嘴。

    真实数据里这类占了开火样本的大多数。若判据在这里开火，就会把几百条
    正确译文退回英文。
    """
    assert check_sentence_drop("Lifia\n「Phew... that was close~」", "莉菲亚\n哎呀…差点就完了～") == []
    assert check_sentence_drop("Monster\n「Whoa, there... Kuuuugh...」", "怪物\n等等… 呜…") == []


def test_quiet_on_non_latin_source() -> None:
    """中日文源文的标点习惯差异大，按句数比会大量误报，所以跳过检查。"""
    assert check_sentence_drop("こんにちは。さようなら。", "你好。") == []


def test_quiet_when_source_has_too_little_latin() -> None:
    assert check_sentence_drop("短", "短") == []


# ---------------------------------------------------------------------------
# 必须是 warning，不能是 fatal
# ---------------------------------------------------------------------------


def test_sentence_drop_is_warning_not_fatal() -> None:
    """★ 核心：这条判据**绝不能**判死。

    把它设成 fatal 会把几百条**内容完整**的译文退回英文 ——
    那比偶尔少译一句更伤体验（与"UI 缩写撞词"同一取舍）。
    """
    src = "Add 4 cards from the deck to hand. Then, randomly discard 2 cards."
    tgt = "加入 4 张牌。"
    r = guard(src, tgt, max_chars=None, length_ratio=2.2, target_lang="zh-Hans")
    assert any(w.startswith("sentence_drop") for w in r.warnings)
    assert not r.fatal, "句子数缺失只是警告，不该判死"
    assert r.text, "译文应被保留（判死后 text 会是空串）"
