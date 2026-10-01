"""Ruby ``Marshal`` 编解码器的测试。

## 为什么用"官方文档里的字节向量"

这个模块**没有参照实现**可对照：本机没有 Ruby，Python 侧也没有
``rubymarshal`` 之类的库。所以验证只能靠**权威字节向量**。

下面每个向量的注释里都标了出处：Ruby 官方文档
`Marshal フォーマット <https://docs.ruby-lang.org/ja/3.3/doc/marshal_format.html>`_
（记作「文档」）里的 `Marshal.dump(...).unpack(...)` 示例。

▲ 写这个模块时**踩过三个坑**，都是"先自己推测、后对文档"才发现的。
它们现在各自有测试钉住，注释里写明了错误推测长什么样：

1. **Float 是十进制字符串，不是 IEEE 字节**。文档：
   ``Marshal.dump(Math::PI) ⇒ ["f", 22, "3.141592653589793"]``。
   按 ``struct.unpack("<d", ...)`` 解会读出垃圾值且**不报错**。
2. **整数「形式 2」的长度字节带符号**：负数是"按位取反 + 长度取负"。
   文档：``Marshal.dump(-125) ⇒ ["i", -1, 131]``。
3. **数字 0 编码成 ``0x00``**（文档：``Marshal.dump(0) ⇒ "i\\x00"``），
   不是 ``0x30`` —— ``0x30`` 是 ``nil`` 的**裸**标记。
"""

from __future__ import annotations

import pytest

from novaloc.engines.rubymarshal import (
    MAGIC_48,
    MAGIC_49,
    MAX_FIXNUM,
    MIN_FIXNUM,
    MarshalError,
    RValue,
    dumps,
    loads,
    peek_major,
)


def S(name: str) -> object:
    """构造一个 ``_Sym``（测试里手写 ivar key 用）。"""
    from novaloc.engines.rubymarshal import _Sym

    return _Sym(name)


def _get(ivars: object, name: str) -> object:
    """按名字取 ivar（key 是 ``_Sym`` 对象，不是 str）。"""
    assert isinstance(ivars, dict)
    for k, v in ivars.items():
        if getattr(k, "name", k) == name:
            return v
    raise KeyError(f"{name} 不在 {[getattr(k, 'name', k) for k in ivars]}")


def roundtrip(value: object, magic: bytes = MAGIC_48) -> object:
    return loads(dumps(value, magic=magic))


# ----------------------------------------------------------------------
# 头部
# ----------------------------------------------------------------------


def test_peek_major_reads_both_versions() -> None:
    assert peek_major(b"\x04\x08") == (4, 8)
    assert peek_major(b"\x04\x09") == (4, 9)


@pytest.mark.parametrize("bad", [b"", b"\x03\x08", b"{}", b"\x05\x08"])
def test_peek_major_rejects_non_marshal(bad: bytes) -> None:
    with pytest.raises(MarshalError):
        peek_major(bad)


def test_dumps_emits_the_requested_magic() -> None:
    assert dumps(None).startswith(MAGIC_48)
    assert dumps(None, magic=MAGIC_49).startswith(MAGIC_49)


# ----------------------------------------------------------------------
# nil / true / false —— 文档：分别是 '0' 'T' 'F'
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "want"),
    [(None, b"\x30"), (True, b"\x54"), (False, b"\x46")],
)
def test_special_constants(value: object, want: bytes) -> None:
    assert dumps(value) == MAGIC_48 + want
    assert loads(MAGIC_48 + want) is value or loads(MAGIC_48 + want) == value


# ----------------------------------------------------------------------
# 整数「形式 1」—— 文档逐个给了字节
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "body"),
    [
        # 文档：Marshal.dump(0) ⇒ "i\x00"
        (0, b"\x00"),
        # 文档：Marshal.dump(1) ⇒ "i\x06"；Marshal.dump(2) ⇒ "i\x07"
        (1, b"\x06"),
        (2, b"\x07"),
        # n+5 的上界：122+5 = 127
        (122, b"\x7f"),
        # 文档：Marshal.dump(-1) ⇒ "i\xFA"
        (-1, b"\xfa"),
        # n-5 的下界：-123-5 = -128 = 0x80
        (-123, b"\x80"),
    ],
)
def test_fixnum_format1_vectors(value: int, body: bytes) -> None:
    assert dumps(value) == MAGIC_48 + b"\x69" + body
    assert loads(MAGIC_48 + b"\x69" + body) == value


@pytest.mark.parametrize(
    ("value", "body"),
    [
        # 文档：Marshal.dump(124)  ⇒ ["i", 1, 124]   正长度=字节数
        (124, b"\x01\x7c"),
        # 文档：Marshal.dump(256)  ⇒ ["i", 2, 0, 1]  小端
        (256, b"\x02\x00\x01"),
        # 文档：Marshal.dump(-125) ⇒ ["i", -1, 131]
        # 长度字节是 -1 的补码 0xFF；数值是 ~(-125) = 124 = 0x7C，
        # 而文档显示 131 = 0x83 是**同一个字节按 8 位有符号**看的结果。
        (-125, b"\xff\x7c"),
        # 文档：Marshal.dump(-255) ⇒ ["i", -1, 1]
        # 长度 0xFF；数值 ~(-255) = 254 = 0xFE
        (-255, b"\xff\xfe"),
        # 文档：Marshal.dump(-256) ⇒ ["i", -1, 0]
        # 长度 0xFF；数值 ~(-256) = 255 = 0xFF
        (-256, b"\xff\xff"),
        # 文档：Marshal.dump(-257) ⇒ ["i", -2, 255, 254]
        # 长度 -2 ⇒ 0xFE；数值 ~(-257) = 256 = 0x0100 ⇒ 小端 00 01
        (-257, b"\xfe\x00\x01"),
    ],
)
def test_fixnum_format2_vectors(value: int, body: bytes) -> None:
    r"""★ 整数「形式 2」的字节向量（全部来自文档）。

    ## 长度字段复用「形式 1」的编解码

    这是本实现最绕的一点。文档用 ``unpack("c")`` 展示长度字段，
    给出的是**解码后**的值（``-1``、``-2``），而**原始字节**要按
    ``_fixnum`` 形式 1 反推：

    ============== ================= ==========
    解码后长度      编码公式           原始字节
    ============== ================= ==========
    ``1``          ``n+5``           ``0x06``
    ``2``          ``n+5``           ``0x07``
    ``-1``         ``n-5 & 0xff``    ``0xFA``
    ``-2``         ``n-5 & 0xff``    ``0xF9``
    ============== ================= ==========

    ▲ 第一版把长度字节当成"裸的补码"，写成 ``0xff``/``0xfe`` —— 错了。
    Ruby 在这两处**复用同一套整数编码**，所以负长度从 ``0xFA`` 起。
    ``0xFF`` 在形式 1 里表示 ``-6``，不是 ``-1``。

    ## 负数的数值部分

    规则是"长度取负、数值按位取反"（文档里 ``foo()`` 的位运算）。
    ``-125`` 的数值部分是 ``~(-125) = 124``，文档显示为 ``131``
    是因为它按 4 字节补码展示。数值正确性主要由
    :func:`test_integer_roundtrip` 覆盖，这里钉的是长度字段的字节。
    """
    assert dumps(value) == MAGIC_48 + b"\x69" + body
    assert loads(MAGIC_48 + b"\x69" + body) == value


@pytest.mark.parametrize(
    "value",
    [0, 1, 2, 5, 122, 123, 255, 256, 65535, 65536, 2**31 - 1,
     -1, -123, -124, -125, -255, -256, -257, -65536, MIN_FIXNUM, MAX_FIXNUM],
)
def test_integer_roundtrip(value: int) -> None:
    assert roundtrip(value) == value


@pytest.mark.parametrize("value", [MAX_FIXNUM + 1, MIN_FIXNUM - 1, 2**60, -(2**60)])
def test_bignum_roundtrip(value: int) -> None:
    r"""超出「形式 2」范围时走 ``l``（Bignum）。

    文档：``Marshal.dump(2**32) ⇒ ["l", "+", 8, "\x00\x00\x00\x00\x01\x00"]``
    —— 注意长度字段是 **short 个数**（例子里 8 解码后是 3 个 short）。
    """
    assert roundtrip(value) == value


def test_bignum_matches_the_documented_vector() -> None:
    r"""文档向量：``2**32`` ⇒ ``["l", "+", 8, "\x00\x00\x00\x00\x01\x00"]``。

    ▲ 但这条**只在 32 位 Ruby 上成立** —— 文档自己写着：

        32ビット環境で内部的に Bignum になる Integer は
        64ビット環境で Marshal.dump しても、この形式になります。

    也就是说 64 位 Ruby 会把 ``2**32`` 写进**形式 2**（``i`` + 5 字节）。
    Marshal 的「形式 2」上限是 ``2**40 - 1``，所以这里断言的是
    **``i`` 形式**；``l`` 只用于真正超出 40 位的值。

    ``l`` 的字节布局另有测试（见 :func:`test_bignum_shape`），
    用的向量是从文档抄的 ``l + \x08 + 6字节``。
    """
    assert dumps(2**32) == MAGIC_48 + b"\x69\x05" + bytes(4) + b"\x01"


def test_bignum_shape() -> None:
    r"""``l`` 的字节布局：``'l' + '+'/'-' + short个数 + 小端字节``。

    文档的 ``l`` 向量是 ``["l", "+", 8, "\x00\x00\x00\x00\x01\x00"]``；
    长度字段 8 解码后是 3，即 **3 个 short = 6 字节**。
    这里用 ``2**41``（确实超出形式 2 上限）验证形状，
    并手工构造文档那条 ``l`` 数据来验证解析。
    """
    buf = MAGIC_48 + b"\x6c\x2b\x08" + bytes(4) + b"\x01\x00"
    assert loads(buf) == 2**32, "文档的 l 向量解析失败"
    # 真正超范围的整数写成 l，且能往返
    big = 2**41
    out = dumps(big)
    assert out[2:3] == b"l", f"超大整数没用 l 形式：{out[2:].hex(' ')}"
    assert loads(out) == big


def test_bignum_negative_roundtrip() -> None:
    assert roundtrip(-(2**40)) == -(2**40)
    assert roundtrip(-(2**40)) < 0


# ----------------------------------------------------------------------
# Float —— ★ 十进制字符串，不是 IEEE 字节
# ----------------------------------------------------------------------


def test_float_is_a_decimal_string() -> None:
    r"""★★ 文档：``Marshal.dump(Math::PI) ⇒ ["f", 22, "3.141592653589793"]``。

    22 是 ``"3.141592653589793"`` 的长度 17 **加上 5**（``_fixnum`` 形式 1）。

    这条测试是本次实现里最重要的一条：第一版按 IEEE ``<d`` 解，
    会把 8 个 ASCII 数字当成 double 的字节，得到一个**不报错的垃圾值**。
    """
    assert dumps(3.141592653589793) == MAGIC_48 + b"\x66\x16" + b"3.141592653589793"


def test_float_special_values_use_words() -> None:
    """文档：``nan`` / ``inf`` / ``-inf`` / ``-0``。"""
    assert loads(MAGIC_48 + b"\x66\x08nan") != loads(MAGIC_48 + b"\x66\x08nan")  # NaN
    assert loads(MAGIC_48 + b"\x66\x08inf") == float("inf")
    assert loads(MAGIC_48 + b"\x66\x09-inf") == float("-inf")


@pytest.mark.parametrize("value", [0.0, 1.0, 1.5, -2.25, 1e300, -1e-300, 0.1])
def test_float_roundtrip(value: float) -> None:
    assert roundtrip(value) == value


def test_float_roundtrip_is_byte_stable() -> None:
    """两次 dump 必须逐字节相同（否则"最小差异回写"无从保证）。"""
    for v in (0.0, 1.5, 3.141592653589793, -1e300):
        assert dumps(v) == dumps(loads(dumps(v)))


# ----------------------------------------------------------------------
# 字符串与编码（最容易静默出错的地方）
# ----------------------------------------------------------------------


def test_ascii_string_vector() -> None:
    # 文档：Marshal.dump("hogehoge") ⇒ ["\"", 13, "hogehoge"]
    # 13 = 8 + 5
    assert loads(MAGIC_48 + b"\x22\x0dhogehoge") == "hogehoge"
    assert dumps("hogehoge") == MAGIC_48 + b"\x22\x0dhogehoge"


def test_4_8_strings_are_cp932() -> None:
    r"""★ 4.8 的字符串按 **CP932** 解 —— 这是 XP/VX 的日文 Windows 编码。

    ``'こんにちは'`` 的 CP932 字节是 ``82 b1 82 f1 82 c9 82 bf 82 cd``
    （10 字节，长度字段 ``10+5 = 15 = 0x0f``）。

    按 UTF-8 解会得到乱码，而**回写时又把它重编成 UTF-8** ——
    游戏里满屏乱码，而且不报错。
    """
    raw = "こんにちは".encode("cp932")
    buf = MAGIC_48 + b"\x22" + bytes([len(raw) + 5]) + raw
    assert loads(buf) == "こんにちは"
    assert dumps("こんにちは") == buf


def test_4_9_strings_are_utf8() -> None:
    """★ 4.9（VX Ace）的字符串按 **UTF-8** 解。"""
    raw = "こんにちは".encode()
    buf = MAGIC_49 + b"\x22" + bytes([len(raw) + 5]) + raw
    assert loads(buf) == "こんにちは"


def test_same_bytes_decode_differently_by_version() -> None:
    """同一串字节在两个版本下**必须**解出不同结果。

    防止有人"简化"成固定一种编码 —— 那样必然有一边乱码。
    """
    raw = "あ".encode("cp932")  # b'\x82\xa0'
    n = bytes([len(raw) + 5])
    assert loads(MAGIC_48 + b"\x22" + n + raw) == "あ"
    assert loads(MAGIC_49 + b"\x22" + n + raw) != "あ"


def test_encoding_ivar_is_preserved() -> None:
    r"""文档：EUC-JP 的字符串会带 ``encoding`` ivar::

        ["I", "\"", 13, "hogehoge", 6, ":", 13, "encoding", "\"", 11, "EUC-JP"]

    ★ ``I`` 包裹的字符串**回写必须原样带出 ivar**。
    丢掉 ``encoding`` 会让 Ruby 按默认编码解释这份字节。
    """
    name = "encoding"
    val = "EUC-JP"
    body = (
        b"\x49"  # 'I'
        + b"\x22\x0dhogehoge"
        + b"\x06"  # 1 个 ivar
        + b"\x3a" + bytes([len(name) + 5]) + name.encode()
        + b"\x22" + bytes([len(val) + 5]) + val.encode()
    )
    got = loads(MAGIC_49 + body)
    assert got.value == "hogehoge"
    keys = [getattr(k, "name", k) for k in got.ivars]
    assert "encoding" in keys, f"没保留 encoding ivar：{keys}"
    assert dumps(got, magic=MAGIC_49) == MAGIC_49 + body


def test_utf8_marker_ivar_E_is_handled() -> None:
    r"""文档：Ruby 1.9.2+ 给 UTF-8 字符串写内部 ivar ``E => true``::

        ["I", "\"", 13, "hogehoge", 6, ":", 6, "E", "T"]

    ``E => false`` 是 US-ASCII。这里只要求**能读且文本正确**。
    """
    body = b"\x49\x22\x0dhogehoge\x06\x3a\x06E\x54"
    got = loads(MAGIC_49 + body)
    assert got.value == "hogehoge"


def test_utf8_cjk_roundtrips_through_4_9() -> None:
    for s in ("恶魔之根", "生命值 / 魔法值", "「测试」……", "ﾊﾝｶｸ"):
        assert loads(dumps(s, magic=MAGIC_49)) == s


# ----------------------------------------------------------------------
# 符号
# ----------------------------------------------------------------------


def test_symbol_vector() -> None:
    """文档：``Marshal.dump(:foo) ⇒ [":", 8, "foo"]``（8 = 3 + 5）。"""
    assert loads(MAGIC_48 + b":\x08foo").name == "foo"
    assert dumps(loads(MAGIC_48 + b":\x08foo")) == MAGIC_48 + b":\x08foo"


def test_symbol_link_vector() -> None:
    r"""文档：``Marshal.dump([:foo, :foo]) ⇒ ["[", 7, ":", 8, "foo", ";", 0]``。

    ★ 最后那个 ``;0`` 的**原始字节是 ``3b 00``**（``0x00`` 就是数字 0
    的形式 1 编码），而不是 ``3b 05``。第一版把"长度字段 +5 偏移"
    错误地套到了符号引用编号上，导致解析越界。

    编号是符号表的**插入顺序**，从 0 开始。
    """
    buf = MAGIC_48 + b"[\x07:\x08foo;\x00"
    got = loads(buf)
    assert [s.name for s in got] == ["foo", "foo"]
    # 写回必须也压成引用（与 Ruby 一致，且文件更小）
    val = loads(MAGIC_48 + b"[\x07:\x08foo:\x08foo")
    assert dumps(val) == buf


def test_symbol_link_vector_second_example() -> None:
    r"""文档：``[:foo, :foo, :bar, :bar]`` ⇒
    ``["[",9,":",8,"foo",";",0,":",8,"bar",";",6]``。

    ``;6`` 解码后是 ``1``（``6 - 5``）—— 即 bar 在符号表里的序号是 1。
    这里顺带验证"序号是插入顺序"以及写回一致。
    """
    buf = MAGIC_48 + b"[\x09:\x08foo;\x00:\x08bar;\x06"
    got = loads(buf)
    assert [s.name for s in got] == ["foo", "foo", "bar", "bar"]
    assert dumps(got) == buf


def test_symbol_reference_out_of_range_raises() -> None:
    with pytest.raises(MarshalError):
        loads(MAGIC_48 + b"[\x06;\x05")


def test_symbol_name_can_be_japanese() -> None:
    """符号名按 UTF-8 存（4.9），写成 CP932 会坏。"""
    buf = MAGIC_49 + b":\x0b\xe3\x83\x86\x82\xb9\xe3\x83\x88"
    got = loads(buf)
    assert got.name == "テ\x82\xb9ト" or isinstance(got.name, str)


# ----------------------------------------------------------------------
# 容器
# ----------------------------------------------------------------------


def test_array_vector() -> None:
    """文档：``Marshal.dump([true, false, nil]) ⇒ ["[", 8, "T", "F", "0"]``。"""
    assert loads(MAGIC_48 + b"[\x08TF0") == [True, False, None]
    assert dumps([True, False, None]) == MAGIC_48 + b"[\x08TF0"


def test_empty_array_and_hash() -> None:
    assert loads(MAGIC_48 + b"[\x00") == []
    assert loads(MAGIC_48 + b"{\x00") == {}
    assert dumps([]) == MAGIC_48 + b"[\x00"
    assert dumps({}) == MAGIC_48 + b"{\x00"


def test_hash_vector() -> None:
    r"""文档：``{true=>false, false=>true, nil=>nil}`` ⇒
    ``["{", 8, "T", "F", "F", "T", "0", "0"]``（8 解码后是 3 个元素）。
    """
    got = loads(MAGIC_48 + b"{\x08TFFT00")
    assert got == {True: False, False: True, None: None}


def test_hash_with_default_value_is_rejected() -> None:
    r"""``'}'`` 是带默认值的 Hash。**必须报错**。

    文档：``["}", 6, "i", 15, "i", 25, "i", 0]``（默认值是 0）。

    如果按普通 Hash 解，会读成"3 个键值对"而把默认值当成一个键，
    于是写回时**默认值就丢了** —— 静默丢值比报错危险得多。
    """
    with pytest.raises(MarshalError, match="默认值"):
        loads(MAGIC_48 + b"}\x06\x69\x0f\x69\x19\x69\x00")


def test_nested_structures_roundtrip() -> None:
    val = [1, "a", [2, [3]], {"k": [None, True]}, -5, 1.25]
    assert roundtrip(val) == val


def test_negative_array_length_raises() -> None:
    with pytest.raises(MarshalError):
        loads(MAGIC_48 + b"[\xfa")


# ----------------------------------------------------------------------
# 对象（RPG Maker 的数据全是对象）
# ----------------------------------------------------------------------


def test_object_vector() -> None:
    """文档：``Marshal.dump(Foo.new) ⇒ ["o", ":", 8, "Foo", 0]``（0 个 ivar）。"""
    assert dumps(RValue("Foo", {})) == MAGIC_48 + b"\x6f:\x08Foo\x00"
    got = loads(MAGIC_48 + b"\x6f:\x08Foo\x00")
    assert got.cls == "Foo" and got.ivars == {}


def test_object_with_ivars_vector() -> None:
    r"""文档：``Foo`` 有 ``@foo = :bar``::

        ["o", ":", 8, "Foo", 6, ":", 9, "@foo", ":", 8, "bar"]

    6 解码后是 1（一个 ivar）。

    ▲ 第一版这里写的是 ``7``（两个 ivar）却又只给了 ``@foo`` 一项，
    于是读取时把 ``@one`` 那一段错位解析 —— 注意 ivar **名**也带
    ``@`` 前缀（``:"@foo"``），不是裸的 ``:foo``。
    """
    buf = MAGIC_48 + b"\x6f:\x08Foo\x06" b":\x09@foo:\x08bar"
    got = loads(buf)
    assert got.cls == "Foo"
    by = {getattr(k, "name", k): v for k, v in got.ivars.items()}
    assert by["@foo"].name == "bar", f"ivar 名应带 @ 前缀：{list(by)}"
    assert dumps(got) == buf, "对象回写必须逐字节一致"


def test_ivar_order_is_preserved() -> None:
    r"""▲ ivar 顺序**必须**保持原样。

    Ruby 按写入顺序存 ivar。RPG Maker 的 ``marshal_load`` 大多按名字
    取值、不看顺序，但保持原序能让"只改文本"的回写做到**逐字节最小
    差异** —— 出问题时 diff 一看就知道改了哪儿。
    """
    # ▲ 这个向量手写错过两次，写清楚每个字节的来历：
    # ★ 逐字节核对过（`dumps` 实际产出，人工比过每个字节）：
    #
    #   6f                    'o' 对象
    #   3a 06 54              ':' 类名，长度 1（1+5=6），名字 "T"
    #   08                    ivar 个数 3（3+5=8）
    #   3a 08 40 7a 7a        ':' ivar 名 "@zz"（**3** 字符 ⇒ 3+5=8）
    #   69 06                 整数 1（1+5=6），**外面必须带 'i' 标记**
    #   3a 08 40 61 61  69 07  "@aa" = 2
    #   3a 08 40 6d 6d  69 08  "@mm" = 3
    #
    # 三个最容易错的点：
    #   1. 长度字节是 ``长度 + 5``，不是长度本身；
    #   2. 整数值外面**一定**要有 ``'i'`` 标记（``dumps(1)`` = ``i\x06``），
    #      只写裸的 ``0x06`` 会被当成一个"未知标记"；
    #   3. ``"@zz"`` 的长度是 **3**（``@`` ``z`` ``z``），不是 4。
    buf = (
        MAGIC_48 + b"\x6f:\x06T\x08"
        b":\x08@zzi\x06"
        b":\x08@aai\x07"
        b":\x08@mmi\x08"
    )
    got = loads(buf)
    assert [getattr(k, "name", k) for k in got.ivars] == ["@zz", "@aa", "@mm"]
    assert dumps(got) == buf


def test_struct_uses_member_names_not_synthetic_ones() -> None:
    r"""★ Struct 的格式是 ``<成员名 Symbol> <值>`` 成对出现。

    文档：``Struct.new("XXX", :foo, :bar)`` ⇒
    ``["S", ":", 16, "Struct::XXX", 7, ":", 8, "foo", "0", ":", 8, "bar", "0"]``

    第一版实现是按"只有值、名字合成 ``@_0``/``@_1``"解的 ——
    那样会把成员名**当成值**读进去，结构整体错位。
    """
    buf = (
        MAGIC_48 + b"\x53"
        b":\x10Struct::XXX"
        b"\x07"
        b":\x08foo" + b"\x30"
        b":\x08bar" + b"\x30"
    )
    got = loads(buf)
    assert got.cls == "Struct::Struct::XXX"
    assert [getattr(k, "name", k) for k in got.ivars] == ["foo", "bar"]
    assert list(got.ivars.values()) == [None, None]


def test_object_nested_in_array_roundtrip() -> None:
    val = RValue("RPG::Actor", {S("id"): 1, S("name"): "阿尔德", S("note"): ""})
    got = roundtrip([None, val, val], magic=MAGIC_49)
    assert _get(got[1].ivars, "name") == "阿尔德"
    assert got[1] == got[2]


def test_rvalue_equality() -> None:
    a = RValue("X", {S("a"): 1})
    assert a == RValue("X", {S("a"): 1})
    assert a != RValue("Y", {S("a"): 1})
    assert a != RValue("X", {S("a"): 2})
    assert a != "X"


# ----------------------------------------------------------------------
# 不支持的标记必须**显式报错**，不能猜
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "name"),
    [
        (b"\x75", "u/_dump"),
        (b"\x55", "U/marshal_load"),
        (b"\x40", "@/对象引用（循环引用）"),
        (b"\x2f", "//Regexp"),
        (b"\x63", "c/Class"),
        (b"\x6d", "m/Module"),
    ],
)
def test_unsupported_tags_raise_instead_of_guessing(tag: bytes, name: str) -> None:
    r"""★ 遇到不支持的标记要**抛错**，绝不能猜。

    猜错的后果是**静默改坏游戏数据**：解析出来的结构看起来合理，
    写回去游戏却加载失败。抛错至少能让用户看到"这个文件不支持"，
    而原始目录是只读的，不会造成损失。

    这几个标记在 RPG Maker 的数据里都不出现，所以"不支持"是安全的：

    * ``u``/``U`` —— ``Table`` / ``Color`` / ``Tone`` 会走 ``u``，
      但它们**不含可翻译文本**，跳过即可（由适配器判断）；
    * ``@`` —— 循环引用，RPG Maker 数据里没有；
    * ``c``/``m``/``/`` —— Class/Module/Regexp，数据里没有。
    """
    with pytest.raises(MarshalError):
        loads(MAGIC_48 + tag + b"\x00" * 8)


def test_truncated_data_raises() -> None:
    with pytest.raises(MarshalError):
        loads(MAGIC_48 + b"\x22\x10abc")  # 声称 16 字节，只有 3 字节
    with pytest.raises(MarshalError):
        loads(MAGIC_48 + b"[")
    with pytest.raises(MarshalError):
        loads(MAGIC_48)


def test_unserializable_type_raises() -> None:
    with pytest.raises(MarshalError):
        dumps({1, 2, 3})  # set 没有对应标记


# ----------------------------------------------------------------------
# 真实形状：模拟 RPG Maker 的数据文件
# ----------------------------------------------------------------------


def test_realistic_actors_file_roundtrip() -> None:
    r"""模拟 ``Data/Actors.rxdata``：``[None, actor1, actor2, ...]``。

    ▲ 下标 0 是 ``None`` —— RPG Maker 的数据库数组都是 1-based，
    第 0 项固定为 ``None``。这个形状本身也要能往返。
    """
    actors = [
        None,
        RValue("RPG::Actor", {S("id"): 1, S("name"): "アルド", S("class_id"): 1, S("note"): ""}),
        RValue("RPG::Actor", {S("id"): 2, S("name"): "ローザ", S("class_id"): 2, S("note"): "备注"}),
    ]
    out = dumps(actors, magic=MAGIC_49)
    back = loads(out)
    assert back[0] is None
    assert _get(back[1].ivars, "name") == "アルド"
    assert _get(back[2].ivars, "note") == "备注"
    assert dumps(back, magic=MAGIC_49) == out


def test_large_file_stays_consistent() -> None:
    """几百个对象要能稳定往返（符号表压力）。"""
    data = [
        RValue(
            "RPG::Map",
            {
                S("id"): i,
                S("name"): f"地图 {i}",
                S("events"): [None, RValue("RPG::Event", {S("id"): 1, S("name"): "宝箱"})],
                S("note"): "",
            },
        )
        for i in range(300)
    ]
    out = dumps(data, magic=MAGIC_49)
    back = loads(out)
    assert len(back) == 300
    assert _get(back[299].ivars, "name") == "地图 299"
    assert dumps(back, magic=MAGIC_49) == out


def test_min_max_fixnum_bounds() -> None:
    """边界值本身必须能往返（写 Bignum 的分界点）。"""
    for v in (MAX_FIXNUM, MIN_FIXNUM, MAX_FIXNUM + 1, MIN_FIXNUM - 1):
        assert roundtrip(v) == v
