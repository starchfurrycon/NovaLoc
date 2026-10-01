"""从 .NET 程序集里抽取**用户字符串字面量**（``#US`` 堆）。

## 为什么需要这个

很多 Unity 游戏的界面文字与对白**不是**散装文件，而是作为 C# 字符串
字面量编译进 ``Assembly-CSharp.dll``（实测 ``UnityAliQ``：游戏根目录下
只有 ``config.xml`` 和一个日志，可翻译文本全在 1.1MB 的 DLL 里）。
本适配器原来只能"检测到是 Unity"然后放弃 —— 那些游戏等于 0 覆盖率。

``#US``（User Strings）堆是 ECMA-335 规定的、专门存放用户字符串字面量的
位置，**不含**标识符、类型名、成员名（那些在 ``#Strings`` 堆里，不能翻）。
所以只抽 ``#US`` 就能天然避开绝大多数代码标记。

## 格式（ECMA-335 II.24.2.4）

``#US`` 是连续的「长度前缀 + UTF-16LE 数据」串：

* 长度用**压缩整数**（compressed unsigned integer）编码；
* 数据末尾**必定**有一个字节，其最高位标记"是否含特殊字符"：
  实际字符数是 ``(len - 1) / 2``，最后一个字节要丢掉；
* 长度为 0 表示堆结束。

## 保守过滤（宁可少抽，不可抽错）

抽出来的东西**可能**会送去翻译，所以这里宁可漏：只保留"像人话"的
字符串。判据全部是**结构性的**，不用长度比之类的坏判据
（见 `docs/ACCEPTANCE.md` 八之二）。

实测（真实游戏 ``UnityAliQ`` 的 ``Assembly-CSharp.dll``，1,347 条字面量）
说明**为什么必须收得很紧**：不过滤时有 1,231 条"可翻"，但实际内容是

* **989 条**是标识符/字段名/类名（``Puppet2D``、``Bone``、``_ProjInfo``、
  ``Unlit/Transparent``）—— 翻这些会**直接改坏游戏**；
* **9 条**是运行时报错（``[BUG:FIXME] FLIP failed to missing triangle``）；
* 真正像"给人看的句子"的只有 233 条，且其中多数仍是**内部日志**。

所以过滤分两级：

* :meth:`AssemblyStrings.translatable` —— 宽松（只排掉明显不是文字的），
  用于**统计**"这游戏有多少字符串"；
* :meth:`AssemblyStrings.dialogue_like` —— **严格**，要求像句子
  （有空格分词、有句末标点或 CJK、或含格式占位符），用于**导出候选**。

## 只读，绝不回写（重要）

本模块**不提供**写回。原因是结构性的：``#US`` 堆里每条字面量前面是
**长度前缀**，改文字长度就要改长度、移动后续所有字节、重算全部堆偏移
与 RVA —— 那是重写 PE 文件。风险远大于收益，而且错了会让游戏**完全
无法启动**（比口口口严重得多）。

所以这里的产出只有两种用途：**让用户看到"这游戏有多少可翻文本"**，
以及**导出给人工/dnSpy 处理**。任何"把译文写回 DLL"的想法都必须先
解决 PE 重写，那不属于本模块。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

#: 至少要这么多字符才可能是有意义的句子
MIN_CHARS = 4

#: 常见格式占位符 —— 这些在翻译时必须原样保留，所以过滤时要先摘掉
_PLACEHOLDER_RE = re.compile(r"\{[^{}]{0,18}\}|%[sdif]|%\{\w+\}|\$\{\w+\}")

#: 允许出现在"人话"里的字符：CJK、拉丁字母、数字、常见标点与空白
_TEXTY_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef"
    r"A-Za-z0-9 \t.,!?;:'\"()\[\]\-_/&%+*=<>~^|@#$…—–、。，！？；：（）【】「」『』《》·]"
)

#: 编译器/代码标记 —— 出现得越少越像人话
_CODERY = set("`{}<>@\\")

#: 明显是"内部日志/报错"的词。命中就不该送给翻译。
#: ⚠️ 这是**黑名单**，天生不完整 —— 所以它只用于收紧
#: :meth:`AssemblyStrings.dialogue_like`，不影响宽松统计。
_LOGGISH_RE = re.compile(
    r"\b(?:BUG|FIXME|TODO|Error|Exception|Assert|StackTrace|NullReference"
    r"|failed|Failed|FAILED|missing|Missing|invalid|Invalid|corrupt|Corrupt"
    r"|not supported|unsupported|Unknown|Unexpected|Warn|Warning)\b"
)

#: 标识符形态：``snake_case`` / ``camelCase`` / ``PascalCase`` 且有下划线或全小写
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 句末标点（含 CJK 全角）
_SENT_END_RE = re.compile(r"[.!?。！？…]$")


def _read_compressed_int(buf: bytes, pos: int) -> tuple[int, int]:
    """读压缩无符号整数（ECMA-335 II.23.2）。

    返回 ``(值, 新位置)``。1 字节时最高位为 0；2 字节时最高两位为 10；
    4 字节时最高三位为 110。越界时抛 ``ValueError``。
    """
    if pos >= len(buf):
        raise ValueError("读压缩整数时越界")
    b0 = buf[pos]
    if b0 & 0x80 == 0:
        return b0, pos + 1
    if b0 & 0xC0 == 0x80:
        if pos + 1 >= len(buf):
            raise ValueError("读 2 字节压缩整数时越界")
        return ((b0 & 0x3F) << 8) | buf[pos + 1], pos + 2
    if b0 & 0xE0 == 0xC0:
        if pos + 3 >= len(buf):
            raise ValueError("读 4 字节压缩整数时越界")
        val = ((b0 & 0x1F) << 24) | (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3]
        return val, pos + 4
    raise ValueError(f"无效的压缩整数首字节 0x{b0:02x}")


@dataclass(frozen=True)
class DotNetString:
    """``#US`` 堆里的一个字面量。"""

    #: 在 ``#US`` 堆内的偏移（回写时用来定位，也用作稳定 id）
    offset: int
    text: str

    @property
    def placeholders(self) -> list[str]:
        """这条文本里必须原样保留的格式占位符。"""
        return _PLACEHOLDER_RE.findall(self.text)


def _looks_like_text(s: str) -> bool:
    """保守判定"这像人话吗"。判据全是结构性的，见模块文档。

    ★ 关键一条：**单个 ASCII 词一律不算**。实测噪声里 989/1231 条是
    标识符与字段名（``Puppet2D``、``_ProjInfo``、``Bone``）—— 它们
    字符上"很干净"，靠字符集判据**拦不住**，只能靠"没有空格分词"
    这个结构特征拦。而单个 **CJK** 词要留（``莉菲娅`` 这种人名是
    真的要翻的）。
    """
    if len(s) < MIN_CHARS:
        return False
    if "\x00" in s or "\ufffd" in s:
        return False
    # 必须有至少一个字母或 CJK 字 —— 排除纯符号/纯数字/纯路径
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", s))
    if not has_cjk and not re.search(r"[A-Za-z]", s):
        return False
    # 去格式占位符后，文字字符要占一半以上
    body = _PLACEHOLDER_RE.sub("", s)
    if not body:
        return False
    texty = len(_TEXTY_RE.findall(body))
    if texty * 2 < len(body):
        return False
    # 代码标记不能太密
    codery = sum(1 for ch in body if ch in _CODERY)
    if codery > 2 and codery * 8 > len(body):
        return False
    # ★ 单个 ASCII 词（没有空格、没有 CJK）⇒ 是标识符，不是人话
    return has_cjk or " " in body.strip()


class AssemblyStrings:
    """从单个 .NET 程序集里读 ``#US`` 堆。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = path.read_bytes()
        self._us: bytes | None = None

    # ------------------------------------------------------------------
    # PE / 元数据定位
    # ------------------------------------------------------------------

    def _pe_offset(self) -> int:
        if self._data[:2] != b"MZ":
            raise ValueError(f"{self.path.name} 不是 PE 文件（缺少 MZ 头）")
        return struct.unpack_from("<I", self._data, 0x3C)[0]

    def _cli_header(self) -> tuple[int, int, int]:
        """返回 CLI header 的 ``(文件偏移, 大小, RVA)``。

        带上 RVA 是因为：元数据目录里存的是 **RVA**，要变成文件偏移
        必须再过一次同一张节表。让调用方自己重算一遍可选头偏移是
        **极易写错**的（第一版就这么写的，而且只在合成文件上暴露出来），
        所以这里一次算清楚。
        """
        pe = self._pe_offset()
        if self._data[pe : pe + 4] != b"PE\x00\x00":
            raise ValueError(f"{self.path.name} 缺少 PE 签名")
        n_sections = struct.unpack_from("<H", self._data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", self._data, pe + 20)[0]
        opt = pe + 24
        if opt + 2 > len(self._data):
            raise ValueError(f"{self.path.name} 可选头被截断")
        magic = struct.unpack_from("<H", self._data, opt)[0]
        if magic not in (0x10B, 0x20B):
            raise ValueError(f"{self.path.name} 可选头 magic 异常：0x{magic:04x}")
        # 数据目录：PE32 从可选头偏移 96 开始，PE32+ 从 112 开始
        dd = opt + (96 if magic == 0x10B else 112)
        if dd + 16 * 8 > len(self._data):
            raise ValueError(f"{self.path.name} 数据目录被截断")
        # 第 15 项（0-based 14）是 CLI header
        cli_rva, _cli_size = struct.unpack_from("<II", self._data, dd + 14 * 8)
        if cli_rva == 0:
            raise ValueError(f"{self.path.name} 是原生程序集，没有 CLI 头（不是 .NET）")
        return *self._rva_to_offset(cli_rva, n_sections, opt, opt_size), cli_rva

    def _rva_to_offset(
        self, rva: int, n_sections: int, opt: int, opt_size: int
    ) -> tuple[int, int]:
        """RVA → ``(文件偏移, 节原始大小)``，查节表。"""
        sec = opt + opt_size
        for i in range(n_sections):
            base = sec + i * 40
            if base + 40 > len(self._data):
                break
            v_size, v_addr, raw_size, raw_ptr = struct.unpack_from(
                "<IIII", self._data, base + 8
            )
            if v_addr <= rva < v_addr + max(v_size, raw_size):
                return raw_ptr + (rva - v_addr), raw_size
        raise ValueError(f"{self.path.name} 里找不到 RVA 0x{rva:x} 所在的节")

    def _us_heap(self) -> bytes:
        """定位并缓存 ``#US`` 堆。"""
        if self._us is not None:
            return self._us
        cli_off, _cli_size, cli_rva = self._cli_header()
        # CLI header 偏移 8 处是元数据目录的 RVA/大小
        md_rva, _md_size = struct.unpack_from("<II", self._data, cli_off + 8)
        # 元数据目录的 RVA 也存的是 RVA，同样过一遍节表 ——
        # **不能**用 `cli_off + (md_rva - cli_rva)`（第一版就是这么写的，
        # 只在 CLI 头与元数据恰好同节同段时才碰巧成立）
        pe = self._pe_offset()
        n_sections = struct.unpack_from("<H", self._data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", self._data, pe + 20)[0]
        md_off, _ = self._rva_to_offset(md_rva, n_sections, pe + 24, opt_size)
        if self._data[md_off : md_off + 4] != b"BSJB":
            raise ValueError(f"{self.path.name} 元数据签名不是 BSJB")
        ver_len = struct.unpack_from("<I", self._data, md_off + 12)[0]
        p = md_off + 16 + ver_len
        p += 2  # flags
        n_streams = struct.unpack_from("<H", self._data, p)[0]
        p += 2
        for _ in range(n_streams):
            off, size = struct.unpack_from("<II", self._data, p)
            p += 8
            # 流名是 NUL 结尾、4 字节对齐的 ASCII
            end = self._data.index(b"\x00", p)
            name = self._data[p:end].decode("ascii", "replace")
            p = end + 1
            p = (p + 3) & ~3
            if name == "#US":
                self._us = self._data[md_off + off : md_off + off + size]
                return self._us
        raise ValueError(f"{self.path.name} 里没有 #US 堆")

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def read(self) -> list[DotNetString]:
        """读出全部字面量（**未过滤**），保留堆内偏移。"""
        us = self._us_heap()
        out: list[DotNetString] = []
        pos = 1  # 第一个字节是 0x00 保留项
        while pos < len(us):
            try:
                blen, pos = _read_compressed_int(us, pos)
            except ValueError:
                break
            if blen == 0:
                break
            # 数据 = blen 字节，其中最后 1 字节是"含特殊字符"标记
            n_chars = (blen - 1) // 2
            raw = us[pos : pos + n_chars * 2]
            pos += blen
            if len(raw) < n_chars * 2:
                break
            try:
                s = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            out.append(DotNetString(offset=pos - blen, text=s))
        return out

    def translatable(self) -> list[DotNetString]:
        """**宽松**过滤 —— 只排掉明显不是文字的东西。

        用途是**统计**（"这游戏有多少字符串"），**不是**导出候选。
        实测在真实游戏上它仍会留下大量标识符与运行时报错，
        因为 ``#US`` 堆里本来就混着那些 —— 见模块文档的实测数字。
        """
        return [s for s in self.read() if _looks_like_text(s.text)]

    def dialogue_like(self) -> list[DotNetString]:
        """**严格**过滤 —— 只留"像给人看的句子"的候选。

        判据（满足其一即可，且都必须是结构性的）：

        * 含**空格分词**（``foo bar baz``）—— 标识符与字段名没有空格；
        * 以**句末标点**结尾，或含 **CJK**；
        * 含**格式占位符**（``{0}`` 这类，通常出现在给玩家看的提示里）。

        并且必须**不含**内部日志/报错词（``_LOGGISH_RE``）。

        ⚠️ 这是**尽力而为**的收窄，不是保证：实测仍会漏进一些内部日志
        （例如 ``[BUG:FIXME] FLIP failed`` 之类已被黑名单拦掉，但
        未见过的日志形态拦不住）。所以任何采用本结果的流程都必须
        **让人过一眼**，不能当作"已经可以直接翻"。
        """
        out: list[DotNetString] = []
        for s in self.read():
            t = s.text
            if not _looks_like_text(t):
                continue
            if _LOGGISH_RE.search(t):
                continue
            body = t.strip()
            words = body.split()
            has_words = len(words) >= 2 and sum(
                1 for w in words if _IDENT_RE.match(w)
            ) < len(words)
            if not (has_words or _SENT_END_RE.search(body) or s.placeholders):
                continue
            if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", body):
                pass  # 已是 CJK，直接算候选
            out.append(s)
        return out


def find_assemblies(game_dir: Path) -> list[Path]:
    """找出游戏自己的程序集。

    只看 ``*_Data/Managed`` 下的，且**排除** Unity/.NET 自带的框架程序集
    —— 那些是引擎与运行时的代码，里面的字符串翻了会造成灾难。
    """
    skip = {
        "mscorlib", "system", "system.core", "system.xml", "system.drawing",
        "mono.security", "mono.posix", "mono.cairo", "mono.data.sqlite",
        "unityengine", "unityengine.ui", "unityengine.coremodule",
        "unityengine.textrenderingmodule", "unityengine.imguimodule",
        "unityengine.uimodule", "unityengine.physic2dmodule",
        "boo.lang", "novaloc",
    }
    out: list[Path] = []
    for data_dir in sorted(game_dir.glob("*_Data")):
        managed = data_dir / "Managed"
        if not managed.is_dir():
            continue
        for dll in sorted(managed.glob("*.dll")):
            stem = dll.stem.lower()
            if stem in skip:
                continue
            # 只挑游戏自己的：Unity 约定是 Assembly-CSharp*，但也允许
            # 开发者自己的命名 —— 用"不在跳过名单里"来判
            if any(stem.startswith(s) for s in ("unityengine", "system", "mono.")):
                continue
            out.append(dll)
    return out
