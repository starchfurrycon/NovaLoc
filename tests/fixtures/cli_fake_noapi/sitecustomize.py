"""测试钩子：让任何 novaloc.api* 的导入失败，模拟"后端尚未就绪"。

只在 .scratch 里用，不会被安装或打包。
"""

from __future__ import annotations

import importlib.abc
import sys


class _BlockApi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname.startswith("novaloc.api"):
            raise ImportError("simulated: 后端尚未就绪")
        return None


sys.meta_path.insert(0, _BlockApi())
