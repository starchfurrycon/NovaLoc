"""ZIP 与 TAR 后端（读写都支持）。

这两种格式 Python 标准库就能处理，所以没有任何可选依赖 —— 也就是说
"游戏资源在 zip 里"这条路径在**裸装环境下也能用**。

写入时会丢掉原归档的时间戳元数据（Python 的 zipfile/tarfile 不接受
"原样复制信息对象"这种写法），所以产物里文件时间会是当前时间。
这不影响游戏读取，但用户如果在意"文件时间被我改了"，看备份文件即可。
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from .base import ArchiveEntry, ArchiveError, safe_member_path


def _zip_entries(z: zipfile.ZipFile) -> list[ArchiveEntry]:
    out: list[ArchiveEntry] = []
    for info in z.infolist():
        if info.is_dir():
            continue
        safe = safe_member_path(info.filename)
        if safe is None:
            continue
        out.append(ArchiveEntry(name=safe, size=int(info.file_size)))
    return out


class ZipArchive:
    """只读 ZIP。条目按需读取。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self._z = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"{self.path.name} 不是有效的 ZIP：{exc}") from exc
        # 先建一次名字映射，避免每次 read 都遍历 infolist
        self._names: dict[str, str] = {}
        for info in self._z.infolist():
            if info.is_dir():
                continue
            safe = safe_member_path(info.filename)
            if safe is not None:
                self._names.setdefault(safe, info.filename)

    def entries(self) -> list[ArchiveEntry]:
        return _zip_entries(self._z)

    def read(self, name: str) -> bytes:
        safe = safe_member_path(name)
        if safe is None or safe not in self._names:
            raise ArchiveError(f"归档里没有 {name!r}")
        try:
            return self._z.read(self._names[safe])
        except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"读取 {name} 失败：{exc}") from exc

    def close(self) -> None:
        try:
            self._z.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> ZipArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TarArchive:
    """只读 TAR（含 .tar.gz / .tar.bz2 / .tar.xz）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # mode "r:*" 让 tarfile 自动认压缩方式。
        # 这里刻意不用 with：句柄的生命周期与对象绑定，由 close() 负责
        # （NOQA 因为 ruff 只看到"打开没关闭"，看不到 close() 里的关闭）。
        try:
            self._t = tarfile.open(self.path, "r:*")  # noqa: SIM115
        except (tarfile.TarError, OSError) as exc:
            raise ArchiveError(f"{self.path.name} 不是有效的 TAR：{exc}") from exc
        self._by_safe: dict[str, str] = {}
        for m in self._t.getmembers():
            if not m.isfile():
                continue
            safe = safe_member_path(m.name)
            if safe is not None:
                self._by_safe.setdefault(safe, m.name)

    def entries(self) -> list[ArchiveEntry]:
        out: list[ArchiveEntry] = []
        for m in self._t.getmembers():
            if not m.isfile():
                continue
            safe = safe_member_path(m.name)
            if safe is None:
                continue
            out.append(ArchiveEntry(name=safe, size=int(m.size)))
        return out

    def read(self, name: str) -> bytes:
        safe = safe_member_path(name)
        if safe is None or safe not in self._by_safe:
            raise ArchiveError(f"归档里没有 {name!r}")
        f = self._t.extractfile(self._by_safe[safe])
        if f is None:
            raise ArchiveError(f"无法读取 {name}")
        with f:
            return f.read()

    def close(self) -> None:
        try:
            self._t.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> TarArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------


def write_zip(dest: Path, members: list[tuple[str, bytes]]) -> int:
    """写 ZIP（deflate 压缩）。返回写入条目数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            safe = safe_member_path(name)
            if safe is None:
                continue
            z.writestr(safe, data)
            n += 1
    return n


def write_tar(dest: Path, members: list[tuple[str, bytes]], *, compress: bool = False) -> int:
    """写 TAR。默认不压缩（游戏资源多是已压缩的图片，再压意义不大且慢）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:gz" if compress else "w"
    n = 0
    with tarfile.open(dest, mode) as t:
        for name, data in members:
            safe = safe_member_path(name)
            if safe is None:
                continue
            info = tarfile.TarInfo(name=safe)
            info.size = len(data)
            info.mtime = 0
            t.addfile(info, io.BytesIO(data))
            n += 1
    return n


__all__ = ["TarArchive", "ZipArchive", "write_tar", "write_zip"]
