r"""写回成功后回收 `out/` 里的**冗余副本**。

## 为什么需要它（实测的结构性浪费）

`prepare_out`（`engines/base.py`）是**整目录复制** —— 那是为了
"游戏运行时可能依赖大量未被修改的资源，少复制一个就可能启动失败"，
这个决定本身没错。但它让 `out/` 变成**整个游戏的副本**：

| 游戏 | `out/` | 备份 | 备份覆盖 `out` 的数据文件 |
|---|---|---|---|
| Academy Love Saga | 1235 文件 / 1955 MB | 66 文件 | **65 / 1050** |
| [Summoner Veil] Homura | 3012 文件 / 804 MB | 589 文件 | 589 / 2677 |

⇒ `out/` 里 **94% 是从未被修改的原样文件**（`.resS`/`.png`/`.dll`），
它们既不是译文来源、也不是恢复依据。实测 `out/` 合计 **11.5 GB**，
而备份 1.7 GB —— 30 倍差距，按全库规模会累积到几十 GB。

## 三条判据（缺一不可）

1. 文件在 `written` 里（**写回确实成功了**）；
2. 备份目录里**有原件**（能回滚）；
3. 文件存在于 `out_dir`。

第 2 条最关键：`write_back` 的备份是**逐文件**做的，
存在"写了但没备份"的可能 —— 那种文件一律保留，
因为它可能是**唯一的"改后版本"**。

## 为什么"整个 out/ 删掉"是错的

没写回的文件分两种，靠 `written` 就能区分：

* **没变的** ⇒ `out/` 副本与现盘相同，删了无所谓；
* **变了但被拦下**（`should_stop` 中止、占位符不合格被拒）
  ⇒ 这份新版本**只在 `out/` 里**，删了就永久丢失。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.batch import prune_written_out  # noqa: E402


def _setup(tmp_path: Path, written: list[str], backed_up: list[str]) -> tuple[Path, Path]:
    """造 ``out/`` 与备份。``written`` 是"写回成功的"，``backed_up`` 是有备份的。"""
    out = tmp_path / "out" / "data"
    bak = tmp_path / "bak" / "G-20261004-000000" / "data"
    out.mkdir(parents=True)
    for rel in written:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"新内容-{rel}", encoding="utf-8")
    for rel in backed_up:
        p = bak / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"原内容-{rel}", encoding="utf-8")
    return out, bak


def test_removes_written_files_that_have_a_backup(tmp_path: Path) -> None:
    """★ 主路径：写回了 + 有备份 ⇒ 删掉 `out/` 副本。"""
    out, bak = _setup(tmp_path, ["a.json", "b.json"], ["a.json", "b.json"])
    n, freed, kept = prune_written_out(out, bak, ["a.json", "b.json"])
    assert n == 2
    assert freed > 0
    assert kept == []
    assert not (out / "a.json").exists()
    assert not (out / "b.json").exists()
    # 备份必须**完好**（那是回滚依据）
    assert (bak / "a.json").read_text(encoding="utf-8") == "原内容-a.json"


def test_keeps_files_that_have_no_backup(tmp_path: Path) -> None:
    r"""★★ 第二条判据：没有备份的一律保留。

    那种文件可能是**唯一的"改后版本"**（`write_back` 的备份是逐文件做的，
    理论上存在"写了但没备份"）。把它删掉就等于永久丢失。
    """
    out, bak = _setup(tmp_path, ["a.json", "b.json"], ["a.json"])
    n, _freed, kept = prune_written_out(out, bak, ["a.json", "b.json"])
    assert n == 1
    assert kept == ["b.json"], kept
    assert (out / "b.json").is_file(), "没有备份的文件被误删了"
    assert not (out / "a.json").exists()


def test_never_touches_files_not_in_written(tmp_path: Path) -> None:
    r"""★★ 最重要的不变量：**没写回的文件一个都不能动**。

    它们里面混着"被闸门拦下的新版本"（`should_stop` 中止、
    占位符不合格被拒），那些只在 `out/` 里有。
    """
    out, bak = _setup(
        tmp_path,
        ["written.json", "blocked.json", "unchanged.json"],
        ["written.json", "blocked.json", "unchanged.json"],
    )
    n, _freed, _kept = prune_written_out(out, bak, ["written.json"])
    assert n == 1
    assert not (out / "written.json").exists()
    assert (out / "blocked.json").is_file(), "被拦下的文件被误删了"
    assert (out / "unchanged.json").is_file(), "未写回的文件被误删了"


def test_missing_out_dir_is_harmless(tmp_path: Path) -> None:
    """`out/` 不存在时不该抛异常（幂等，可重复调用）。"""
    assert prune_written_out(tmp_path / "nope", tmp_path / "bak", ["a"]) == (0, 0, [])


def test_idempotent(tmp_path: Path) -> None:
    """跑两次：第二次没有可删的了，且不报错。"""
    out, bak = _setup(tmp_path, ["a.json"], ["a.json"])
    assert prune_written_out(out, bak, ["a.json"])[0] == 1
    assert prune_written_out(out, bak, ["a.json"])[0] == 0


def test_freed_bytes_is_accurate(tmp_path: Path) -> None:
    """报告的释放量要等于实际大小（别报虚数）。"""
    out, bak = _setup(tmp_path, ["a.json"], ["a.json"])
    size = (out / "a.json").stat().st_size
    _n, freed, _kept = prune_written_out(out, bak, ["a.json"])
    assert freed == size


def test_nested_paths(tmp_path: Path) -> None:
    """嵌套路径（`www/data/Map001.json`）也要正确处理。"""
    rel = "www/data/Map001.json"
    out, bak = _setup(tmp_path, [rel], [rel])
    n, _f, kept = prune_written_out(out, bak, [rel])
    assert n == 1 and kept == []
    assert not (out / rel).exists()


def test_backup_is_never_deleted(tmp_path: Path) -> None:
    """★ 这个函数**只删 out/**，绝不碰备份目录。"""
    out, bak = _setup(tmp_path, ["a.json"], ["a.json"])
    prune_written_out(out, bak, ["a.json"])
    assert bak.is_dir()
    assert (bak / "a.json").is_file()


# ----------------------------------------------------------------------
# 两种模式：safe_to_clear=True 整目录清空 / False 逐文件
# ----------------------------------------------------------------------


def test_safe_to_clear_removes_the_whole_out_dir(tmp_path: Path) -> None:
    r"""★★ 写回**完全成功**时整目录清空 —— 包括"没进 written"的文件。

    ## 这个测试推翻了我先前的假设

    我原以为"没进 `written` 的文件是'被拦下的新版本'，必须保留"。
    **实测推翻了它**：`changed_files()` 用**大小 + 哈希**判定"变了没"，
    所以进了 `written` 的就是"内容真的不同"的那些；其余文件在 `out/`
    里就是**原样副本**，与现盘逐字节相同。

    实测（抽 ButtKnight 的 40 个文件比对现盘）：

        与现盘完全相同: 40 / 40      与现盘不同: 0 / 40

    ⇒ 删了无损失，而且省下 94% 的空间（11.5 GB → 0）。
    """
    out, bak = _setup(
        tmp_path,
        ["written.json", "not_written.json", "also_not.json"],
        ["written.json"],
    )
    n, freed, kept = prune_written_out(
        out, bak, ["written.json"], safe_to_clear=True
    )
    assert n == 3, "整目录清空应删掉全部文件"
    assert freed > 0
    assert kept == []
    # 所有文件都没了
    assert not [p for p in out.rglob("*") if p.is_file()]
    # 备份仍在
    assert (bak / "written.json").is_file()


def test_safe_to_clear_false_keeps_unwritten(tmp_path: Path) -> None:
    r"""★★ 有失败/中止时**只能逐文件**删，未写回的一律保留。

    这时 `out/` 里可能真有"没能写回的新版本"（`should_stop` 中止、
    占位符不合格被拒），那是**唯一副本**，删了就永久丢失。
    """
    out, bak = _setup(
        tmp_path,
        ["written.json", "blocked.json"],
        ["written.json", "blocked.json"],
    )
    n, _f, _k = prune_written_out(out, bak, ["written.json"], safe_to_clear=False)
    assert n == 1, "非安全模式只该删 written 里的"
    assert not (out / "written.json").exists()
    assert (out / "blocked.json").is_file(), "被拦下的文件被误删了"


def test_default_is_the_conservative_mode(tmp_path: Path) -> None:
    """默认必须是保守模式 —— 危险的行为不能是默认值。"""
    out, bak = _setup(tmp_path, ["a.json", "b.json"], ["a.json", "b.json"])
    n, _f, _k = prune_written_out(out, bak, ["a.json"])
    assert n == 1
    assert (out / "b.json").is_file(), "默认模式不该删 written 之外的文件"


def test_safe_to_clear_keeps_out_dir_itself(tmp_path: Path) -> None:
    """清空内容但**保留目录本身**（用户/调用方仍能看出它的位置）。"""
    out, bak = _setup(tmp_path, ["a.json"], ["a.json"])
    prune_written_out(out, bak, ["a.json"], safe_to_clear=True)
    assert out.is_dir()


def test_safe_to_clear_still_never_touches_backup(tmp_path: Path) -> None:
    """整目录清空模式下，备份**同样**一个都不能少。"""
    out, bak = _setup(tmp_path, ["a.json", "b.json"], ["a.json", "b.json"])
    prune_written_out(out, bak, ["a.json", "b.json"], safe_to_clear=True)
    assert (bak / "a.json").is_file()
    assert (bak / "b.json").is_file()
