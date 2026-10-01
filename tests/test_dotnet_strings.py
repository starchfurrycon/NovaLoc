"""``#US`` 堆解析的测试。

## 为什么要给这个模块写测试

它是**二进制解析**代码，出错方式是"静默读出垃圾" —— 而它的产出
（字符串候选）会被拿去翻译、甚至可能被写回游戏。所以这里既测
正常路径，也测**边界与拒绝**：

* 压缩整数三档编码必须都正确（1/2/4 字节）—— 错了会让偏移全乱、
  读出一串垃圾而不是报错；
* 非 PE / 非 .NET 文件必须**明确抛错**，不能"尽力而为"地返回空 ——
  空结果会被上层误读成"这游戏没有可翻文本"；
* 过滤判据必须**真的拦住**标识符与运行时报错（实测这两类是主要噪声，
  分别占 989/1231 与 9 条），否则翻下去会改坏游戏。

测试里**不依赖**真实游戏文件（那些不在仓库里，也不该进仓库），
所以用 ``struct`` 现场合成一个最小 PE + 元数据 + ``#US`` 堆。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.engines.dotnet_strings import (  # noqa: E402
    AssemblyStrings,
    _looks_like_text,
    _read_compressed_int,
)

# ---------------------------------------------------------------------------
# 压缩整数
# ---------------------------------------------------------------------------


def test_compressed_int_one_byte() -> None:
    assert _read_compressed_int(bytes([0x03]), 0) == (3, 1)
    assert _read_compressed_int(bytes([0x7F]), 0) == (0x7F, 1)


def test_compressed_int_two_bytes() -> None:
    # 0x80 0x80 -> ((0x80 & 0x3F) << 8) | 0x80 = 0x80
    assert _read_compressed_int(bytes([0x80, 0x80]), 0) == (0x80, 2)
    assert _read_compressed_int(bytes([0xBF, 0xFF]), 0) == (0x3FFF, 2)


def test_compressed_int_four_bytes() -> None:
    val, nxt = _read_compressed_int(bytes([0xC0, 0x00, 0x40, 0x00]), 0)
    assert val == (0x00 << 24) | (0x00 << 16) | (0x40 << 8) | 0x00
    assert nxt == 4


def test_compressed_int_rejects_invalid_and_truncated() -> None:
    with pytest.raises(ValueError):
        _read_compressed_int(bytes([0xE0]), 0)  # 0b111xxxxx 无效
    with pytest.raises(ValueError):
        _read_compressed_int(b"", 0)  # 越界
    with pytest.raises(ValueError):
        _read_compressed_int(bytes([0x80]), 0)  # 2 字节但只有 1 字节


# ---------------------------------------------------------------------------
# 过滤判据
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Activate the emergency destruction system.",
        "NotConvex: Polygon is not convex.",
        "Gain {0} gold pieces.",
        "莉菲娅：我必须走了。",
    ],
)
def test_looks_like_text_accepts_real_text(text: str) -> None:
    assert _looks_like_text(text), f"应当接受：{text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空
        "ab",  # 太短
        "_ProjInfo",  # 着色器属性：单标识符、无空格
        "12345",  # 纯数字
        "\\n\\t",  # 纯控制/转义
        "Puppet2D",  # 类名
    ],
)
def test_looks_like_text_rejects_non_dialogue(text: str) -> None:
    # ⚠️ `_ProjInfo` 与 `Puppet2D` 是**实测**噪声里的真实样本
    assert not _looks_like_text(text), f"应当拒绝：{text!r}"


# ---------------------------------------------------------------------------
# 合成一个最小 .NET 程序集
# ---------------------------------------------------------------------------


def _compressed(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([0x80 | (n >> 8), n & 0xFF])
    return bytes([0xC0 | (n >> 24), (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def _us_blob(strings: list[str]) -> bytes:
    """按 ECMA-335 II.24.2.4 拼出 ``#US`` 堆。"""
    out = bytearray(b"\x00")  # 第一个字节是保留项
    for s in strings:
        raw = s.encode("utf-16-le")
        # 长度 = 字符字节数 + 1（末尾那个标记字节）
        blen = len(raw) + 1
        out += _compressed(blen)
        out += raw
        out += b"\x00"  # "不含特殊字符"标记
    out += b"\x00"  # 堆结束（长度 0）
    return bytes(out)


def _make_assembly(tmp_path: Path, strings: list[str], *, name: str = "T.dll") -> Path:
    """造一个结构合法、只含 ``#US`` 堆的最小 PE + 元数据。"""
    us = _us_blob(strings)
    # --- 元数据根 + 流头 ---
    ver = b"v4.0.30319\x00\x00"
    stream_name = b"#US\x00"
    header_len = 16 + len(ver) + 2 + 2 + (8 + len(stream_name))
    # #US 流偏移要对齐到 4
    us_off = (header_len + 3) & ~3
    md = bytearray()
    md += b"BSJB"
    md += struct.pack("<HH", 1, 1)
    md += struct.pack("<I", 0)
    md += struct.pack("<I", len(ver))
    md += ver
    md += struct.pack("<H", 0)  # flags
    md += struct.pack("<H", 1)  # 流数量
    md += struct.pack("<II", us_off, len(us))
    md += stream_name
    md += b"\x00" * (us_off - len(md))
    md += us

    # --- 把它放进一个节的原始数据里 ---
    md_off_in_sec = 0x200
    sec_data = bytearray(b"\x00" * md_off_in_sec) + md
    sec_data += b"\x00" * ((-len(sec_data)) % 0x200)

    sec_rva = 0x2000
    # 节表里第一个节同时承载 CLI 头与元数据
    cli_off_in_sec = 0x100
    cli_rva = sec_rva + cli_off_in_sec
    md_rva = sec_rva + md_off_in_sec
    cli = bytearray(72)
    struct.pack_into("<I", cli, 0, 72)  # cb
    struct.pack_into("<HH", cli, 4, 2, 5)  # MajorRuntimeVersion, Minor
    struct.pack_into("<I", cli, 8, md_rva)  # 元数据目录 RVA
    struct.pack_into("<I", cli, 12, len(md))  # 元数据目录大小
    struct.pack_into("<I", cli, 16, 1)  # flags（IL only）
    sec_data[cli_off_in_sec : cli_off_in_sec + 72] = cli

    # --- PE 头 ---
    # 布局必须与真实 PE 完全一致，否则测出来的是**夹具的**错，不是解析器的：
    #   DOS 头 0x00..0x7F
    #   PE 签名        0x80（偏移 0x3C 处那个值指向这里）
    #   COFF 头        0x84..0x97（20 字节）
    #   可选头         0x98..（opt_size 字节，PE32 = 224）
    #   节表           紧跟可选头（每项 40 字节）
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    opt_size = 224  # PE32
    n_sections = 1

    coff = bytearray(20)
    struct.pack_into("<HH", coff, 0, 0x14C, n_sections)
    struct.pack_into("<HH", coff, 16, opt_size, 0x2102)

    # 可选头
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x10B)  # PE32
    # 数据目录从可选头偏移 96 开始；第 15 项（0-based 14）= CLI 头
    struct.pack_into("<II", opt, 96 + 14 * 8, cli_rva, 72)

    # 节表
    #
    # ★ `PointerToRawData` 必须**真的**等于 `sec_data` 在文件里的位置。
    #   第一版把 `sec_data` 紧接在头之后写（偏移 0x1a0），却在节表里声明
    #   0x200 —— 夹具自相矛盾，于是解析器按节表算出来的 CLI 偏移指到
    #  了一片零。这暴露的**不是**解析器的错，而是"夹具必须自洽"：
    #   二进制格式的测试夹具一旦内部不一致，测的就是夹具自己。
    raw_ptr = 0x200
    sec = bytearray(40)
    sec[0:8] = b".text\x00\x00\x00"
    struct.pack_into("<IIII", sec, 8, len(sec_data), sec_rva, len(sec_data), raw_ptr)

    head = (
        bytes(dos) + b"PE\x00\x00" + bytes(coff) + bytes(opt) + bytes(sec)
    )
    # 头与节数据之间补齐到节表声明的 raw_ptr
    padding = b"\x00" * max(0, raw_ptr - len(head))
    path = tmp_path / name
    path.write_bytes(head + padding + bytes(sec_data))
    return path


# ---------------------------------------------------------------------------
# 端到端：读堆
# ---------------------------------------------------------------------------


def test_reads_us_heap_strings(tmp_path: Path) -> None:
    """★ 正路：能按偏移读出字面量，且**顺序与内容**都对。"""
    want = [
        "Activate the emergency destruction system.",
        "Gain {0} gold pieces.",
        "short",
    ]
    p = _make_assembly(tmp_path, want)
    got = AssemblyStrings(p).read()
    assert [s.text for s in got] == want
    # 偏移必须严格递增（回写定位靠它，乱了就是错位）
    assert all(
        a.offset < b.offset for a, b in zip(got, got[1:], strict=False)
    )


def test_placeholders_are_extracted(tmp_path: Path) -> None:
    """格式占位符必须能被取出来 —— 翻译时要原样保留，丢了就是玩家看到 ``{0}``。"""
    p = _make_assembly(tmp_path, ["Gain {0} gold and {1} gems."])
    s = AssemblyStrings(p).read()[0]
    assert s.placeholders == ["{0}", "{1}"]


def test_dialogue_like_filters_identifiers_and_errors(tmp_path: Path) -> None:
    """★ 严格过滤必须拦住实测噪声的两大类：标识符与运行时报错。

    真实数据里这两类是主要噪声（989 条标识符 / 9 条报错），
    翻下去会改坏游戏。
    """
    p = _make_assembly(
        tmp_path,
        [
            "Activate the emergency destruction system.",  # 真句子 → 留
            "Puppet2D",  # 类名 → 拦
            "_ProjInfo",  # 着色器属性 → 拦
            "[BUG:FIXME] FLIP failed due to missing triangle",  # 报错 → 拦
            "Gain {0} gold pieces.",  # 占位符 → 留
        ],
    )
    A = AssemblyStrings(p)
    strict = [s.text for s in A.dialogue_like()]
    assert "Activate the emergency destruction system." in strict
    assert "Gain {0} gold pieces." in strict
    assert "Puppet2D" not in strict
    assert "_ProjInfo" not in strict
    assert not any("BUG:FIXME" in t for t in strict)


def test_loose_filter_is_wider_than_strict(tmp_path: Path) -> None:
    """**对照**：严格是宽松的子集，且严格确实更少。

    这条防的是"把严格写成等于宽松" —— 那样噪声会全量漏进候选，
    而测试如果只测严格（不知道宽松有多少）就看不出来。

    ▲ 选的样本要**真的**体现两级判据的差别：``Puppet2D`` 这种裸标识符
      **连宽松都过不了**（见 `_looks_like_text` 里"单个 ASCII 词不算"
      那一条），所以它不能用来证明"宽松更宽"。这里用"像句子但含
      运行时报错词"的样本 —— 它过得了宽松，过不了严格的日志黑名单。
    """
    p = _make_assembly(
        tmp_path,
        [
            "Error loading the save file from disk.",  # 宽松留、严格拦
            "Activate the emergency destruction system.",  # 两级都留
            "Puppet2D",  # 两级都拦
        ],
    )
    A = AssemblyStrings(p)
    loose = {s.text for s in A.translatable()}
    strict = {s.text for s in A.dialogue_like()}
    assert strict <= loose, "严格必须是宽松的子集"
    assert "Error loading the save file from disk." in loose, (
        "含报错词的句子应当过得了宽松（宽松只用于统计）"
    )
    assert "Error loading the save file from disk." not in strict, (
        "含报错词的句子必须被严格拦住（翻下去会让玩家看到报错被翻译）"
    )
    assert "Puppet2D" not in loose, "裸标识符连宽松都该拦（实测这是 989/1231 的主要噪声）"
    assert len(strict) < len(loose)


# ---------------------------------------------------------------------------
# 拒绝路径
# ---------------------------------------------------------------------------


def test_non_pe_file_raises(tmp_path: Path) -> None:
    """非 PE 文件必须**明确抛错**。

    ⚠️ 不能"尽力而为"地返回空列表：上层会把空结果读成
    "这游戏没有可翻文本"，那是静默的错误结论。
    """
    p = tmp_path / "not-a-pe.dll"
    p.write_bytes(b"this is definitely not a PE file")
    with pytest.raises(ValueError, match="MZ"):
        AssemblyStrings(p).read()


def test_native_pe_without_cli_raises(tmp_path: Path) -> None:
    """有 PE 头但没有 CLI 头（原生 DLL）也要明确抛错。

    ▲ 可选头必须**够长**（PE32 至少到数据目录那一项，即 ≥ 96+8 字节），
      否则会先在"数据目录被截断"处报错 —— 那也是明确抛错，但测不到
      "没有 CLI 头"这条分支。第一版就写了 224 字节的 **DOS 头**、
      却没写可选头内容，于是提前截断。
    """
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    coff = bytearray(20)
    struct.pack_into("<HH", coff, 0, 0x14C, 0)  # 0 个节
    struct.pack_into("<HH", coff, 16, 224, 0x2102)
    opt = bytearray(224)  # PE32 可选头，magic=0x10b
    struct.pack_into("<H", opt, 0, 0x10B)
    # 数据目录第 15 项（CLI 头）故意留 0 ⇒ 原生程序集
    p = tmp_path / "native.dll"
    p.write_bytes(bytes(dos) + b"PE\x00\x00" + bytes(coff) + bytes(opt))
    with pytest.raises(ValueError, match="CLI|原生"):
        AssemblyStrings(p).read()


def test_truncated_us_heap_does_not_crash(tmp_path: Path) -> None:
    """堆被截断时应当**停在那里**，而不是抛异常或读出无穷数据。

    真实 DLL 可能是加壳/改过的，健壮性必须是"停"而不是"炸"。
    """
    p = _make_assembly(tmp_path, ["Activate the emergency destruction system."])
    raw = p.read_bytes()
    # 把文件尾部砍掉一半（顺手破坏 #US 数据）
    p.write_bytes(raw[: len(raw) - len(raw) // 4])
    got = AssemblyStrings(p).read()  # 不抛异常即可
    assert isinstance(got, list)
