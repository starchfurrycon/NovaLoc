"""游戏资源归档：解包、回写。

支持情况：

| 格式 | 读取 | 写入 | 说明 |
| --- | --- | --- | --- |
| `.rpa`（Ren'Py） | ✅ | ✅ | 自建实现（见 `rpa.py`），无需额外依赖 |
| `.zip` | ✅ | ✅ | 标准库 |
| `.tar` / `.tar.gz` / `.tar.bz2` / `.tar.xz` | ✅ | ✅ | 标准库 |
| `.7z` | ✅ | ❌ | 需 `py7zr` 或 7z 命令行；**只读**是刻意的（见 `sevenzip.py`） |

**不支持**加密容器与私有格式（`.xp3` 加密变体、自定义打包）。
这类格式未公开、每个游戏可能不一样，猜错了会让用户以为工具坏了 ——
宁可明确说"请你先自己解包"。
"""

from __future__ import annotations

from .base import (
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    MAX_TOTAL_BYTES,
    ArchiveEntry,
    ArchiveError,
    ArchiveInfo,
    UnsafeArchiveError,
    check_limits,
    detect_kind,
    safe_member_path,
)
from .service import (
    ARCHIVE_SUFFIXES,
    BACKUP_SUFFIX,
    UnpackedArchive,
    changed_members,
    open_archive,
    probe,
    repack,
    unpack_into,
)

__all__ = [
    "ARCHIVE_SUFFIXES",
    "BACKUP_SUFFIX",
    "MAX_ENTRIES",
    "MAX_ENTRY_BYTES",
    "MAX_TOTAL_BYTES",
    "ArchiveEntry",
    "ArchiveError",
    "ArchiveInfo",
    "UnpackedArchive",
    "UnsafeArchiveError",
    "changed_members",
    "check_limits",
    "detect_kind",
    "open_archive",
    "probe",
    "repack",
    "safe_member_path",
    "unpack_into",
]
