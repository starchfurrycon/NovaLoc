"""翻译层：屏蔽占位符 → 调模型 → 校验还原 → 兜底守卫。

本包原先缺少 ``__init__.py``，靠命名空间包"碰巧能用"。那是个隐患：
``from novaloc.translate import placeholders`` 之所以成功，是因为
``novaloc`` 本身是常规包，命名空间子包才被解析出来 —— 一旦用
zip / 冻结 / 某些打包器分发就会静默失效。

这里刻意**不做即时导入**（不在 ``__init__`` 里 import 子模块），
原因有两个：

1. 子模块之间有真实的循环依赖风险（provider 依赖 placeholders，
   guards 又依赖 prompts），即时导入会把顺序问题变成导入即崩；
2. 子模块会向 ``core.registry`` 注册 provider，注册时机应该由调用方
   显式控制（见 ``ollama_setup`` / ``Providers``），而不是"一 import
   这个包就产生副作用"。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

#: 本包公开的子模块。写成显式列表而不是自动发现，是为了让
#: "这个包有哪些东西" 一眼可见，也便于 ruff/静态检查跟进。
_SUBMODULES = (
    "glossary",
    "guards",
    "json_parse",
    "memory",
    "ollama_client",
    "ollama_provider",
    "ollama_setup",
    "placeholders",
    "prompts",
    "vision_ollama",
)

__all__ = list(_SUBMODULES)


def __getattr__(name: str) -> Any:
    """允许 ``from novaloc.translate import ollama_provider`` 这种懒加载写法。"""
    if name in _SUBMODULES:
        mod = import_module(f"{__name__}.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_SUBMODULES})
