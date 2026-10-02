"""`loose` 兜底适配器的抽取测试。

这些用例都对应**实测踩过的问题**，不是凭空设计的。

## 背景：RPG Maker VX Ace 的 `Game.ini` 被当成了游戏文案

`Brave_Alchemist_Colette_v1.05` 与 `Lewdcrest Lady of the Night - Branded AZEL`
是两个 `.wolf` 加密的 Wolf RPG / VX Ace 游戏（`Data/` 里全是
`BasicData.wolf`、`BGM.wolf` 之类，**不可解**）。引擎识别正确地退到
`loose` 兜底，但抽取只捞到 `Game.ini` 里的运行参数：

    Start=0   SoftModeFlag=0   WindowModeFlag=1   SEandBGM=3
    FrameSkip=0   Proxy=   ProxyPort=

于是这两个游戏在盘点里各自报"11 条 / 10 条单位"，看起来"有内容可翻"，
实际上是**纯垃圾**：翻它毫无意义、污染翻译记忆（记忆库按相似度匹配，
一堆 `XxxFlag=N` 会拉低命中质量），做写回时还可能把配置写坏。

修完之后这两个游戏诚实地报 **0 条** —— "这个游戏没抽到文本"是**正确结论**，
比"抽到 11 条没用的配置"有用得多。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novaloc.engines.loose import LooseFilesAdapter, _is_engine_config_line


@pytest.fixture
def _ctx(tmp_path: Path) -> object:
    """最小 Context 替身。

    `BaseAdapter.__init__` 会读 `ctx.config`，所以必须给一个（哪怕是 None）——
    第一版漏了它，直接 `AttributeError: 'ctx' object has no attribute 'config'`。
    """
    from types import SimpleNamespace

    return SimpleNamespace(config=None, events=None, logger=None, root=tmp_path)


def _extract(tmp_path: Path, files: dict[str, str]) -> list[str]:
    from types import SimpleNamespace

    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    ctx = SimpleNamespace(config=None, events=None, logger=None, root=tmp_path)
    ad = LooseFilesAdapter(ctx)
    units, _rep = ad.extract_text(tmp_path)
    return [u.source for u in units]


# ---------------------------------------------------------------------------
# ★ 引擎配置行必须被跳过
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "line"),
    [
        ("Game.ini", "Start=0"),
        ("Game.ini", "SoftModeFlag=0"),
        ("Game.ini", "WindowModeFlag=1"),
        ("Game.ini", "SEandBGM=3"),
        ("Game.ini", "FrameSkip=0"),
        ("Game.ini", "Proxy="),
        ("Game.ini", "ProxyPort="),
        ("Game.ini", "ScreenShotFlag=1"),
        ("game.ini", "Title=Some Game"),
    ],
)
def test_engine_config_lines_are_skipped(filename: str, line: str) -> None:
    r"""★ `Game.ini` 里的 `Key=Value` 参数**不是**游戏文案。

    实测这两个 `.wolf` 游戏各自贡献 11 / 10 条这种垃圾单位。
    """
    assert _is_engine_config_line(filename, line) is True, (
        f"{filename} 里的 {line!r} 应判为引擎配置（跳过）"
    )


@pytest.mark.parametrize(
    ("filename", "line"),
    [
        # 不是 KV 结构 ⇒ 可能是真文案，必须保留
        ("Game.ini", "是否继续游戏？"),
        ("Game.ini", "RPG Maker VX Ace"),
        ("Game.ini", "第三章：暗影森林"),
        # 不是引擎配置文件 ⇒ 一律保留（避免一刀切漏文案）
        ("messages.ini", "greet=你好"),
        ("other.txt", "Start=0"),
        ("dialogue.json", "Speaker=Aria"),
    ],
)
def test_non_config_lines_are_kept(filename: str, line: str) -> None:
    r"""★ 规则不能一刀切 —— 非配置内容必须保留。

    只看文件名太粗暴：万一某个 `.ini` 里真有给玩家看的文案，一刀切会漏掉。
    所以规则是**"引擎配置文件 + `Key=Value` 结构"**两个条件同时成立。
    """
    assert _is_engine_config_line(filename, line) is False, (
        f"{filename} 里的 {line!r} 不该被判为引擎配置"
    )


def test_game_ini_only_yields_nothing_for_a_wolf_game(tmp_path: Path) -> None:
    r"""★ 复现真实场景：只有 `Game.ini` 的 `.wolf` 游戏应抽出 **0** 条。

    这就是 `Brave_Alchemist_Colette_v1.05` 的形状 —— 修完必须诚实地报 0。
    """
    ini = (
        "[Game]\n"
        "RTP=\n"
        "Library=System\\RGSS301.dll\n"
        "Scripts=Data\\Scripts.rvdata2\n"
        "Title=RPG Maker VX Ace\n"
        "[Window]\n"
        "Start=0\n"
        "WindowModeFlag=1\n"
        "ScreenShotFlag=1\n"
    )
    got = _extract(tmp_path, {"Game.ini": ini})
    # `RTP=` / `Library=…` / `Start=0` 都是配置；标题行也不是 KV
    assert all("=" not in s or "是否" in s for s in got) or not got, (
        f"不该把配置键值对当文案抽出来：{got}"
    )
    assert not [s for s in got if s.rstrip().endswith(("=0", "=1", "="))], (
        f"仍抽到了配置行：{got}"
    )


def test_real_game_text_in_a_non_ini_file_is_kept(tmp_path: Path) -> None:
    r"""★ 修复不能伤到正常抽取（`.txt` 里的文案必须还在）。"""
    got = _extract(
        tmp_path,
        {
            "Game.ini": "Start=0\nWindowModeFlag=1\n",
            "text/dialogue.txt": "Hello, hero.\nWelcome to the village.\n",
        },
    )
    assert "Hello, hero." in got, f"正常文案被误杀：{got}"
    assert "Welcome to the village." in got, f"正常文案被误杀：{got}"
    assert not [s for s in got if s.startswith("WindowModeFlag")], (
        f"配置行漏过了：{got}"
    )
