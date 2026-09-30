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

    @property
    def _host(self) -> str:
        """Ollama 服务地址。环境变量 ``OLLAMA_HOST`` 优先于配置。

        注意 ``resolved_ollama_host()`` 是**方法不是属性** ——
        少写括号会拿到 bound method，随后 ``rstrip`` 报
        ``'function' object has no attribute 'rstrip'``。
        """
        return self.cfg.resolved_ollama_host()

    def _client(self) -> Any:
        """构造客户端。

        ``OllamaClient.__init__`` 收的是 **host 字符串**，不是配置对象。
        这里之前写成 ``OllamaClient(cfg)``（cfg 是 ``OllamaConfig``），
        于是 ``host.rstrip("/")`` 抛
        ``'OllamaConfig' object has no attribute 'rstrip'``，
        被 ``available()`` 的 except 吞成"Ollama 探测失败" ——
        视觉兜底**整条链路从来没工作过**，而日志看起来只是"VLM 不可用"。
        """
        from ..translate.ollama_client import OllamaClient

        return OllamaClient(self._host, timeout=float(self.cfg.ollama.request_timeout_s))

    def available(self) -> tuple[bool, str]:
        o = self.cfg.ollama
        if not o.enabled:
            return False, "Ollama 未启用"
        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001
            return False, f"无法创建 Ollama 客户端：{exc}"

        try:
            # ``OllamaClient`` 上判断服务是否在跑的方法是 ``is_running()``，
            # 没有 ``ping()``。之前调 ``ping()`` 会 AttributeError，
            # 同样被吞成"探测失败"。
            if not client.is_running():
                return False, f"连不上 Ollama（{self._host}）"
            tags = client.list_models()
        except Exception as exc:  # noqa: BLE001
            return False, f"Ollama 探测失败：{exc}"

        if not self._has_model(tags, self._model):
            return False, f"未拉取视觉模型 {self._model}（已安装：{', '.join(tags) or '无'}）"
        return True, "就绪"

    @staticmethod
    def _has_model(names: list[str], model: str) -> bool:
        """容忍 ``name`` 与 ``name:latest`` 的写法差异。"""
        if model in names:
            return True
        if ":" not in model and f"{model}:latest" in names:
            return True
        base = model.split(":")[0]
        return any(n.split(":")[0] == base for n in names)

    # ------------------------------------------------------------------

    def read_text(self, image: Any, *, hint: str = "", timeout_s: float | None = None) -> str:
        """读图里的文字。失败返回空串（调用方会保留原 OCR 结果）。

        ``timeout_s`` 是读取超时（秒）；超时**不抛异常**，返回空串，
        调用方保留原 OCR 结果。贴图兜底会传一个较短的值，
        避免 thinking 模型拖死整条流水线。
        """
        ok, why = self.available()
        if not ok:
            log.debug("视觉模型不可用：%s", why)
            return ""

        prompt = VISION_READ_PROMPT
        if hint:
            prompt = f"{prompt}\n\n补充线索：{hint}"

        try:
            raw = self._chat_vision(prompt, image, read_timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            # 这里的异常也包含 httpx.ReadTimeout（它继承自 Exception，
            # 没有更专用的基类）。保持"失败返回空串"的契约不变 ——
            # `OcrEngine` 协议就是这么定的，调用方靠空串判断"没读到"。
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

    def _chat_vision(self, prompt: str, image: Any, *, read_timeout: float | None = None) -> str:
        """Ollama 的 ``/api/chat`` 支持在 message 里带 ``images``（base64）。

        ``read_timeout`` 覆盖读取超时（秒）。调用方（贴图兜底）会传一个
        比全局 ``ollama.request_timeout_s`` 短得多的值：全局超时是按
        "翻译一整段文字"定的，而兜底是**可有可无**的逐块重读 ——
        实测本机 ``qwen3-vl:4b`` 对 389x57 的小裁剪要 10～48 秒
        （thinking 模型，一次回答生成 600～750 个推理 token），
        让每个低置信块都能等 48 秒会把整个贴图阶段拖死。
        """
        import httpx

        o = self.cfg.ollama
        host = self._host.rstrip("/")
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt, "images": [_png_b64(image)]}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        timeout = httpx.Timeout(
            connect=5.0,
            read=float(read_timeout if read_timeout is not None else o.request_timeout_s),
            write=60.0,
            pool=5.0,
        )
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{host}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message") or {}
        return str(msg.get("content") or "")


def _extract_text(raw: str) -> str:
    """从模型输出里取出文字。

    模型经常包一层解释或代码围栏，甚至回一段 JSON。这里按
    "JSON 数组 → JSON 对象 → 围栏 → 裸文本" 的顺序降级处理。

    **数组这一支是必须的**：``VISION_READ_PROMPT`` 明确要求模型
    "只输出 JSON 数组，每项形如 ``{"text": "...", "bbox": [...]}``"。
    而原先的实现只认 JSON **对象**，数组解析不出来就一路降级到"裸文本"，
    把**整段 JSON 字符串本身**当成识别结果返回 —— 实测得到
    ``'[\\n  {"text": "Level", "bbox": [50, 300, 170, 400]}, ...]'``。
    这种"看起来有输出、其实是原始 JSON"的失败最难发现：文字非空，
    于是兜底会把这段乱码当作"更可信的读数"覆盖掉原本正确的 OCR 结果。
    """
    if not raw:
        return ""
    s = raw.strip()

    # 1) JSON 数组（视觉读字的标准返回形状）
    arr = _extract_json_array(s)
    if arr is not None:
        parts: list[str] = []
        for it in arr:
            if isinstance(it, str):
                if it.strip():
                    parts.append(it.strip())
            elif isinstance(it, dict):
                for k in ("text", "content", "t", "文字"):
                    v = it.get(k)
                    if isinstance(v, str) and v.strip():
                        parts.append(v.strip())
                        break
        # 数组为空（模型回答"图里没文字"）→ 明确返回空串，
        # **不要**继续降级去把 "[]" 当文字返回
        return "\n".join(parts).strip()

    # 2) JSON 对象
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

    # 3) 剥掉 ``` 围栏
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", s, re.S)
    if m:
        s = m.group(1).strip()

    # 4) 去掉常见的客套前缀
    #
    # **必须循环到不再变化**，不能"每条模式试一次"就结束。
    # 原因：客套话可以叠着出现 —— 实测 `好的，图中文字是：Continue`。
    # 第一条模式要匹配开头，但此刻开头是"好的"，匹配失败；等最后一条把
    # "好的，"剥掉，开头才变成"图中文字是："，可循环已经走完了。
    # 于是返回值里留着 `图中文字是：Continue`（注释里预言过这个坑，
    # 但只调整顺序并不能解决：任意顺序都挡不住"前缀叠前缀"）。
    # 循环收敛对任意顺序都成立，是真正正确的做法。
    _PREFIX_PATTERNS = (
        r"图中(的)?文字(是|为)[:：]?",
        r"以下是[^\n]*?[:：]",
        r"识别结果[^\n]*?[:：]",
        r"好的[，,]?",
    )
    for _ in range(8):  # 上限防病态输入下反复自我匹配
        before = s
        for pat in _PREFIX_PATTERNS:
            s = re.sub(r"^(?:" + pat + r")\s*", "", s)
        if s == before:
            break
    # 兜底也要挡一下：整段看起来就是 JSON 的话，宁可返回空串
    # （返回空串 → 调用方保留原 OCR 结果，安全；返回 JSON → 覆盖成乱码，危险）
    if s[:1] in "[{" and s[-1:] in "]}":
        return ""
    return s.strip()


def _extract_json_array(raw: str) -> list[Any] | None:
    """从输出里取出 JSON 数组；取不到返回 ``None``（区别于"取到空数组"）。"""
    if not raw:
        return None
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```", s, re.S)
    if m:
        s = m.group(1).strip()
    cands = [s, *re.findall(r"\[.*\]", s, re.S)]
    for cand in cands:
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, list):
            return obj
    return None


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
