"""视觉大模型兜底：当 OCR 认不准时，让 VLM 再读一遍。

**为什么需要兜底，又为什么不能当主力**

实测数据（旋转文本检测，Hmean 越高越好）：

    PP-OCRv6 small    93.7
    PP-OCRv6 medium   93.8
    Qwen3-VL-235B      2.1
    GPT-5.5           10.0

VLM 在**定位**（文字框在哪儿、是不是斜的）上被专用 OCR 碾压了两个数量级，
所以**绝不能用 VLM 做检测**。

但 VLM 有一个 OCR 比不了的能力：它能"猜"。美术字、描边字、
低对比度字上 PP-OCRv6 会识别失败或给出低置信度结果，
而 VLM 靠语义上下文往往能读对。

所以分工是：

* **检测和常规识别** —— PP-OCRv6（快、准、框准）；
* **兜底重读** —— 只在 PP-OCRv6 置信度低于阈值时，把**那个区域**
  裁出来交给 VLM 重读，用来**修正文字内容**，但**绝不改动文字框位置**。

这样既拿到了 VLM 的语义纠错能力，又不会因为 VLM 定位不准而毁掉排版。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any

from ..core.registry import Context, register
from ..translate.prompts import VISION_READ_PROMPT

log = logging.getLogger(__name__)


def _png_b64(image: Any) -> str:
    """把图像编码成 base64 PNG。用内存流，不落盘。"""
    import numpy as np
    from PIL import Image as PILImage

    if isinstance(image, PILImage.Image):
        img = image.convert("RGB")
    else:
        arr = np.asarray(image)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, -1)
        elif arr.ndim == 3 and arr.shape[2] == 4:
            # 透明背景合成到白底 —— VLM 在透明区上会读到乱码
            a = arr[:, :, 3:4].astype("float32") / 255.0
            arr = (arr[:, :, :3].astype("float32") * a + 255.0 * (1 - a)).astype("uint8")
        elif arr.ndim == 3 and arr.shape[2] == 3:
            arr = arr[:, :, ::-1]  # BGR → RGB（OpenCV 读进来的顺序）
        else:
            raise ValueError(f"无法处理的图像形状：{arr.shape}")
        img = PILImage.fromarray(arr.astype("uint8"))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@register("vision", "ollama")
class OllamaVisionEngine:
    """通过 Ollama 调用本地视觉大模型（默认 qwen3-vl）。"""

    name = "ollama"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.config

    @property
    def _model(self) -> str:
        return self.cfg.ollama.vision_model

    def available(self) -> tuple[bool, str]:
        cfg = self.cfg.ollama
        if not cfg.enabled:
            return False, "Ollama 未启用"
        from ..translate.ollama_client import OllamaClient

        try:
            client = OllamaClient(cfg)
            if not client.ping():
                return False, f"连不上 Ollama（{cfg.host}）"
            tags = client.list_models()
        except Exception as exc:  # noqa: BLE001
            return False, f"Ollama 探测失败：{exc}"

        if self._model not in tags:
            return False, f"未拉取视觉模型 {self._model}"
        return True, "就绪"

    # ------------------------------------------------------------------

    def read_text(self, image: Any, *, hint: str = "") -> str:
        """读图里的文字。失败返回空串（调用方会保留原 OCR 结果）。"""
        ok, why = self.available()
        if not ok:
            log.debug("视觉模型不可用：%s", why)
            return ""

        prompt = VISION_READ_PROMPT
        if hint:
            prompt = f"{prompt}\n\n补充线索：{hint}"

        try:
            raw = self._chat_vision(prompt, image)
        except Exception as exc:  # noqa: BLE001
            log.warning("视觉模型读字失败：%s", exc)
            return ""

        return _extract_text(raw)

    def describe_style(self, image: Any) -> dict[str, Any]:
        """让模型描述文字样式。

        注意：**返回值只作为参考**，不直接用于渲染。
        真正的渲染参数（前景色/背景色/描边色）一律从原图像素采样得到 ——
        模型对颜色的判断不可靠，而像素采样是确定的。
        """
        ok, _ = self.available()
        if not ok:
            return {}
        from ..translate.prompts import VISION_STYLE_PROMPT

        try:
            raw = self._chat_vision(VISION_STYLE_PROMPT, image)
        except Exception as exc:  # noqa: BLE001
            log.warning("视觉模型描述样式失败：%s", exc)
            return {}
        return _extract_json(raw)

    # ------------------------------------------------------------------

    def _chat_vision(self, prompt: str, image: Any) -> str:
        """Ollama 的 ``/api/chat`` 支持在 message 里带 ``images``（base64）。"""
        import httpx

        cfg = self.cfg.ollama
        host = cfg.host.rstrip("/")
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt, "images": [_png_b64(image)]}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        timeout = httpx.Timeout(connect=5.0, read=cfg.timeout_s, write=60.0, pool=5.0)
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{host}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message") or {}
        return str(msg.get("content") or "")


def _extract_text(raw: str) -> str:
    """从模型输出里取出文字。

    模型经常包一层解释或代码围栏，甚至回一段 JSON。这里按
    "JSON 对象 → 围栏 → 裸文本" 的顺序降级处理。
    """
    if not raw:
        return ""
    s = raw.strip()

    # 1) 可能是 {"text": "..."} 或 {"lines": [...]}
    obj = _extract_json(s)
    if obj:
        for key in ("text", "content", "result", "文字", "译文"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key in ("lines", "items", "texts"):
            v = obj.get(key)
            if isinstance(v, list) and v:
                parts = []
                for it in v:
                    if isinstance(it, str):
                        parts.append(it)
                    elif isinstance(it, dict):
                        for k in ("text", "content", "t"):
                            if isinstance(it.get(k), str):
                                parts.append(it[k])
                                break
                if parts:
                    return "\n".join(parts).strip()

    # 2) 剥掉 ``` 围栏
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", s, re.S)
    if m:
        s = m.group(1).strip()

    # 3) 去掉常见的客套前缀
    s = re.sub(r"^(好的[，,]?|以下是[^\n]*[:：]|图中(的)?文字(是|为)[:：]?)\s*", "", s)
    return s.strip()


def _extract_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```", s, re.S)
    if m:
        s = m.group(1).strip()
    for cand in (s, *re.findall(r"\{.*\}", s, re.S)):
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            return obj
    return {}


__all__ = ["OllamaVisionEngine"]
