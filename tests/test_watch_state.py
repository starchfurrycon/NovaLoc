r"""守望模式状态（`novaloc.watch`）的回归测试。

## 为什么这批测试重要

守望模式 = "**后续新加游戏自动汉化**"。它原本整个内联在
`cli.auto()` 里，而那里下面就是 `while True: sleep()`
⇒ **一个测试都写不了**。于是两种**代价极高**的失效方式只能靠人肉发现：

| 失效 | 后果 |
| --- | --- |
| `seen` 没落盘 / 读回来是空的 | 重启一次，**整库重跑**（实测很久） |
| 状态文件坏掉被当成"空的" | 同上 |
| 路径大小写不归一 | 同一个游戏被反复当成新的 |

所以下面每一例都对准其中一种。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.watch import STATE_NAME, WatchState, normalize  # noqa: E402


def test_first_run_is_marked_fresh(tmp_path: Path) -> None:
    st = WatchState.load(tmp_path / STATE_NAME)
    assert st.fresh is True
    assert st.load_error == ""
    assert st.seen == set()


def test_bootstrap_prevents_whole_library_rerun(tmp_path: Path) -> None:
    """★★ 第一次跑必须把**现有游戏**全部记为已见。

    不这么做的话 `seen` 是空的 ⇒ 库里每个游戏都算"新出现"⇒
    整库重跑。这正是"重启后静默重跑整库"的一半病因。
    """
    games = [tmp_path / "A", tmp_path / "B", tmp_path / "C"]
    st = WatchState.load(tmp_path / STATE_NAME)
    st.bootstrap(games)
    assert st.new_games(games) == [], "现有游戏不该被当成新的"
    # 但**真的**新加一个要能认出来
    assert st.new_games([*games, tmp_path / "D"]) == [tmp_path / "D"]


def test_state_survives_restart(tmp_path: Path) -> None:
    """★★ 落盘再读回 ⇒ 重启不会重跑。"""
    p = tmp_path / STATE_NAME
    games = [tmp_path / "A", tmp_path / "B"]
    st = WatchState.load(p)
    st.bootstrap(games)
    st.save()

    again = WatchState.load(p)
    assert again.fresh is False
    assert again.load_error == ""
    assert again.new_games(games) == [], "重启后现有游戏仍不该算新的"


def test_new_game_detected_after_restart(tmp_path: Path) -> None:
    """★ 用户的核心需求：重启后**新加进库的游戏**要被处理。"""
    p = tmp_path / STATE_NAME
    st = WatchState.load(p)
    st.bootstrap([tmp_path / "Old1", tmp_path / "Old2"])
    st.save()

    again = WatchState.load(p)
    fresh = again.new_games([tmp_path / "Old1", tmp_path / "New", tmp_path / "Old2"])
    assert fresh == [tmp_path / "New"], f"只该认出新增的那个：{fresh}"


def test_corrupt_state_is_reported_not_silently_empty(tmp_path: Path) -> None:
    """★★ 状态文件坏掉**不能**静默当成"空的"。

    "读不出来"与"本来就空"看起来一模一样，但后果差了一整个库的重跑。
    所以必须留下 `load_error`，让调用方走 `bootstrap` 而不是从空开始。
    """
    p = tmp_path / STATE_NAME
    p.write_text("{ this is not json", encoding="utf-8")
    st = WatchState.load(p)
    assert st.load_error, "损坏必须被报出来"
    assert st.fresh is False, "文件存在 ⇒ 不算 first run"
    assert st.seen == set()


def test_corrupt_state_bootstrap_still_prevents_rerun(tmp_path: Path) -> None:
    """★ 坏文件的完整行为：报错 + 按现有游戏重建 ⇒ 不重跑。"""
    p = tmp_path / STATE_NAME
    p.write_text("[]", encoding="utf-8")  # 合法 JSON，但顶层不是对象
    st = WatchState.load(p)
    assert st.load_error
    games = [tmp_path / "A", tmp_path / "B"]
    st.bootstrap(games)
    assert st.new_games(games) == []


def test_missing_seen_key_is_reported(tmp_path: Path) -> None:
    p = tmp_path / STATE_NAME
    p.write_text('{"library": "x"}', encoding="utf-8")
    st = WatchState.load(p)
    assert st.load_error == "缺少 seen 列表"
    assert st.seen == set()


def test_case_difference_is_not_a_new_game(tmp_path: Path) -> None:
    """★★ 路径大小写不归一 ⇒ 同一个游戏被反复重跑（Windows 上必现）。"""
    p = tmp_path / STATE_NAME
    st = WatchState.load(p)
    st.remember([tmp_path / "MyGame"])
    assert not st.is_new(Path(str(tmp_path / "mygame")))
    assert not st.is_new(Path(str(tmp_path / "MYGAME")))


def test_normalize_only_lowercases(tmp_path: Path) -> None:
    """★ `normalize` **故意**不做 `resolve()`。

    理由是"游戏目录暂时不可达"（网络盘掉线、移动盘没插）时，
    守望模式**必须**还能认出"已经见过它" —— 否则一掉线就重跑整库。
    `resolve()` 在不可达时的行为依平台而异，不能用。
    """
    missing = tmp_path / "does-not-exist" / ".." / "Game"
    assert normalize(missing) == str(missing).lower()
    assert ".." in normalize(missing), "不该把路径规范化掉"


def test_unreachable_directory_still_matches(tmp_path: Path) -> None:
    """★ 上面那条规则的**行为**证明：目录不存在也不算新。"""
    st = WatchState.load(tmp_path / STATE_NAME)
    st.remember([tmp_path / "Gone"])
    # 目录并不存在（没创建过），但仍应认出
    assert not st.is_new(tmp_path / "Gone")
    assert (tmp_path / "Gone").exists() is False


def test_remember_is_idempotent(tmp_path: Path) -> None:
    st = WatchState.load(tmp_path / STATE_NAME)
    st.remember([tmp_path / "A", tmp_path / "A", tmp_path / "a"])
    assert len(st.seen) == 1


def test_new_games_keeps_input_order(tmp_path: Path) -> None:
    """★ 保持传入顺序 —— `find_games` 的顺序是有意义的（稳定输出）。"""
    st = WatchState.load(tmp_path / STATE_NAME)
    st.remember([tmp_path / "B"])
    got = st.new_games([tmp_path / "A", tmp_path / "B", tmp_path / "C"])
    assert got == [tmp_path / "A", tmp_path / "C"]


def test_empty_seen_list_needs_bootstrap(tmp_path: Path) -> None:
    """★ 合法的空 `seen`（不是损坏）也要能读出来 —— 但调用方需 bootstrap。

    这一例记录的是**边界**：`seen: []` 是合法状态文件，`load_error` 为空，
    所以 CLI 靠 `fresh`/`load_error` 判断"该不该 bootstrap"。
    文件存在且合法但 `seen` 为空 ⇒ 不该 bootstrap（用户可能故意清空
    来强制重扫）。⇒ 因此 CLI 里 bootstrap 的条件就是
    `fresh or load_error`，不含"seen 为空"。
    """
    p = tmp_path / STATE_NAME
    p.write_text('{"seen": []}', encoding="utf-8")
    st = WatchState.load(p)
    assert st.load_error == ""
    assert st.fresh is False
    assert st.seen == set()


def test_save_writes_library_and_timestamp(tmp_path: Path) -> None:
    p = tmp_path / STATE_NAME
    lib = tmp_path / "lib"
    st = WatchState.load(p, library=lib)
    st.remember([tmp_path / "A"])
    st.save()

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["library"] == str(lib)
    assert raw["updated"], "应当有时间戳，便于排查'状态是什么时候写的'"
    assert raw["seen"] == [str(tmp_path / "A").lower()]


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / STATE_NAME
    st = WatchState.load(p)
    st.remember([tmp_path / "A"])
    st.save()
    assert p.is_file()


def test_save_failure_does_not_raise(tmp_path: Path) -> None:
    """★ 状态写不出去不该让守望模式崩掉（只记日志）。"""
    # 用一个"父路径是文件"的路径来制造 OSError
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    st = WatchState.load(blocker / STATE_NAME)
    st.save()  # 不该抛
    assert not (blocker / STATE_NAME).exists()


def test_describe_covers_all_three_cases(tmp_path: Path) -> None:
    """★ 三种来源要能分辨 —— "看着像在工作"与"真在工作"必须可区分。"""
    fresh = WatchState.load(tmp_path / STATE_NAME)
    assert "新建" in fresh.describe()

    broken = tmp_path / "bad.json"
    broken.write_text("nope", encoding="utf-8")
    st_bad = WatchState.load(broken)
    assert "损坏" in st_bad.describe()

    p = tmp_path / "ok.json"
    st = WatchState.load(p)
    st.remember([tmp_path / "A"])
    st.save()
    st_ok = WatchState.load(p)
    assert "读回 1 个" in st_ok.describe()
