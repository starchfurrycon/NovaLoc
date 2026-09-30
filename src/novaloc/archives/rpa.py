"""Ren'Py `.rpa` 归档的读取与写入。

## 格式（从零实现，见下方"格式来源"）

```
偏移 0   : b"RPA-3.0 "                    魔术 + 版本
偏移 8   : b"%016x" % index_offset         索引起始偏移（16 位十六进制）
偏移 24  : b" " + b"%08x" % key            （仅 3.2）异或密钥
偏移 ... : b"\\n"                          头部结束
偏移 index_offset : zlib( pickle( 索引 ) )
索引之后 : 各文件的原始字节
```

索引是 ``dict[str, tuple[int, int] | tuple[int, int, bytes]]``：

* ``(offset, length)`` —— 该文件从 ``offset`` 起、长 ``length``；
* ``(offset, length, prefix)`` —— 前若干字节与 ``prefix`` **循环异或**。

这份实现对写入一律**不加密**（不写 prefix）：prefix 只是混淆，不是加密，
读的一方（Ren'Py 与所有社区工具）都能正确读无 prefix 的归档。这样既不用
猜密钥，也让产物对用户完全透明。

## 格式来源

没有抄任何实现（unrpa / rpatool 与本项目许可不兼容），格式理解来自
Ren'Py 官方文档 `doc/rpa.rst` 与公开的格式说明。读取逻辑用自建归档往返
验证（见 `tests/test_archives_rpa.py`）。

## 安全问题：索引是 pickle

`pickle.loads` 能**执行任意代码** —— 一个恶意 `.rpa` 就能在用户机器上
跑命令。所以这里用 :class:`_RestrictedUnpickler`：只允许基础容器与字符串
类型，遇到任何其它类名直接拒绝。真实的 RPA 索引只用这些类型，所以
合法归档不受影响。
"""

from __future__ import annotations

import io
import pickle
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .base import ArchiveEntry, ArchiveError, safe_member_path

#: 支持的版本（全都只有"是否带 key"这一点差别）
_VERSIONS = (b"RPA-2.0 ", b"RPA-3.0 ", b"RPA-3.2 ")

_HEADER_RE = re.compile(rb"^RPA-(\d)\.(\d) ([0-9a-fA-F]{16})(?: ([0-9a-fA-F]{8}))?")

#: 读取上限：头部最多这么长（防"头部声称索引在 10 TB 处"）
_MAX_HEADER = 256


class RestrictedUnpickleError(ArchiveError):
    """归档的 pickle 索引里出现了不允许的类型。"""


class _RestrictedUnpickler(pickle.Unpickler):
    """只允许基础容器类型（以及解出它们所必需的少数内建函数）的 unpickler。

    `pickle` 的 `find_class` 是唯一的代码执行入口，所以在这里白名单即可。
    真实的 RPA 索引只用 ``dict`` / ``tuple`` / ``list`` / ``bytes`` / ``str``
    / ``int``，所以合法归档完全不受影响。

    ## `_codecs.encode` 为什么必须在白名单里

    这不是"顺手放宽"，而是**必需**：pickle 协议 2 编码 ``bytes`` 时走的是
    ``_codecs.encode(文本, 'latin1')``。而协议 2 正是 Ren'Py 与社区工具
    实际使用的协议，**带异或前缀的 `.rpa` 索引里一定有 bytes**。

    第一版白名单漏了它，结果是：读**自己写的**归档没问题（我们写索引时
    没有 prefix），但读**真实的**带 prefix 归档会被拒 —— 属于
    "安全措施把正常功能打死了"。所以这里补上，并加测试守住。

    `_codecs.encode` 本身是纯函数（文本 → 字节），不能执行命令，
    放进白名单不降低安全性；真正的危险形态是 ``os.system`` / ``eval`` /
    ``builtins.exec`` 这类，它们仍然被拒。
    """

    _ALLOWED: dict[str, dict[str, str]] = {
        "builtins": {
            "dict": "dict", "list": "list", "tuple": "tuple", "set": "set",
            "frozenset": "frozenset", "bytes": "bytes", "bytearray": "bytearray",
            "str": "str", "int": "int", "float": "float", "bool": "bool",
        },
        # pickle 协议 2 的 bytes 编码路径（见类文档）
        "_codecs": {"encode": "encode"},
        # 有些工具用 OrderedDict 存索引
        "collections": {"OrderedDict": "OrderedDict"},
    }

    def find_class(self, module: str, name: str) -> Any:  # noqa: ANN401
        mod = (module or "").lstrip("_") if module not in ("_codecs",) else module
        table = self._ALLOWED.get(mod)
        if table and name in table:
            if mod == "_codecs":
                import _codecs  # noqa: PLC0415

                return getattr(_codecs, name)
            if mod == "collections":
                import collections  # noqa: PLC0415

                return getattr(collections, name)
            import builtins  # noqa: PLC0415

            return getattr(builtins, name)
        raise RestrictedUnpickleError(
            f"归档索引试图构造不允许的对象 {module}.{name} —— "
            "出于安全考虑已拒绝（pickle 可以执行任意代码）"
        )


def _safe_unpickle(data: bytes) -> Any:  # noqa: ANN401
    try:
        return _RestrictedUnpickler(io.BytesIO(data)).load()
    except RestrictedUnpickleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ArchiveError(f"归档索引无法解析：{exc}") from exc


@dataclass
class RpaIndex:
    """解析出来的索引：条目名 → (数据偏移, 长度, 异或前缀)。"""

    entries: dict[str, tuple[int, int, bytes]]
    index_offset: int
    data_start: int
    """索引区结束、文件数据开始的偏移。"""

    version: str = "3.0"


def parse_header(head: bytes) -> tuple[int, int | None, str]:
    """解析 RPA 头部 → ``(索引偏移, 异或密钥或 None, 版本)``。"""
    m = _HEADER_RE.match(head)
    if not m:
        raise ArchiveError("不是有效的 RPA 归档（头部魔术字节不匹配）")
    major, minor, off_hex = m.group(1).decode(), m.group(2).decode(), m.group(3)
    key_hex = m.group(4)
    index_offset = int(off_hex, 16)
    key = int(key_hex, 16) if key_hex else None
    return index_offset, key, f"{major}.{minor}"


def decode_index(raw: bytes) -> dict[str, tuple[int, int, bytes]]:
    """解码索引区（先 zlib 解压，再受限 pickle）。

    索引里所有条目名都过 :func:`safe_member_path`，不安全的条目**直接丢掉**
    并返回 —— 恶意归档不能靠一个 `../../x` 的条目名写到游戏目录外面去。
    """
    try:
        plain = zlib.decompress(raw)
    except zlib.error as exc:
        raise ArchiveError(f"归档索引解压失败（可能不是 RPA，或已损坏）：{exc}") from exc
    obj = _safe_unpickle(plain)
    if not isinstance(obj, dict):
        raise ArchiveError(f"归档索引不是字典，而是 {type(obj).__name__}")

    out: dict[str, tuple[int, int, bytes]] = {}
    for name, val in obj.items():
        if not isinstance(name, str):
            continue
        safe = safe_member_path(name)
        if safe is None:
            continue  # 丢弃危险条目
        if isinstance(val, (tuple, list)):
            if len(val) < 2:
                continue
            off, ln = val[0], val[1]
            prefix = val[2] if len(val) > 2 and isinstance(val[2], bytes) else b""
            if isinstance(off, int) and isinstance(ln, int) and off >= 0 and ln >= 0:
                out[safe] = (off, ln, prefix)
    return out


def _xor(data: bytes, prefix: bytes) -> bytes:
    """把前 ``len(prefix)`` 个字节与 prefix 循环异或（RPA 的混淆方案）。"""
    if not prefix:
        return data
    n = min(len(prefix), len(data))
    head = bytes(b ^ prefix[i % len(prefix)] for i, b in enumerate(data[:n]))
    return head + data[n:]


class RpaArchive:
    """读一个 `.rpa`。条目按需读取，不整包载入内存。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: BinaryIO = self.path.open("rb")
        try:
            self.index = self._load()
        except Exception:
            self._fh.close()
            raise

    def _load(self) -> RpaIndex:
        head = self._fh.read(_MAX_HEADER)
        if not head:
            raise ArchiveError(f"{self.path.name} 是空文件")
        # 头部到第一个换行为止
        nl = head.find(b"\n")
        if nl < 0:
            raise ArchiveError(f"{self.path.name} 的 RPA 头部没有换行符，文件可能损坏")
        header = head[:nl]
        index_offset, _key, version = parse_header(header)

        size = self.path.stat().st_size
        if index_offset <= 0 or index_offset >= size:
            raise ArchiveError(
                f"{self.path.name} 声称索引在偏移 {index_offset}，"
                f"但文件只有 {size} 字节 —— 归档已损坏"
            )
        self._fh.seek(index_offset)
        raw = self._fh.read()
        if not raw:
            raise ArchiveError(f"{self.path.name} 的索引区为空")
        entries = decode_index(raw)
        # 数据区从索引区末尾开始（索引长度未知，用所有条目偏移的最小值推）
        data_start = min((v[0] for v in entries.values()), default=index_offset)
        return RpaIndex(
            entries=entries, index_offset=index_offset,
            data_start=min(data_start, index_offset), version=version,
        )

    def entries(self) -> list[ArchiveEntry]:
        return [
            ArchiveEntry(name=n, size=ln)
            for n, (_off, ln, _pfx) in sorted(self.index.entries.items())
        ]

    def read(self, name: str) -> bytes:
        safe = safe_member_path(name)
        if safe is None or safe not in self.index.entries:
            raise ArchiveError(f"归档里没有 {name!r}")
        off, ln, prefix = self.index.entries[safe]
        size = self.path.stat().st_size
        if off + ln > size:
            raise ArchiveError(
                f"{safe} 声称在偏移 {off} 处有 {ln} 字节，超出文件大小 {size} —— 归档已损坏"
            )
        self._fh.seek(off)
        data = self._fh.read(ln)
        if len(data) != ln:
            raise ArchiveError(f"读取 {safe} 时提前结束（要 {ln} 字节，实际 {len(data)}）")
        return _xor(data, prefix)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> RpaArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------


def write_rpa(
    dest: Path,
    members: list[tuple[str, bytes]],
    *,
    version: str = "3.0",
) -> int:
    """把 ``(name, data)`` 列表写成 `.rpa`，返回写入的条目数。

    一律**不写异或前缀**（见模块文档）。`.rpa` 没有"部分更新"能力 ——
    文件必须整体重写，所以这里先在内存里拼好索引，再一次性顺序写出。
    """
    clean: list[tuple[str, bytes]] = []
    for name, data in members:
        safe = safe_member_path(name)
        if safe is None:
            continue
        clean.append((safe, data))
    clean.sort(key=lambda x: x[0])

    header_ver = b"RPA-3.0 " if version not in ("2.0", "3.2") else f"RPA-{version} ".encode()
    if version == "3.2":
        # 3.2 要求带 key 字段；写 0 表示不异或
        placeholder_head = header_ver + b"0" * 16 + b" " + b"0" * 8 + b"\n"
    else:
        placeholder_head = header_ver + b"0" * 16 + b"\n"

    # 先占位算出索引偏移与各文件偏移，再回填。
    # 索引长度取决于内容（里面存着 offset），而 offset 又取决于索引长度 ——
    # 所以这里迭代到自洽：改一次索引长度，重算一遍所有 offset，直到收敛。
    index_offset = len(placeholder_head)
    offsets: dict[str, tuple[int, int]] = {}
    index_blob = b""
    for _ in range(8):
        # 这一轮假设"索引正好是上一轮那么长"，据此把所有文件排在它后面
        data_start = index_offset + len(index_blob)
        offsets = {}
        c = data_start
        for name, data in clean:
            offsets[name] = (c, len(data))
            c += len(data)
        index_obj = {n: (offsets[n][0], offsets[n][1]) for n, _d in clean}
        index_blob = zlib.compress(pickle.dumps(index_obj, protocol=2), 9)
        if index_offset + len(index_blob) == data_start:
            break  # 收敛：索引长度没变，offset 也就不会变

    if not clean:
        # 空归档：索引仍然要是一个合法的空字典，否则读的一方会报错
        offsets = {}
        index_blob = zlib.compress(pickle.dumps({}, protocol=2), 9)

    head = header_ver + f"{index_offset:016x}".encode()
    if version == "3.2":
        head += b" " + b"0" * 8
    head += b"\n"
    if len(head) != len(placeholder_head):
        raise ArchiveError("内部错误：RPA 头部长度与占位不一致")

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as f:
        f.write(head)
        pos = f.write(index_blob)
        if pos != len(index_blob):
            raise ArchiveError("写入索引失败（磁盘可能已满）")
        for name, data in clean:
            want_off = offsets[name][0]
            if f.tell() != want_off:
                raise ArchiveError(
                    f"内部错误：写入 {name} 时位置是 {f.tell()}，索引声明 {want_off}"
                )
            f.write(data)
            written += 1
    return written


__all__ = [
    "RpaArchive",
    "RpaIndex",
    "RestrictedUnpickleError",
    "decode_index",
    "parse_header",
    "write_rpa",
]
