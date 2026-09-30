"""贴图读取层必须**透明地**解开 RPG Maker 加密资源。

## 为什么这是分水岭

`images_scan` 靠 `extract_images()` 列候选图，`images_localize` 靠
`imread_bgr()` 读图。真实 RPG Maker 游戏默认开启资源加密，所以：

* 只 glob `*.png` → 574 MB 的 MZ 游戏报"**0 张候选贴图**"；
* `imread_bgr` 不认加密头 → 即使列出来了也读不出，报"无法读取图片"。

两条都会让"贴图类文本也汉化"这条承诺**静默失效**：阶段报 ✅，
用户以为没字的图就是没字。

## 测试策略

优先用**本机真实游戏的加密图**做断言（尺寸来自真实解码结果）。
没有真实游戏时退回合成样本（自己加密一张、再读回来），
但合成样本只能验证"往返自洽"，验证不了"算法符合引擎格式" ——
所以真实样本那几条是 `skipif`，不是删掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.engines import rpgmv_crypt as rc  # noqa: E402
from novaloc.engines.rpgmaker import RpgMakerAdapter  # noqa: E402
from novaloc.images.io import imread_bgr, imread_rgba, imwrite_bgr  # noqa: E402


def _adapter() -> RpgMakerAdapter:
    """适配器需要一个 Context（与 `test_engine_rpgmaker.py` 一致）。"""
    return RpgMakerAdapter(Context(config=Config(), events=EventBus()))

_REAL = [
    (r"D:\NovaLocData\real\ElfLifia", r"www\img\pictures\auto.rpgmvp", (42, 42)),
    (r"D:\NovaLocData\real\BeyondPortal", r"img\system\GameOver.png_", (816, 624)),
    (
        r"D:\NovaLocData\real\BeyondPortal",
        r"img\enemies\Otros\SF_Armymonkey.png_",
        (174, 188),
    ),
]


def _real_cases():
    for game, rel, size in _REAL:
        p = Path(game) / rel
        if p.is_file():
            yield p, size


_HAS_REAL = any(True for _ in _real_cases())
requires_real = pytest.mark.skipif(not _HAS_REAL, reason="本机没有真实加密贴图样本")


def _png_bytes(w: int, h: int) -> bytes:
    """用 cv2 编一张真实可用的 PNG（这样解出来能被 imdecode 认）。

    ⚠️ 尺寸要够大：`extract_images` 会跳过 **小于 512 字节** 的图
    （"太小的图不可能有字"）。纯色小图压完只有一百多字节，会被过滤掉，
    于是测试看到空列表 —— 那是夹具的问题，不是发现逻辑坏了。
    所以这里加一点噪声让文件体积过线。
    """
    import cv2

    rng = np.random.default_rng(1234)
    img = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    data = buf.tobytes()
    assert len(data) >= 512, (
        f"{w}x{h} 噪声图只有 {len(data)} 字节，过不了 extract_images 的 512 字节门槛，"
        "请把测试图改大"
    )
    return data


# ----------------------------------------------------------------------
# 一、真实样本（关键证据）
# ----------------------------------------------------------------------

@requires_real
def test_real_encrypted_texture_reads_through_imread_bgr() -> None:
    """真实加密贴图必须能被 `imread_bgr` 读出正确尺寸。"""
    for p, want in _real_cases():
        img = imread_bgr(p)
        assert img is not None, f"{p.name} 读不出来 —— 加密贴图会被静默跳过"
        assert (img.shape[1], img.shape[0]) == want, (
            f"{p.name} 尺寸 {(img.shape[1], img.shape[0])} != {want}"
        )


@requires_real
def test_real_encrypted_texture_reads_as_rgba() -> None:
    for p, want in _real_cases():
        img = imread_rgba(p)
        assert img is not None and img.shape[2] == 4
        assert (img.shape[1], img.shape[0]) == want


@requires_real
def test_plain_png_still_reads_normally(tmp_path: Path) -> None:
    """**回归**：普通 PNG 的读取路径不能被加密逻辑弄坏。"""
    p = tmp_path / "plain.png"
    cv2_png = _png_bytes(20, 12)
    p.write_bytes(cv2_png)
    img = imread_bgr(p)
    assert img is not None and (img.shape[1], img.shape[0]) == (20, 12)


# ----------------------------------------------------------------------
# 二、合成往返（无真实游戏时也能跑）
# ----------------------------------------------------------------------

def test_synthetic_encrypted_texture_round_trip(tmp_path: Path) -> None:
    """自己包一个加密图，`imread_bgr` 要能解开。

    注意合成样本只能证明"加解密自洽"，**证明不了符合引擎格式** ——
    后者靠上面那几条真实样本。
    """
    key = bytes.fromhex("d41d8cd98f00b204e9800998ecf8427e")
    png = _png_bytes(24, 16)
    enc = rc.encrypt(png, key)
    # 造出游戏目录结构，让 key 查找能生效
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    import json

    (game / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": key.hex()}), encoding="utf-8"
    )
    d = game / "img" / "system"
    d.mkdir(parents=True)
    f = d / "Thing.rpgmvp"
    f.write_bytes(enc)

    from novaloc.images.io import _KEY_CACHE

    _KEY_CACHE.clear()
    img = imread_bgr(f)
    assert img is not None, "合成加密图读不出来"
    assert (img.shape[1], img.shape[0]) == (24, 16)


def test_encrypted_texture_without_key_returns_none_not_garbage(tmp_path: Path) -> None:
    """**安全性质**：读不到 key 时必须返回 None（跳过），不能返回垃圾图。

    垃圾图会被送去 OCR，然后把乱字画上去回写游戏 —— 比跳过严重得多。
    """
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    enc = rc.encrypt(_png_bytes(16, 16), key)
    game = tmp_path / "game"
    (game / "img" / "system").mkdir(parents=True)
    f = game / "img" / "system" / "NoKey.rpgmvp"
    f.write_bytes(enc)
    # 故意不写 System.json

    from novaloc.images.io import _KEY_CACHE

    _KEY_CACHE.clear()
    assert imread_bgr(f) is None, "没有 key 却读出了内容 —— 那必然是垃圾"


def test_corrupt_encrypted_texture_returns_none(tmp_path: Path) -> None:
    """损坏的加密图返回 None，不要抛异常打断整批处理。"""
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    enc = bytearray(rc.encrypt(_png_bytes(16, 16), key))
    enc[40:60] = b"\xff" * 20          # 破坏载荷
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    import json

    (game / "data" / "System.json").write_text(
        json.dumps({"encryptionKey": key.hex()}), encoding="utf-8"
    )
    d = game / "img" / "system"
    d.mkdir(parents=True)
    f = d / "Broken.rpgmvp"
    f.write_bytes(bytes(enc))

    from novaloc.images.io import _KEY_CACHE

    _KEY_CACHE.clear()
    assert imread_bgr(f) is None


# ----------------------------------------------------------------------
# 三、写图：加密扩展名不能把编码器搞挂
# ----------------------------------------------------------------------

@pytest.mark.parametrize("suffix", [".png_", ".rpgmvp", ".png", ".PNG_"])
def test_imwrite_handles_encrypted_suffixes(tmp_path: Path, suffix: str) -> None:
    """`.png_` / `.rpgmvp` 不是真实图片格式，`cv2.imencode` 认不出来。

    以前 `imwrite_bgr` 直接 `cv2.imencode(p.suffix, ...)`，遇到这些
    扩展名会返回 False（只打一行 WARN），写入**静默失败**。
    """
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:, :, 0] = 123
    out = tmp_path / f"x{suffix}"
    assert imwrite_bgr(out, img) is True, f"{suffix} 写入失败"
    assert out.is_file() and out.stat().st_size > 0
    # 写出来的应该是一个**纯 PNG**（加密包装由适配器负责）
    assert out.read_bytes()[:8] == rc.PNG_MAGIC, (
        f"{suffix}: 应编码为 PNG，实际头 = {out.read_bytes()[:8].hex()}"
    )


def test_imwrite_rejects_empty_image(tmp_path: Path) -> None:
    assert imwrite_bgr(tmp_path / "e.png", np.zeros((0, 0, 3), dtype=np.uint8)) is False


# ----------------------------------------------------------------------
# 四、适配器的发现与回写
# ----------------------------------------------------------------------

def _make_encrypted_game(root: Path) -> tuple[Path, bytes]:
    """造一个带加密贴图的 MV 游戏（NW.js 布局）。"""
    import json

    key = bytes.fromhex("d41d8cd98f00b204e9800998ecf8427e")
    w = root / "www"
    (w / "data").mkdir(parents=True)
    (w / "img" / "system").mkdir(parents=True)
    (w / "img" / "pictures").mkdir(parents=True)
    (w / "fonts").mkdir(parents=True)
    (w / "js").mkdir(parents=True)
    (w / "data" / "System.json").write_text(
        json.dumps({
            "encryptionKey": key.hex(),
            "hasEncryptedImages": True,
            "gameTitle": "T",
            "terms": {"basic": ["Level", "Lv", "HP", "HP", "MP", "MP", "TP", "TP"]},
        }),
        encoding="utf-8",
    )
    (w / "data" / "MapInfos.json").write_text("[]", encoding="utf-8")
    # 两张加密贴图 + 一张明文
    (w / "img" / "system" / "Enc.rpgmvp").write_bytes(
        rc.encrypt(_png_bytes(64, 32), key)
    )
    (w / "img" / "pictures" / "Enc2.png_").write_bytes(
        rc.encrypt(_png_bytes(40, 20), key)
    )
    (w / "img" / "system" / "Plain.png").write_bytes(_png_bytes(30, 10))
    return key, key


def test_extract_images_includes_encrypted_assets(tmp_path: Path) -> None:
    """**核心断言**：加密贴图必须出现在候选列表里。

    修好之前这里只有明文 `Plain.png`，加密的 2 张全漏 ——
    真实游戏上就是"0 张候选贴图"。
    """
    game = tmp_path / "g"
    _make_encrypted_game(game)
    ad = _adapter()
    assets, rep = ad.extract_images(game)
    paths = sorted(a.path for a in assets)
    assert any(p.endswith(".rpgmvp") for p in paths), (
        f"`extract_images` 漏掉了 `.rpgmvp`：{paths}"
    )
    assert any(p.endswith(".png_") for p in paths), (
        f"`extract_images` 漏掉了 `.png_`：{paths}"
    )
    assert any(p.endswith("Plain.png") for p in paths), "明文图仍应被列出"
    assert len(assets) == 3, f"应列出 3 张，实际 {len(assets)}：{paths}"


def test_extract_images_warns_when_key_missing(tmp_path: Path) -> None:
    """有加密图但没 key：要**明确告警**，不能静默当成"没图"。"""
    game = tmp_path / "g"
    key, _ = _make_encrypted_game(game)
    # 抹掉 key
    import json

    sysp = game / "www" / "data" / "System.json"
    doc = json.loads(sysp.read_text(encoding="utf-8"))
    doc.pop("encryptionKey")
    sysp.write_text(json.dumps(doc), encoding="utf-8")

    from novaloc.images.io import _KEY_CACHE

    _KEY_CACHE.clear()
    ad = _adapter()
    assets, rep = ad.extract_images(game)
    assert rep.errors, "有加密贴图却读不到 key，必须报出来"
    assert any("加密" in e for e in rep.errors), f"告警内容不对：{rep.errors}"
    # 明文图仍应被列出
    assert any(a.path.endswith("Plain.png") for a in assets)


def test_apply_re_encrypts_rebuilt_textures(tmp_path: Path) -> None:
    """**核心断言**：回写加密贴图时必须重新加密。

    引擎只认带 `RPGMV` 头的格式。把纯 PNG 直接拷进去 = 游戏里图全裂。
    这条是这条链路上最容易被忘掉的逆操作（读侧解密了，写侧忘了加密）。
    """
    game = tmp_path / "g"
    key, _ = _make_encrypted_game(game)
    out = tmp_path / "out"
    ad = _adapter()

    # 模拟"重绘后的纯 PNG"
    rebuilt_png = tmp_path / "rebuilt" / "Enc.rpgmvp"
    rebuilt_png.parent.mkdir(parents=True)
    new_png = _png_bytes(64, 32)
    rebuilt_png.write_bytes(new_png)

    rel = "www/img/system/Enc.rpgmvp"
    res = ad.apply(
        game,
        out,
        units=[],
        translations={},
        rebuilt_images={rel: rebuilt_png},
    )
    assert res.ok, f"回写失败：{res.error} / {res.warnings}"
    assert res.files_written == 1, f"应写入 1 个文件，实际 {res.files_written}"
    dest = out / rel
    assert dest.is_file(), f"回写没落盘：{dest}"
    blob = dest.read_bytes()
    assert rc.is_rpgmv_encrypted(blob), (
        "回写的是纯 PNG，没有重新加密 —— 游戏会读不出这张图（图全裂）"
    )
    assert rc.decrypt(blob, key) == new_png, "重新加密后解回来应等于重绘结果"


def test_apply_keeps_plain_png_as_plain(tmp_path: Path) -> None:
    """**边界**：本来不加密的游戏不能被无端加密。

    没有 key（或图本来不是加密格式）时必须原样拷贝 ——
    给不加密的游戏包上加密头，图会直接读不出来。
    """
    game = tmp_path / "g"
    _make_encrypted_game(game)
    out = tmp_path / "out"
    ad = _adapter()

    rebuilt_png = tmp_path / "rebuilt2" / "Plain.png"
    rebuilt_png.parent.mkdir(parents=True)
    plain = _png_bytes(30, 10)
    rebuilt_png.write_bytes(plain)

    rel = "www/img/system/Plain.png"
    ad.apply(game, out, units=[], translations={}, rebuilt_images={rel: rebuilt_png})
    assert (out / rel).read_bytes() == plain, "明文图被改动了"
