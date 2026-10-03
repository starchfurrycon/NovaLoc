r"""动态资源占用（让路闸门）与**不打扰正在玩的游戏**。

## 这两件事为什么必须一起测

它们共享同一套"机器被占用"的判定，但**后果完全不同**：

* **让路闸门**判错 ⇒ 最多是慢（该让路没让，或者该干活时干等）；
* **正在玩的游戏**判错 ⇒ 后果是**覆盖用户正在读的文件**
  （存档可能损坏），是不可逆的。所以后者的判据必须更硬。

详细设计见 `src/novaloc/translate/busy.py` 的模块 docstring。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.translate import busy  # noqa: E402


# ----------------------------------------------------------------------
# 一、闸门文件
# ----------------------------------------------------------------------
def test_pause_file_lifecycle(tmp_path: Path) -> None:
    """创建 / 检测 / 删除。"""
    assert not busy.is_paused(tmp_path)
    busy.set_paused(tmp_path, True, "测试")
    assert busy.is_paused(tmp_path)
    assert (tmp_path / busy.PAUSE_NAME).is_file()
    busy.set_paused(tmp_path, False)
    assert not busy.is_paused(tmp_path)


def test_pause_file_records_why(tmp_path: Path) -> None:
    """理由要写进文件 —— 否则用户看到"卡住了"却不知为什么。"""
    busy.set_paused(tmp_path, True, "GPU 80%")
    assert "GPU 80%" in (tmp_path / busy.PAUSE_NAME).read_text(encoding="utf-8")


def test_unpause_is_idempotent(tmp_path: Path) -> None:
    """删一个不存在的闸门不能抛异常（监视器会反复调）。"""
    busy.set_paused(tmp_path, False)
    busy.set_paused(tmp_path, False)


# ----------------------------------------------------------------------
# 二、自家进程判定（真实踩过的坑）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["llama-server", "llama-server.exe", "llama_server", "ollama", "novaloc", "python", "pythonw"],
)
def test_own_process_names_are_recognised(name: str) -> None:
    r"""★ `llama-server` 必须算"自家"。

    真实 bug：Ollama 0.35 把推理放在独立的 `llama-server.exe` 里，
    **进程名里既没有 "ollama" 也没有 "python"**。
    我最初的正则 `ollama|novaloc|python` 匹配不到它，于是实测出现

        gpu_percent(exclude_own=True)  -> 82.0   ← 没排掉 llama-server
        gpu_percent(exclude_own=False) -> 74.3

    "排除自家后反而更大"在逻辑上不可能 —— 这个用例就是钉住它。
    """
    assert busy._is_own_name(name) is True, f"{name} 应被认作自家进程"


@pytest.mark.parametrize("name", ["SomeGame", "BiliBili", "chrome", "steam", "Train45"])
def test_foreign_process_names_are_not_own(name: str) -> None:
    """外部程序不能被误判成自家（否则游戏在跑也不让路）。"""
    assert busy._is_own_name(name) is False


def test_empty_name_counts_as_own() -> None:
    """空进程名当"自家"处理 —— 取不到名字时**不**触发暂停。

    理由：`foreground_process()` 取不到名字时返回空串，
    若把空串当"外部程序"，GPU 一高就会永久暂停。
    """
    assert busy._is_own_name("") is True
    assert busy._is_own_name("   ") is True


# ----------------------------------------------------------------------
# 三、忙闲判定（用假信号，不依赖真机负载）
# ----------------------------------------------------------------------
def test_busy_when_cpu_over_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(busy, "cpu_percent", lambda: 90.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 5.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("devenv", 1))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is True
    assert any("CPU" in r for r in st.reasons)


def test_idle_when_everything_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(busy, "cpu_percent", lambda: 5.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 3.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("explorer", 1))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is False, st.describe()


def test_running_game_always_means_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 有游戏在跑 ⇒ 一定让路（哪怕 CPU/GPU 都很低）。

    这条不能依赖阈值：有些 galgame 几乎不吃 CPU/GPU，
    但它**正在读游戏文件**，此时回写就是灾难。
    """
    monkeypatch.setattr(busy, "cpu_percent", lambda: 2.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 1.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("explorer", 1))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [("SomeGame", 123, "x.exe")])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is True
    assert any("游戏" in r for r in st.reasons), st.reasons


def test_high_gpu_from_our_own_foreground_does_not_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    r"""★ GPU 高但前台是自家进程 ⇒ **不**暂停。

    这是防"自己把自己判忙 ⇒ 永久暂停"的关键一条：
    Ollama 推理时 GPU 必然高，若因此暂停就再也不会继续。
    """
    monkeypatch.setattr(busy, "cpu_percent", lambda: 10.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 95.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("llama-server", 999))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is False, st.describe()


def test_high_gpu_with_external_foreground_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPU 高**且**前台是外部程序 ⇒ 让路（用户在打游戏/跑渲染）。"""
    monkeypatch.setattr(busy, "cpu_percent", lambda: 20.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 88.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("SomeGame", 5))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is True
    assert any("GPU" in r for r in st.reasons)


def test_unknown_signals_do_not_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """信号取不到（返回 -1）时**不能**判忙 —— 否则计数器一失效就永久停摆。"""
    monkeypatch.setattr(busy, "cpu_percent", lambda: -1.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: -1.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("", 0))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    st = busy.evaluate(busy.BusyConfig())
    assert st.busy is False, st.describe()


def test_describe_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`describe()` 要能直接打给用户看（含数值）。"""
    monkeypatch.setattr(busy, "cpu_percent", lambda: 5.0)
    monkeypatch.setattr(busy, "gpu_percent", lambda **_kw: 7.0)
    monkeypatch.setattr(busy, "foreground_process", lambda: ("x", 1))
    monkeypatch.setattr(busy, "running_games", lambda *_a, **_k: [])
    txt = busy.evaluate(busy.BusyConfig()).describe()
    assert "空闲" in txt and "5" in txt


# ----------------------------------------------------------------------
# 四、正在运行的游戏
# ----------------------------------------------------------------------
def test_is_game_running_matches_path_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """exe 在游戏目录下 ⇒ 认为在跑。"""
    game = tmp_path / "MyGame"
    game.mkdir()
    exe = game / "game.exe"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(busy, "running_process_paths", lambda: {42: str(exe)})
    running, why = busy.is_game_running(game)
    assert running is True
    assert "game.exe" in why


def test_is_game_running_rejects_sibling_with_shared_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r"""★ 前缀相同的**兄弟目录**不能被误判成同一个游戏。

    `C:\Games\Foo2` 的字符串以 `C:\Games\Foo` 开头。
    如果实现里用 `str.startswith` 就会误判 —— 后果是
    "玩 Foo2 时把 Foo 判成在运行"（只是慢），
    但反过来"Foo 在跑却判成没跑"就会**覆盖正在读的文件**。
    所以这里要求按**路径分量**比较。
    """
    foo = tmp_path / "Foo"
    foo2 = tmp_path / "Foo2"
    foo.mkdir()
    foo2.mkdir()
    exe2 = foo2 / "g.exe"
    exe2.write_text("x", encoding="utf-8")
    monkeypatch.setattr(busy, "running_process_paths", lambda: {7: str(exe2)})
    assert busy.is_game_running(foo2)[0] is True
    assert busy.is_game_running(foo)[0] is False, "Foo2 在跑不能把 Foo 判成在跑"


def test_is_game_running_false_when_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    game = tmp_path / "G"
    game.mkdir()
    monkeypatch.setattr(busy, "running_process_paths", lambda: {})
    assert busy.is_game_running(game) == (False, "")


def test_running_games_groups_by_top_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`running_games(library)` 应报出**游戏目录名**。"""
    lib = tmp_path / "lib"
    g = lib / "Cool Game"
    g.mkdir(parents=True)
    exe = g / "run.exe"
    exe.write_text("x", encoding="utf-8")
    other = tmp_path / "outside.exe"
    other.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        busy, "running_process_paths", lambda: {1: str(exe), 2: str(other)}
    )
    got = busy.running_games(lib)
    names = [n for n, _p, _x in got]
    assert names == ["Cool Game"], got
