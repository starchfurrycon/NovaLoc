r"""守望模式的**端到端**验收（CLI 接线 + 隔离数据根）。

## 为什么需要这一层（`test_watch_state.py` 不够）

`test_watch_state.py` 的 17 个用例测的是 `WatchState` **这个类**。
它测不到两件同样会坏的事：

1. **CLI 到底有没有接上** —— 选项写错名字、`bootstrap` 没被调用、
   分支进不去，类本身仍然 100% 正确；
2. **真实数据根有没有被写脏** —— 测试必须用 `NOVALOC_DATA_ROOT` 隔离，
   否则会污染 `D:\NovaLoc`。

历史上这个项目吃过"单测证明函数对了 ≠ 链路通了"的亏（见 CHANGELOG
v1.6.0 那条"只修了一半"）。所以这里补一层**从 CLI onward** 的验收。

## 判据（事先写死，共 6 条）

1. `auto --help` 里必须有 `--watch-only`（接线断了必须能被发现）；
2. 首次运行把库里**现有**游戏全部记为已见；
3. 再次运行是"读回"而不是"重建"；
4. 库不变 ⇒ 认不出新游戏；
5. **新加一个目录 ⇒ 必须认出它**（用户需求的核心）；
6. 全程**不触碰**真实数据根。

## 怎么造一个"能被认出来"的游戏

用 `find_games` 的真判据：一个 `<名字>.rpgproject` 加
`data/` 目录。这样测的是**真实发现逻辑**，不是 mock 出来的列表。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
REAL_STATE = Path(r"D:\NovaLoc\watch-state.json")

pytestmark = pytest.mark.skipif(
    not PY.is_file(), reason="需要项目 venv 里的 python 才能起子进程"
)

#: 在子进程里执行的探针 —— 与 `cli.auto` 用**同一套组件**。
_PROBE = r'''
import json, sys
from pathlib import Path
from novaloc.batch import find_games
from novaloc.watch import STATE_NAME, WatchState

lib = Path(sys.argv[1]); data = Path(sys.argv[2]); phase = sys.argv[3]
games = find_games(lib, max_depth=2)
st = WatchState.load(data / STATE_NAME, library=lib)
if phase == "first":
    # 与 cli.auto 完全一致的条件
    if st.fresh or st.load_error:
        st.bootstrap(games)
    st.save()
    print(json.dumps({"fresh": st.fresh, "err": st.load_error,
                      "seen": len(st.seen), "games": len(games)}))
elif phase == "second":
    st.save()
    print(json.dumps({"fresh": st.fresh, "err": st.load_error,
                      "seen": len(st.seen), "desc": st.describe()}))
else:
    print(json.dumps({"new": [p.name for p in st.new_games(games)]}))
'''


def _make_game(parent: Path, tag: str) -> Path:
    """造一个 `find_games` 真能认出来的最小游戏目录。"""
    g = parent / f"TestGame_{tag}"
    g.mkdir(parents=True, exist_ok=True)
    (g / f"{tag}.rpgproject").write_text("{}", encoding="utf-8")
    d = g / "data"
    d.mkdir(exist_ok=True)
    (d / "Map001.json").write_text(
        json.dumps(
            {"events": [{"pages": [{"list": [
                {"code": 401, "parameters": ["Hello, a test line."]}
            ]}]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return g


class _Env:
    """隔离的库 + 数据根，并保证真实数据根不被触碰。"""

    def __init__(self, tmp_path: Path) -> None:
        self.lib = tmp_path / "lib"
        self.data = tmp_path / "data"
        self.lib.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.probe = self.data / "_probe.py"
        self.probe.write_text(_PROBE, encoding="utf-8")

    def run(self, phase: str) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        env["NOVALOC_DATA_ROOT"] = str(self.data)
        out = subprocess.run(
            [str(PY), str(self.probe), str(self.lib), str(self.data), phase],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=str(ROOT), timeout=300,
        )
        lines = [
            ln for ln in (out.stdout or "").splitlines() if ln.startswith("{")
        ]
        assert lines, (
            f"phase={phase} 没有输出 JSON\n"
            f"stdout={out.stdout[:800]}\nstderr={out.stderr[:800]}"
        )
        return json.loads(lines[-1])


def test_help_exposes_watch_only() -> None:
    """★ 1) CLI 接线：断线必须能被发现。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "novaloc.exe"), "auto", "--help"],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(ROOT), timeout=300,
    )
    assert "--watch-only" in (out.stdout or ""), (
        f"`auto --help` 里没有 --watch-only ⇒ 接线断了\n{out.stdout[:600]}"
    )
    assert "--watch" in (out.stdout or "")


def test_first_run_remembers_existing_games(tmp_path: Path) -> None:
    """★ 2) 首次运行必须把现有游戏全记为已见（否则重启重跑整库）。"""
    e = _Env(tmp_path)
    _make_game(e.lib, "One")
    _make_game(e.lib, "Two")
    r = e.run("first")
    assert r["fresh"] is True
    assert r["games"] >= 2, f"应当发现至少 2 个游戏：{r}"
    assert r["seen"] == r["games"], (
        f"现有游戏必须全部记入 seen（否则会整库重跑）：{r}"
    )


def test_second_run_reads_back_not_rebuilds(tmp_path: Path) -> None:
    """★ 3) 重启是"读回"而不是"重建"。"""
    e = _Env(tmp_path)
    _make_game(e.lib, "One")
    e.run("first")
    r = e.run("second")
    assert r["fresh"] is False, f"不该是首次运行：{r}"
    assert r["err"] == "", f"不该有读取错误：{r}"
    assert r["seen"] >= 1
    assert "读回" in r["desc"], f"描述应当表明是读回：{r}"


def test_unchanged_library_yields_no_new_games(tmp_path: Path) -> None:
    """★ 4) 库没变 ⇒ 不该认出任何"新游戏"（否则会反复重跑）。"""
    e = _Env(tmp_path)
    _make_game(e.lib, "One")
    e.run("first")
    r = e.run("new")
    assert r["new"] == [], f"不该有新游戏：{r}"


def test_newly_added_game_is_detected(tmp_path: Path) -> None:
    """★★ 5) **用户需求的核心**：新加进库的游戏必须被认出来。

    这一条如果坏了，"自动对后续新加游戏汉化"就完全不成立，
    而它坏掉时**没有任何报错** —— 只是安静地什么都不做。
    """
    e = _Env(tmp_path)
    _make_game(e.lib, "One")
    e.run("first")
    assert e.run("new")["new"] == [], "前提：加之前没有新游戏"

    _make_game(e.lib, "Three")
    r = e.run("new")
    assert any("Three" in n for n in r["new"]), (
        f"新加的游戏没被认出来：{r}"
    )


def test_real_data_root_is_never_touched(tmp_path: Path) -> None:
    """★ 6) 隔离：真实 `D:\\NovaLoc\\watch-state.json` 不许被创建。

    这条防的是"测试把用户的真实守望状态覆盖掉" —— 那会让用户
    要么整库重跑，要么丢失"已经看过哪些游戏"的记录。
    """
    existed_before = REAL_STATE.exists()
    e = _Env(tmp_path)
    _make_game(e.lib, "One")
    e.run("first")
    r = e.run("new")
    assert r["new"] == []
    assert (e.data / "watch-state.json").is_file(), "临时状态应当写在隔离目录里"
    assert REAL_STATE.exists() is existed_before, (
        f"真实数据根被改动了：{REAL_STATE}"
    )


def test_probe_uses_the_same_bootstrap_condition_as_cli() -> None:
    """★ 防漂移：探针里的 `bootstrap` 条件必须与 `cli.auto` 一致。

    如果 CLI 改成别的条件而探针没跟着改，上面的端到端用例就会
    "验的是另一个逻辑" —— 那就是假绿。这里直接把两边对齐检查。
    """
    cli_src = (ROOT / "src" / "novaloc" / "cli.py").read_text(encoding="utf-8")
    assert "state.fresh or state.load_error" in cli_src, (
        "cli.auto 里的 bootstrap 条件变了 —— 探针与本文档都要同步"
    )
    assert "state.bootstrap(games)" in cli_src, (
        "cli.auto 必须在首次/损坏时 bootstrap"
    )
    assert "if st.fresh or st.load_error:" in _PROBE, (
        "探针的条件必须与 CLI 一致"
    )


def test_python_executable_exists() -> None:
    """★ 基础前提：venv 的 python 必须在（否则上面全是 skip）。"""
    assert PY.is_file(), f"找不到 {PY}"
    assert sys.version_info >= (3, 11)
