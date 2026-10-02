"""游戏库批量汉化：找游戏、判断已是中文、**就地写回**的备份与哈希比对。

## 为什么这些测试值得写

``write_back`` 是**不可逆的破坏性操作** —— 它会覆盖用户原始的游戏收藏。
所以这里重点测三件事，而不是测"能不能跑通"：

1. **只回写变了的文件**。`apply` 阶段的 `out/` 是整份游戏副本（含 .exe/.dll），
   整目录覆盖回去会重写几百个二进制文件。实测里这就是最危险的一点。
2. **绝不按 mtime 判断"变了"**。`unpack` 拷贝会刷新 mtime，按时间比会让
   **每个**文件都算"变了" —— 于是变成了整目录覆盖。所以专门写一条测试
   模拟"内容相同但 mtime 不同"。
3. **写回前必须留下备份**。万一译错，用户还能还原。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.batch import (  # noqa: E402
    changed_files,
    detect_already_chinese,
    find_games,
    write_back,
)

# ---------------------------------------------------------------------------
# find_games
# ---------------------------------------------------------------------------


def _mk_game(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "Game.exe").write_bytes(b"MZ")


def test_find_games_one_level(tmp_path: Path) -> None:
    _mk_game(tmp_path / "Alpha")
    _mk_game(tmp_path / "Beta")
    got = {p.name for p in find_games(tmp_path)}
    assert got == {"Alpha", "Beta"}


def test_find_games_skips_non_game_dirs(tmp_path: Path) -> None:
    _mk_game(tmp_path / "Real")
    (tmp_path / "NotAGame").mkdir()
    (tmp_path / "NotAGame" / "readme.txt").write_text("hi", encoding="utf-8")
    got = {p.name for p in find_games(tmp_path)}
    assert got == {"Real"}


def test_find_games_finds_rpgmaker_by_data_json(tmp_path: Path) -> None:
    """RPG Maker MV 的判据是 ``www/`` 或 ``data/*.json``。"""
    g = tmp_path / "MVGame"
    (g / "www" / "data").mkdir(parents=True)
    (g / "www" / "data" / "System.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in find_games(tmp_path)] == ["MVGame"]


def test_find_games_finds_renpy_by_game_dir(tmp_path: Path) -> None:
    g = tmp_path / "RenpyGame"
    (g / "game").mkdir(parents=True)
    (g / "game" / "script.rpy").write_text("label start:", encoding="utf-8")
    assert [p.name for p in find_games(tmp_path)] == ["RenpyGame"]


def test_find_games_finds_unity_by_data_dir(tmp_path: Path) -> None:
    g = tmp_path / "UnityGame"
    (g / "UnityGame_Data").mkdir(parents=True)
    assert [p.name for p in find_games(tmp_path)] == ["UnityGame"]


def test_find_games_respects_depth(tmp_path: Path) -> None:
    _mk_game(tmp_path / "a" / "b" / "Deep")
    assert find_games(tmp_path, max_depth=1) == []
    assert len(find_games(tmp_path, max_depth=3)) == 1


def test_find_games_skips_recycle_bin(tmp_path: Path) -> None:
    _mk_game(tmp_path / "$RECYCLE.BIN" / "Junk")
    assert find_games(tmp_path) == []


# ---------------------------------------------------------------------------
# detect_already_chinese
# ---------------------------------------------------------------------------


def test_detects_chinese_by_system_terms(tmp_path: Path) -> None:
    d = tmp_path / "www" / "data"
    d.mkdir(parents=True)
    (d / "System.json").write_text(
        json.dumps({"terms": {"basic": ["等级", "生命值", "魔法值"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    chinese, why = detect_already_chinese(tmp_path, "rpgmaker")
    assert chinese
    assert "System.json" in why


def test_detects_chinese_with_nested_terms_lists(tmp_path: Path) -> None:
    """★ 回归测试：RPG Maker 的 ``terms.basic`` 是**列表的列表**。

    最初的实现只取 ``isinstance(v, str)``，于是**一个字符串都取不到**、
    永远判 False —— 用户库里本来就是中文的游戏会被整份重翻。
    """
    d = tmp_path / "www" / "data"
    d.mkdir(parents=True)
    (d / "System.json").write_text(
        json.dumps(
            {
                "terms": {
                    "basic": [["等级", "生命值", "魔法值", "经验值"]],
                    "commands": [["战斗", "逃跑", "物品", "技能"]],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    chinese, why = detect_already_chinese(tmp_path, "rpgmaker")
    assert chinese, "嵌套列表里的中文没被统计到"
    assert "System.json" in why


def test_locale_zh_is_chinese(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir(parents=True)
    (d / "System.json").write_text(
        json.dumps({"locale": "zh_CN", "terms": {}}), encoding="utf-8"
    )
    chinese, why = detect_already_chinese(tmp_path, "rpgmaker")
    assert chinese
    assert "locale" in why


def test_english_system_terms_is_not_chinese(tmp_path: Path) -> None:
    d = tmp_path / "www" / "data"
    d.mkdir(parents=True)
    (d / "System.json").write_text(
        json.dumps({"terms": {"basic": ["Level", "HP", "MP"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    chinese, _why = detect_already_chinese(tmp_path, "rpgmaker")
    assert not chinese


def test_detects_chinese_by_font_file(tmp_path: Path) -> None:
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "SourceHanSansSC-Regular.otf").write_bytes(b"OTTO")
    chinese, why = detect_already_chinese(tmp_path, "unity")
    assert chinese
    assert "字体" in why


def test_detects_previous_novaloc_run(tmp_path: Path) -> None:
    (tmp_path / "mplus-1m-regular.novaloc.ttf").write_bytes(b"\x00\x01")
    chinese, why = detect_already_chinese(tmp_path, "rpgmaker")
    assert chinese
    assert "本工具" in why


def test_empty_dir_is_not_chinese(tmp_path: Path) -> None:
    chinese, why = detect_already_chinese(tmp_path, "unity")
    assert not chinese
    assert why == ""


# ---------------------------------------------------------------------------
# changed_files —— 关键：只认内容，不认 mtime
# ---------------------------------------------------------------------------


def test_changed_files_only_reports_real_changes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    out = tmp_path / "out"
    for d in (src, out):
        (d / "data").mkdir(parents=True)
    (src / "data" / "same.json").write_text("A", encoding="utf-8")
    (out / "data" / "same.json").write_text("A", encoding="utf-8")
    (src / "data" / "diff.json").write_text("B", encoding="utf-8")
    (out / "data" / "diff.json").write_text("C", encoding="utf-8")

    rels = {str(p) for p in changed_files(src, out)}
    assert rels == {str(Path("data") / "diff.json")}


def test_changed_files_ignores_mtime_difference(tmp_path: Path) -> None:
    """★ 核心回归测试。

    `unpack` 拷贝会刷新 mtime。若实现按 mtime 判断"变了"，这里会
    报出两个文件 —— 于是一次汉化会把**整份游戏目录**都重写一遍。
    """
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "a.dat").write_text("same", encoding="utf-8")
    time.sleep(0.01)
    (out / "a.dat").write_text("same", encoding="utf-8")

    assert changed_files(src, out) == []


def test_changed_files_picks_up_new_file(tmp_path: Path) -> None:
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (out / "added.txt").write_text("new", encoding="utf-8")
    assert [str(p) for p in changed_files(src, out)] == ["added.txt"]


def test_changed_files_excludes_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (out / "_novaloc_backup").mkdir()
    (out / "_novaloc_backup" / "x.txt").write_text("x", encoding="utf-8")
    assert changed_files(src, out, exclude_dirs={"_novaloc_backup"}) == []


# ---------------------------------------------------------------------------
# write_back
# ---------------------------------------------------------------------------


def test_write_back_overwrites_and_backs_up(tmp_path: Path) -> None:
    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "text.json").write_text("ENGLISH", encoding="utf-8")
    (out / "text.json").write_text("中文", encoding="utf-8")

    bak = tmp_path / "backups"
    rep = write_back(src, out, backup_root=bak)

    assert rep.written == ["text.json"]
    assert (src / "text.json").read_text(encoding="utf-8") == "中文"
    # ★ 备份必须存在，且内容是**原件**
    assert Path(rep.backup_dir).is_dir()
    assert (Path(rep.backup_dir) / "text.json").read_text(encoding="utf-8") == "ENGLISH"


def test_backup_dir_is_unique_across_runs_in_same_second(tmp_path: Path) -> None:
    r"""★ 同一秒内的两次写回必须各有**自己的**备份目录。

    ## 实测的缺陷

    原来备份目录名用 `%Y%m%d-%H%M%S`（只到**秒**），同一秒内两次写回会
    拿到同一个目录；而目录内 `if not bak.is_file()` 会让第二次**跳过备份**，
    却**照常覆盖**目标文件。实测：

        # 第一次：V0 -> V1（备份 V0）
        # 第二次：V1 -> V2（同一秒）—— 备份目录同名
        备份内容 = V0      ← 中间的 V1 版本**没有**被保存

    回滚到"最初"仍可行，但目录名让人以为这是第二次写回的独立备份 ——
    真出事时会**少一个可回滚的版本**，而且完全没有提示。

    这条测试就是那个缺陷的哨兵：两次写回必须产出**不同**的备份目录，
    且各自保存**改写前**的内容。

    ## 为什么要对齐到"刚跨过秒边界"

    缺陷只在**同一秒内**触发。若测试恰好跨过秒边界，旧实现也能通过，
    这条测试就成了空话。所以先 `sleep` 到刚进入新的一秒，再做两次写回 ——
    两次写回各自只做一次 `copy2`，远快于 1 秒，必然落在同一秒里。
    """
    import time as _time

    # 对齐：等到"刚进入新的一秒"
    now = _time.time()
    _time.sleep(max(0.0, 1.0 - (now % 1.0)) + 0.02)

    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "text.json").write_text("V0", encoding="utf-8")
    bak = tmp_path / "backups"

    # 第一次：V0 -> V1
    (out / "text.json").write_text("V1", encoding="utf-8")
    r1 = write_back(src, out, backup_root=bak)

    # 第二次：V1 -> V2（紧接在同一秒内）
    (out / "text.json").write_text("V2", encoding="utf-8")
    r2 = write_back(src, out, backup_root=bak)

    assert r1.backup_dir and r2.backup_dir, "两次都应有备份目录"
    assert r1.backup_dir != r2.backup_dir, (
        f"同一秒内的两次写回不能共用备份目录：{r1.backup_dir}"
    )
    # 各自保存**改写前**的版本
    assert (Path(r1.backup_dir) / "text.json").read_text(encoding="utf-8") == "V0"
    assert (Path(r2.backup_dir) / "text.json").read_text(encoding="utf-8") == "V1", (
        "第二次的备份必须是 V1（它改写前的内容）—— 旧实现会漏掉这一版"
    )
    # 最终文件是 V2，而两个历史版本都还在
    assert (src / "text.json").read_text(encoding="utf-8") == "V2"
    assert len(list(bak.iterdir())) == 2, "应有两个独立的备份目录"


def test_write_back_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "t.txt").write_text("old", encoding="utf-8")
    (out / "t.txt").write_text("new", encoding="utf-8")

    rep = write_back(src, out, backup_root=tmp_path / "bak", dry_run=True)
    assert rep.written == ["t.txt"]
    assert (src / "t.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "bak").exists()


def test_write_back_no_change_is_noop(tmp_path: Path) -> None:
    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "t.txt").write_text("same", encoding="utf-8")
    (out / "t.txt").write_text("same", encoding="utf-8")

    rep = write_back(src, out, backup_root=tmp_path / "bak")
    assert rep.written == []
    assert rep.backup_dir == ""
    assert not (tmp_path / "bak").exists()


def test_write_back_creates_nested_dirs(tmp_path: Path) -> None:
    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    (out / "www" / "data").mkdir(parents=True)
    (out / "www" / "data" / "Map001.json").write_text("中文", encoding="utf-8")

    rep = write_back(src, out, backup_root=tmp_path / "bak")
    assert rep.written == [str(Path("www") / "data" / "Map001.json")]
    assert (src / "www" / "data" / "Map001.json").read_text(encoding="utf-8") == "中文"


def test_write_back_does_not_touch_untouched_files(tmp_path: Path) -> None:
    """未变化的文件**不能**被重写（否则会刷新 mtime、破坏校验和/签名）。"""
    src = tmp_path / "game"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "binary.dll").write_bytes(b"\x00" * 64)
    (out / "binary.dll").write_bytes(b"\x00" * 64)
    before = (src / "binary.dll").stat().st_mtime_ns

    write_back(src, out, backup_root=tmp_path / "bak")
    assert (src / "binary.dll").stat().st_mtime_ns == before
