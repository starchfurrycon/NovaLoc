"""跨平台的图像读写。

必须存在的原因：**OpenCV 的 ``imread``/``imwrite`` 不支持非 ASCII 路径。**
本工具的工作目录、游戏安装目录经常带中文（例如 ``D:\\小工具\\翻译工具``），
直接用 ``cv2.imwrite`` 会静默失败（只打一行 WARN，返回 False），
排查起来非常费时间。所以统一走这里的包装函数：

* 读：先用 ``np.fromfile`` 把字节读进来，再 ``cv2.imdecode``；
* 写：先 ``cv2.imencode`` 编码，再用 ``Path.write_bytes`` 落盘。

另外这里还负责**透明地解密 RPG Maker 加密贴图**（``.rpgmvp`` / ``.png_``）——
理由见下面 :func:`imread_bgr` 的说明。
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from ..engines.rpgmv_crypt import (
    DecryptError,
    decode_resource,
    find_encryption_key,
    is_encrypted_image_name,
    is_rpgmv_encrypted,
)

log = logging.getLogger(__name__)

#: 每个游戏目录的 encryptionKey 缓存（一张图一次会读很多遍，别反复解析 JSON）
_KEY_CACHE: dict[str, bytes | None] = {}


def _find_game_root(p: Path) -> Path | None:
    """从一个贴图路径往上找"游戏根"（含 `data/System.json` 的那一层）。

    加密贴图的有效路径形如 ``<游戏根>/www/img/system/Loading.rpgmvp``，
    所以要从文件往上走到含 ``data`` 或 ``www/data`` 的目录。
    """
    for parent in p.parents:
        if (parent / "data" / "System.json").is_file():
            return parent
        if (parent / "www" / "data" / "System.json").is_file():
            return parent
    return None


def encryption_key_for(path: str | Path) -> bytes | None:
    """取这张图所属游戏的解密 key（带缓存）。"""
    p = Path(path)
    root = _find_game_root(p)
    if root is None:
        return None
    key = str(root)
    if key not in _KEY_CACHE:
        _KEY_CACHE[key] = find_encryption_key(root)
    return _KEY_CACHE[key]


def imread_bgr(path: str | Path) -> np.ndarray | None:
    """读图（BGR ndarray）。路径含中文也能正常工作。

    **加密贴图会被透明解密。** RPG Maker MV/MZ 默认开启资源加密，
    真实游戏里绝大多数贴图是 ``.rpgmvp`` / ``.png_``。如果这里不处理，
    那些图会在 ``cv2.imdecode`` 处失败（头 16 字节是 ``RPGMV``，
    不是 PNG 签名），然后被报成"无法读取图片"——
    实测一个 574 MB 的 MZ 游戏因此 `images_scan` 报"0 张候选贴图"。

    写回时由适配器重新加密（见 `rpgmaker.apply`），
    所以这里解密不会让游戏收到明文图片。
    """
    p = Path(path)
    if not p.is_file():
        log.debug("文件不存在：%s", p)
        return None
    try:
        buf = np.fromfile(str(p), dtype=np.uint8)
        if buf.size == 0:
            return None
        raw = buf.tobytes()
        if is_rpgmv_encrypted(raw):
            key = encryption_key_for(p)
            if key is None:
                log.warning(
                    "贴图 %s 是 RPG Maker 加密格式，但读不到 encryptionKey，跳过", p
                )
                return None
            try:
                raw = decode_resource(raw, key, expect="png")
            except DecryptError as exc:
                log.warning("解密贴图失败 %s：%s", p, exc)
                return None
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        return img
    except Exception as exc:  # noqa: BLE001
        log.warning("读取图片失败 %s：%s", p, exc)
        return None


def imread_rgba(path: str | Path) -> np.ndarray | None:
    """读图并保证是 4 通道 BGRA。原图没有 alpha 就补一个全不透明的。"""
    img = imread_bgr(path)
    if img is None:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 4:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)


def imwrite_bgr(path: str | Path, image: np.ndarray) -> bool:
    """写图。路径含中文也能正常工作，返回是否成功。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("创建目录失败 %s：%s", p.parent, exc)
        return False
    if image is None or image.size == 0:
        log.warning("拒绝写入空图像：%s", p)
        return False
    ext = p.suffix.lower()
    # RPG Maker 的加密扩展名（`.rpgmvp` / `.png_`）不是真实图片格式，
    # `cv2.imencode` 认不出来会直接返回 False（而且只打一行 WARN）。
    # 遇到它们按 PNG 编码；真正"包成加密格式"由适配器回写时做
    # （见 `rpgmaker.apply`），这里只负责把像素编码出来。
    if is_encrypted_image_name(p):
        ext = ".png"
    ext = ext or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
        log.warning("不支持的图片扩展名 %s，按 PNG 写入：%s", ext, p)
        ext = ".png"
    try:
        ok, buf = cv2.imencode(ext, image)
        if not ok:
            log.warning("编码失败：%s", p)
            return False
        p.write_bytes(buf.tobytes())
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("写入图片失败 %s：%s", p, exc)
        return False


def alpha_from(img: np.ndarray) -> np.ndarray:
    """取出 alpha 通道；没有就返回全 255。"""
    if img.ndim == 3 and img.shape[2] == 4:
        return img[:, :, 3]
    return np.full(img.shape[:2], 255, dtype=np.uint8)


__all__ = ["alpha_from", "imread_bgr", "imread_rgba", "imwrite_bgr"]
