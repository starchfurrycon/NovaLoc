r"""写回时的**逐文件紧急刹车**（`write_back(should_stop=...)`）。

## 为什么需要它 —— 一个真实的危险窗口

用户要求"我随时玩对应文件夹的游戏时不会影响"。
最初的实现只在**进入写回之前**查一次"游戏在不在跑"。这不够：

写回是**逐个文件** `copy2` 的（`batch.write_back` 里就是一个 `for rel in rels`）。
一个上千文件的游戏要好几秒，上万文件的要几十秒。
用户完全可能**恰好在这段时间里**双击启动游戏 —— 那一刻游戏开始读的
正是被替换到一半的资源目录：

* 读到新旧混合文件 ⇒ **崩溃**；
* 退出时把内存里的旧数据写回存档 ⇒ 与刚替换的资源不匹配 ⇒ **存档损坏**，
  而且**不可逆**（备份只保证能退回原版，玩家那一刻的进度保不住）。

所以检查必须下沉到**文件粒度**：每个文件写之前重新确认一次。

## 本文件测什么

1. `should_stop` 返回非空 ⇒ **立刻**停止，不再写后面的文件；
2. 停止原因要**如实记进** `rep.aborted`（调用方据此告诉用户
   "游戏目录现在是部分新部分旧"，不能默默当成功）；
3. `should_stop` 返回空 ⇒ 行为与原来**完全一致**（不影响其它调用方）；
4. 不传 `should_stop` ⇒ 不检查（向后兼容）；
5. **已经写的文件不回滚** —— 这是有意的：回滚本身要再写一遍文件，
   在"游戏正在读"的时刻做这个反而更危险（docstring 里有说明）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.batch import write_back  # noqa: E402


def _make_pair(tmp_path: Path, names: list[str]) -> tuple[Path, Path]:
    """造 ``(source_dir, out_dir)``，同名的文件内容不同 ⇒ 都算"变了"。"""
    src = tmp_path / "game" / "data"
    out = tmp_path / "out" / "data"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    for n in names:
        (src / n).write_text(f"原始-{n}", encoding="utf-8")
        (out / n).write_text(f"译文-{n}", encoding="utf-8")
    return src, out


def test_stop_before_first_file_writes_nothing(tmp_path: Path) -> None:
    """一开始就该停 ⇒ 一个文件都不写。"""
    src, out = _make_pair(tmp_path, ["a.json", "b.json"])
    rep = write_back(
        src,
        out,
        backup_root=tmp_path / "bak",
        should_stop=lambda: "游戏在运行",
    )
    assert rep.aborted == "游戏在运行"
    assert rep.written == [], rep.written
    assert (src / "a.json").read_text(encoding="utf-8") == "原始-a.json"


def test_stop_after_one_file_keeps_that_file(tmp_path: Path) -> None:
    r"""★ 中间叫停：已写的不回滚，剩下的不写。

    这条钉住"部分新部分旧"这个**已知且有意**的状态 ——
    调用方必须如实报告，而不是假装成功。
    """
    src, out = _make_pair(tmp_path, ["a.json", "b.json", "c.json"])
    calls = {"n": 0}

    def stop() -> str:
        calls["n"] += 1
        # 第 1 次调用（第 1 个文件之前）放行，之后一律叫停
        return "" if calls["n"] == 1 else "游戏被启动（g.exe）"

    rep = write_back(src, out, backup_root=tmp_path / "bak", should_stop=stop)
    assert rep.aborted == "游戏被启动（g.exe）"
    assert len(rep.written) == 1, rep.written
    # 写了的那个已经是新内容
    assert (src / "a.json").read_text(encoding="utf-8").startswith("译文")
    # 剩下的还是原文
    assert (src / "b.json").read_text(encoding="utf-8").startswith("原始")
    assert (src / "c.json").read_text(encoding="utf-8").startswith("原始")


def test_no_stop_writes_everything(tmp_path: Path) -> None:
    """`should_stop` 一直返回空 ⇒ 全部写完，且 `aborted` 为空。"""
    src, out = _make_pair(tmp_path, ["a.json", "b.json", "c.json"])
    rep = write_back(
        src, out, backup_root=tmp_path / "bak", should_stop=lambda: ""
    )
    assert rep.aborted == ""
    assert len(rep.written) == 3
    for n in ("a.json", "b.json", "c.json"):
        assert (src / n).read_text(encoding="utf-8").startswith("译文")


def test_without_should_stop_behaves_as_before(tmp_path: Path) -> None:
    """不传 `should_stop` ⇒ 不检查（向后兼容，其它调用方不受影响）。"""
    src, out = _make_pair(tmp_path, ["a.json", "b.json"])
    rep = write_back(src, out, backup_root=tmp_path / "bak")
    assert rep.aborted == ""
    assert len(rep.written) == 2


def test_backup_is_complete_up_to_the_stop_point(tmp_path: Path) -> None:
    r"""★ 备份必须覆盖**所有已经写过**的文件。

    这是"部分新部分旧"之后唯一能恢复的依据。
    注意备份是在**每个文件写之前**做的（见实现），所以刹车点之前
    处理的文件都有备份。
    """
    src, out = _make_pair(tmp_path, ["a.json", "b.json", "c.json"])
    calls = {"n": 0}

    def stop() -> str:
        calls["n"] += 1
        return "" if calls["n"] == 1 else "停"

    rep = write_back(src, out, backup_root=tmp_path / "bak", should_stop=stop)
    bak = Path(rep.backup_dir)
    assert bak.is_dir()
    # a.json 被写过 ⇒ 一定有它的备份
    assert (bak / "a.json").is_file()
    assert (bak / "a.json").read_text(encoding="utf-8") == "原始-a.json"


def test_should_stop_not_called_when_nothing_changed(tmp_path: Path) -> None:
    """没有"变了的文件"时不该调用 `should_stop`（省掉无用的进程查询）。"""
    src = tmp_path / "game" / "data"
    out = tmp_path / "out" / "data"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    (src / "same.json").write_text("一样", encoding="utf-8")
    (out / "same.json").write_text("一样", encoding="utf-8")

    called = {"n": 0}

    def stop() -> str:
        called["n"] += 1
        return ""

    rep = write_back(src, out, backup_root=tmp_path / "bak", should_stop=stop)
    assert rep.written == []
    assert called["n"] == 0, "没有变更却调用了 should_stop"


@pytest.mark.parametrize("n_files", [1, 5, 20])
def test_stop_reason_is_propagated_verbatim(tmp_path: Path, n_files: int) -> None:
    """原因字符串原样传递（调用方要把它展示给用户）。"""
    names = [f"f{i}.json" for i in range(n_files)]
    src, out = _make_pair(tmp_path, names)
    rep = write_back(
        src,
        out,
        backup_root=tmp_path / "bak",
        should_stop=lambda: "游戏被启动（SomeGame.exe，pid 42）",
    )
    assert rep.aborted == "游戏被启动（SomeGame.exe，pid 42）"
