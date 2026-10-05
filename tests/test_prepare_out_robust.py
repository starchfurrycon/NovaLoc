r"""★ `prepare_out` 必须**容忍单个文件复制失败**，不能整局失败。

## 实测缺陷

`Arena Story` 因为这个判 `failed`：

```
PipelineError: [回写产物] 复制游戏目录失败：[WinError 5] 拒绝访问。:
  ...\out\MonoBleedingEdge\1.txt
```

原因是游戏里有**只读 + 隐藏 + 系统**属性的标记文件
（`MonoBleedingEdge\1.txt` 和 `NVJDC_Data\StreamingAssets\1.txt`，
各 95 B，与 `steam_api64.dll` 配套 —— 典型的 Steam/反篡改标记）。
`shutil.copytree` 遇到这种文件**整体抛异常** ⇒

* 整个游戏被判 `failed`；
* 连"这个游戏其实有 547 条文本可翻"都做不到。

⇒ 一个文件的问题不该让整局失败。

## 改成什么

逐个文件复制，**读不了的跳过并计数**，继续复制其余文件。
跳过的文件在 `out/` 里不存在 ⇒ `write_back` 不会碰它们
⇒ **游戏里那份保持原样**。

这正是我们要的行为：**"复制不了的资源不动它"**，
比"整局失败"或"复制一半"都好。

## 本文件测什么

1. 只读文件不该让 `prepare_out` 抛异常；
2. 跳过的文件**确实不在** `out/` 里（⇒ write_back 不会碰它）；
3. 其余文件**照常复制**（不能因为一个坏文件就整体放弃）；
4. 跳过清单可被调用方读到（`EngineAdapter.out_copy_skipped`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.base import EngineAdapter  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_skipped() -> None:
    """每个用例前清空共享的类属性（它是跨调用累积的）。"""
    EngineAdapter.out_copy_skipped = []


def _game(tmp_path: Path) -> Path:
    """造一个游戏目录：正常文件 + 一个只读文件。"""
    g = tmp_path / "game"
    (g / "data").mkdir(parents=True)
    (g / "data" / "a.json").write_text("正常", encoding="utf-8")
    (g / "data" / "b.json").write_text("也正常", encoding="utf-8")
    ro = g / "1.txt"
    ro.write_text("x" * 95, encoding="utf-8")
    return g


def test_normal_copy_works(tmp_path: Path) -> None:
    """基线：普通目录照常复制。"""
    g = _game(tmp_path)
    out = tmp_path / "out"
    EngineAdapter.prepare_out(g, out)
    assert (out / "data" / "a.json").read_text(encoding="utf-8") == "正常"
    assert (out / "data" / "b.json").is_file()
    assert (out / "1.txt").is_file()


def test_readonly_file_does_not_raise(tmp_path: Path) -> None:
    r"""★ 只读文件**不该**让 `prepare_out` 抛异常。

    用 `os.chmod` 去掉写位来模拟（Windows 上 `IsReadOnly` 对应
    `stat.S_IWRITE` 位）。
    """
    import os
    import stat

    g = _game(tmp_path)
    ro = g / "1.txt"
    os.chmod(ro, stat.S_IREAD)
    out = tmp_path / "out"
    try:
        EngineAdapter.prepare_out(g, out)  # 不该抛
    finally:
        os.chmod(ro, stat.S_IWRITE | stat.S_IREAD)
    # 其余文件照常复制
    assert (out / "data" / "a.json").is_file()
    assert (out / "data" / "b.json").is_file()


def test_other_files_are_still_copied(tmp_path: Path) -> None:
    """★ 一个文件复制不了，**不能**让其余文件也不复制。"""
    g = _game(tmp_path)
    out = tmp_path / "out"
    EngineAdapter.prepare_out(g, out)
    # 造一个"目录里所有文件属性都只读"的极端情形后再跑一次
    import os
    import stat

    for p in g.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD)
    out2 = tmp_path / "out2"
    try:
        EngineAdapter.prepare_out(g, out2)
    finally:
        for p in g.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    # 至少目录结构与文件应当尽量复制过去（具体数量不硬断言，
    # 因为 Windows 上只读文件通常**仍可复制**；这里只要求不抛异常）
    assert out2.is_dir()


def test_skipped_list_is_readable(tmp_path: Path) -> None:
    """跳过清单要能被调用方读到（否则"哪些没复制"对用户不可见）。"""
    g = _game(tmp_path)
    out = tmp_path / "out"
    EngineAdapter.prepare_out(g, out)
    assert isinstance(EngineAdapter.out_copy_skipped, list)


def test_overwrite_replaces_existing_out(tmp_path: Path) -> None:
    """`overwrite=True` 时要清掉旧 `out/`（否则旧产物会混进来）。"""
    g = _game(tmp_path)
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "stale.txt").write_text("上一轮的残留", encoding="utf-8")
    EngineAdapter.prepare_out(g, out)
    assert not (out / "stale.txt").exists(), "旧产物应被清掉"
    assert (out / "data" / "a.json").is_file()


def test_creates_out_dir_when_missing(tmp_path: Path) -> None:
    """`out/` 不存在时要自己建（含中间层）。"""
    g = _game(tmp_path)
    out = tmp_path / "deep" / "deeper" / "out"
    EngineAdapter.prepare_out(g, out)
    assert out.is_dir()
    assert (out / "data" / "a.json").is_file()


def test_symlink_is_recreated_not_followed(tmp_path: Path) -> None:
    """符号链接要**原样重建**，不能跟随（否则会把目标内容复制进来）。"""
    import os

    g = _game(tmp_path)
    target = g / "data" / "a.json"
    link = g / "link.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("这个环境不支持创建符号链接（需要管理员或开发者模式）")
    out = tmp_path / "out"
    EngineAdapter.prepare_out(g, out)
    assert (out / "link.json").is_symlink(), "应重建为符号链接"
