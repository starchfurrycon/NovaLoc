r"""`Workspace.find_for_game` 的回归测试 —— "续跑"能不能真的续上。

## 要钉住的缺陷（实测）

`novaloc auto` 原来对每个游戏**无条件** `Workspace.create(...)`，
每次都生成新 id。实测后果：同一个游戏目录下堆了 **4 个**工作区，
而新工作区的 `translations/entries.jsonl` 是**空的**
⇒ `stage_translate(only_pending=True)` 没有东西可跳过
⇒ **整轮从第一条重新翻一遍**。

对"自动汉化 174 个游戏"这种要跑好几天的任务，这是**致命**的：
任何中断都意味着从零开始。

## 判据

1. 同一个游戏目录第二次调用必须**返回已有的**那个（不是新建）；
2. 多个同时存在时，挑**已翻译条目最多**的（真实工作量的度量，
   比 mtime 靠谱 —— mtime 会被"只跑了一次 detect"这种空操作刷新）；
3. 路径带 ``..`` 时仍要能匹配（用 `resolve()` 规范化）；
4. 没建过就返回 `None`（调用方据此新建）；
5. **别的游戏**的工作区绝不能被误当成这个游戏的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.core.workspace import Workspace  # noqa: E402


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据根指到临时目录 —— 绝不能碰真实的 `D:\\NovaLoc`。

    ⚠️ 这一点很重要：真数据根里有用户 174 个游戏的工作区，
    测试若写进去会污染真实状态（而且不可逆）。
    """
    root = tmp_path / "data"
    (root / "workspaces").mkdir(parents=True)

    import novaloc.core.paths as paths_mod

    monkeypatch.setattr(paths_mod, "data_root", lambda: root, raising=False)
    monkeypatch.setattr(
        paths_mod, "workspaces_dir", lambda: root / "workspaces", raising=False
    )
    return root


def _make_game(tmp_path: Path, name: str = "MyGame") -> Path:
    g = tmp_path / "lib" / name
    (g / "G_Data").mkdir(parents=True)
    (g / "G_Data" / "resources.assets").write_bytes(b"\x00" * 16)
    return g


def _create(name: str, game: Path, n_entries: int = 0) -> Workspace:
    ws = Workspace.create(name, game)
    if n_entries:
        ent_dir = ws.root / "translations"
        ent_dir.mkdir(parents=True, exist_ok=True)
        with (ent_dir / "entries.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(n_entries):
                fh.write(
                    json.dumps(
                        {"uid": f"u{i}", "source": "a", "target": "b",
                         "status": "translated"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return ws


def test_returns_none_when_never_created(tmp_path: Path, data_root: Path) -> None:
    """没建过 ⇒ None（调用方据此走新建分支）。"""
    game = _make_game(tmp_path)
    assert Workspace.find_for_game(game) is None


def test_reuses_the_existing_workspace(tmp_path: Path, data_root: Path) -> None:
    """★ 核心：同一个游戏目录第二次必须返回**同一个**工作区。"""
    game = _make_game(tmp_path)
    first = _create("MyGame", game, n_entries=3)

    found = Workspace.find_for_game(game)
    assert found is not None, "已有工作区却找不到 ⇒ 会新建空工作区并重翻一遍"
    assert found.project.id == first.project.id


def test_picks_the_one_with_most_work(tmp_path: Path, data_root: Path) -> None:
    """★★ 多个同时存在时挑**已翻译条目最多**的。

    这是"续跑"能省多少的直接决定项 —— 挑错了就等于白丢之前的工作量。
    """
    game = _make_game(tmp_path)
    _create("MyGame", game, n_entries=5)
    rich = _create("MyGame", game, n_entries=99)
    _create("MyGame", game, n_entries=1)

    found = Workspace.find_for_game(game)
    assert found is not None
    assert found.project.id == rich.project.id, (
        f"挑了工作量最少的那个：{found.project.id} != {rich.project.id}"
    )


def test_path_with_dotdot_still_matches(tmp_path: Path, data_root: Path) -> None:
    """★ 路径带 ``..`` 也要匹配上（否则库扫描与用户输入会各建一个）。"""
    game = _make_game(tmp_path)
    ws = _create("MyGame", game, n_entries=2)

    weird = game / "G_Data" / ".."
    assert ".." in str(weird)
    found = Workspace.find_for_game(weird)
    assert found is not None, "带 .. 的等价路径没匹配上 ⇒ 会重复建工作区"
    assert found.project.id == ws.project.id


def test_other_games_workspace_is_not_matched(
    tmp_path: Path, data_root: Path
) -> None:
    """★ 别的游戏的工作区**绝不能**被当成这个游戏的。"""
    a = _make_game(tmp_path, "GameA")
    b = _make_game(tmp_path, "GameB")
    ws_a = _create("GameA", a, n_entries=10)

    found = Workspace.find_for_game(b)
    assert found is None, f"把 GameA 的工作区给了 GameB：{found.project.id}"
    assert Workspace.find_for_game(a).project.id == ws_a.project.id  # type: ignore[union-attr]


def test_game_dir_moved_is_not_matched(tmp_path: Path, data_root: Path) -> None:
    """游戏目录被移走/改名 ⇒ 新位置**不该**匹配到旧工作区（否则会翻错对象）。

    ⚠️ 这是**故意保守**：路径不同就是不同项目。
    代价是"游戏改名后要重翻一遍"，但比"把 A 的译文写进 B"安全得多。

    ⚠️ 旧路径**仍然会**匹配到那个工作区 —— 这是**正确**的
    （`Path.resolve()` 对不存在的路径也能算，这正是我们要的：
    续跑时目录可能暂时不可达，但仍应认得出是这个项目）。
    我第一版测试断言"旧路径也该是 None"，实测红了 —— 是我写错了。
    """
    game = _make_game(tmp_path, "OldName")
    ws = _create("OldName", game, n_entries=4)

    moved = tmp_path / "lib" / "NewName"
    game.rename(moved)

    assert Workspace.find_for_game(moved) is None, (
        "新路径不该匹配旧工作区 —— 那会把旧译文写进新游戏"
    )
    # 旧路径仍能认出来（resolve 不需要路径存在）
    still = Workspace.find_for_game(game)
    assert still is not None
    assert still.project.id == ws.project.id


def test_survives_corrupt_project_json(tmp_path: Path, data_root: Path) -> None:
    """★ 一个坏掉的 `project.json` 不能让整个查找崩掉。

    实测教训（#41）：单个坏输入不该中断整条批量流程。
    """
    game = _make_game(tmp_path)
    good = _create("MyGame", game, n_entries=7)

    bad_dir = data_root / "workspaces" / "corrupt00000"
    bad_dir.mkdir(parents=True)
    (bad_dir / "project.json").write_text("{ 这不是合法 JSON", encoding="utf-8")

    found = Workspace.find_for_game(game)
    assert found is not None, "坏工作区把查找整个搞崩了"
    assert found.project.id == good.project.id


def test_no_workspaces_dir_returns_none(tmp_path: Path, data_root: Path) -> None:
    """工作区目录都还不存在 ⇒ 返回 None，不抛异常。"""
    import shutil

    shutil.rmtree(data_root / "workspaces")
    game = _make_game(tmp_path)
    assert Workspace.find_for_game(game) is None
