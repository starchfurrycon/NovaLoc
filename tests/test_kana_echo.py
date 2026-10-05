r"""★★ 假名回显守卫：批内原样回显的日文假名必须判**失败**，交给逐条重译。

## 实测缺陷（这个文件就是为它写的回归测试）

批处理时模型会把**纯假名条目原样回显**，而流水线把它记成"已译"：

```
批内：'ポイズンガード' → 'ポイズンガード'
单条：'ポイズンガード' → '毒药卫'          ← 单独问就对
```

**30/30 条单独复测全部得到真译文（100%）**
⇒ 是"批内该翻没翻"，不是"本来就该保留"。

规模：已译条目里 **2,218 条**（全库 19,353 条回显的 11.5%）。

## ★ 为什么只处理"含假名"，不处理所有回显

全库 19,353 条回显的分类（实测）：

| 类别 | 条数 | 该不该重译 |
| --- | --- | --- |
| 纯拉丁（`Rockman` / `IN-cubus001`） | 15,892 | **不该**（专名） |
| **含假名** | **2,218** | **该**（实测 30/30） |
| 含汉字（`剣士` / `清宮 真白`） | 1,243 | 不确定，**先不动** |

⇒ 只在**能用行为验证**的最小集合上动手。
这条纪律来自我上一轮在"跳过不翻译"上翻车
（静态启发式收益 5% / 风险 25%，已废弃）。

## 两道防线都要测

1. **判据本身**（`is_kana_echo`）不能误伤纯拉丁专名；
2. **判失败时必须清空 target** —— `FAILED` 的语义是"没有可用译文"，
   留着假名会让字体阶段把日文字符收进字符集，
   最终**满屏口口口**（这是项目里记录过的事故）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate.ollama_provider import is_kana_echo  # noqa: E402


# ----------------------------------------------------------------------
# 判据：应该判为"回显"的
# ----------------------------------------------------------------------
def test_katakana_echo_is_detected() -> None:
    """★ 核心：片假名条目的原样回显必须被抓到（实测 30/30 该重译）。"""
    assert is_kana_echo("ポイズンガード", "ポイズンガード")
    assert is_kana_echo("ゴブリン", "ゴブリン")
    assert is_kana_echo("ポーション", "ポーション")


def test_hiragana_echo_is_detected() -> None:
    """平假名同样要抓（`薙ぎ払い` 这类混假名的词）。"""
    assert is_kana_echo("薙ぎ払い", "薙ぎ払い")


def test_echo_with_punctuation_difference_is_detected() -> None:
    """只差标点也算回显（判据用"实义字符"比较）。"""
    assert is_kana_echo("ポーション！", "ポーション。")


def test_mixed_kana_latin_echo_is_detected() -> None:
    """假名 + 拉丁混排（`ＴＰストッカー`）也要抓。"""
    assert is_kana_echo("ＴＰストッカー", "ＴＰストッカー")


# ----------------------------------------------------------------------
# 判据：**不该**误伤的
# ----------------------------------------------------------------------
def test_pure_latin_echo_is_not_detected() -> None:
    r"""★★ 纯拉丁专名**不能**被判回显（15,892 条，是回显里的大头）。

    实测那些是 `Rockman` / `IN-cubus001` 这类**名字**，
    重译只会浪费 token 且结果一样 ⇒ 必须放过。
    """
    assert not is_kana_echo("Rockman", "Rockman")
    assert not is_kana_echo("IN-cubus001", "IN-cubus001")
    assert not is_kana_echo("[Dauntless] Toriko", "[Dauntless] Toriko")


def test_real_translation_is_not_detected() -> None:
    """真的翻了 ⇒ 当然不算回显。"""
    assert not is_kana_echo("ポイズンガード", "毒药卫")
    assert not is_kana_echo("ゴブリン", "绿皮")


def test_empty_inputs_are_not_detected() -> None:
    """空串不该判回显（否则会把空译文当"回显"再标一次失败）。"""
    assert not is_kana_echo("", "ポイズンガード")
    assert not is_kana_echo("ポイズンガード", "")
    assert not is_kana_echo("", "")


def test_punctuation_only_is_not_detected() -> None:
    """两边实义字符都空时不算回显（`'...'` → `'...'` 没东西可翻）。"""
    assert not is_kana_echo("ポーション…", "……")


def test_kanji_echo_is_not_detected() -> None:
    r"""★★ 含汉字但**无假名**的回显暂时**不动**（1,243 条，判据未验证）。

    日文汉字可能本来就该保留（`剣士` 在中文里也写作"剑士"，
    但 `清宮 真白` 是人名，可能该保留）。**没有实测依据就不动手**
    —— 这是本文件的核心纪律。
    """
    assert not is_kana_echo("剣士", "剣士")
    assert not is_kana_echo("清宮 真白", "清宮 真白")


def test_source_without_kana_returning_same_latin_is_ignored() -> None:
    """假名只在**原文**里才算（译文里冒出假名不是"回显"）。"""
    assert not is_kana_echo("Potion", "ポーション")
