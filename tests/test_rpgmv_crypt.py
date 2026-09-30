"""RPG Maker 加密资源（`.rpgmvp` / `.png_`）的解密与**回写加密**。

## 这些测试为什么值得写这么细

这是"贴图汉化"能不能在真实游戏上生效的分水岭。实测：

| 游戏 | 明文 png | 加密图 | 修之前能扫到 |
| --- | --- | --- | --- |
| Elf Lifia（MV） | 12 | **783** `.rpgmvp` | 11 |
| Beyond the Portal（MZ） | 95 | **1453** `.png_` | **0** |

也就是说，在 574 MB 的真实 MZ 游戏上 `images_scan` 报"0 张候选贴图"，
**贴图汉化等于没做**。所以这里既测"能解开"，也测"解得对"。

## 用真实数据做夹具

下面 `_REAL_SAMPLES` 里的 (key, 文件) 都来自本机真实游戏，
并且断言的尺寸 / CRC 是从真实解码结果里核出来的 ——
不是我自己编一个再自己解。合成数据很容易"自证正确"：
我先写了错的算法（全文件异或），自己造的夹具照样能通过，
是真实文件才把它戳穿（IHDR 尺寸读出 `3558706419x2399187502`）。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.engines.rpgmv_crypt import (  # noqa: E402
    AUDIO_SUFFIXES,
    ENCRYPTED_HEAD_LEN,
    HEADER_LEN,
    IMAGE_SUFFIXES,
    PNG_MAGIC,
    RPGMV_MAGIC,
    DecryptError,
    decode_resource,
    decrypt,
    encrypt,
    find_encryption_key,
    is_encrypted_image_name,
    is_rpgmv_encrypted,
    sniff_format,
    verify_png,
)

#: 本机真实游戏里的样本。用 `(游戏目录, 相对路径, 期望尺寸)`
_REAL_SAMPLES = [
    (
        r"D:\NovaLocData\real\ElfLifia",
        r"www\img\pictures\auto.rpgmvp",
        (42, 42),
    ),
    (
        r"D:\NovaLocData\real\BeyondPortal",
        r"img\system\GameOver.png_",
        (816, 624),
    ),
    (
        r"D:\NovaLocData\real\BeyondPortal",
        r"img\enemies\Otros\SF_Armymonkey.png_",
        (174, 188),
    ),
]


def _real(key_len_ok: bool = True):
    """产出真实样本，缺文件就跳过（CI 上没有这些游戏）。"""
    for game, rel, size in _REAL_SAMPLES:
        p = Path(game) / rel
        if not p.is_file():
            continue
        key = find_encryption_key(game)
        if key is None or (key_len_ok and len(key) != ENCRYPTED_HEAD_LEN):
            continue
        yield p, key, size


_HAS_REAL = any(True for _ in _real())
requires_real = pytest.mark.skipif(
    not _HAS_REAL, reason="本机没有真实 RPG Maker 加密样本（需要真实游戏副本）"
)

KEY = bytes.fromhex("d41d8cd98f00b204e9800998ecf8427e")


def _fake_png(w: int = 8, h: int = 4, *, crc_ok: bool = True) -> bytes:
    """造一个**结构合法**的最小 PNG（签名 + IHDR）。"""
    ihdr_body = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr_body
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    if not crc_ok:
        crc ^= 0xFFFFFFFF
    return PNG_MAGIC + struct.pack(">I", len(ihdr_body)) + chunk + struct.pack(">I", crc) + b"IEND"


# ----------------------------------------------------------------------
# 一、真实样本：这是关键证据
# ----------------------------------------------------------------------

@requires_real
def test_real_encrypted_images_decrypt_and_verify() -> None:
    """真实游戏的加密图必须能解开，且通过 IHDR CRC 校验。

    CRC 是"key 对不对"的硬证据：key 错一个字节，16 字节签名可能
    碰巧还对（只有那 16 字节参与异或），但 IHDR 的 CRC 必错。
    """
    n = 0
    for p, key, expected_size in _real():
        raw = p.read_bytes()
        assert is_rpgmv_encrypted(raw), f"{p.name} 没被认成加密文件"
        plain = decode_resource(raw, key, expect="png")
        ok, detail = verify_png(plain)
        assert ok, f"{p.name} 解密后校验失败：{detail}"
        assert detail == f"{expected_size[0]}x{expected_size[1]}", (
            f"{p.name} 尺寸应为 {expected_size}，实际 {detail}"
        )
        n += 1
    assert n > 0


@requires_real
def test_real_samples_decrypt_with_pil() -> None:
    """PIL 必须能**完整解码**解密结果（不是只看签名）。

    这条是防"半对"的：全文件异或那版能让前 8 字节签名正确，
    但 PIL 会直接报 `cannot identify image file`。
    """
    PIL = pytest.importorskip("PIL.Image")
    import io

    checked = 0
    for p, key, expected_size in _real():
        plain = decode_resource(p.read_bytes(), key, expect="png")
        im = PIL.open(io.BytesIO(plain))
        im.load()
        assert im.size == expected_size, f"{p.name} PIL 尺寸 {im.size} != {expected_size}"
        checked += 1
    assert checked > 0


@requires_real
def test_real_round_trip_is_byte_identical() -> None:
    """解密 → 加密 必须**逐字节还原原文件**。

    回写游戏时就是走这条路：解开、改图、再包回去。
    如果往返不还原，游戏里会出现"改了但读不了"或图片损坏。
    """
    for p, key, _ in _real():
        raw = p.read_bytes()
        assert encrypt(decrypt(raw, key), key) == raw, (
            f"{p.name} 解密-加密往返没有逐字节还原"
        )


@requires_real
def test_real_key_is_found_from_system_json() -> None:
    """key 必须能从 `System.json` 自动读到（两条布局都试）。"""
    for game, _, _ in _REAL_SAMPLES:
        if not Path(game).is_dir():
            continue
        key = find_encryption_key(game)
        assert key is not None, f"{game} 没找到 encryptionKey"
        assert len(key) == ENCRYPTED_HEAD_LEN


@requires_real
def test_wrong_key_is_rejected_not_silently_accepted() -> None:
    """**关键的安全性质**：错 key 必须报错，不能产出"看起来能读"的垃圾。

    如果错 key 被放过去，垃圾图会被送去 OCR（浪费几分钟），
    再把乱字画上去回写游戏 —— 比直接失败严重得多。
    """
    for p, key, _ in _real():
        wrong = bytes((b + 1) & 0xFF for b in key)
        with pytest.raises(DecryptError):
            decode_resource(p.read_bytes(), wrong, expect="png")
        return


# ----------------------------------------------------------------------
# 二、合成样本：往返与边界
# ----------------------------------------------------------------------

def test_encrypt_decrypt_round_trip_synthetic() -> None:
    plain = _fake_png()
    blob = encrypt(plain, KEY)
    assert blob[:HEADER_LEN] == RPGMV_MAGIC
    assert is_rpgmv_encrypted(blob)
    assert decrypt(blob, KEY) == plain


def test_only_the_first_16_bytes_are_encrypted() -> None:
    """**这条守的是最容易搞错的地方**。

    只有紧跟头的 16 字节参与异或，其余原样。
    我第一版写成全文件 `data[i] ^ key[i % 16]`，结果只有前 16 字节对，
    后面全成垃圾。这条会在那种实现下失败。
    """
    plain = _fake_png(w=16, h=16)
    blob = encrypt(plain, KEY)
    # 头之后 16 字节确实被异或过（除非 key 恰好全 0）
    assert blob[HEADER_LEN : HEADER_LEN + 16] != plain[:16]
    # 再往后的数据**逐字节相同** —— 没有被加密
    assert blob[HEADER_LEN + 16 :] == plain[16:], (
        "第 32 字节之后的数据被改动了 —— 但 RPG Maker 只加密那 16 字节"
    )


def test_magic_layout_is_what_the_real_games_have() -> None:
    """**回归**：`RPGMV` 是 **5** 字节，版本 `03 01` 在偏移 9..11。

    我第一版把它当成 4 字节 magic（`RPGMV` 里那个 `V` 就是第 5 字节），
    于是比对 `b"RPGMV" + b"\\x00" * 11` 永远不成立 ——
    **真实游戏的文件全都判不出"这是加密文件"**，白漏 2200 多张图
    （`images_scan` 在 574 MB 的 MZ 游戏上报"0 张候选"，就是这个）。
    """
    assert RPGMV_MAGIC[:5] == b"RPGMV", f"前 5 字节应是 RPGMV，实际 {RPGMV_MAGIC[:5]!r}"
    assert RPGMV_MAGIC[5:9] == b"\x00" * 4
    assert RPGMV_MAGIC[9:11] == bytes([0x03, 0x01]), (
        f"版本应在偏移 9..11 为 03 01，实际 {RPGMV_MAGIC[9:11].hex()}"
    )
    assert len(RPGMV_MAGIC) == HEADER_LEN == 16


def test_encrypted_detection_does_not_depend_on_version_bytes() -> None:
    """只比 `RPGMV` + 4 个 0，不比版本位。

    将来引擎换版本号时，硬编码整条 16 字节会让新文件判不出来 ——
    那是最糟的失败方式（静默漏掉全部贴图）。
    """
    body = bytes(range(64))
    for ver in (bytes([0x03, 0x01]), bytes([0x04, 0x02]), bytes([0x00, 0x00])):
        blob = b"RPGMV" + b"\x00" * 4 + ver + b"\x00" * 5 + body
        assert is_rpgmv_encrypted(blob), f"版本 {ver.hex()} 的文件没被认出来"


@pytest.mark.parametrize("n", [0, 1, 15, 16, 31])
def test_too_short_data_is_rejected(n: int) -> None:
    assert not is_rpgmv_encrypted(b"\x00" * n)
    with pytest.raises(DecryptError):
        decrypt(b"\x00" * n, KEY)


@pytest.mark.parametrize("bad_key", [b"", b"short", b"x" * 15, b"x" * 17, b"x" * 32])
def test_wrong_key_length_rejected(bad_key: bytes) -> None:
    blob = encrypt(_fake_png(), KEY)
    with pytest.raises(DecryptError):
        decrypt(blob, bad_key)
    with pytest.raises(DecryptError):
        encrypt(_fake_png(), bad_key)


def test_encrypt_rejects_too_short_data() -> None:
    with pytest.raises(DecryptError):
        encrypt(b"tiny", KEY)


def test_decrypt_rejects_plain_png() -> None:
    """明文 PNG 不该被当成加密文件（否则会误改游戏里本来正常的图）。"""
    assert not is_rpgmv_encrypted(_fake_png())
    with pytest.raises(DecryptError):
        decrypt(_fake_png(), KEY)


# ----------------------------------------------------------------------
# 三、校验的严格度
# ----------------------------------------------------------------------

def test_verify_png_rejects_bad_crc() -> None:
    """CRC 不对必须判失败 —— 这是"key 对不对"的硬证据。"""
    ok, detail = verify_png(_fake_png(crc_ok=False))
    assert not ok, "IHDR CRC 错误却通过了校验"
    assert "CRC" in detail


@pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (70000, 10)])
def test_verify_png_rejects_absurd_dimensions(w: int, h: int) -> None:
    """尺寸离谱说明 key 错了。真实塞错 key 时读出过 3558706419x2399187502。"""
    ok, detail = verify_png(_fake_png(w=w, h=h))
    assert not ok, f"{w}x{h} 这种尺寸竟然通过了校验"
    assert "尺寸" in detail


def test_verify_png_rejects_truncated() -> None:
    ok, detail = verify_png(PNG_MAGIC + b"\x00" * 10)
    assert not ok
    assert "短" in detail or "IHDR" in detail


def test_sniff_format_identifies_common_types() -> None:
    assert sniff_format(_fake_png()) == "png"
    assert sniff_format(b"\xff\xd8\xff\xe0hello") == "jpeg"
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert sniff_format(b"OggS\x00\x02") == "ogg"
    assert sniff_format(b"fLaC\x00\x00") == "flac"
    assert sniff_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"
    assert sniff_format(b"ID3\x04") == "mp3"
    assert sniff_format(b"\x00\x01\x02\x03") == "unknown"


def test_decode_resource_checks_the_expected_format() -> None:
    """可以让调用方指定期望格式（音频资源走同一条路）。"""

    # 造一个"看起来像 ogg"的加密体
    plain = b"OggS" + b"\x00" * 60
    blob = encrypt(plain, KEY)
    assert decode_resource(blob, KEY, expect="ogg") == plain
    with pytest.raises(DecryptError):
        decode_resource(blob, KEY, expect="png")


# ----------------------------------------------------------------------
# 四、扩展名与 key 发现
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.rpgmvp", True), ("a.RPGMVP", True), ("a.png_", True),
        ("a.PNG_", True),
        ("a.png", False), ("a.ogg", False), ("a.rpgmvo", False),
        ("a.txt", False),
    ],
)
def test_is_encrypted_image_name(name: str, expected: bool) -> None:
    assert is_encrypted_image_name(name) is expected


def test_image_and_audio_suffixes_cover_both_games() -> None:
    """扩展名表必须覆盖实测出现的两种图片格式。"""
    assert ".rpgmvp" in IMAGE_SUFFIXES      # MV
    assert ".png_" in IMAGE_SUFFIXES        # MZ / 加密插件
    assert ".rpgmvo" in AUDIO_SUFFIXES
    assert ".ogg_" in AUDIO_SUFFIXES


def test_find_encryption_key_prefers_consistent_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    """两条布局都在时：key 相同就用，**不同就返回 None**（不猜）。"""
    import json

    root_key = "3df884122cfe344a3f82923ea1406938"
    other_key = "00112233445566778899aabbccddeeff"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": root_key.upper()}), encoding="utf-8"
    )
    # 只有根布局
    assert find_encryption_key(tmp_path) == bytes.fromhex(root_key)

    # 加上 www 布局，key **相同** → 仍然可用
    (tmp_path / "www" / "data").mkdir(parents=True)
    (tmp_path / "www" / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": root_key}), encoding="utf-8"
    )
    assert find_encryption_key(tmp_path) == bytes.fromhex(root_key)

    # key **冲突** → 必须返回 None，不能随便挑一个
    (tmp_path / "www" / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": other_key}), encoding="utf-8"
    )
    assert find_encryption_key(tmp_path) is None, (
        "两个 System.json 的 key 不一致时必须返回 None。"
        "挑错的那个会把图解得一片模糊，还会被送去 OCR 再把乱字回写游戏 —— "
        "比直接失败严重得多。"
    )


def test_find_encryption_key_works_with_only_www_layout(tmp_path: Path) -> None:
    """NW.js 打包布局是主流，必须支持只有 `www/data/` 的情况。"""
    import json

    key = "d41d8cd98f00b204e9800998ecf8427e"
    (tmp_path / "www" / "data").mkdir(parents=True)
    (tmp_path / "www" / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": key}), encoding="utf-8"
    )
    assert find_encryption_key(tmp_path) == bytes.fromhex(key)


def test_find_encryption_key_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_encryption_key(tmp_path) is None


@pytest.mark.parametrize("bad", ["", "abc", "zz" * 16])
def test_find_encryption_key_ignores_garbage(tmp_path: Path, bad: str) -> None:
    """key 太短或不是十六进制时返回 None，不要崩。"""
    import json

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": bad}), encoding="utf-8"
    )
    assert find_encryption_key(tmp_path) is None
