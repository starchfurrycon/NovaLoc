r"""★★ 「语言码文件名」判据：`locales/zh-CN.pak` 这类自带中文的铁证。

## 实测缺陷（用户指正："文件夹里带中文选项的游戏很多，收益应该很高"）

原 `detect_builtin_chinese_assets` 要求**两条同时满足**：

1. **目录名**是语言码（`zh-cn`/`chn`/`简体`…）；
2. 该目录里 **≥3 个含汉字的文本文件**（后缀在 `_ZH_TEXT_EXT` 白名单里）。

而 Godot 等引擎的形态是**语言码文件名**：

```
locales\am.pak  ar.pak  bg.pak  …  **zh-CN.pak**  zh-CN.pak.info
```

* 目录名是 `locales`（不是 `zh-cn`）⇒ 条件 1 失败；
* `.pak` 不在白名单里，而且是**二进制**（`read_text` 读不出汉字）
  ⇒ 条件 2 失败。

⇒ **判据对整个 `locales/` 形态完全失效。**

### 后果（全库实测）

```
194 个游戏里 **57 个（29.4%）** 带中文语言资产
其中 **7 个已经被处理过**（判据没拦住，白烧）：
  [Summoner Veil] 49,733 条    Battle Demon Kirsten 31,263 条
  Ambrosia 18,785 条           072 Project 15,003 条  …
另有 **50 个尚未处理**
```

## 修法

新增**判据 0**：文件名主干（**递归剥后缀**、归一 `_`→`-`）
是中文语言码 ⇒ 直接认定自带中文。

* **不读内容** ⇒ 对二进制语言包有效；
* **不判单条文本** ⇒ 没有 `'清宮 真白'`（全汉字日文人名）那种误杀；
* 与原有判据**并列**，不替换。

## 本文件测什么

1. 真实形态命中：`zh-CN.pak` / `zh_CN.pak.info` / `zh-Hans.xaml` / `zh.dat`；
2. **★ 误判守卫**：`sc.stylizedwater2.runtime.dll` 这类**必须不命中**
   （`sc` 是合法语言码，但它是 Unity Shader 库）；
3. 非中文语言码不命中（`en.pak` / `ja.pak` / `de.pak`）；
4. 只是"提到中文"的文件名不命中（`zh-CN-notes.txt`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.batch import _zh_language_file  # noqa: E402


def _mk(tmp_path: Path, rel: str) -> Path:
    """在临时游戏目录里造一个文件，返回游戏根。"""
    g = tmp_path / "game"
    p = g / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x01binary")
    return g


# ----------------------------------------------------------------------
# 1. 真实形态必须命中
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel",
    [
        "locales/zh-CN.pak",          # 实测 52 个游戏就是这个
        "locales/zh_CN.pak",          # 下划线写法
        "locales/zh-CN.pak.info",     # 双后缀（要剥两次）
        "Content/zh-Hans.xaml",       # 脚本语言包
        "data/zh.dat",                # 裸两字母
        "zh_Hans.ini",
    ],
)
def test_hits_real_locale_forms(tmp_path: Path, rel: str) -> None:
    g = _mk(tmp_path, rel)
    assert _zh_language_file(g) is not None, f"{rel} 是中文语言包，应当命中"


def test_hits_chinese_word_names(tmp_path: Path) -> None:
    """`汉化.zip` 这类中文词命名也应当命中（实测有游戏这样放汉化包）。"""
    g = _mk(tmp_path, "汉化和画廊/汉化.zip")
    assert _zh_language_file(g) is not None


# ----------------------------------------------------------------------
# 2. ★★ 误判守卫（这条最重要）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel",
    [
        # ★ 实测误判：这两个 DLL 剥到 `sc` 恰好命中语言码，
        #   但它是 Unity 的 Shader 库，与语言毫无关系。
        "Game_Data/Managed/sc.stylizedwater2.runtime.dll",
        "Game_Data/Managed/sc.posteffects.runtime.dll",
    ],
)
def test_rejects_dotted_dll_stem(tmp_path: Path, rel: str) -> None:
    r"""★★ 多段主干 + 裸两字母码 ⇒ **必须拒绝**。

    守卫要看**原始文件名**有没有多余的点 —— 我第一版写成看 `stem`
    （已剥掉所有点 ⇒ 永远为假），守卫形同虚设，误判照样发生。
    """
    g = _mk(tmp_path, rel)
    assert _zh_language_file(g) is None, f"{rel} 不是语言文件，不该命中"


# ----------------------------------------------------------------------
# 3. 非中文语言码不命中
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel",
    ["locales/en.pak", "locales/ja.pak", "locales/de.pak", "locales/jp.pak"],
)
def test_rejects_other_languages(tmp_path: Path, rel: str) -> None:
    g = _mk(tmp_path, rel)
    assert _zh_language_file(g) is None, f"{rel} 不是中文，不该命中"


# ----------------------------------------------------------------------
# 4. "提到中文"≠"中文语言包"
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel",
    ["docs/zh-CN-notes.txt", "readme/zh-CN-guide.md", "src/zh-CN-helper.py"],
)
def test_rejects_mention_not_package(tmp_path: Path, rel: str) -> None:
    """整串必须**等于**语言码，带后缀词的不算。"""
    g = _mk(tmp_path, rel)
    assert _zh_language_file(g) is None, f"{rel} 只是提到中文，不该命中"


# ----------------------------------------------------------------------
# 5. 空目录不崩
# ----------------------------------------------------------------------
def test_empty_dir_is_safe(tmp_path: Path) -> None:
    g = tmp_path / "empty"
    g.mkdir()
    assert _zh_language_file(g) is None


# ----------------------------------------------------------------------
# 6. 结构性守卫：判据 0 确实接进了主函数
# ----------------------------------------------------------------------
def test_detector_uses_language_file_evidence() -> None:
    r"""★★ `detect_builtin_chinese_assets` 必须调用 `_zh_language_file`。

    防止有人后来把它摘掉 —— 症状是"50 个带中文的游戏又被白翻一遍"，
    很难归因（本轮就花了很久才发现判据对 `locales/` 形态失效）。
    """
    src = (ROOT / "src" / "novaloc" / "batch.py").read_text(encoding="utf-8")
    assert "_zh_language_file(game_dir)" in src, (
        "主判据没有调用 `_zh_language_file` ⇒ locales/zh-CN.pak 形态又会漏判"
    )
    assert "自带中文语言文件" in src, "没有把文件名证据写进跳过的理由里"
