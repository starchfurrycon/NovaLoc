r"""`preserve_pristine_backup` 的回归测试 —— "永远留一份原版"。

## 要钉住的隐患

`auto --watch` 正在对一个游戏写回（备份目录已建），此时若又跑一次 `auto`，
它会判定"还没有备份"而**再建一个**目录、再写一遍。

结果：**没有一份备份是"最初的原版"** —— 每份备份的都是"上一次被改过的
版本"。真出事想回滚到出厂状态时，**回不去**。

## 判据

1. 第一次调用把**未标记**的备份目录改名成 `<名字>-orig-<时间戳>`；
2. 被标记过的目录**不再被标记第二次**（幂等）；
3. 别的游戏的备份**不许**被碰（前缀匹配必须严格）；
4. 目录不存在时不抛异常（返回 None）；
5. 真的能"保住原版"：标记后新建的备份目录是**另一份**，
   标记的那份内容**没变**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.batch import preserve_pristine_backup  # noqa: E402


def _mk(backup_root: Path, name: str, content: bytes = b"ORIGINAL") -> Path:
    d = backup_root / name
    d.mkdir(parents=True)
    (d / "game.exe").write_bytes(content)
    return d


def test_marks_the_earliest_backup(tmp_path: Path) -> None:
    _mk(tmp_path, "MyGame-20260101-120000", b"V0")
    marked = preserve_pristine_backup(tmp_path, "MyGame")
    assert marked is not None, "必须标记出一份原版"
    assert "-orig-" in marked.name
    assert (marked / "game.exe").read_bytes() == b"V0"


def test_is_idempotent(tmp_path: Path) -> None:
    """★ 已经标记过的**不许**再标记（否则每跑一次就多一层后缀）。"""
    _mk(tmp_path, "MyGame-20260101-120000")
    first = preserve_pristine_backup(tmp_path, "MyGame")
    assert first is not None
    second = preserve_pristine_backup(tmp_path, "MyGame")
    assert second is None, f"重复标记了：{second}"
    assert first.is_dir()


def test_does_not_touch_other_games(tmp_path: Path) -> None:
    """★ 前缀匹配必须严格 —— ``MyGame`` **不许**吃掉 ``MyGame2`` 的备份。"""
    other = _mk(tmp_path, "MyGame2-20260101-120000", b"OTHER")
    marked = preserve_pristine_backup(tmp_path, "MyGame")
    assert marked is None, "库里没有 MyGame 的备份，不该标记任何东西"
    assert other.name == "MyGame2-20260101-120000"
    assert (other / "game.exe").read_bytes() == b"OTHER"


def test_prefix_game_name_is_matched(tmp_path: Path) -> None:
    """标准形态 ``<游戏名>-<时间戳>`` 必须能被识别并标记。"""
    _mk(tmp_path, "MyGame-20260101-120000")
    marked = preserve_pristine_backup(tmp_path, "MyGame")
    assert marked is not None
    assert marked.name == "MyGame-orig-20260101-120000"


def test_two_unmarked_backups_are_left_alone(tmp_path: Path) -> None:
    """★★ **故意不标记**：两个未标记备份 ⇒ 都**不是**原版。

    第二个备份里的内容是第一遍**改过**的版本，不是原版。
    此时宁可没有 orig，也不能把改过的版本谎称成原版 ——
    一个错误的"原版备份"比没有备份**更危险**（用户会以为能回滚到出厂）。
    """
    _mk(tmp_path, "MyGame-20260101-120000", b"V0")
    _mk(tmp_path, "MyGame-20260102-090000", b"V1")
    assert preserve_pristine_backup(tmp_path, "MyGame") is None
    # 两个目录都保持原样
    names = sorted(d.name for d in tmp_path.iterdir())
    assert names == ["MyGame-20260101-120000", "MyGame-20260102-090000"]


def test_non_timestamp_suffix_is_ignored(tmp_path: Path) -> None:
    """后缀不像时间戳的目录（用户自己的文件夹）不许被当成备份动掉。"""
    _mk(tmp_path, "MyGame-backup-please-keep")
    assert preserve_pristine_backup(tmp_path, "MyGame") is None
    assert (tmp_path / "MyGame-backup-please-keep").is_dir()


def test_missing_dir_is_not_an_error(tmp_path: Path) -> None:
    """备份目录还不存在（第一次跑）⇒ 返回 None，不抛异常。"""
    assert preserve_pristine_backup(tmp_path / "nope", "MyGame") is None


def test_pristine_content_survives_a_second_write_back(tmp_path: Path) -> None:
    """★★ 端到端意图：标记之后，再建一个备份目录**不会**动到原版。

    这正是这个函数存在的理由：保证"出厂状态"永远可回滚。
    """
    _mk(tmp_path, "MyGame-20260101-120000", b"V0")
    orig = preserve_pristine_backup(tmp_path, "MyGame")
    assert orig is not None

    # 第二次写回会产生一个新目录（模拟 `_unique_backup_dir`）
    _mk(tmp_path, "MyGame-20260102-130000", b"V1")

    # 原版**没被动**
    assert (orig / "game.exe").read_bytes() == b"V0"
    # 而且现在再调也不会把 V1 那份标记成 orig
    assert preserve_pristine_backup(tmp_path, "MyGame") is None


@pytest.mark.parametrize("name", ["Game", "游戏", "A B"])
def test_handles_cjk_and_spaces_in_game_name(tmp_path: Path, name: str) -> None:
    """游戏名常含中文和空格（实测库里有 ``Jerez's Arena`` 这种）。"""
    _mk(tmp_path, f"{name}-20260101-120000")
    marked = preserve_pristine_backup(tmp_path, name)
    assert marked is not None
    assert marked.name.startswith(name)
