"""RPG Maker MV/MZ 加密资源（`.rpgmvp` / `.rpgmvo` / `.png_` / `.ogg_`）。

## 为什么必须做这件事

RPG Maker MV/MZ 游戏**默认开启资源加密**。真实游戏实测：

| 游戏 | 明文 png | 加密图 | 我们能扫到的 |
| --- | --- | --- | --- |
| Elf Lifia（MV, `www/` 布局） | 12 | **783** (`.rpgmvp`) | 11 |
| Beyond the Portal（MZ, 根布局） | 95 | **1453** (`.png_`) | **0** |

也就是说：**贴图汉化在真实游戏上几乎完全没生效** ——
能被扫到的只是没被加密的那十几张（引擎自带 Loading/Window 之类）。
`images_scan` 在 574 MB 的 MZ 游戏上报"0 张候选贴图"，就是这个原因。

引擎自己靠 `System.json` 里的 `encryptionKey` 还原这些文件。
不实现这一步，"贴图类文本也能汉化"这条承诺对绝大多数真实游戏是空的。

## 文件布局（实测确认，不是猜的）

    偏移      内容
    0..16     `RPGMV` + 版本头（16 字节）
    16..32    真实文件头 **与 key 逐字节异或** 后的结果
    32..      **原样**的真实数据

只有**那 16 字节**是加密的 —— 这是最容易搞错的地方。
我第一版以为全文件都是 `data[i] ^ key[i % 16]`，
结果只有前 16 字节对得上、后面全是垃圾（IHDR 尺寸读出
`3558706419x2399187502` 这种离谱值，PIL 直接拒绝解码）。

## 实测验证

```
auto.rpgmvp        → 42x42    IHDR CRC ✅  PIL 解码 ✅ RGBA
GameOver.png_      → 816x624  IHDR CRC ✅  PIL 解码 ✅ P
SF_Armymonkey.png_ → 174x188  IHDR CRC ✅  PIL 解码 ✅ P
```

三张都通过 IHDR CRC 校验且 PIL 能完整解码，所以算法是对的。

## 写回时必须重新加密

解密 → 改图 → 存成纯 PNG 会让游戏读不了（引擎只认带头的格式）。
所以 :func:`encrypt` 是必需的，不是可选的便利函数。
"""

from __future__ import annotations

import json
import logging
import struct
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

#: 实测的真实头（`RPGMV` + 版本）。
#:
#: 逐字节看是 `52 50 47 4d 56 | 00 00 00 00 | 03 01 | 00 00 00 00 00`：
#: 前 5 字节 `RPGMV`、4 字节 0、然后是 `03 01` 版本。
#:
#: ⚠️ 写这条常量时踩过两个坑：
#:
#: 1. 把 `RPGMV` 当成 4 字节（它是 **5** 字节，`56` 就是那个 `V`）——
#:    于是把偏移算错，比对 `b"RPGMV" + b"\\x00" * 11` 永远不成立，
#:    **真实游戏的文件全都判不出"这是加密文件"**，白漏 2200 多张图。
#: 2. 以为第 8 字节是版本号（其实是 0；版本 `03 01` 在偏移 9..11）。
#:
#: 所以下面的 :func:`is_rpgmv_encrypted` 只比对前 5 字节 `RPGMV`
#: 加上前 9 字节全零，不再硬编码整条 16 字节 —— 版本位将来变了也不至于判不出来。
RPGMV_MAGIC = bytes.fromhex("5250474d560000000003010000000000")

#: 判定加密文件时比对的前缀长度（`RPGMV` + 4 个 0）
_RPGMV_PREFIX_LEN = 9

HEADER_LEN = 16
"""头长度。"""

ENCRYPTED_HEAD_LEN = 16
"""真正被异或的那一段长度（紧跟头之后的 16 字节）。"""

#: 加密图片扩展名（含 MV 的 `.rpgmvp` 与 MZ/加密插件的 `.png_`）
IMAGE_SUFFIXES: tuple[str, ...] = (".rpgmvp", ".png_")

#: 加密音频扩展名（本工具不改音频，但识别出来可以避免误判成"未知文件"）
AUDIO_SUFFIXES: tuple[str, ...] = (".rpgmvo", ".rpgmvm", ".ogg_", ".m4a_")

PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
OGG_MAGIC = b"OggS"


def is_rpgmv_encrypted(data: bytes) -> bool:
    """``data`` 是不是 RPG Maker 加密资源（只看头）。

    只比对 `RPGMV` + 紧随的 4 个 0，**不比对版本位** ——
    不同引擎版本的版本字节可能不同，硬编码整条 16 字节会让
    新版本的文件判不出来（那是最糟的失败方式：静默漏掉全部贴图）。
    """
    return (
        len(data) >= HEADER_LEN + ENCRYPTED_HEAD_LEN
        and data[:5] == RPGMV_MAGIC[:5]
        and data[5:_RPGMV_PREFIX_LEN] == b"\x00" * (_RPGMV_PREFIX_LEN - 5)
    )


def is_encrypted_image_name(path: str | Path) -> bool:
    """按扩展名判断是不是加密图片。"""
    return Path(str(path)).suffix.lower() in IMAGE_SUFFIXES


def _read_key_file(p: Path) -> bytes | None:
    try:
        doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    raw = str(doc.get("encryptionKey") or "")
    if len(raw) < 32:
        return None
    try:
        return bytes.fromhex(raw[:32])
    except ValueError:
        return None


def find_encryption_key(game_dir: str | Path) -> bytes | None:
    """从 ``System.json`` 里读 ``encryptionKey``。

    两个布局都找：``www/data/``（NW.js 打包）与 ``data/``（根布局）。

    ## 两条都有时怎么办

    **两个 key 不一致就返回 ``None``**（让调用方报错），而不是随便挑一个。

    这不是假想的边界：如果两个 `System.json` 的 key 不同，说明
    **真实资源用的是其中一个**，而挑错的那个会把图解得一片模糊，
    然后被送去做 OCR、再把乱字画上去回写 —— 比直接失败严重得多。
    挑不出来的情况应该**显式失败**，不该猜。

    （正常情况下游戏只有其中一种布局，或两条的 key 相同。）
    """
    base = Path(game_dir)
    root_key = _read_key_file(base / "data" / "System.json")
    www_key = _read_key_file(base / "www" / "data" / "System.json")

    if root_key is not None and www_key is not None:
        if root_key != www_key:
            log.warning(
                "游戏同时存在 data/ 与 www/data/ 两个 System.json，"
                "且 encryptionKey 不一致（%s / %s）—— 无法判断哪个是真的，"
                "为安全起见不解密资源",
                root_key.hex(),
                www_key.hex(),
            )
            return None
        return root_key
    return root_key if root_key is not None else www_key


class DecryptError(Exception):
    """解密失败。"""


def decrypt(data: bytes, key: bytes) -> bytes:
    """还原一个 RPG Maker 加密资源。

    Raises:
        DecryptError: 头不匹配、key 长度不对、或解密后仍然认不出格式。
    """
    if len(key) != ENCRYPTED_HEAD_LEN:
        raise DecryptError(
            f"key 必须是 {ENCRYPTED_HEAD_LEN} 字节，实际 {len(key)} 字节"
        )
    if not is_rpgmv_encrypted(data):
        raise DecryptError(
            f"不是 RPG Maker 加密文件（头 = {data[:HEADER_LEN].hex() or '空'}）"
        )
    head = bytes(
        a ^ b
        for a, b in zip(data[HEADER_LEN : HEADER_LEN + ENCRYPTED_HEAD_LEN], key, strict=True)
    )
    return head + data[HEADER_LEN + ENCRYPTED_HEAD_LEN :]


def encrypt(data: bytes, key: bytes) -> bytes:
    """把一个明文资源包成 RPG Maker 加密格式（回写游戏时必需）。

    Raises:
        DecryptError: 数据太短或 key 长度不对。
    """
    if len(key) != ENCRYPTED_HEAD_LEN:
        raise DecryptError(
            f"key 必须是 {ENCRYPTED_HEAD_LEN} 字节，实际 {len(key)} 字节"
        )
    if len(data) < ENCRYPTED_HEAD_LEN:
        raise DecryptError(f"数据太短（{len(data)} 字节），无法生成加密头")
    head = bytes(a ^ b for a, b in zip(data[:ENCRYPTED_HEAD_LEN], key, strict=True))
    return RPGMV_MAGIC + head + data[ENCRYPTED_HEAD_LEN:]


# ----------------------------------------------------------------------
# 校验：解出来到底是不是一张能用的图
# ----------------------------------------------------------------------

def sniff_format(data: bytes) -> str:
    """认一认解密结果是什么格式（用于校验 key 对不对）。"""
    if data[:8] == PNG_MAGIC:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "mp3"
    return "unknown"


def verify_png(data: bytes) -> tuple[bool, str]:
    """校验解密结果是不是**完整可用**的 PNG。

    不只看签名 —— 还要查 IHDR 的 CRC 和尺寸是否合理。
    这一步是"key 是否正确"的硬证据：key 错一个字节，
    签名可能碰巧对（只有 16 字节参与异或），但 IHDR CRC 必错。
    """
    if data[:8] != PNG_MAGIC:
        return False, f"不是 PNG 签名（{data[:8].hex()}）"
    if len(data) < 33:
        return False, f"太短（{len(data)} 字节），没有完整 IHDR"
    if data[12:16] != b"IHDR":
        return False, f"首个 chunk 不是 IHDR，而是 {data[12:16]!r}"
    w, h = struct.unpack(">II", data[16:24])
    if not (0 < w <= 65535 and 0 < h <= 65535):
        return False, f"尺寸不合理（{w}x{h}），key 很可能不对"
    crc_calc = zlib.crc32(data[12:29]) & 0xFFFFFFFF
    crc_file = struct.unpack(">I", data[29:33])[0]
    if crc_calc != crc_file:
        return False, f"IHDR CRC 不匹配（算出 {crc_calc:08x}，文件里 {crc_file:08x}）"
    return True, f"{w}x{h}"


def decode_resource(data: bytes, key: bytes, *, expect: str = "png") -> bytes:
    """解密并校验；校验不过就抛 :class:`DecryptError`。

    校验是**刻意做严**的：一个错误的 key 会静默产出垃圾图片，
    然后被送去 OCR（浪费几分钟）再被画上乱字回写游戏。
    宁可在解密这一步就硬失败。
    """
    plain = decrypt(data, key)
    fmt = sniff_format(plain)
    if expect == "png":
        ok, detail = verify_png(plain)
        if not ok:
            raise DecryptError(f"解密结果不是可用 PNG：{detail}")
    elif fmt != expect:
        raise DecryptError(f"解密结果是 {fmt}，期望 {expect}")
    return plain


__all__ = [
    "AUDIO_SUFFIXES",
    "DecryptError",
    "ENCRYPTED_HEAD_LEN",
    "HEADER_LEN",
    "IMAGE_SUFFIXES",
    "PNG_MAGIC",
    "RPGMV_MAGIC",
    "decode_resource",
    "decrypt",
    "encrypt",
    "find_encryption_key",
    "is_encrypted_image_name",
    "is_rpgmv_encrypted",
    "sniff_format",
    "verify_png",
]
