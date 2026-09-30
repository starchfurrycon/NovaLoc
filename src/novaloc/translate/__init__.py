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

import logging
from importlib import import_module
from typing import Any

_log = logging.getLogger(__name__)

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
    return sorted({*globals(), *_SUBMODULES, "register_providers"})


# ---------------------------------------------------------------------------
# provider 注册引导
# ---------------------------------------------------------------------------

#: 含 ``@register("translate", ...)`` 的模块。**必须显式导入**才会注册 ——
#: 装饰器只在模块被 import 时执行，而"没人 import"就是一个极易漏掉的
#: 空注册表：``Providers.resolve_translate()`` 会报
#: "translate 适配器 'ollama' 未注册。已注册：（空）"，
#: 用户看到的是"没有可用的翻译适配器"，但环境其实完全正常。
#
#: 这里刻意用**显式列表**而不是遍历 ``pkgutil`` 自动导入：自动导入会执行
#: 包内任意模块的顶层代码（包括未来可能存在的、有网络或文件副作用的模块），
#: 一个"注册一下 provider"的动作不该有这个权限。
_PROVIDER_MODULES = (
    "ollama_provider",
    "vision_ollama",
)

_bootstrapped = False


def register_providers(*, force: bool = False) -> list[str]:
    """导入所有 provider 模块，触发 ``@register`` 装饰器。

    幂等：重复调用只做一次实际导入（``force=True`` 可强制重跑，
    给测试用）。返回本次导入的模块名，便于断言与排错。
    单个模块导入失败**不**应让整个翻译层不可用 —— 记 WARN 后继续，
    因为某个可选依赖缺失（比如 vision 走的不同后端）不该连带
    把文本翻译也弄坏。
    """
    global _bootstrapped
    if _bootstrapped and not force:
        return []
    _bootstrapped = True
    loaded: list[str] = []
    for name in _PROVIDER_MODULES:
        try:
            import_module(f"{__name__}.{name}")
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 - 可选依赖缺失不该致命
            _log.warning("翻译 provider 模块 %s 导入失败：%s", name, exc)
    return loaded
