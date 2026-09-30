"""并发写工作区 —— 我自己踩出来的事故。

## 事故经过

翻译阶段跑到第 **50 分钟**（21655 条）时，我在另一个终端并发跑了
`apply`（它也要写 `translations/entries.jsonl`）。结果：

1. 两边抢**同一个** `entries.jsonl.tmp`，Windows 上第二个 rename 抛
   `[WinError 32] 另一个程序正在使用此文件，进程无法访问` ——
   翻译在**最后一步保存时崩掉**，50 分钟白跑；
2. 更糟的是 `entries.jsonl` **中间**出现了 1 行截断（读到一半被替换），
   说明"先写 tmp 再原子改名"在并发时序下**并不安全**。

## 两个根因，两个修法（都要）

* **同一个 `.tmp` 文件名** → `_tmp_sibling()` 用
  `os.getpid()` + 线程 id 生成独有名字。这是底层保险：
  最坏情况只是"最后一次写入赢"，绝不会出现半个文件。
* **没有工作区互斥** → `Workspace.lock()` 独占工作区。
  这才是根因：两个进程各自读到旧的 `entries.jsonl`、
  各自写回自己的版本，**后写的把先写的成果全抹掉，而且不报错**。
  用户只会发现"莫名其妙少了一批译文"。

## 一个反直觉的坑：Windows 上 `os.kill(pid, 0)` 判不出死进程

POSIX 里 `os.kill(pid, 0)` 是判活的标准做法。但**实测 Windows 上
它对已退出的进程不抛任何异常**：

    已退出进程 pid=37100
    os.kill(37100, 0) → 没有抛异常

用它判活会**永远返回 True** → 陈旧锁永远解不开 →
用户被一个早已不存在的进程**永久挡住**，和"锁不释放"是同一个病。
所以 Windows 走 `OpenProcess` + `GetExitCodeProcess`。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.workspace import (  # noqa: E402
    Workspace,
    WorkspaceBusyError,
    _pid_alive,
    _tmp_sibling,
)
from novaloc.models import Project  # noqa: E402


def _ws(tmp_path: Path, name: str = "t") -> Workspace:
    return Workspace(Project(name=name, game_dir=str(tmp_path / "game")), tmp_path / "ws")


# ----------------------------------------------------------------------
# 一、`_pid_alive`：判错方向必须保守
# ----------------------------------------------------------------------

def test_dead_process_is_reported_dead() -> None:
    """**核心回归**：Windows 上 `os.kill(pid, 0)` 对死进程不抛异常。

    如果这里退化成"总是 True"，陈旧锁就永远解不开。
    """
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    time.sleep(0.3)
    assert _pid_alive(p.pid) is False, (
        "已退出的进程被判成活着 —— 陈旧锁会永远解不开（"
        "Windows 上 os.kill(pid, 0) 不抛异常，必须用 OpenProcess）"
    )


def test_current_process_is_reported_alive() -> None:
    assert _pid_alive(os.getpid()) is True


def test_nonexistent_pid_is_reported_dead() -> None:
    assert _pid_alive(999999999) is False


def test_nonpositive_pid_is_dead() -> None:
    for pid in (0, -1, -9999):
        assert _pid_alive(pid) is False


# ----------------------------------------------------------------------
# 二、独有 tmp 名：两个"进程"不会撞在同一个文件上
# ----------------------------------------------------------------------

def test_tmp_sibling_is_unique_per_process(tmp_path: Path) -> None:
    """同一个目标文件，不同进程必须得到**不同**的 tmp 名。

    这就是 `WinError 32` 的根治办法。
    """
    target = tmp_path / "entries.jsonl"
    mine = _tmp_sibling(target)
    assert mine != target
    assert mine.parent == target.parent, "tmp 必须同目录，否则 replace 会跨卷失败"
    assert mine.name.startswith(target.name), mine.name

    # 换个"进程"看：pid 不同 → 名字必须不同
    code = (
        "import sys; sys.path.insert(0, r'{src}');"
        "from pathlib import Path;"
        "from novaloc.core.workspace import _tmp_sibling;"
        "print(_tmp_sibling(Path(r'{target}')).name)"
    ).format(src=ROOT / "src", target=target)
    other = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert other != mine.name, f"不同进程拿到了同一个 tmp 名：{other}"


def test_tmp_sibling_keeps_the_real_suffix_visible(tmp_path: Path) -> None:
    """`entries.jsonl` 的 tmp 不该变成 `entries.tmp`（丢掉了 `.jsonl`）。

    早先用的 `with_suffix(suffix + ".tmp")` 在 `.jsonl` 这种
    "多段后缀"上行为容易踩坑；这里明确要求前缀完整。
    """
    assert _tmp_sibling(tmp_path / "entries.jsonl").name.startswith("entries.jsonl")


# ----------------------------------------------------------------------
# 三、工作区锁
# ----------------------------------------------------------------------

def test_lock_is_exclusive_within_a_process(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with ws.lock(what="test"):
        with pytest.raises(WorkspaceBusyError) as ei:
            with ws.lock(what="test2"):
                pass
        assert "正被另一个进程使用" in str(ei.value)


def test_lock_is_released_after_use(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with ws.lock():
        assert ws.lock_path.is_file()
    assert not ws.lock_path.exists(), "正常退出后锁文件必须删掉"
    # 能再次拿到
    with ws.lock():
        pass


def test_lock_is_released_even_if_body_raises(tmp_path: Path) -> None:
    """阶段失败/异常也必须释放锁，否则用户被自己永久挡住。"""
    ws = _ws(tmp_path)
    with pytest.raises(ValueError):
        with ws.lock():
            raise ValueError("阶段炸了")
    assert not ws.lock_path.exists()


def test_stale_lock_is_taken_over(tmp_path: Path) -> None:
    """持有者已经不存在 → 直接接管，不能让用户手动删文件。

    这是"锁文件"方案相对 `msvcrt.locking` 的关键优势：
    进程被强杀（Ctrl+C / taskkill / 关机）后，文件锁不会释放，
    用户下次运行会永远被挡住且查不出原因。
    """
    ws = _ws(tmp_path)
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.lock_path.write_text("999999999 早就没这个进程了\n", encoding="utf-8")
    with ws.lock(what="new"):
        assert ws.lock_path.read_text(encoding="utf-8").startswith(str(os.getpid()))


def test_garbage_lock_file_is_taken_over(tmp_path: Path) -> None:
    """锁文件内容损坏（手工编辑、磁盘截断）也必须能恢复。"""
    ws = _ws(tmp_path)
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.lock_path.write_text("乱七八糟，没有 pid\n", encoding="utf-8")
    with ws.lock():
        pass


def test_live_holder_is_not_stolen(tmp_path: Path) -> None:
    """**关键安全性质**：持有者活着时绝不能抢锁。

    否则"锁"就退化成了"建议"，并发写照样发生。
    """
    ws = _ws(tmp_path)
    ws.root.mkdir(parents=True, exist_ok=True)
    # 用**当前**进程当"活着的持有者"
    ws.lock_path.write_text(f"{os.getpid()} 我正在跑\n", encoding="utf-8")
    with pytest.raises(WorkspaceBusyError):
        with ws.lock():
            pass


def test_lock_across_real_processes(tmp_path: Path) -> None:
    """真·跨进程：子进程持锁时父进程必须被挡住，子进程退出后能拿到。"""
    ws = _ws(tmp_path)
    code = (
        "import sys, time; sys.path.insert(0, r'{src}');"
        "from pathlib import Path;"
        "from novaloc.core.workspace import Workspace;"
        "from novaloc.models import Project;"
        "ws = Workspace(Project(name='x', game_dir='.'), Path(r'{root}'));"
        "ctx = ws.lock(what='child'); ctx.__enter__();"
        "print('LOCKED', flush=True); time.sleep(4)"
    ).format(src=ROOT / "src", root=ws.root)
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        got = child.stdout.readline().strip()
        assert got == "LOCKED", f"子进程没能拿到锁：{got!r}"
        with pytest.raises(WorkspaceBusyError):
            with ws.lock(what="parent"):
                pass
    finally:
        child.wait(timeout=30)
    # 子进程已退出 → 锁是陈旧的 → 能接管
    with ws.lock(what="parent2"):
        pass


def test_does_not_delete_someone_elses_lock(tmp_path: Path) -> None:
    """释放时只删**自己写的**锁。

    接管竞态：A 判定 B 的锁陈旧、接管并写入自己的 pid；
    此时 B 恰好退出并执行 finally —— 若 B 无条件 unlink，
    就会把 A 的锁删掉，于是 C 又能进来，三者一起写。
    """
    ws = _ws(tmp_path)
    ws.root.mkdir(parents=True, exist_ok=True)
    with ws.lock():  # 正常释放，删自己的
        pass
    # 伪造"别人的锁"，然后走一遍释放逻辑
    ws.lock_path.write_text("999999999 someone-else\n", encoding="utf-8")
    with ws.lock(what="me"):
        # 接管后锁是我们的；在退出前把它换成"别人的"
        ws.lock_path.write_text("999999999 someone-else\n", encoding="utf-8")
    assert ws.lock_path.exists(), "不该删掉别人的锁文件"
