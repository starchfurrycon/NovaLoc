"""游戏资源归档的读写抽象。

本模块只定义"归档长什么样"与安全规则，具体格式在 `zip_tar` / `sevenzip` /
`rpa` 里实现，`service.py` 负责编排。

## 为什么需要这一层

原先只有 `fonts/downloader.py::extract_archive()` 会解压，而且它是**为字体包
写的**，有两个不能用于游戏资源的行为：

1. 它只按后缀挑文件（`.ttf`/`.otf`），其余全丢；
2. 它把解出来的文件**拍平**到一个目录（`out_dir / name`），目录结构丢失。

游戏资源必须保留结构：`game/script.rpy` 与 `game/sub/script.rpy` 是两个
不同的文件，拍平会互相覆盖。所以这里是独立实现，不复用那个函数。

## 安全规则（三条，都在这里强制）

归档是**不可信输入**。游戏文件可能来自任何地方，所以：

* **目录穿越**：条目名一律过 :func:`safe_member_path`，`../`、绝对路径、
  盘符、UNC 一律拒绝 —— 否则一个恶意归档能往 `C:\\Windows` 写文件；
* **解压炸弹**：单个文件与总解压量都有上限（:data:`MAX_ENTRY_BYTES` /
  :data:`MAX_TOTAL_BYTES`）。上限检查发生在**读取之前**（用归档自己声明的
  长度），所以不会先解压一个 10 GB 的东西再发现超限；
* **不可信 pickle**：`.rpa` 的索引是 pickle 格式。`pickle.loads` 可以执行
  任意代码，所以 RPA 后端用 :class:`_RestrictedUnpickler` 只允许基础容器
  类型，遇到其它任何类名直接拒绝（见 `rpa.py`）。

## 不做什么

**不碰加密/私有容器**（`.xp3` 的加密变体、自定义打包格式）。那是一个无底洞：
格式未公开、每个游戏可能不一样、做错了会让用户以为工具坏了。
本项目宁可明确说"这个不支持，请你先自己解包"，也不去猜格式。
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: 单个条目解压后的字节上限（默认 256 MiB）
MAX_ENTRY_BYTES = 256 * 1024 * 1024

#: 一个归档全部条目的解压总量上限（默认 2 GiB）—— 防"解压炸弹"
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

#: 条目数上限。防"一百万个空文件"这类把 inode 用光的攻击
MAX_ENTRIES = 200_000

#: 条目名里不允许出现的字符（Windows 保留字符 + 控制字符）
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')


class ArchiveError(RuntimeError):
    """归档读取/写入失败。"""


class UnsafeArchiveError(ArchiveError):
    """归档包含危险条目（目录穿越、解压炸弹等）。"""


@dataclass(frozen=True)
class ArchiveEntry:
    """归档里的一个文件。"""

    name: str
    """归档内的 POSIX 相对路径（用 `/`，不带前导 `/`）。"""

    size: int
    """解压后的字节数（由归档**自己声明**，用于读取前就拦下解压炸弹）。"""

    @property
    def suffix(self) -> str:
        return posixpath.splitext(self.name)[1].lower()


def safe_member_path(name: str) -> str | None:
    """把归档里的条目名规整成安全的相对 POSIX 路径；不安全返回 ``None``。

    拒绝的情况（都对应真实攻击手法）：

    * 绝对路径 ``/etc/passwd``、``C:\\Windows\\x``；
    * 目录穿越 ``../../x``、``a/../../b``；
    * UNC 路径 ``\\\\server\\share``；
    * Windows 保留字符与控制字符（``<``、``:``、``\\x00`` 等）；
    * 规整之后变成空串或只剩分隔符。

    返回的路径**保证**不含 ``..``、不以 ``/`` 开头、不含反斜杠。
    """
    if not name or not isinstance(name, str):
        return None
    # 统一分隔符：归档里混用 / 与 \ 的情况真实存在（Windows 上打的包）
    raw = name.replace("\\", "/").strip()
    if not raw:
        return None
    # 绝对路径与 UNC
    if raw.startswith("/") or raw.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", raw):  # C:/...
        return None
    if _BAD_NAME_CHARS.search(raw):
        return None
    # posixpath.normpath 会把 a/../b 化简成 b，把 ../x 化简成 ../x
    norm = posixpath.normpath(raw)
    if norm in (".", "/", ""):
        return None
    if norm.startswith("../") or norm == "..":
        return None
    if norm.startswith("/"):
        return None
    # 逐段再确认一次：normpath 对 "a/./../b" 这类已经处理了，但双保险
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


@dataclass
class ArchiveInfo:
    """探测结果，用于向用户说明"这是什么、能不能改"。"""

    path: Path
    kind: str
    """`rpa` / `zip` / `tar` / `7z` / `unknown`。"""

    entry_count: int = 0
    total_bytes: int = 0
    writable: bool = False
    """本工具能否**安全回写**这个格式。

    `zip` / `tar` / `rpa` 可以（我们完整实现了写入）；
    `7z` 只读 —— 回写需要 py7zr，而它不在核心依赖里，
    所以宁可明确说"只读"，也不产出一个半坏不坏的归档。
    """

    note: str = ""

    @property
    def label(self) -> str:
        return {
            "rpa": "Ren'Py 归档 (.rpa)",
            "zip": "ZIP 归档",
            "tar": "TAR 归档",
            "7z": "7z 归档",
        }.get(self.kind, self.kind)


@runtime_checkable
class ArchiveReader(Protocol):
    """只读访问一个归档。"""

    def entries(self) -> list[ArchiveEntry]: ...

    def read(self, name: str) -> bytes: ...

    def close(self) -> None: ...


def check_limits(entries: list[ArchiveEntry]) -> None:
    """在**读取任何数据之前**检查条目数与体积上限。

    这是解压炸弹防御的关键位置：`ArchiveEntry.size` 来自归档自己的元数据，
    不需要真正解压就能拿到，所以能在付出代价之前就拒绝。
    """
    if len(entries) > MAX_ENTRIES:
        raise UnsafeArchiveError(
            f"归档条目过多（{len(entries)} > {MAX_ENTRIES}），拒绝解压 —— "
            "这可能是解压炸弹"
        )
    total = sum(max(0, e.size) for e in entries)
    if total > MAX_TOTAL_BYTES:
        raise UnsafeArchiveError(
            f"归档解压后体积过大（{total / 2**30:.1f} GiB > "
            f"{MAX_TOTAL_BYTES / 2**30:.0f} GiB），拒绝解压 —— 这可能是解压炸弹"
        )
    for e in entries:
        if e.size > MAX_ENTRY_BYTES:
            raise UnsafeArchiveError(
                f"归档内单个文件过大（{e.name}：{e.size / 2**20:.0f} MiB），拒绝解压"
            )


# ----------------------------------------------------------------------
# 格式探测
# ----------------------------------------------------------------------

#: 各格式的魔术字节（按需读前若干字节）
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"RPA-3.0 ", "rpa"),
    (b"RPA-3.2 ", "rpa"),
    (b"RPA-2.0 ", "rpa"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),  # 空 zip
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "tar"),  # gzip
    (b"BZh", "tar"),  # bzip2
    (b"\xfd7zXZ", "tar"),  # xz
)


def detect_kind(path: Path) -> str:
    """按**内容**判断归档格式，不只看后缀。

    为什么不信后缀：游戏目录里的归档后缀经常是错的（`.dat`、`.bin`、
    没有后缀），而魔术字节不会骗人。后缀只在内容判断不出来时作兜底。
    """
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind == "tar":
                # gzip/bzip2/xz 也可能是别的东西，但游戏资源里基本就是 tar
                return "tar"
            return kind

    # tar 没有前导魔术字节，签名在第 257 字节
    if head[:2] not in (b"\x1f\x8b", b"BZ"):
        try:
            with path.open("rb") as f:
                f.seek(257)
                if f.read(5) == b"ustar":
                    return "tar"
        except OSError:
            pass

    name = path.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        return "tar"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(".rpa"):
        return "rpa"
    return "unknown"


__all__ = [
    "MAX_ENTRIES",
    "MAX_ENTRY_BYTES",
    "MAX_TOTAL_BYTES",
    "ArchiveEntry",
    "ArchiveError",
    "ArchiveInfo",
    "ArchiveReader",
    "UnsafeArchiveError",
    "check_limits",
    "detect_kind",
    "safe_member_path",
]
