"""OCR 结果的磁盘缓存。

## 为什么必须有

两个原因，都很实际。

### 1. OCR 在真实游戏资产上是**不确定**的

同一张图跑两次，识别出的块数/文字可能不同（模型推理与缩放路径上都有
非确定性）。于是：

* 一次跑到第 600 张崩了，重跑一遍**结果和上一次不一样** ——
  产物无法复现，用户报的问题我复现不出来；
* 调一个下游参数（比如重绘边距）就要把 750 张图全部重认一遍，
  而 OCR 是整条贴图链路里最慢的一步（实测 medium 档约 0.6 秒/张，
  750 张 ≈ 8 分钟，还没算 inpaint 与重绘）。

缓存把"最慢、且不确定"的那一步变成**确定且只做一次**：
重跑总是拿到同一批框和文字，下游怎么调都不影响上游结论。

### 2. 缓存键必须覆盖"会让结果变化的所有输入"

只按文件内容做键是不够的：换了模型档位（tiny/small/medium）、
调了置信度下限、或者换了 min_box_size，结果都会变。
所以键里带上这些参数 + **引擎版本标识**。

引擎版本标识是防"升级了模型却命中旧缓存"这种最难查的问题 ——
产物看起来正常，但用的是上一个模型的识别结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 缓存格式版本。**改 `OcrPage`/块字段的序列化方式时必须 +1**，
#: 否则旧缓存会被当成新格式读，产出缺字段的块（下游会莫名其妙地空引用）。
CACHE_VERSION = 1


def cache_key(
    content: bytes,
    *,
    tier: str,
    min_score: float,
    max_side: int,
    lang: str,
    engine_tag: str,
) -> str:
    """算缓存键。

    参数全部参与哈希：任何一个变了结果都可能不同。
    """
    h = hashlib.sha256()
    h.update(f"v{CACHE_VERSION}\x00".encode())
    h.update(str(engine_tag).encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(str(tier).encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(f"{float(min_score):.6f}".encode())
    h.update(b"\x00")
    h.update(str(int(max_side)).encode())
    h.update(b"\x00")
    h.update(str(lang).encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(content)
    return h.hexdigest()


def _digest(content: bytes) -> str:
    """内容哈希。用 blake2b（比 sha256 快，用于大图字节）。"""
    return hashlib.blake2b(content, digest_size=16).hexdigest()


class OcrCache:
    """一个目录，按内容+参数存 `OcrPage` 的 JSON。

    目录结构用**两级前缀**（``ab/abcdef...json``）：一个游戏可能有一千多张
    贴图，单目录塞几千个文件在 Windows 上会变慢。
    """

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root) if root is not None else None
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _path(self, key: str) -> Path:
        assert self.root is not None
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """取缓存（未命中返回 ``None``）。"""
        if not self.enabled:
            return None
        p = self._path(key)
        try:
            if not p.is_file():
                self.misses += 1
                return None
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 缓存坏了就当没命中 —— 绝不能让缓存问题变成功能问题
            log.debug("OCR 缓存读取失败（按未命中处理）%s：%s", p, exc)
            self.misses += 1
            return None
        if not isinstance(doc, dict) or doc.get("v") != CACHE_VERSION:
            self.misses += 1
            return None
        self.hits += 1
        return doc

    def put(self, key: str, payload: dict[str, Any]) -> None:
        """写缓存。失败**只记日志**，不能影响主流程。"""
        if not self.enabled:
            return
        p = self._path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            doc = dict(payload)
            doc["v"] = CACHE_VERSION
            # 先写临时文件再替换：并发/中断时不会留下半个 JSON
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(p)
            self.writes += 1
        except OSError as exc:
            log.debug("OCR 缓存写入失败（忽略）%s：%s", p, exc)

    def clear(self) -> int:
        """清空缓存，返回删掉的文件数。"""
        if not self.enabled or not self.root.is_dir():
            return 0
        n = 0
        for p in self.root.rglob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


def default_cache_root() -> Path | None:
    """默认缓存目录（``<data_root>/cache/ocr``）；拿不到就返回 ``None``。"""
    try:
        from ..core.paths import cache_dir

        return cache_dir() / "ocr"
    except Exception:  # noqa: BLE001
        return None


__all__ = ["CACHE_VERSION", "OcrCache", "cache_key", "default_cache_root"]
