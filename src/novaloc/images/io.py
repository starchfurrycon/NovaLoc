"""跨平台的图像读写。

必须存在的原因：**OpenCV 的 ``imread``/``imwrite`` 不支持非 ASCII 路径。**
本工具的工作目录、游戏安装目录经常带中文（例如 ``D:\\小工具\\翻译工具``），
直接用 ``cv2.imwrite`` 会静默失败（只打一行 WARN，返回 False），
排查起来非常费时间。所以统一走这里的包装函数：

* 读：先用 ``np.fromfile`` 把字节读进来，再 ``cv2.imdecode``；
* 写：先 ``cv2.imencode`` 编码，再用 ``Path.write_bytes`` 落盘。
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


def imread_bgr(path: str | Path) -> np.ndarray | None:
    """读图（BGR ndarray）。路径含中文也能正常工作。"""
    p = Path(path)
    if not p.is_file():
        log.debug("文件不存在：%s", p)
        return None
    try:
        buf = np.fromfile(str(p), dtype=np.uint8)
        if buf.size == 0:
            return None
        img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
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
    ext = p.suffix.lower() or ".png"
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
