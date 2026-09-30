"""NovaLoc 核心层：路径、配置、工作区、事件、适配器注册。"""

from __future__ import annotations

from .config import Config, get_config, set_config
from .events import Event, EventBus, ProgressThrottle
from .registry import (
    Context,
    InpaintEngine,
    OcrEngine,
    ProviderError,
    Providers,
    TranslateItem,
    TranslationProvider,
    VisionEngine,
    register,
    registry_snapshot,
)

__all__ = [
    "Config",
    "Context",
    "Event",
    "EventBus",
    "InpaintEngine",
    "OcrEngine",
    "ProgressThrottle",
    "ProviderError",
    "Providers",
    "TranslateItem",
    "TranslationProvider",
    "VisionEngine",
    "get_config",
    "register",
    "registry_snapshot",
    "set_config",
]
