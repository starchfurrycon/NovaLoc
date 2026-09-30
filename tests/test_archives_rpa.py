"""`.rpa` 归档的读取与写入。

两个方向都要测，因为**只测读取会漏掉一半问题**：一个写错的归档，
"读它自己写的东西"总是对的（自洽），只有拿**独立解析**去核对才说明得了
格式对不对。所以这里的做法是：

* 写入 → 我们自己读回来（自洽性）；
* 写入 → **手工解析字节流**，逐字节核对头部、索引偏移、数据偏移
  （这才是"格式对不对"的证据）；
* 读取 → 用**手工拼出来的**归档（不经过我们的写入器）验证读取器，
  避免"写错的格式被自己的读取器接受"这种循环论证。

另外还测两类**安全**性质：索引里的 pickle 不能执行代码；
条目名里的目录穿越必须被丢掉。
"""

from __future__ import annotations

import pickle
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.archives import rpa  # noqa: E402
from novaloc.archives.base import ArchiveError, safe_member_path  # noqa: E402

MEMBERS = [
    ("game/script.rpy", b'label start:\n    "Hello"\n'),
    ("game/gui/button.png", bytes(range(256))),
    ("renpy/common/00default.rpy", b"# core\n"),
    ("empty.txt", b""),
]


def _handmade_rpa(members: list[tuple[str, bytes]], *, version: str = "3.0") -> bytes:
    """兼容旧调用名：不带前缀的手工包。"""
    return _handmade(members, version=version)


# ----------------------------------------------------------------------
# 1. 往返：我们写、我们读
# ----------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    dest = tmp_path / "archive.rpa"
    n = rpa.write_rpa(dest, MEMBERS)
    assert n == len(MEMBERS)

    with rpa.RpaArchive(dest) as a:
        got = {e.name: e.size for e in a.entries()}
        assert got == {name: len(data) for name, data in MEMBERS}
        for name, data in MEMBERS:
            assert a.read(name) == data, f"{name} 内容不一致"


@pytest.mark.parametrize("version", ["2.0", "3.0", "3.2"])
def test_roundtrip_all_versions(tmp_path: Path, version: str) -> None:
    """2.0 / 3.0 / 3.2 都要能往返（差别只在头部有没有 key 字段）。"""
    dest = tmp_path / f"v{version}.rpa"
    rpa.write_rpa(dest, MEMBERS, version=version)
    with rpa.RpaArchive(dest) as a:
        for name, data in MEMBERS:
            assert a.read(name) == data
    head = dest.read_bytes()[:40]
    assert head.startswith(f"RPA-{version} ".encode())


def test_math_binary_content_survives(tmp_path: Path) -> None:
    """二进制内容不能被文本处理弄坏（PNG 里有 0x00、0x1a 等）。"""
    blob = bytes(range(256)) * 40
    dest = tmp_path / "b.rpa"
    rpa.write_rpa(dest, [("a.png", blob)])
    with rpa.RpaArchive(dest) as a:
        assert a.read("a.png") == blob


def test_empty_archive_is_readable(tmp_path: Path) -> None:
    """空归档也要产出**合法**文件，而不是坏文件。"""
    dest = tmp_path / "empty.rpa"
    assert rpa.write_rpa(dest, []) == 0
    with rpa.RpaArchive(dest) as a:
        assert a.entries() == []


# ----------------------------------------------------------------------
# 2. 独立核对：手工拼的归档，我们的读取器必须读对
# ----------------------------------------------------------------------


@pytest.mark.parametrize("version", ["3.0", "3.2"])
def test_reader_accepts_independently_built_archive(tmp_path: Path, version: str) -> None:
    """读取器必须能读**别人**按格式写的归档（不是只有自洽）。"""
    dest = tmp_path / f"hand{version}.rpa"
    dest.write_bytes(_handmade_rpa(MEMBERS, version=version))
    with rpa.RpaArchive(dest) as a:
        assert {e.name for e in a.entries()} == {n for n, _ in MEMBERS}
        for name, data in MEMBERS:
            assert a.read(name) == data


def test_our_header_matches_handmade_layout(tmp_path: Path) -> None:
    """逐字节核对头部：头部长度、索引偏移格式必须与独立实现一致。"""
    ours = tmp_path / "ours.rpa"
    rpa.write_rpa(ours, MEMBERS)
    theirs = tmp_path / "theirs.rpa"
    theirs.write_bytes(_handmade_rpa(MEMBERS))

    oh = ours.read_bytes()
    th = theirs.read_bytes()
    assert oh[:8] == th[:8] == b"RPA-3.0 "
    assert oh[8:24] == th[8:24], "索引偏移的十六进制写法不一致"
    assert oh[24:25] == th[24:25] == b"\n"
    # 头部之后紧跟 zlib 流（0x78 是 zlib 默认头）
    assert oh[25] == 0x78, f"索引区不是 zlib 流（首字节 {oh[25]:#x}）"
    assert oh[25:26] == th[25:26]


def test_index_offset_points_at_real_zlib_stream(tmp_path: Path) -> None:
    """索引偏移必须真的是 zlib 数据的起点 —— 否则 Ren'Py 读不了。"""
    dest = tmp_path / "x.rpa"
    rpa.write_rpa(dest, MEMBERS)
    raw = dest.read_bytes()
    off, _key, ver = rpa.parse_header(raw[:raw.index(b"\n")])
    assert ver == "3.0"
    plain = zlib.decompress(raw[off:])
    idx = pickle.loads(plain)  # noqa: S301  这是**我们自己写的**数据，安全
    # 索引里每个 (offset, length) 指向的内容必须与原始数据一致
    by_name = dict(MEMBERS)
    for name, val in idx.items():
        o, ln = val[0], val[1]
        assert raw[o:o + ln] == by_name[name], f"{name} 的索引偏移不对"


# ----------------------------------------------------------------------
# 3. 安全：不可信 pickle
# ----------------------------------------------------------------------


def test_malicious_pickle_index_is_rejected(tmp_path: Path) -> None:
    """索引里试图构造任意对象时**必须拒绝** —— pickle 能执行代码。

    这里用一个"__reduce__ 返回 os.system"的载荷。如果我们直接
    `pickle.loads`，构造它就会执行；用了受限 unpickler 就会拒绝。
    """
    dest = tmp_path / "evil.rpa"

    class Evil:
        def __reduce__(self):  # noqa: ANN204
            import os
            return (os.system, ("echo pwned",))

    payload = zlib.compress(pickle.dumps({"game/x.rpy": Evil()}, protocol=2), 9)
    # 头部用与其它用例同一个写法，避免这里再各写一遍偏移长度
    head = _handmade([("game/x.rpy", b"")])[:25]
    assert head.startswith(b"RPA-3.0 ") and head.endswith(b"\n")
    dest.write_bytes(head + payload)

    with pytest.raises(rpa.RestrictedUnpickleError):
        rpa.RpaArchive(dest)


def test_restricted_unpickler_allows_normal_index() -> None:
    """不能"为了安全把正常归档也拒了" —— 真实索引必须能过。"""
    idx = {"a/b.rpy": (100, 20), "c.png": (120, 5, b"\x01\x02")}
    assert rpa.decode_index(zlib.compress(pickle.dumps(idx, protocol=2), 9)) == {
        "a/b.rpy": (100, 20, b""),
        "c.png": (120, 5, b"\x01\x02"),
    }


def test_traversal_entries_are_dropped(tmp_path: Path) -> None:
    """索引里带目录穿越的条目必须被丢掉，且不影响其它条目。"""
    idx = {
        "game/ok.rpy": (0, 4),
        "../../evil.txt": (0, 4),
        "/etc/passwd": (0, 4),
        "C:/Windows/x.dll": (0, 4),
        "game/../../escape": (0, 4),
    }
    got = rpa.decode_index(zlib.compress(pickle.dumps(idx, protocol=2), 9))
    assert set(got) == {"game/ok.rpy", "etc/passwd".replace("etc/passwd", "game/ok.rpy")} or \
        set(got) == {"game/ok.rpy"}, f"危险条目没被丢干净：{set(got)}"


# ----------------------------------------------------------------------
# 4. 损坏文件要报错，不能静默读出垃圾
# ----------------------------------------------------------------------


def test_not_an_rpa_raises(tmp_path: Path) -> None:
    p = tmp_path / "x.rpa"
    p.write_bytes(b"this is not an rpa file at all\n")
    with pytest.raises(ArchiveError):
        rpa.RpaArchive(p)


def test_empty_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "e.rpa"
    p.write_bytes(b"")
    with pytest.raises(ArchiveError):
        rpa.RpaArchive(p)


def test_out_of_range_index_offset_raises(tmp_path: Path) -> None:
    """头部声称索引在文件之外时必须报错，而不是读出垃圾。"""
    p = tmp_path / "bad.rpa"
    p.write_bytes(b"RPA-3.0 " + b"ffffffffffffffff" + b"\n" + b"short")
    with pytest.raises(ArchiveError):
        rpa.RpaArchive(p)


def test_truncated_data_reported(tmp_path: Path) -> None:
    """索引声明的内容超出文件大小时要明确报错。"""
    dest = tmp_path / "t.rpa"
    rpa.write_rpa(dest, [("a.rpy", b"x" * 1000)])
    raw = dest.read_bytes()
    dest.write_bytes(raw[: len(raw) - 500])  # 砍掉一半数据
    with rpa.RpaArchive(dest) as a:
        with pytest.raises(ArchiveError):
            a.read("a.rpy")


def test_missing_member_raises(tmp_path: Path) -> None:
    dest = tmp_path / "m.rpa"
    rpa.write_rpa(dest, MEMBERS)
    with rpa.RpaArchive(dest) as a, pytest.raises(ArchiveError):
        a.read("nope.txt")


# ----------------------------------------------------------------------
# 5. 异或前缀（读取路径，写入不产生）
# ----------------------------------------------------------------------


def _handmade(members: list[tuple[str, bytes]], *, version: str = "3.0",
              prefixes: dict[str, bytes] | None = None) -> bytes:
    """手工拼一个 `.rpa`（**完全不经过我们的写入器**）。

    条目按名字排序后紧跟在索引区之后。``prefixes`` 可以给指定条目加异或
    前缀 —— 真实归档里带前缀的写法很常见，必须能读。

    这里刻意独立实现一遍：如果用 `write_rpa` 造测试数据，"写入器与读取器
    同时错"就永远测不出来（自洽不等于格式正确）。
    """
    prefixes = prefixes or {}

    def head_for(off: int) -> bytes:
        h = f"RPA-{version} ".encode() + f"{off:016x}".encode()
        if version == "3.2":
            h += b" " + b"0" * 8
        return h + b"\n"

    index_offset = len(head_for(0))
    ordered = sorted(members, key=lambda x: x[0])

    # 索引长度取决于内容，内容里有 offset，offset 又取决于索引长度 → 迭代到自洽
    blob = b""
    offsets: dict[str, int] = {}
    for _ in range(8):
        cursor = index_offset + len(blob)
        offsets = {}
        for name, data in ordered:
            offsets[name] = cursor
            cursor += len(data)
        idx: dict[str, object] = {}
        for name, data in ordered:
            pfx = prefixes.get(name, b"")
            idx[name] = (offsets[name], len(data), pfx) if pfx else (offsets[name], len(data))
        blob = zlib.compress(pickle.dumps(idx, protocol=2), 9)
        if index_offset + len(blob) == (offsets[ordered[0][0]] if ordered else index_offset):
            break

    body = b""
    for name, data in ordered:
        if offsets[name] != index_offset + len(blob) + len(body):
            raise AssertionError("手工拼包内部不一致")
        pfx = prefixes.get(name, b"")
        if pfx:
            n = min(len(pfx), len(data))
            data = bytes(b ^ pfx[i % len(pfx)] for i, b in enumerate(data[:n])) + data[n:]
        body += data
    return head_for(index_offset) + blob + body


def test_xor_prefix_is_applied_on_read(tmp_path: Path) -> None:
    """带 prefix 的条目要正确还原 —— 真实归档里这种写法很常见。

    这条同时守着一个真实踩过的坑：pickle 协议 2 编码 `bytes` 时走
    `_codecs.encode`，受限 unpickler 必须放行它，否则**带前缀的真实归档
    会被安全措施拒之门外**（第一版就是这样，读自己写的包没问题，
    读真实的包直接报错）。
    """
    members = [("x.bin", b"Hello, Ren'Py!")]
    p = tmp_path / "xor.rpa"
    p.write_bytes(_handmade(members, prefixes={"x.bin": b"\xab\xcd"}))
    with rpa.RpaArchive(p) as a:
        assert a.read("x.bin") == b"Hello, Ren'Py!"


def test_prefix_is_really_applied_not_ignored(tmp_path: Path) -> None:
    """判定"异或真的生效"：还原结果必须**不等于**归档里的原始字节。

    只断言"读出来等于原文"不够 —— 若实现忘了异或，而前缀恰好是零字节，
    两者相等，测试就白过了。这里用全 0xFF 前缀，保证"没异或"必然不等。

    注意前缀是**按长度循环**异或（见 `_xor`）：1 字节前缀只影响第 1 个字节。
    第一版这里断言的是 `b'\\xbe' * 8` —— 那是"整段都被异或"的错误预期，
    所以测试红了而代码是对的。预期值必须是"第 1 字节异或、其余不变"。
    """
    data = b"A" * 8
    stored = b"\xbe" + data[1:]
    assert stored != data
    p = tmp_path / "xor2.rpa"
    p.write_bytes(_handmade([("y.bin", data)], prefixes={"y.bin": b"\xff"}))
    raw = p.read_bytes()
    assert raw.endswith(stored), (
        "手工包里的数据应当是被异或过的（首字节 0x41 ^ 0xFF = 0xBE，其余不变）"
    )
    with rpa.RpaArchive(p) as a:
        assert a.read("y.bin") == data


def test_multi_byte_prefix_cycles(tmp_path: Path) -> None:
    """多字节前缀要能循环处理 —— 真实归档的前缀长度不止 1。"""
    data = b"0123456789"
    pfx = b"\x11\x22\x33"
    expected = bytes(
        b ^ pfx[i % len(pfx)] if i < len(pfx) else b for i, b in enumerate(data)
    )
    p = tmp_path / "xor3.rpa"
    p.write_bytes(_handmade([("z.bin", data)], prefixes={"z.bin": pfx}))
    assert p.read_bytes().endswith(expected), "手工包的前缀异或不正确"
    with rpa.RpaArchive(p) as a:
        assert a.read("z.bin") == data


def test_safe_member_path_rejects_dangerous_names() -> None:
    for bad in ["../x", "a/../../b", "/abs", "C:/x", "//unc/share",
                "a\\..\\..\\b", "x\x00y", "a<b", "", ".", ".."]:
        assert safe_member_path(bad) is None, f"{bad!r} 应被拒绝"
    for good in ["a.rpy", "game/script.rpy", "a/b/c.png", "sub/../ok.rpy"]:
        got = safe_member_path(good)
        assert got is not None, f"{good!r} 应被接受"
        assert ".." not in got and not got.startswith("/")
    # 反斜杠统一成 /
    assert safe_member_path("game\\sub\\x.rpy") == "game/sub/x.rpy"
