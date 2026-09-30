"""游戏资源归档：解包、回写与安全规则。

对外只暴露少量函数（:func:`probe` / :func:`unpack_into` / :func:`repack`），
格式差异全部收在各后端里。

## 为什么要"解包到工作区"而不是"就地解包"

原游戏目录是**只读**的（贯穿整个项目的硬约束）。所以流程是：

    <游戏目录>  ──探测──▶  归档清单
                              │
                              ├─ 解包到 <工作区>/unpacked/<序号>_<名字>/
                              │    （保留目录结构，见 base.safe_member_path）
                              │
                              └─ 回写时：把解包树里被改过的文件重新打回归档，
                                  原归档先备份成 .novaloc.bak

## 回写策略：整包重写 + 备份

`.rpa` / `.zip` 都没有"就地改一个条目"的能力，必须整体重写。
所以回写是"读原归档所有条目 → 用解包树里的新版本替换改过的
→ 整体写到临时文件 → 原子替换 → 原文件先备份"。

**只在真的有文件变化时才重写**：没变化就不动归档，避免无意义地
改动用户的游戏文件（时间戳、体积都变了，用户会以为出问题了）。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .base import (
    ArchiveEntry,
    ArchiveError,
    ArchiveInfo,
    check_limits,
    detect_kind,
)

log = logging.getLogger(__name__)

#: 探测时最多往下走几层（游戏目录一般不深，防病态目录树）
MAX_DEPTH = 3

#: 归档名后缀过滤：这些一律不当归档（它们是运行库/数据，不是资源包）
_SKIP_NAMES = frozenset({"python.zip", "python36.zip", "python38.zip", "python39.zip"})

#: 默认探测的后缀
ARCHIVE_SUFFIXES = (".rpa", ".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".dat", ".bin")

#: 回写时给原归档留的备份后缀
BACKUP_SUFFIX = ".novaloc.bak"


@dataclass
class UnpackedArchive:
    """一个已解包的归档，以及"回写时怎么找回去"的信息。"""

    source: Path
    """原归档路径（在游戏目录里）。"""

    dest: Path
    """解包目录（在工作区里）。"""

    kind: str
    entries: int
    written: int
    skipped: list[str] = field(default_factory=list)
    """被安全规则丢掉的条目名（目录穿越等），供用户审阅。"""

    writable: bool = True

    @property
    def rel(self) -> str:
        return self.source.name


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------


def probe(
    game_dir: Path,
    *,
    max_depth: int = MAX_DEPTH,
    suffixes: tuple[str, ...] = ARCHIVE_SUFFIXES,
) -> list[ArchiveInfo]:
    """扫描游戏目录，返回**确认是归档**的文件清单。

    判定顺序是"先看魔术字节，再看后缀"：游戏目录里的归档后缀经常是错的
    （`.dat`、`.bin`、甚至没有后缀），而魔术字节不会骗人。

    识别不出来的文件**不会**进这个清单 —— 宁可漏报也不误报：
    把一个普通数据文件当归档解包，只会浪费时间和磁盘。
    """
    root = Path(game_dir)
    if not root.is_dir():
        return []

    found: list[ArchiveInfo] = []
    seen: set[Path] = set()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = sorted(cur.iterdir())
        except OSError as exc:
            log.debug("无法列出 %s：%s", cur, exc)
            continue
        for p in children:
            try:
                if p.is_dir():
                    stack.append((p, depth + 1))
                    continue
                if not p.is_file() or p.name in _SKIP_NAMES:
                    continue
            except OSError:
                continue
            if p in seen:
                continue
            # 后缀快速过滤：既不是已知后缀、内容也认不出来，就跳过
            lower = p.name.lower()
            if not (lower.endswith(suffixes) or lower.endswith(".rpa")):
                # 还是给不认识后缀的文件一次机会：只看魔术字节
                kind = detect_kind(p)
                if kind == "unknown":
                    continue
            else:
                kind = detect_kind(p)
            if kind == "unknown":
                continue
            seen.add(p)
            info = ArchiveInfo(path=p, kind=kind, writable=kind in ("rpa", "zip", "tar"))
            if kind == "7z":
                info.writable = False
                info.note = "7z 只支持解包：回写需要 py7zr，未纳入核心依赖"
            found.append(info)

    # 打开一次拿到条目数与体积（失败就留 0，不影响"这是个归档"的结论）
    for info in found:
        try:
            with open_archive(info.path) as ar:
                ents = ar.entries()
                check_limits(ents)
                info.entry_count = len(ents)
                info.total_bytes = sum(e.size for e in ents)
        except Exception as exc:  # noqa: BLE001
            info.note = f"（无法读取条目清单：{exc}）"
    return found


def open_archive(path: Path) -> object:
    """按格式打开归档，返回一个有 ``entries/read/close`` 的对象。"""
    kind = detect_kind(path)
    if kind == "rpa":
        from .rpa import RpaArchive

        return RpaArchive(path)
    if kind in ("zip", "tar"):
        from .zip_tar import TarArchive, ZipArchive

        return ZipArchive(path) if kind == "zip" else TarArchive(path)
    if kind == "7z":
        from .sevenzip import SevenZipArchive

        return SevenZipArchive(path)
    raise ArchiveError(f"不认识的归档格式：{path.name}")


# ----------------------------------------------------------------------
# 解包
# ----------------------------------------------------------------------


def unpack_into(
    archive: Path,
    dest: Path,
    *,
    only_suffixes: tuple[str, ...] | None = None,
) -> UnpackedArchive:
    """把一个归档解到 ``dest``，返回解包结果。

    ``only_suffixes`` 非空时只解这些后缀（例如只解 `.rpy` / `.png`），
    用于"我只想改脚本"这种诉求，能省大量磁盘和时间。

    条目名一律过 :func:`safe_member_path`；不安全的**丢掉并记录**，
    不抛异常 —— 一个坏条目不该让整个游戏无法处理，但用户必须能看到它。
    """
    src = Path(archive)
    out = Path(dest)
    kind = detect_kind(src)
    res = UnpackedArchive(source=src, dest=out, kind=kind, entries=0, written=0)

    with open_archive(src) as ar:
        entries: list[ArchiveEntry] = ar.entries()
        check_limits(entries)  # 读取前就拦下解压炸弹
        res.entries = len(entries)

        for e in entries:
            if e.name in ("", "/"):
                res.skipped.append(e.name)
                continue
            if only_suffixes is not None and e.suffix not in only_suffixes:
                continue
            target = out / e.name
            # 再确认一次最终路径没跑出 dest（safe_member_path 已经保证，
            # 这里是第二道防线：符号链接、大小写等平台差异）
            try:
                target.resolve().relative_to(out.resolve())
            except ValueError:
                res.skipped.append(e.name)
                log.warning("归档条目 %r 会写到解包目录之外，已跳过", e.name)
                continue
            data = ar.read(e.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            res.written += 1
    return res


# ----------------------------------------------------------------------
# 回写
# ----------------------------------------------------------------------


def changed_members(unpacked: UnpackedArchive) -> list[str]:
    """列出解包树里**与归档内原始内容不同**的条目名。

    需要一个"原始内容的指纹"来比较。这里不额外存指纹文件，而是直接
    重新读归档比对字节 —— 归档不大（脚本与图片，通常几十 MB），
    而**比错方向的代价更高**：多回写一次是浪费，少回写一次是"用户以为
    汉化成功了但游戏里没变"。

    注意：**流水线不用这个函数**（它传 `changes_from=out/`），
    因为流水线把产物写在 `out/` 而不是解包树里 —— 见 `repack` 的文档。
    """
    out: list[str] = []
    with open_archive(unpacked.source) as ar:
        for e in ar.entries():
            f = unpacked.dest / e.name
            if not f.is_file():
                continue  # 解包时按后缀过滤掉了，不算"改动"
            try:
                if f.read_bytes() != ar.read(e.name):
                    out.append(e.name)
            except (OSError, ArchiveError) as exc:
                log.debug("比对 %s 失败：%s", e.name, exc)
                # 读不出来就当作"可能改了"，宁可多写
                out.append(e.name)
    return out


def repack(
    unpacked: UnpackedArchive,
    *,
    backup: bool = True,
    changes_from: Path | None = None,
) -> tuple[bool, str]:
    """把改动打回原归档，返回 ``(是否有改动, 说明)``。

    ## 两种"改动从哪来"

    * ``changes_from is None``（默认）：拿**解包树自己**和归档逐字节比对。
      用于"用户手工改了文件，然后单独回写"。
    * ``changes_from`` 给一个目录（流水线传的是 `out/`）：凡是归档里存在、
      且在该目录下有同名文件的条目，就用那份内容替换。

    ## 为什么流水线必须传 `changes_from`

    流水线的工作方式是：解包 → 各阶段读解包树 → `apply()` 把产物写到
    `out/`（原目录与解包树都不改，这是"原游戏只读"约束的延伸）。
    所以跑完之后**解包树里一个字都没变**。

    第一版这里没这个参数，于是 `changed_members()` 返回空 →
    回写什么都没做 → `apply` 阶段报 `repacked: 0`，
    **所有阶段都 ok，归档里却还是原文**。这是最坏的一类 bug：
    统计上完全成功，产物却是旧的。
    """
    if not unpacked.writable:
        return False, f"{unpacked.kind} 不支持回写（{unpacked.source.name}）"

    with open_archive(unpacked.source) as ar:
        entries = ar.entries()

        if changes_from is None:
            changed_set = set(changed_members(unpacked))
            source_of: dict[str, Path] = {
                e.name: unpacked.dest / e.name for e in entries if e.name in changed_set
            }
        else:
            root = Path(changes_from)
            source_of = {}
            for e in entries:
                cand = root / e.name
                if cand.is_file():
                    source_of[e.name] = cand
            changed_set = set(source_of)

        if not changed_set:
            return False, "没有文件变化，未改动归档"

        members: list[tuple[str, bytes]] = []
        for e in entries:
            src = source_of.get(e.name)
            if src is None:
                members.append((e.name, ar.read(e.name)))
                continue
            try:
                members.append((e.name, src.read_bytes()))
            except OSError as exc:
                raise ArchiveError(f"读取改动文件 {e.name} 失败：{exc}") from exc

    backup_path = unpacked.source.with_name(unpacked.source.name + BACKUP_SUFFIX)
    if backup and not backup_path.exists():
        shutil.copy2(unpacked.source, backup_path)

    tmp = unpacked.source.with_name(unpacked.source.name + ".novaloc.tmp")
    try:
        if unpacked.kind == "rpa":
            from .rpa import write_rpa

            write_rpa(tmp, members)
        elif unpacked.kind in ("zip", "tar"):
            from .zip_tar import write_tar, write_zip

            (write_zip if unpacked.kind == "zip" else write_tar)(tmp, members)
        else:
            return False, f"{unpacked.kind} 不支持回写"

        tmp.replace(unpacked.source)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    note = f"已把 {len(changed_set)} 个改动文件打回 {unpacked.source.name}"
    if backup and backup_path.exists():
        note += f"（原文件备份为 {backup_path.name}）"
    return True, note


__all__ = [
    "ARCHIVE_SUFFIXES",
    "BACKUP_SUFFIX",
    "MAX_DEPTH",
    "UnpackedArchive",
    "changed_members",
    "open_archive",
    "probe",
    "repack",
    "unpack_into",
]
