r"""★★ 守望模式**必须一直在等**，不能在"没有待处理游戏"时直接退出。

## 这个 bug（实测，守望服务从来没真正工作过）

`--watch-only` 的语义是"**不处理现有游戏**，只等新游戏"。
于是它走到 `if not todo:` 那个分支时，`todo` **本来就是空的**
（这正是它要达到的效果）。而原先那里无条件 `return`
⇒ 进程立刻退出 ⇒ **守望循环从来没被执行过**。

### 为什么很久没被发现

现象**极像正常工作**。`scripts/watch-new-games.ps1` 是一个
"崩了就重启"的循环：它看到子进程退出（code=0）就"5 秒后重启"。
于是日志里刷出一片

    第 1 轮：开始扫描（--watch-only，不重跑现有游戏）
    第 N 轮退出（code=0），5 秒后重启
    第 2 轮：开始扫描 …

看起来"服务一直在跑"，**实际每 5 秒空转一轮**，新游戏永远等不到处理。
退出码还是 0（"成功"），没有任何报错。

### 判据

**真实判据是"这个命令会不会阻塞"**，不是看它输出了什么。
所以这里用子进程实测：

* 给它一个**空库** + `--watch-only --interval 5`；
* 等 12 秒（> interval）；
* 若进程**还活着** ⇒ 循环在 sleep，正确；
* 若进程**已退出** ⇒ 又是那个 bug。

⚠️ 不能用"检查有没有打印守望模式那行字"来代替 ——
打印发生在退出**之前**，两种情况下都会打出来
（我第一版差点就这么写）。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _novaloc_exe() -> Path:
    exe = ROOT / ".venv" / "Scripts" / "novaloc.exe"
    if not exe.is_file():
        pytest.skip(f"找不到 {exe}")
    return exe


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONWARNINGS": "ignore",
        "PYTHONPATH": "src",
        # 隔离：别动生产数据根（否则会读/写真实的 watch-state.json）
        "NOVALOC_DATA_ROOT": str(tmp_path / "data"),
    })
    return env


def _spawn(args: list[str], tmp_path: Path, log: Path) -> subprocess.Popen:
    fh = log.open("wb")
    return subprocess.Popen(
        [str(_novaloc_exe()), *args],
        cwd=str(ROOT), env=_env(tmp_path),
        stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _kill(proc: subprocess.Popen) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
        capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_watch_only_blocks_on_empty_library(tmp_path: Path) -> None:
    """★★ 空库 + `--watch-only` ⇒ **必须还活着**（在等新游戏）。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "data").mkdir()
    log = tmp_path / "out.log"

    proc = _spawn(["auto", str(lib), "--watch-only", "--interval", "5"], tmp_path, log)
    try:
        time.sleep(12)
        alive = proc.poll() is None
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        assert alive, (
            "★ `--watch-only` 在空库上直接退出了 —— 守望循环没被执行。\n"
            f"  退出码={proc.returncode}\n"
            f"  输出：\n{text[-1500:]}"
        )
    finally:
        _kill(proc)


def test_watch_only_prints_it_is_watching(tmp_path: Path) -> None:
    """守望模式要**说清楚它在守望**（否则用户不知道它在干活）。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "data").mkdir()
    log = tmp_path / "out.log"

    proc = _spawn(["auto", str(lib), "--watch-only", "--interval", "5"], tmp_path, log)
    try:
        time.sleep(12)
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        assert "守望" in text, f"应当提到守望模式：\n{text[-1200:]}"
        assert "只等新游戏" in text, f"应当说明只等新游戏：\n{text[-1200:]}"
    finally:
        _kill(proc)


def test_no_watch_flag_still_returns_immediately(tmp_path: Path) -> None:
    """★ 反向保护：**不加** `--watch-only` 时必须立刻退出。

    否则脚本/CI 里一个普通 `auto` 调用会永久挂住。
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "data").mkdir()
    log = tmp_path / "out.log"

    proc = _spawn(["auto", str(lib)], tmp_path, log)
    try:
        proc.wait(timeout=90)
        assert proc.returncode == 0, f"应当正常退出，实际 {proc.returncode}"
    finally:
        if proc.poll() is None:
            _kill(proc)


@pytest.mark.parametrize("flag", ["--watch-only", "--watch"])
def test_both_watch_flags_enter_the_loop(tmp_path: Path, flag: str) -> None:
    """`--watch` 与 `--watch-only` 都要进循环（空库时两者都该等）。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "data").mkdir()
    log = tmp_path / "out.log"

    proc = _spawn(["auto", str(lib), flag, "--interval", "5"], tmp_path, log)
    try:
        time.sleep(12)
        assert proc.poll() is None, (
            f"`{flag}` 在空库上退出了（退出码={proc.returncode}）\n"
            f"{log.read_text(encoding='utf-8', errors='replace')[-1200:]}"
        )
    finally:
        _kill(proc)


def test_source_does_not_return_before_watch_block() -> None:
    """★ 源码级守卫：`if not todo:` 分支里不得有无条件的 `return`。

    行为测试只能覆盖我构造出来的那一种情况；这条守卫钉住**结构**，
    防止以后有人为了"早点退出"又把 `return` 加回来。
    """
    import ast
    import inspect

    import novaloc.cli as climod

    src = inspect.getsource(climod)
    # 用 AST 找 `if not todo:` 的 if 语句，检查它的 body 里有没有裸 return
    offenders: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # 匹配 `not todo` / `not <name>` 形态
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        operand = test.operand
        if not (isinstance(operand, ast.Name) and operand.id == "todo"):
            continue
        # 该分支的 body 里是否有"非条件"的 return（即直接挂在 body 上的）
        for stmt in node.body:
            if isinstance(stmt, ast.Return):
                offenders.append(node.lineno)
    assert not offenders, (
        f"`if not todo:` 分支里还有无条件的 return（行 {offenders}）—— "
        "这会让 `--watch-only` 永远进不了守望循环（见本测试的 docstring）"
    )
