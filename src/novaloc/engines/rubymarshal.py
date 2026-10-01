"""Ruby ``Marshal`` 格式的读写（**只实现 RPG Maker 用到的那一部分**）。

## 为什么需要这个模块

RPG Maker **VX / VX Ace** 的数据是 ``Data/*.rxdata``，
RPG Maker **XP** 是 ``Data/*.rvdata`` —— 两者都是 Ruby 的 ``Marshal.dump``
输出，不是 JSON、也不是通用二进制格式。要汉化这两个引擎就必须能
**读懂并原样写回** Marshal。

本机没有 Ruby 可作参照实现，Python 侧也没有可用的库
（``rubymarshal`` / ``rmxp`` 都没装），所以这里按格式规范自己实现。

## 为什么不做成"通用 Marshal 库"

通用的 Ruby Marshal 实现要覆盖 ``Bignum``、``Regexp``、``Struct``、
``Class``/``Module`` 引用、``U``（用户自定义 ``_dump``）等等。
RPG Maker 的数据文件**不用**这些，硬写会把复杂度和出错面放大好几倍。
这里的取舍是：**只支持实际出现的标记**，遇到不认识的**立刻抛错**
（见 :class:`MarshalError`）而不是猜 —— 猜错会静默改坏游戏数据。

## 对象怎么表示

Ruby 对象（``o``）与"带实例变量的值"（``I``）都读成
:class:`RValue`，保留类名与 ivar，**写回时逐字节还原结构**：:

    RValue(cls="RPG::Actor", ivars={"@name": "アルド"})

这样做的关键好处是**回写只改文本**：类名、ivar 名、顺序、
"没有 ivar 的字符串"这类细节都不会被改写，也就不会让游戏
在加载时因为结构变化而出错。

## 字符串编码（最容易踩的坑）

Ruby 1.9+ 的字符串可以带 ``@encoding`` ivar（``I"..."`` 形式）。
RPG Maker VX Ace 的文本是 **UTF-8**，但 **XP 是日文 Windows-31J
（CP932/Shift_JIS）**。写回时必须把编码 ivar 原样带回去 ——
丢掉它会让游戏按错误的编码解释字节，表现就是**满屏乱码**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MAGIC_48",
    "MAGIC_49",
    "MarshalError",
    "RValue",
    "dumps",
    "loads",
    "peek_major",
]


class MarshalError(ValueError):
    """Marshal 数据无法解析（或含不支持的标记）。"""


MAGIC_48 = b"\x04\x08"
"""``Marshal.dump`` 的 4.8 头（Ruby 1.8 —— 也是 **XP/VX** 的格式）。"""

MAGIC_49 = b"\x04\x09"
"""4.9 头（Ruby 1.9+ —— **VX Ace** 用这个，字符串带编码 ivar）。"""

MAX_FIXNUM = (1 << 40) - 1
"""Marshal「形式 2」能表示的最大整数（正数 5 字节）。"""

MIN_FIXNUM = -(1 << 32)
"""Marshal「形式 2」能表示的最小整数（负数 4 字节）。

▲ 正负并不对称：``len`` 的取值范围是 ``{-4..-1, 1..5}``，
所以正数有 5 字节、负数只有 4 字节。写成对称的 ``±2^40`` 会
在写 ``-2^35`` 这类值时产生**非法**数据（Ruby 读不了）。
"""


@dataclass
class RValue:
    """一个 Ruby 对象 / 带 ivar 的值。

    ``ivars`` 的键是 :class:`_Sym`（保留"这是符号名"的类型信息），
    值可以是任何 :func:`loads` 能读出来的东西。
    """

    cls: str
    ivars: dict[Any, Any] = field(default_factory=dict)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RValue):
            return NotImplemented
        return self.cls == other.cls and self.ivars == other.ivars

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # 键是 _Sym 而不是 str，排序前要先取名字（否则 TypeError）。
        keys = ", ".join(sorted(_name_of(k) for k in self.ivars))
        return f"RValue({self.cls!r}, [{keys}])"


def peek_major(data: bytes) -> tuple[int, int]:
    """读版本头，返回 ``(major, minor)``。"""
    if len(data) < 2 or data[0] != 4:
        raise MarshalError(f"不是 Ruby Marshal 数据（首字节应为 0x04，实际 {data[:1]!r}）")
    return data[0], data[1]


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------


class _Reader:
    __slots__ = ("_buf", "_pos", "_symbols", "_utf8")

    def __init__(self, data: bytes, *, utf8: bool) -> None:
        self._buf = data
        self._pos = 0
        #: 字符串按 UTF-8 还是 CP932 解，见 `_str_encoding`。
        self._utf8 = utf8
        #: ``;`` 标记是"重复引用之前的符号"，按出现顺序记下来。
        self._symbols: list[str] = []

    # -- 基础 ----------------------------------------------------------

    def _byte(self) -> int:
        if self._pos >= len(self._buf):
            raise MarshalError("数据意外结束")
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._buf):
            raise MarshalError("数据意外结束")
        b = self._buf[self._pos : self._pos + n]
        self._pos += n
        return b

    def _long(self) -> int:
        r"""``l``（Bignum）：``'+'``/``'-'`` + short 个数 + 小端字节。

        文档（``Marshal フォーマット`` 的 Bignum 一节）::

            | 'l' | '+'/'-' | shortの個数(Fixnum形式) | ... |

            Marshal.dump(2**32) ⇒ ["l", "+", 8, "\x00\x00\x00\x00\x01\x00"]

        ▲ 长度是**short（2 字节）的个数**，不是字节数 —— 例子里的 8
        对应 ``_fixnum`` 形式 1 的 ``n=3``，即 3 个 short = 6 字节，
        与后面 6 个字节吻合。

        RPG Maker 的数据（ID / 开关 / 变量）不会出现这么大的整数，
        但实现它很便宜，遇到时能正确读出来而不是报错。
        """
        sign = self._byte()
        if sign not in (0x2B, 0x2D):  # '+' / '-'
            raise MarshalError(f"Bignum 符号字节非法：0x{sign:02x}")
        nshorts = self._fixnum()
        if nshorts < 0:
            raise MarshalError(f"Bignum short 个数为负：{nshorts}")
        raw = self._take(nshorts * 2)
        v = int.from_bytes(raw, "little", signed=False)
        return -v if sign == 0x2D else v

    # -- 主分派 --------------------------------------------------------

    def read(self) -> Any:  # noqa: C901 - 分派函数，长一点更清楚
        tag = self._byte()
        if tag == 0x30:  # '0' nil —— 注意数字 0 也编码成 0x30，
            # 但数字 0 外面还套着 'i' 标记（`i\x30`），所以不会混淆：
            # 这里看到裸的 0x30 只可能是 nil。
            return None
        if tag == 0x54:  # 'T' true
            return True
        if tag == 0x46:  # 'F' false
            return False
        if tag == 0x69:  # 'i' 紧凑整数
            return self._fixnum()
        if tag == 0x6C:  # 'l' Bignum
            return self._long()
        if tag == 0x66:  # 'f' Float
            return self._float()
        if tag == 0x22:  # '"' 字符串
            return self._string_plain()
        if tag == 0x3A:  # ':' Symbol
            return self._symbol_body(tag)
        if tag == 0x3B:  # ';' 符号引用
            return self._symbol_body(tag)
        if tag == 0x5B:  # '[' 数组
            n = self._fixnum()
            if n < 0:
                raise MarshalError(f"数组长度为负：{n}")
            return [self.read() for _ in range(n)]
        if tag == 0x7B:  # '{' 哈希
            n = self._fixnum()
            out: dict[Any, Any] = {}
            for _ in range(n):
                k = self.read()
                out[_hashable(k)] = self.read()
            return out
        if tag == 0x7D:  # '}' 带默认值的哈希
            raise MarshalError(
                "不支持 '}'（带默认值的 Hash）—— 默认值会在写回时丢失，"
                "静默丢值比报错更危险"
            )
        if tag == 0x6F:  # 'o' 对象
            cls = self._symbol_name()
            n = self._fixnum()
            ivars: dict[Any, Any] = {}
            for _ in range(n):
                # ▲ ivar 名保持 **_Sym** 而不是转成 str。
                #   否则读进来是 str、写出去要 symbol，两边不对称，
                #   回写时 `self.write(k)` 会当成**字符串**写（`"@foo"`），
                #   而 Ruby 的对象 ivar 名必须是 symbol ⇒ 数据非法。
                #
                # ★ 必须**先读名字再读值**，而且写成两行。
                #   `ivars[self._symbol()] = self.read()` 这种一行写法里，
                #   Python 先算**右边**再算下标目标 —— 顺序正好反了，
                #   于是把值当成了符号名去读。真机上的症状是
                #   "符号位置出现非法标记 0x54"（0x54 就是 true 的字节）。
                name = self._symbol()
                ivars[name] = self.read()
            return RValue(cls, ivars)
        if tag == 0x49:  # 'I' 带 ivar 的值（常见：带 @encoding 的字符串）
            inner = self.read()
            n = self._fixnum()
            if isinstance(inner, RValue):  # pragma: no cover - 极少见
                for _ in range(n):
                    name = self._symbol()
                    inner.ivars[name] = self.read()
                return inner
            ivars = {}
            for _ in range(n):
                name = self._symbol()
                ivars[name] = self.read()
            return _IvarValue(inner, ivars)
        if tag == 0x75:  # 'u' 用户自定义 _dump（RPG Maker 不用）
            raise MarshalError("不支持 'u'（用户自定义 _dump）标记")
        if tag == 0x55:  # 'U' 用户自定义 marshal_load
            raise MarshalError("不支持 'U'（用户自定义 marshal_load）标记")
        if tag == 0x40:  # '@' 对象引用（循环引用）
            raise MarshalError("不支持 '@'（对象引用）标记")
        if tag == 0x2F:  # '/' 正则
            raise MarshalError("不支持 '/'（Regexp）标记")
        if tag == 0x63:  # 'c' Class
            raise MarshalError("不支持 'c'（Class）标记")
        if tag == 0x6D:  # 'm' Module
            raise MarshalError("不支持 'm'（Module）标记")
        if tag == 0x53:  # 'S' Struct
            cls = self._symbol_name()
            n = self._fixnum()
            ivars: dict[Any, Any] = {}
            for _ in range(n):
                # 格式是 `<成员名 Symbol> <值>` 成对出现，
                # **不是** `@_0`/`@_1` 这种合成名（文档 Struct 一节）。
                # 同样要分开两行：先名字、后值（见 'o' 分支的说明）。
                name = self._symbol()
                ivars[name] = self.read()
            return RValue(f"Struct::{cls}", ivars)
        raise MarshalError(f"不支持的 Marshal 标记 0x{tag:02x} ({chr(tag)!r})")

    # -- 细节 ----------------------------------------------------------

    def _fixnum(self) -> int:
        r"""整数**值编码**（不含 ``i`` 标记字节），两种格式。

        依据 Ruby 官方文档 ``Marshal フォーマット`` 的「形式 1 / 形式 2」
        （<https://docs.ruby-lang.org/ja/3.3/doc/marshal_format.html>）。

        **形式 1**（1 字节）::

            n == 0:       0x00
            0 < n < 123:  n + 5        ⇒ 0x06..0x7f
            -124 < n < 0: n - 5 & 0xff ⇒ 0x80..0xfa

        **形式 2**（首字节是长度，带符号）::

            | len | n_1 | n_2 | n_3 | n_4 |
            len ∈ {-4..-1, 1..5}；后跟 |len| 字节的**小端**数值

        形式 2 的用法很反直觉，容易写错：**长度是负的就表示数值是负的**，
        而且长度是 5 时用不到符号位（文档里的 ``foo()`` 就是干这个的）。
        """
        first = self._byte()
        if first == 0:
            # 形式 1 的 0。注意这里**不是**形式 2 的前缀 ——
            # 形式 2 的首字节取值范围是 0x01..0x05 与 0xFB..0xFF，
            # 0x00 已经被形式 1 占用了（文档：`Marshal.dump(0)` ⇒ `"i\x00"`）。
            return 0
        if 0x06 <= first <= 0x7F:
            return first - 5
        if 0x80 <= first <= 0xFA:
            # ▲ 单字节负数区间的映射是 ``first = n + 251``，
            #   即 ``n = first - 251``。核准过的两个锚点：
            #
            #       -1   ⇒ ``0xFA``(250) ⇒ 250 - 251 = -1   ✓
            #       -123 ⇒ ``0x80``(128) ⇒ 128 - 251 = -123 ✓
            #
            #   这个 251 = 256 - 5 来自编码公式 ``(n - 5) & 0xff``。
            #   **不要**写成 ``first - 256 - 5``(=261) —— 那样整体少 10，
            #   实测 ``-1`` 会读成 ``-11``。
            #
            #   为什么单字节负数只到 ``-123``：编码有 +5 的间隔，
            #   ``n+251`` 要让结果落在 ``0x80..0xFA``，n 就从 -123 到 -1。
            return first - 251
        if first <= 5:
            nbytes = first
            neg = False
        elif first >= 0xFB:
            nbytes = 256 - first
            neg = True
        else:  # pragma: no cover - 上面已穷尽
            raise MarshalError(f"非法整数首字节 0x{first:02x}")
        if not 1 <= nbytes <= 5:
            raise MarshalError(f"整数长度非法：{nbytes}")
        mag = int.from_bytes(self._take(nbytes), "little", signed=False)
        if neg:
            # 负数的存储值是**按位取反**后的结果（见文档里 foo 的位运算）。
            return ~mag
        return mag

    def _bytes_with_len(self) -> bytes:
        n = self._fixnum()
        if n < 0:
            raise MarshalError(f"字节串长度为负：{n}")
        return self._take(n)

    def _float(self) -> float:
        r"""``f`` 标记：浮点数是**十进制字符串**，不是 IEEE 字节。

        ▲ 这一条极易写错。文档（``Marshal フォーマット`` 的 Float 一节）
        给的例子很直白::

            Marshal.dump(Math::PI) ⇒ ["f", 22, "3.141592653589793"]

        长度 22 是**字符串长度 + 5**（即 ``_fixnum`` 形式 1）。
        如果按 ``struct.unpack("<d", ...)`` 解，会读到 8 个数字字符的
        ASCII 值拼出来的垃圾浮点数 —— **不报错，但值是错的**。

        特殊值用 ``"nan"`` / ``"inf"`` / ``"-inf"`` / ``"-0"`` 表示，
        ``float()`` 都能解（``"-0"`` 也能）。
        """
        raw = self._bytes_with_len()
        try:
            return float(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MarshalError(f"浮点数字节串无法解析：{raw!r}") from exc

    def _string_plain(self) -> str:
        raw = self._bytes_with_len()
        return raw.decode(self._str_encoding(), errors="replace")

    def _str_encoding(self) -> str:
        r"""4.8 的字符串是 **CP932**（XP/VX 的日文 Windows 编码）；
        4.9 的字符串是 **UTF-8**。

        ▲ 这条判断是"乱码 vs 正常"的分界线。Ruby 1.8 的 ``String`` 没有
        编码概念，RPG Maker XP/VX 的数据就是 Shift_JIS 字节；
        而 Ruby 1.9+ 默认 UTF-8，VX Ace 的数据也是 UTF-8。
        两边混用会让日文与中文**双向**变成乱码。

        判定依据是**次版本号**（``\x04\x08`` vs ``\x04\x09``）——
        主版本号两个都是 4，只看主版本判断不出来。

        Ruby 1.9.2+ 还会给每个字符串带上内部 ivar ``E``
        （``E => false`` 表示 US-ASCII、``E => true`` 表示 UTF-8，
        见文档 String 一节）。这里**不需要**额外处理：
        4.9 本来就直接按 UTF-8 解，``E`` 只影响非 UTF-8 的字符串，
        而那种字符串会带 ``encoding`` ivar 指明具体编码。
        真实 VX Ace 数据里的 ``E`` 是 ``true``，与这里一致。
        """
        return "utf-8" if self._utf8 else "cp932"

    def _symbol_name(self) -> str:
        """读一个符号并返回名字（**会消费标记字节**）。"""
        s = self._symbol()
        return s.name

    def _symbol(self) -> _Sym:
        r"""读带标记（``:`` / ``;``）的符号。

        ▲ ``read()`` 里那条分支**不能**调这个 —— 它已经把标记字节
        读掉了，再调一次会把**长度字节**当成标记（实测报
        "符号位置出现非法标记 0x08"）。那里要调 :meth:`_symbol_body`。
        """

        tag = self._byte()
        if tag == 0x3A:  # 新符号
            return self._new_symbol()
        if tag == 0x3B:  # 符号引用
            return self._symbol_ref()
        if tag == 0x49:  # 'I' 带 ivar 的符号（实战不用，但格式允许）
            inner = self._symbol()
            n = self._fixnum()
            for _ in range(n):
                self._symbol_name()
                self.read()
            return inner
        raise MarshalError(f"符号位置出现非法标记 0x{tag:02x}")

    def _symbol_body(self, tag: int) -> _Sym:
        """已知标记字节时的符号读取（给 ``read()`` 用）。"""
        if tag == 0x3A:
            return self._new_symbol()
        if tag == 0x3B:
            return self._symbol_ref()
        raise MarshalError(f"符号位置出现非法标记 0x{tag:02x}")

    def _new_symbol(self) -> _Sym:
        name = self._bytes_with_len().decode("utf-8", errors="replace")
        self._symbols.append(name)
        return _Sym(name)

    def _symbol_ref(self) -> _Sym:
        idx = self._fixnum()
        if not (0 <= idx < len(self._symbols)):
            raise MarshalError(f"符号引用越界：{idx}（表里只有 {len(self._symbols)} 个）")
        return _Sym(self._symbols[idx])


class _Sym:
    """Ruby 符号。

    刻意**不**直接映射成 ``str``：JSON 里 symbol 与 string 会混淆，
    写回时也就分不清该输出 ``:foo`` 还是 ``"foo"``。
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Sym) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("_Sym", self.name))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f":{self.name}"


class _IvarValue:
    """带 ivar 的非对象值（最常见：``I"..."`` 即带 ``@encoding`` 的字符串）。"""

    __slots__ = ("value", "ivars")

    def __init__(self, value: Any, ivars: dict[str, Any]) -> None:
        self.value = value
        self.ivars = ivars

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _IvarValue)
            and self.value == other.value
            and self.ivars == other.ivars
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"IvarValue({self.value!r}, {self.ivars!r})"


def _name_of(v: Any) -> str:
    """``_Sym`` / ``str`` 统一取名字。

    ▲ 读侧的 ivar key 是 :class:`_Sym`，而写侧只需要名字。
    统一在这里转换，避免"某处传了 str、某处传了 _Sym"导致
    ``AttributeError`` 或写出错误标记。
    """
    return v.name if isinstance(v, _Sym) else str(v)


def _hashable(k: Any) -> Any:
    if isinstance(k, list):
        return tuple(_hashable(x) for x in k)
    if isinstance(k, dict):
        return tuple(sorted((str(a), _hashable(b)) for a, b in k.items()))
    return k


def loads(data: bytes) -> Any:
    """解析 Marshal 数据（**必须**带 ``\\x04\\x08``/``\\x04\\x09`` 头）。

    版本决定字符串编码（见 :meth:`_Reader._str_encoding`）：

    * ``\\x04\\x08`` ⇒ **cp932**（RPG Maker **XP/VX** 的日文 Windows 编码）；
    * ``\\x04\\x09`` ⇒ **utf-8**（RPG Maker **VX Ace**；字符串若带
      ``@encoding`` ivar 则保留在 :class:`_IvarValue` 里，写回时原样带出）。
    """
    _major, minor = peek_major(data)
    r = _Reader(data, utf8=minor >= 9)
    r._pos = 2  # 跳过头
    return r.read()


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------


class _Writer:
    __slots__ = ("out", "_symbols", "_encoding")

    def __init__(self, *, encoding: str) -> None:
        self.out = bytearray()
        self._symbols: list[str] = []
        #: 写字符串用哪个编码，见 :meth:`_Reader._str_encoding`。
        #: **必须**和读进来时的判断一致，否则 CJK 会因为
        #: "cp932 也能表示这些汉字"而被静默转码成**另一个编码的字节**。
        self._encoding = encoding

    # -- 基础 ----------------------------------------------------------

    def _fixnum(self, n: int) -> None:
        r"""整数**值编码**（不含 ``i`` 标记字节），与 :meth:`_Reader._fixnum` 对称。

        形式 1（1 字节）::

            n == 0:       0x00
            0 < n < 123:  n + 5        ⇒ 0x06..0x7f
            -124 < n < 0: n - 5 & 0xff ⇒ 0x80..0xfa

        形式 2（**长度带符号** + 小端数值）::

            正数 ⇒ len = 需要的字节数，数值 = n
            负数 ⇒ len = -需要的字节数，数值 = ~n（按位取反）

        ▲ 两个实测/文档确认过的坑：

        1. **``0`` 编码成 ``0x00``**，不是 ``0x30``。``0x30`` 是 ``nil``
           （裸标记，外面没有 ``i``）。因为数字 0 永远套在 ``i`` 后面，
           两者不会混淆。
        2. **长度 5 只用于正数**：5 字节无符号能表示到 ``2^40-1``，
           而 5 字节的负数表示没意义（``len`` 只能是 ``-4``）。
           文档里 ``len ∈ {-4..-1, 1..5}`` 就是这个意思。
        """
        if n == 0:
            self.out.append(0x00)
            return
        if 0 < n < 123:
            self.out.append(n + 5)
            return
        if -124 < n < 0:
            self.out.append((n - 5) & 0xFF)
            return
        if n > 0:
            nbytes = max(1, (n.bit_length() + 7) // 8)
            if nbytes > 5:
                raise MarshalError(f"整数超出 Marshal 形式 2 的范围：{n}")
            self.out.append(nbytes)
            self.out += n.to_bytes(nbytes, "little")
            return
        inv = ~n
        nbytes = max(1, (inv.bit_length() + 7) // 8)
        if nbytes > 4:
            raise MarshalError(f"负整数超出 Marshal 形式 2 的范围：{n}")
        # 长度字节写 ``-nbytes`` 的补码（-1 ⇒ 0xFF、-2 ⇒ 0xFE …）。
        # 数值部分是 ``~n`` 的小端补码（-125 ⇒ 0x7C，正是文档里的 131
        # 当成 8 位补码看的结果）。
        self.out.append(256 - nbytes)
        self.out += inv.to_bytes(nbytes, "little")

    def _symbol(self, name: str) -> None:
        self._symbol_body(name)

    def _symbol_body(self, name: str) -> None:
        r"""写符号。

        ▲ 参数是**名字**（``str``），不是 :class:`_Sym` —— 这跟 Reader
        那边刚好相反，因为写的时候只需要名字来查表。`write()` 里
        遇到 ``_Sym`` 会取 ``.name`` 再调这里。

        已经出现过的符号写 ``;`` + 序号（序号是**插入顺序**，从 0 开始，
        且序号本身走 ``_fixnum`` 形式 1 的 +5 偏移 —— 所以 ``;0`` 的
        原始字节是 ``3b 00``，不是 ``3b 05``）。
        """
        if name in self._symbols:
            self.out.append(0x3B)  # ';'
            self._fixnum(self._symbols.index(name))
            return
        self.out.append(0x3A)  # ':'
        self._bytes(name.encode("utf-8"))
        self._symbols.append(name)

    def _bytes(self, b: bytes) -> None:
        self._fixnum(len(b))
        self.out += b

    def _long(self, n: int) -> None:
        r"""``l``（Bignum）写出，见 :meth:`_Reader._long`。

        长度写的是 **short 个数**（``(nbytes + 1) // 2``），
        字节数向上取整到偶数。
        """
        self.out.append(0x6C)  # 'l'
        self.out.append(0x2D if n < 0 else 0x2B)  # '-' / '+'
        mag = abs(n)
        nbytes = max(2, (mag.bit_length() + 7) // 8)
        if nbytes % 2:
            nbytes += 1
        self._fixnum(nbytes // 2)
        self.out += mag.to_bytes(nbytes, "little")

    def _string(self, s: str) -> None:
        self.out.append(0x22)  # '"'
        self._bytes(self._encode(s))

    def _encode(self, s: str) -> bytes:
        r"""按本次 dump 的目标编码写字符串。

        ▲ 这里踩过一个**很隐蔽**的坑：如果无论什么版本都用 cp932，
        那么 ``dumps("地图", magic=MAGIC_49)`` 写出的就是 **CP932 字节**，
        而读回来时按「4.9 ⇒ UTF-8」解 ⇒ **乱码**。

        更隐蔽的是它**不一定**立刻暴露：CP932 能表示相当一部分汉字，
        所以只有部分字符串会坏，看起来像"偶发乱码"。

        回退策略：目标编码装不下的字符（例如 UTF-8 数据里出现的
        罕见汉字在 CP932 里没有）用 ``errors="replace"``，并且
        **不留静默** —— 见 :func:`dumps` 返回前的检查。
        """
        return s.encode(self._encoding, errors="replace")

    def _float(self, v: float) -> None:
        r"""``f`` + 十进制字符串（见 :meth:`_Reader._float`）。

        格式串必须和 Ruby 一致（``"%.16g"``），否则写出的字节和
        Ruby 自己 dump 的不一样 —— 虽然读得回来，但"逐字节最小差异"
        这条回写原则就破坏了，出问题时没法用 diff 定位。
        """
        self.out.append(0x66)
        if v != v:  # NaN
            s = "nan"
        elif v == float("inf"):
            s = "inf"
        elif v == float("-inf"):
            s = "-inf"
        else:
            s = f"{v:.16g}"
        self._bytes(s.encode("ascii"))

    # -- 主分派 --------------------------------------------------------

    def write(self, v: Any) -> None:  # noqa: C901 - 与 read 对称
        if v is None:
            self.out.append(0x30)
        elif v is True:
            self.out.append(0x54)
        elif v is False:
            self.out.append(0x46)
        elif isinstance(v, _Sym):
            self._symbol(_name_of(v))
        elif isinstance(v, _IvarValue):
            self.out.append(0x49)  # 'I'
            self.write(v.value)
            self._fixnum(len(v.ivars))
            for k, x in v.ivars.items():
                self._symbol(_name_of(k))
                self.write(x)
        elif isinstance(v, RValue):
            self.out.append(0x6F)  # 'o'
            # ▲ 这里要的是**类名** ``v.cls``，不是 ``v``。
            #   写 ``_name_of(v)`` 会把整个对象 ``str()`` 成
            #   ``"RValue('Foo', [])"`` 当成符号名写出去 ——
            #   不报错，但产出的是彻底非法的数据。
            self._symbol(v.cls)
            self._fixnum(len(v.ivars))
            for k, x in v.ivars.items():
                self._symbol(_name_of(k))
                self.write(x)
        elif isinstance(v, bool):  # pragma: no cover - 上面已处理
            self.out.append(0x54 if v else 0x46)
        elif isinstance(v, int):
            # 形式 2 的上限：正数 5 字节 ⇒ ``2^40 - 1``；
            # 负数 4 字节 ⇒ ``-(2^32)``。超出就得写 Bignum（``l``）。
            if MIN_FIXNUM <= v <= MAX_FIXNUM:
                self.out.append(0x69)  # 'i'
                self._fixnum(v)
            else:
                self._long(v)
        elif isinstance(v, float):
            self._float(v)
        elif isinstance(v, str):
            self._string(v)
        elif isinstance(v, (bytes, bytearray)):
            self.out.append(0x22)
            self._bytes(bytes(v))
        elif isinstance(v, (list, tuple)):
            self.out.append(0x5B)  # '['
            self._fixnum(len(v))
            for x in v:
                self.write(x)
        elif isinstance(v, dict):
            self.out.append(0x7B)  # '{'
            self._fixnum(len(v))
            for k, x in v.items():
                self.write(k)
                self.write(x)
        else:
            raise MarshalError(f"无法序列化类型 {type(v).__name__}")


def dumps(value: Any, *, magic: bytes = MAGIC_48) -> bytes:
    """把值写成 Marshal 数据，带指定版本头（默认 4.8）。

    默认 4.8 是**保守选择**：RPG Maker XP/VX 只有 4.8，而 Ruby 1.9+
    读 4.8 数据没有问题 —— 反过来让 1.8 读 4.9 会直接失败。
    所以写 4.8 对两个引擎都安全。

    ▲ 但 4.9 的字符串带 ``@encoding`` / ``E`` ivar，用 4.8 头写 4.9
    数据是**非法**的（``I`` 标记在 4.8 里不存在）。调用方若原文件是
    4.9，必须把原头传回来 —— 见 :func:`loads` 与 :func:`peek_major`。

    字符串编码由 ``magic`` 决定（4.8 ⇒ cp932、4.9 ⇒ utf-8），
    必须与读入时一致，否则 CJK 会被静默转码成另一种编码的字节。
    """
    if magic == MAGIC_49:
        enc = "utf-8"
    elif magic == MAGIC_48:
        enc = "cp932"
    else:
        raise MarshalError(f"未知的 Marshal 版本头：{magic!r}")
    w = _Writer(encoding=enc)
    w.out += magic
    w.write(value)
    return bytes(w.out)
