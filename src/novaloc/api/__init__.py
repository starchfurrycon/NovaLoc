"""FastAPI 后端：HTTP + WebSocket 接口。

**注意 ``app`` 这个名字在本包里指的是子模块，不是 FastAPI 实例。**
``from .app import ...` 会让 import 机制把子模块绑到包属性 ``app`` 上，
而包属性一旦存在就不会再走模块级 ``__getattr__``（PEP 562 只在常规查找
失败时兜底），所以包级无法同时再提供一个叫 ``app`` 的实例 ——
这里不再假装可以，需要实例请用 :func:`get_app`。

实例本身仍然是**惰性**的：``import novaloc.api`` 不会构造应用，
CLI 的 ``serve`` 因此能精确控制谁在何时建 App。
"""

from .app import create_app, get_app
from .jobs import Job, JobManager, load_config, save_config

__all__ = ["Job", "JobManager", "create_app", "get_app", "load_config", "save_config"]
