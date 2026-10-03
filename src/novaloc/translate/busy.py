r"""★ 动态资源占用：机器忙时让路、空闲时占满；以及**不打扰正在玩的游戏**。

用户的两条硬要求（原话）：

1. 「我随时玩对应文件夹的游戏时不会影响」——**翻译过程中不能覆盖正在玩的游戏文件**；
2. 「不影响我可能会进行的别的游戏/项目运行…在机器空闲时占用多，
   有别的需要资源的时候释放出占用的资源」。

## 为什么"让路"必须是**协作式**的，不能靠杀进程

翻译跑到一半被 `taskkill` 有两个真实后果：

* `entries.jsonl` 虽然用"独有 tmp + 原子改名"写，但**正在写的那一条**会丢；
* 更糟的是**回写阶段**被杀 —— `write_back` 会把 `out/` 覆盖到游戏目录，
  覆盖到一半中断就留下**半新半旧**的游戏（存档可能直接损坏）。

所以这里用的是**暂停闸门**：翻译循环每处理一批就来问一次
「现在该让路吗」，该让路就**睡**（不退出、不写文件），
醒了继续。代价最多是 `poll_interval_s` 的延迟，收益是任何时刻被杀都安全。

## 闸门怎么传（为什么用文件而不是进程间消息）

翻译进程和"忙闲监视器"是两个独立进程（监视器要跨重启存活，
见 `scripts/` 里的自启工具链）。文件是唯一不需要额外依赖、
且**关机重启后依然有效**的信道。

* `<数据根>/_pause` —— 存在 ⇒ 暂停。监视器创建/删除它；
  用户也可以**手动 `New-Item` 一个来立刻暂停**（这是有意保留的口子）。

## 判"机器忙"的三个信号

单看一个都会误判：

* **只看 CPU**：打游戏时 CPU 可能只有 20%，游戏靠 GPU，会被判成空闲；
* **只看 GPU**：本工具自己就在用 GPU（Ollama 推理），会自己把自己判成忙 ⇒ 死锁；
  所以 GPU 信号**必须排除自家进程**（见 `_own_pids`）；
* **只看前台窗口**：看视频/写代码时窗口也在前台，但那时**应该让路**，
  这是对的；而"挂着下载"不算忙，所以不能只看有没有前台窗口。

⇒ 用 `(GPU高 且 有外部占用者) 或 (CPU高) 或 (前台是游戏)` 的组合。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field

#: 本工具自己的进程名 —— 它们的 CPU/GPU 占用**不算**"用户在用机器"。
#: 少列一个的后果是：翻译自己把自己判成忙 ⇒ 永久暂停（比不暂停更糟）。
#:
#: ★ `llama-server` 是实测踩到的漏网之鱼：**真正吃 GPU 的就是它**。
#: Ollama 0.35 把推理放在独立的 `llama-server.exe` 里（路径
#: `...\ollama\bin\lib\ollama\llama-server.exe`），**进程名里既没有
#: "ollama" 也没有 "python"**。我最初的正则 `ollama|novaloc|python`
#: 匹配不到它，于是实测出现：
#:
#:     gpu_percent(exclude_own=True)  -> 82.0   ← 没排掉 llama-server
#:     gpu_percent(exclude_own=False) -> 74.3
#:
#: "排除自家后反而更大"在逻辑上不可能，正是这个漏网导致的假象。
_OWN_NAMES = {
    "novaloc",
    "novaloc.exe",
    "ollama",
    "ollama.exe",
    "ollama_llama_server",
    "ollama_llama_server.exe",
    "llama-server",
    "llama-server.exe",
    "llama_server",
    "python",
    "python.exe",
    "pythonw.exe",
}

#: 用正则一次匹配上面这些（`-match` 是子串匹配，所以给紧凑的片段即可）
_OWN_NAME_PATTERN = "llama-server|llama_server|ollama|novaloc|python"

#: 用户手动暂停用的文件名（相对数据根）
PAUSE_NAME = "_pause"


# ----------------------------------------------------------------------
# 信号采集
# ----------------------------------------------------------------------
def _run_ps(script: str, timeout: float = 20.0) -> str:
    """调 PowerShell 取一个数。失败一律返回空串，让调用方当"未知"处理。"""
    exe = "powershell.exe"
    try:
        p = subprocess.run(  # noqa: S603
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return p.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return p.stdout.decode("utf-8", errors="replace")


def cpu_percent() -> float:
    """整机 CPU 使用率（0~100）。取不到返回 -1。"""
    out = _run_ps(
        "(Get-Counter '\\Processor(_Total)\\% Processor Time' "
        "-SampleInterval 1 -MaxSamples 1).CounterSamples[0].CookedValue"
    )
    try:
        return float(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1.0


def gpu_percent(*, exclude_own: bool = True) -> float:
    """GPU 利用率（所有引擎求和，0~100+）。取不到返回 -1。

    ``exclude_own`` 会把本工具自己的进程占的那部分**扣掉** ——
    否则 Ollama 一推理，GPU 就"忙"，翻译自己把自己暂停。

    ⚠️ 代价说清楚：PowerShell 的 GPU 计数器**不给进程名**，
    只能拿到 `pid_XXXX_engtype_3D` 这样的实例名。
    所以"扣掉自家"只能靠 **pid 列表**（见 `_own_pids`）。
    """
    out = _run_ps(
        "(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' "
        "-SampleInterval 1 -MaxSamples 1).CounterSamples | "
        "Where-Object { $_.CookedValue -gt 0 } | "
        "ForEach-Object { $_.InstanceName + ' ' + $_.CookedValue }",
        timeout=25.0,
    )
    if not out.strip():
        return -1.0
    own = _own_pids() if exclude_own else set()
    total = 0.0
    for line in out.splitlines():
        parts = line.strip().rsplit(" ", 1)
        if len(parts) != 2:
            continue
        inst, val = parts
        try:
            v = float(val)
        except ValueError:
            continue
        # 实例名形如 pid_12345_luid_0x00000000_phys_0_eng_0_engtype_3D
        if own and inst.startswith("pid_"):
            seg = inst.split("_")
            if len(seg) > 1 and seg[1].isdigit() and int(seg[1]) in own:
                continue
        total += v
    return total


def _own_pids() -> set[int]:
    """本工具相关进程的 pid 集合（含 ollama / llama-server / python / novaloc）。

    ★ 必须包含 **llama-server** —— 真正吃 GPU 的是它，而它的进程名里
    没有 "ollama"（见 `_OWN_NAMES` 的说明）。
    """
    pids: set[int] = set()
    script = (
        "Get-Process | Where-Object { $_.ProcessName -match "
        f"'{_OWN_NAME_PATTERN}'"
        " } | ForEach-Object { $_.Id }"
    )
    try:
        p = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15.0,
            check=False,
        )
        for tok in p.stdout.decode("utf-8", errors="replace").split():
            if tok.strip().isdigit():
                pids.add(int(tok))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return pids


def foreground_process() -> tuple[str, int]:
    """前台窗口的 (进程名, pid)。取不到返回 ("", 0)。"""
    out = _run_ps(
        "Add-Type -Namespace W -Name U -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();"
        "[DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId("
        "IntPtr h, out uint pid);' -ErrorAction SilentlyContinue; "
        "$h = [W.U]::GetForegroundWindow(); $p = 0; "
        "[void][W.U]::GetWindowThreadProcessId($h, [ref]$p); "
        "$n = (Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName; "
        "Write-Output ($n + '|' + $p)"
    )
    for line in reversed(out.strip().splitlines()):
        if "|" in line:
            n, _, pid = line.rpartition("|")
            try:
                return n.strip(), int(pid)
            except ValueError:
                return n.strip(), 0
    return "", 0


# ----------------------------------------------------------------------
# 正在运行的游戏
# ----------------------------------------------------------------------
def running_process_paths() -> dict[int, str]:
    """``{pid: 可执行文件完整路径}``。取不到的进程不在里面。

    ## 为什么用可执行文件路径而不是"窗口标题里含游戏名"

    窗口标题是**游戏自己写的**，可能完全不含目录名（实测很多 galgame
    标题只有角色名）。而"这个进程的 exe 就在游戏目录里"是**结构性事实**，
    不依赖游戏怎么命名。
    """
    out = _run_ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath } | "
        "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.ExecutablePath }",
        timeout=30.0,
    )
    res: dict[int, str] = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        pid_txt, _, path = line.partition("|")
        if pid_txt.strip().isdigit() and path.strip():
            res[int(pid_txt)] = path.strip()
    return res


def running_games(library: pathlib.Path | str | None = None) -> list[tuple[str, int, str]]:
    """找出**正在运行的游戏**：``[(游戏名, pid, exe路径)]``。

    ``library`` 给了就只报该目录下的；没给就报所有"路径像游戏"的进程
    （判定较松，仅用于提示）。
    """
    lib = pathlib.Path(library).resolve() if library else None
    found: list[tuple[str, int, str]] = []
    for pid, path in running_process_paths().items():
        try:
            resolved = pathlib.Path(path).resolve()
        except (OSError, ValueError):
            continue
        if lib is not None:
            try:
                resolved.relative_to(lib)
            except ValueError:
                continue
            # 归一化大小写比较（Windows 路径不区分大小写）
            rel = resolved.relative_to(lib)
            found.append((rel.parts[0] if len(rel.parts) > 1 else resolved.name, pid, path))
    return found


def is_game_running(game_dir: pathlib.Path | str) -> tuple[bool, str]:
    """这个游戏目录下**有没有进程在跑**。返回 ``(是否在跑, 说明)``。"""
    gd = pathlib.Path(game_dir)
    try:
        gd_res = gd.resolve()
    except (OSError, ValueError):
        return False, ""
    for pid, path in running_process_paths().items():
        try:
            p = pathlib.Path(path).resolve()
        except (OSError, ValueError):
            continue
        # 路径前缀匹配要按"目录边界"，否则 `C:\Games\Foo2` 会被 `C:\Games\Foo` 命中
        if p == gd_res or gd_res in p.parents:
            return True, f"{p.name}(pid {pid})"
    return False, ""


# ----------------------------------------------------------------------
# 忙闲判定 + 闸门
# ----------------------------------------------------------------------
@dataclass
class BusyConfig:
    """阈值。默认值取得**偏保守**（宁可多让一点路，也不要卡住用户）。"""

    cpu_busy: float = 50.0
    """CPU 超过这个百分比就算"用户在用机器"。"""

    gpu_busy: float = 55.0
    """GPU 超过这个百分比**且**有外部占用者才算忙。"""

    poll_interval_s: float = 20.0
    """多久查一次。太短会自己耗资源（每次要起 PowerShell）。"""

    calm_down_s: float = 90.0
    """恢复空闲后**再多让路**这么久，避免用户刚退出游戏就又被打扰。"""


@dataclass
class BusyState:
    busy: bool = False
    reasons: list[str] = field(default_factory=list)
    cpu: float = -1.0
    gpu: float = -1.0
    foreground: str = ""
    games: list[tuple[str, int, str]] = field(default_factory=list)

    def describe(self) -> str:
        if not self.busy:
            return f"空闲（CPU {self.cpu:.0f}%  GPU {self.gpu:.0f}%）"
        return "忙：" + "；".join(self.reasons)


def evaluate(cfg: BusyConfig | None = None, *, library: str | None = None) -> BusyState:
    """综合三个信号给出当前忙闲。"""
    cfg = cfg or BusyConfig()
    st = BusyState()
    st.cpu = cpu_percent()
    st.gpu = gpu_percent()
    fg_name, _fg_pid = foreground_process()
    st.foreground = fg_name
    st.games = running_games(library)

    if st.games:
        st.reasons.append(f"有游戏在运行（{st.games[0][0]}）")
    if st.cpu >= 0 and st.cpu >= cfg.cpu_busy:
        st.reasons.append(f"CPU {st.cpu:.0f}% ≥ {cfg.cpu_busy:.0f}%")
    # GPU 高**且**前台不是自家进程 ⇒ 有外部程序在用显卡
    if (
        st.gpu >= 0
        and st.gpu >= cfg.gpu_busy
        and fg_name
        and not _is_own_name(fg_name)
    ):
        st.reasons.append(f"GPU {st.gpu:.0f}% ≥ {cfg.gpu_busy:.0f}% 且前台是 {fg_name}")
    st.busy = bool(st.reasons)
    return st


def _is_own_name(name: str) -> bool:
    """这个进程名是不是本工具自己的（大小写无关，子串匹配）。"""
    low = (name or "").strip().lower()
    if not low:
        return True  # 空名当"自家"处理 → 不因它触发暂停
    return any(part in low for part in ("llama-server", "llama_server", "ollama", "novaloc", "python"))


def pause_path(data_root: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(data_root) / PAUSE_NAME


def is_paused(data_root: pathlib.Path | str) -> bool:
    """闸门是否已放下（存在 `_pause` 文件）。"""
    return pause_path(data_root).exists()


def set_paused(data_root: pathlib.Path | str, paused: bool, why: str = "") -> None:
    """创建/删除闸门文件。``why`` 会写进文件里便于排查。

    ## ★ 为什么用 `utf-8-sig`（实测踩到的坑）

    一开始写的是纯 `utf-8`。结果用户/排查者用 **Windows PowerShell 的
    `Get-Content`** 去看这个文件时全是乱码：

        文件内容: 浜哄伐娴嬭瘯锛氭ā鎷熸満鍣ㄥ繖

    因为 PS 5.1 默认按 **ANSI(GBK)** 解码无 BOM 的文件。
    加 BOM 后 PS 才认得出这是 UTF-8。

    另一种做法是"只写 ASCII"，但那样就没法在文件里说明**为什么**暂停
    （理由里必然有中文），排查时反而更麻烦。所以选择加 BOM。
    """
    p = pause_path(data_root)
    if paused:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(
                f"{why}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                encoding="utf-8-sig",
            )
        except OSError:
            pass
    else:
        with contextlib.suppress(OSError):
            p.unlink()


# ----------------------------------------------------------------------
# 监视器（独立进程跑这个）
# ----------------------------------------------------------------------
def monitor(
    data_root: pathlib.Path | str,
    *,
    cfg: BusyConfig | None = None,
    library: str | None = None,
    log_path: pathlib.Path | str | None = None,
    once: bool = False,
) -> int:
    """轮询忙闲并维护闸门文件。**自己几乎不耗资源**（一次查询约 1~2 秒 CPU）。"""
    cfg = cfg or BusyConfig()
    root = pathlib.Path(data_root)
    logf = pathlib.Path(log_path) if log_path else None

    def say(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if logf:
            with contextlib.suppress(OSError):
                logf.parent.mkdir(parents=True, exist_ok=True)
                with logf.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

    was_busy = False
    free_since = 0.0
    while True:
        st = evaluate(cfg, library=library)
        now = time.time()
        # 空闲要**连续空闲**超过 calm_down_s 才真正解除
        if st.busy:
            free_since = 0.0
            if not was_busy:
                say(f"⇒ 暂停让路 —— {st.describe()}")
            set_paused(root, True, st.describe())
            was_busy = True
        else:
            if free_since == 0.0:
                free_since = now
            left = cfg.calm_down_s - (now - free_since)
            if left > 0:
                set_paused(root, True, f"用户刚忙完，再让路 {left:.0f}s")
            else:
                if was_busy:
                    say(f"⇒ 恢复翻译 —— {st.describe()}")
                set_paused(root, False)
                was_busy = False
        if once:
            return 0 if st.busy else 1
        time.sleep(cfg.poll_interval_s)


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="busy_watch", description="忙闲监视器")
    ap.add_argument("--data-root", default=os.environ.get("NOVALOC_DATA_ROOT", r"D:\NovaLoc"))
    ap.add_argument("--library", default=None)
    ap.add_argument("--interval", type=float, default=BusyConfig.poll_interval_s)
    ap.add_argument("--calm-down", type=float, default=BusyConfig.calm_down_s)
    ap.add_argument("--log", default=None)
    ap.add_argument("--once", action="store_true", help="只查一次并打印（调试用）")
    ns = ap.parse_args(argv)
    cfg = BusyConfig(poll_interval_s=ns.interval, calm_down_s=ns.calm_down)
    if ns.once:
        st = evaluate(cfg, library=ns.library)
        print(st.describe())
        return 0
    return monitor(ns.data_root, cfg=cfg, library=ns.library, log_path=ns.log)


if __name__ == "__main__":
    # 允许 `python -m novaloc.translate.busy`
    sys.exit(_main(sys.argv[1:]))


# `ctypes` 只在极少数需要直接查窗口样式时用；保留导入以免下游 `from ... import`
# 失败。真实前台窗口查询走 PowerShell（见 `foreground_process`）。
_ = ctypes
