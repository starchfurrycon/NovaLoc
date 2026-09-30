"""模型/引擎适配器的注册中心。

四类可替换组件：

* ``translate`` —— 文本翻译（Ollama / 本地 GGUF / 未来的云 API）
* ``ocr``       —— 贴图文字检测与识别（RapidOCR / PaddleOCR / VLM）
* ``inpaint``   —— 抹除原文字（LaMa / MIGAN / OpenCV 兜底）
* ``vision``    —— 视觉大模型（美术字识别、风格描述）

用装饰器注册，靠名字解析，实例按需创建并缓存。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import (
    GlossaryEntry,
    ImageTextBlock,
    TextUnit,
    TranslationEntry,
)


class ProviderError(RuntimeError):
    """适配器不可用或调用失败。"""


@dataclass
class Context:
    """传给所有适配器的运行时上下文。"""

    config: Any
    events: Any
    workspace: Any = None
    logger: Any = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def cache_get(self, key: str, factory) -> Any:  # noqa: ANN001
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]


# --------------------------------------------------------------------------
# 协议定义
# --------------------------------------------------------------------------


@dataclass
class TranslateItem:
    unit: TextUnit
    glossary: list[GlossaryEntry] = field(default_factory=list)
    context_lines: list[str] = field(default_factory=list)


@runtime_checkable
class TranslationProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """返回 ``(是否可用, 说明)``。不抛异常。"""
        ...

    def translate_batch(self, items: list[TranslateItem], target_lang: str) -> list[TranslationEntry]:
        """批量翻译。返回顺序与 ``items`` 一一对应。失败时抛 :class:`ProviderError`。"""
        ...


@runtime_checkable
class OcrEngine(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def detect_and_recognize(self, image: Any, *, lang_hint: str | None = None) -> list[ImageTextBlock]:
        """输入 PIL.Image / ndarray，返回带框与文本的块。"""
        ...


@runtime_checkable
class InpaintEngine(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def inpaint(self, image: Any, mask: Any) -> Any:
        """``image``/``mask`` 为 ndarray（mask 单通道 0/255），返回修复后的 ndarray。"""
        ...


@runtime_checkable
class VisionEngine(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def read_text(self, image: Any, *, hint: str = "") -> str:
        """读取图中文字。"""
        ...

    def describe_style(self, image: Any) -> dict[str, Any]:
        """描述文字风格（颜色/描边/字重/是否艺术字）。"""
        ...


# --------------------------------------------------------------------------
# 注册中心
# --------------------------------------------------------------------------

_REGISTRY: dict[str, dict[str, type]] = {
    "translate": {},
    "ocr": {},
    "inpaint": {},
    "vision": {},
    "engine": {},
}


def register(kind: str, name: str):
    """把类注册进某一类组件。"""

    if kind not in _REGISTRY:
        raise ValueError(f"未知组件类别：{kind}")

    def deco(cls: type) -> type:
        cls.provider_name = name  # type: ignore[attr-defined]
        _REGISTRY[kind][name] = cls
        return cls

    return deco


def list_registered(kind: str) -> list[str]:
    """列出某一类里已注册的组件名。"""
    return sorted(_REGISTRY.get(kind, {}))


class Providers:
    """按名称解析并缓存适配器实例。"""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._instances: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def names(self, kind: str) -> list[str]:
        return sorted(_REGISTRY.get(kind, {}))

    def get(self, kind: str, name: str) -> Any:
        key = (kind, name)
        with self._lock:
            if key in self._instances:
                return self._instances[key]
            cls = _REGISTRY.get(kind, {}).get(name)
            if cls is None:
                raise ProviderError(
                    f"{kind} 适配器 '{name}' 未注册。已注册：{', '.join(self.names(kind)) or '（空）'}"
                )
            try:
                inst = cls(self.ctx)
            except TypeError as exc:
                raise ProviderError(f"{kind}/{name} 构造失败：{exc}") from exc
            self._instances[key] = inst
            return inst

    def translate_provider(self, name: str) -> Any:
        return self.get("translate", name)

    def resolve_translate(self) -> Any:
        """按配置挑一个当前可用的翻译适配器，主选不可用就依次回退。"""
        cfg = self.ctx.config
        order = [cfg.translate.primary_provider, *cfg.translate.fallback_providers]
        errors: list[str] = []
        for name in order:
            if not name:
                continue
            try:
                p = self.get("translate", name)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            ok, why = p.available()
            if ok:
                return p
            errors.append(f"{name}: {why}")
        raise ProviderError("没有可用的翻译适配器：\n  " + "\n  ".join(errors or ["（未配置）"]))

    def diagnostics(self) -> dict[str, list[dict[str, Any]]]:
        """给 UI 用的健康检查。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for kind in _REGISTRY:
            rows: list[dict[str, Any]] = []
            for name in self.names(kind):
                try:
                    inst = self.get(kind, name)
                    ok, why = inst.available()
                except Exception as exc:  # noqa: BLE001
                    ok, why = False, str(exc)
                rows.append({"name": name, "available": ok, "detail": why})
            out[kind] = rows
        return out


def registry_snapshot() -> dict[str, list[str]]:
    return {k: sorted(v) for k, v in _REGISTRY.items()}
