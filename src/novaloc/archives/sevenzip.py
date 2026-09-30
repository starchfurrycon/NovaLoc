"""7z 后端：**只读**。

7z 的写入需要 `py7zr`，而它不在核心依赖里（核心依赖保持精简，
游戏资源里的 7z 也远少于 zip/rpa）。所以这里的策略是：

* **能读**：优先 `py7zr`，其次系统 `7z` / `7za` / `7zr` 命令行；
* **不回写**：两者都不可用时明确报错，并在 `probe()` 里把
  `writable` 标成 `False`，让 UI/CLI 提前告诉用户"这个只能解包"。

为什么不"用命令行凑合着写"：`7z u` 就地更新会改动整个容器结构，
而我们对它的格式约束没有把握 —— 与其产出一个可能半坏的归档，
不如明确说"请你解包后手工打包"。这与项目一贯的取舍一致：
**宁可明确告知需要人工介入，也不产出"看着成功、实际没用"的结果。**
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import ArchiveEntry, ArchiveError, safe_member_path

#: 依次尝试的命令行工具
_CLI_CANDIDATES = ("7z", "7za", "7zr")


def _find_cli() -> str | None:
    for name in _CLI_CANDIDATES:
        exe = shutil.which(name)
        if exe:
            return exe
    return None


class SevenZipArchive:
    """只读 7z。优先 py7zr，退到命令行。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._impl = ""
        self._py7zr = None
        self._cli: str | None = None
        self._listing: list[tuple[str, int]] | None = None

        try:
            import py7zr  # noqa: PLC0415

            self._py7zr = py7zr
            self._impl = "py7zr"
            return
        except Exception:  # noqa: BLE001
            pass

        cli = _find_cli()
        if cli:
            self._cli = cli
            self._impl = Path(cli).name
            return

        raise ArchiveError(
            "需要读取 .7z 但既没有 py7zr，也找不到 7z 命令行工具。"
            "请运行 `pip install py7zr`，或安装 7-Zip 后重试"
        )

    # ------------------------------------------------------------------

    def _list_cli(self) -> list[tuple[str, int]]:
        if self._listing is not None:
            return self._listing
        assert self._cli is not None
        # `7z l -slt` 输出便于解析的键值对；用 utf-8 读，失败再退 locale
        try:
            r = subprocess.run(
                [self._cli, "l", "-slt", "-ba", str(self.path)],
                capture_output=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError(f"调用 {self._cli} 失败：{exc}") from exc
        text = r.stdout.decode("utf-8", errors="replace")
        if r.returncode != 0 and not text.strip():
            err = r.stderr.decode("utf-8", errors="replace")[:200]
            raise ArchiveError(f"{Path(self._cli).name} 无法列出 {self.path.name}：{err}")

        out: list[tuple[str, int]] = []
        cur_path: str | None = None
        cur_size = 0
        is_dir = True
        for line in text.splitlines():
            line = line.rstrip()
            if line.startswith("Path = "):
                if cur_path is not None and not is_dir:
                    out.append((cur_path, cur_size))
                cur_path = line[7:]
                cur_size = 0
                is_dir = True
            elif line.startswith("Size = "):
                try:
                    cur_size = int(line[7:].strip())
                except ValueError:
                    cur_size = 0
            elif line.startswith("Folder = "):
                is_dir = line[9:].strip() not in ("", "-")
            elif line.startswith("Attributes = "):
                if "D" in line[13:]:
                    is_dir = True
        if cur_path is not None and not is_dir:
            out.append((cur_path, cur_size))
        self._listing = out
        return out

    def entries(self) -> list[ArchiveEntry]:
        if self._py7zr is not None:
            with self._py7zr.SevenZipFile(self.path, "r") as z:
                out: list[ArchiveEntry] = []
                for name, info in z.list():
                    if getattr(info, "is_directory", False):
                        continue
                    safe = safe_member_path(name)
                    if safe is None:
                        continue
                    size = int(getattr(info, "uncompressed", 0) or 0)
                    out.append(ArchiveEntry(name=safe, size=size))
                return out
        out = []
        for name, size in self._list_cli():
            safe = safe_member_path(name)
            if safe is None:
                continue
            out.append(ArchiveEntry(name=safe, size=size))
        return out

    def read(self, name: str) -> bytes:
        safe = safe_member_path(name)
        if safe is None:
            raise ArchiveError(f"非法条目名 {name!r}")

        if self._py7zr is not None:
            # py7zr 不支持随机读单个文件，得整包解到临时目录（已在上层限过体积）
            with tempfile.TemporaryDirectory(prefix="novaloc-7z-") as td:
                with self._py7zr.SevenZipFile(self.path, "r") as z:
                    z.extract(path=td, targets=[safe])
                p = Path(td) / safe
                if not p.is_file():
                    raise ArchiveError(f"7z 里没有 {safe!r}")
                return p.read_bytes()

        assert self._cli is not None
        with tempfile.TemporaryDirectory(prefix="novaloc-7z-") as td:
            try:
                r = subprocess.run(
                    [self._cli, "x", "-y", f"-o{td}", str(self.path), safe],
                    capture_output=True, timeout=600,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ArchiveError(f"调用 {self._cli} 解压失败：{exc}") from exc
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", errors="replace")[:200]
                raise ArchiveError(f"{Path(self._cli).name} 解压 {safe} 失败：{err}")
            p = Path(td) / safe
            if not p.is_file():
                raise ArchiveError(f"7z 里没有 {safe!r}")
            return p.read_bytes()

    def close(self) -> None:  # noqa: PLR6301
        pass

    def __enter__(self) -> SevenZipArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["SevenZipArchive"]
