"""引擎适配器注册与调度。

按优先级依次尝试识别，取置信度最高的适配器。
新增引擎只需继承 :class:`~novaloc.engines.base.EngineAdapter`
并用 ``@register("engine", "<id>")`` 注册。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.registry import Context
from .base import ApplyResult, EngineAdapter, EngineInfo

log = logging.getLogger(__name__)

# 导入即注册（顺序不影响调度，调度看 priority）
from . import loose as _loose  # noqa: E402,F401
from . import renpy as _renpy  # noqa: E402,F401
from . import rpgmaker as _rpgmaker  # noqa: E402,F401
from . import unity as _unity  # noqa: E402,F401

_ALL: list[type[EngineAdapter]] = []


def _discover() -> list[type[EngineAdapter]]:
    global _ALL
    if _ALL:
        return _ALL
    found: list[type[EngineAdapter]] = [c for c in _CLASSES if c is not None]
    found.sort(key=lambda c: c.priority)
    _ALL = found
    return _ALL


def _resolve(name: str) -> type[EngineAdapter] | None:
    """把注册名映射到类。"""
    mapping: dict[str, type[EngineAdapter]] = {
        "rpgmaker": _rpgmaker.RpgMakerAdapter,
        "renpy": _renpy.RenPyAdapter,
        "unity": _unity.UnityAdapter,
        "loose": _loose.LooseFilesAdapter,
    }
    return mapping.get(name)


#: 显式列出所有适配器类。用显式表而不是靠注册中心反射，
#: 是因为注册中心在包导入期可能还没被填满，显式更可靠也更好读。
_CLASSES: tuple[type[EngineAdapter] | None, ...] = (
    _rpgmaker.RpgMakerAdapter,
    _renpy.RenPyAdapter,
    _unity.UnityAdapter,
    _loose.LooseFilesAdapter,
)


def available_engines() -> list[tuple[str, str]]:
    """返回 ``[(id, 显示名)]``。"""
    return [(c.id, c.display_name) for c in _discover()]


def detect_engine(game_dir: str | Path, ctx: Context) -> EngineInfo:
    """识别游戏引擎，返回置信度最高的结果。"""
    root = Path(game_dir)
    best = EngineInfo(engine_id="unknown", display_name="未知引擎", root=root)
    for cls in _discover():
        try:
            ad = cls(ctx)
            info = ad.detect(root)
        except Exception as exc:  # noqa: BLE001 - 一个适配器出错不该影响其它
            log.debug("适配器 %s 识别异常：%s", cls.id, exc)
            continue
        if info.confidence > best.confidence:
            best = info
    return best


def get_adapter(engine_id: str, ctx: Context) -> EngineAdapter | None:
    """按 id 取适配器实例。"""
    for cls in _discover():
        if cls.id == engine_id:
            return cls(ctx)
    return None


__all__ = [
    "ApplyResult",
    "EngineAdapter",
    "EngineInfo",
    "available_engines",
    "detect_engine",
    "get_adapter",
]
