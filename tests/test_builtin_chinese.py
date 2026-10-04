r"""★ "自带中文"检测：**多语言资产**形态（本轮漏判，代价很大）。

## 用户指正的原话

> 有可能很多支持中文的游戏被你误处理了，因为支持中文的同时支持别的语言，
> 导致游戏里有别的语言的资产，而你就进行了多余操作。
> 比如 Alice in Cradle 就支持中文，可是你依旧在翻译。
> 支持中文的游戏只需要将其换为其自带的中文就行了（比如在其设置里）。

## 实测确认的漏判

`AliceInCradle_ver029` 里：

    AliceInCradle_Data\StreamingAssets\localization\
        en/  ko-kr/  th/  zh-cn/  zh-tc/  _/  __AdditionalFonts/
    zh-cn\ 里 70 个文件，`ev_book.txt` 就是中文对白

**官方中文一直在那里**，玩家在设置里选一下就有 —— 而我给它翻了
127,693 条，白烧几个小时 GPU。

## 为什么原来的判据抓不到

它只查 RPG Maker 的 `System.json` 和有没有中文字体文件。
**"每种语言一个目录"这种形态它完全看不见**：主配置里一个中文都没有，
字体还是共用的。所以"支持中文"这个事实在旧判据里**不可见**。

## 本文件测什么

1. 认得各种语言目录名（`zh-cn`/`cn`/`chn`/`简体`…）；
2. **空壳目录不算**（只看目录名会把"预留但没填"的误判成有中文）；
3. 汉字要在**该目录内**的文件里（别处有中文不算这个语言目录有效）；
4. 不对没有该形态的游戏误报（否则会漏翻真正需要翻译的游戏）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.batch import (  # noqa: E402
    detect_already_chinese,
    detect_builtin_chinese_assets,
)

ZH_LINE = "「好痛……」诺艾儿用手捂着肿胀的皮肤。"


def _make(game: Path, lang_dir: str, *, n_files: int = 4, chinese: bool = True) -> Path:
    """造一个多语言游戏：``lang_dir`` 下放 ``n_files`` 个本地化文件。"""
    d = game / "StreamingAssets" / "localization" / lang_dir
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        body = ZH_LINE if chinese else "This is an English line."
        (d / f"ev_{i}.txt").write_text(body, encoding="utf-8")
    return game


# ----------------------------------------------------------------------
# 一、认得出来
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "lang_dir",
    ["zh-cn", "zh_cn", "zh", "zh-Hans", "zh-tw", "cn", "CHS", "cht", "chinese",
     "sc", "tc", "简体", "繁体", "中文"],
)
def test_recognises_chinese_language_dirs(tmp_path: Path, lang_dir: str) -> None:
    """常见的中文语言目录名都能认出来。"""
    game = _make(tmp_path / "game", lang_dir)
    hit, why = detect_builtin_chinese_assets(game)
    assert hit is True, f"{lang_dir} 应被认出（why={why!r}）"


def test_case_insensitive(tmp_path: Path) -> None:
    """游戏里大小写很随意（`CN` / `Cn` / `ZH-CN` 都出现过）。"""
    for name in ("CN", "Cn", "ZH-CN", "Zh-Hans"):
        game = _make(tmp_path / f"g_{name}", name)
        assert detect_builtin_chinese_assets(game)[0] is True, name


def test_detects_in_nested_plugin_dir(tmp_path: Path) -> None:
    """实测 `Night_of_Revenge` 的中文在 BepInEx 插件深处，不是顶层目录。"""
    d = tmp_path / "game" / "BepInEx" / "plugins" / "HellGateJson" / "EventCore" / "Cn"
    d.mkdir(parents=True)
    for i in range(4):
        (d / f"e{i}.txt").write_text(ZH_LINE, encoding="utf-8")
    assert detect_builtin_chinese_assets(tmp_path / "game")[0] is True


# ----------------------------------------------------------------------
# 二、不误报（比"能认出"更重要 —— 误报会漏翻真需要翻的游戏）
# ----------------------------------------------------------------------
def test_empty_shell_dir_is_not_chinese(tmp_path: Path) -> None:
    r"""★ `zh-cn` 目录存在但**是空的** ⇒ 不算有中文。

    只看目录名会把"预留了但没填内容"的空壳误判成有中文，
    后果是**漏翻一个本来该翻的游戏**。
    """
    d = tmp_path / "game" / "localization" / "zh-cn"
    d.mkdir(parents=True)
    assert detect_builtin_chinese_assets(tmp_path / "game")[0] is False


def test_dir_with_english_content_is_not_chinese(tmp_path: Path) -> None:
    """目录名叫 `zh-cn` 但里面是英文（复制粘贴错误）⇒ 不算。"""
    game = _make(tmp_path / "game", "zh-cn", chinese=False)
    assert detect_builtin_chinese_assets(game)[0] is False


def test_too_few_chinese_files_is_not_chinese(tmp_path: Path) -> None:
    """只有 1~2 个含汉字文件达不到门槛（可能是噪声/个别注释）。"""
    game = _make(tmp_path / "game", "zh-cn", n_files=1)
    assert detect_builtin_chinese_assets(game)[0] is False


def test_no_language_dir_at_all(tmp_path: Path) -> None:
    """没有语言目录 ⇒ 不算。"""
    d = tmp_path / "game" / "data"
    d.mkdir(parents=True)
    (d / "a.txt").write_text(ZH_LINE, encoding="utf-8")
    assert detect_builtin_chinese_assets(tmp_path / "game")[0] is False


def test_english_only_language_dirs(tmp_path: Path) -> None:
    """只有 en/ja 这类非中文语言目录 ⇒ 不算（这才是该翻译的游戏）。"""
    game = tmp_path / "game"
    for name in ("en", "ja", "ko-kr"):
        _make_lang(game, name, chinese=False)
    assert detect_builtin_chinese_assets(game)[0] is False


def _make_lang(game: Path, lang_dir: str, *, chinese: bool, n: int = 4) -> None:
    d = game / "localization" / lang_dir
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        body = ZH_LINE if chinese else "English line."
        (d / f"f{i}.txt").write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------
# 三、接进 detect_already_chinese（这才是真正让游戏被跳过的那条路）
# ----------------------------------------------------------------------
def test_detect_already_chinese_uses_the_new_criterion(tmp_path: Path) -> None:
    """★ 新判据必须**接进** `detect_already_chinese`，否则等于没修。

    只写一个独立函数、忘了在主管线里调用，是很常见的"看着修好了、
    实际没生效"。
    """
    game = _make(tmp_path / "game", "zh-cn")
    hit, why = detect_already_chinese(game, "unity")
    assert hit is True, f"自带中文的游戏没被跳过（why={why!r}）"
    assert "中文" in why


def test_detect_already_chinese_still_false_for_translatable(tmp_path: Path) -> None:
    """反向：只有英文语言目录的游戏必须**继续被翻译**。"""
    game = tmp_path / "game"
    _make_lang(game, "en", chinese=False)
    assert detect_already_chinese(game, "unity")[0] is False


def test_reason_mentions_the_directory(tmp_path: Path) -> None:
    """理由里要写清**是哪个目录** —— 否则用户无法核对。"""
    game = _make(tmp_path / "game", "zh-cn")
    _hit, why = detect_builtin_chinese_assets(game)
    assert "zh-cn" in why, why
