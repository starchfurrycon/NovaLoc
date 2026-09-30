"""兼容转发：净化逻辑的实际实现在 :mod:`novaloc.core.sanitize`。

## 为什么搬家了

模块最早放在 `translate/` 下，因为**触发它的第一个现场**是发 HTTP 请求
（httpx 的 `json=` 编码失败）。但拿到真实崩溃的完整 traceback 之后发现，
真正的崩点在**产物落盘**：

    stages.py:416  self.ws.save_entries(...)
    workspace.py:311  _write_jsonl(...)
    workspace.py:89   fh.write(it.model_dump_json())
    PydanticSerializationError: ... '\\uddd1' ...

`save_entries` 属于 `core.workspace`，它不该 import `novaloc.translate`
（那会把 httpx、prompts 等一整条翻译链路拖进核心层，
也会让 `core -> translate -> core.registry` 形成绕圈）。

所以实现搬到 `core/sanitize.py`，这里只做转发，保留原有 import 路径
（`from novaloc.translate.sanitize import sanitize_for_json`）继续可用。
"""

from __future__ import annotations

from ..core.sanitize import (  # noqa: F401
    REPLACEMENT,
    has_lone_surrogate,
    sanitize_for_json,
    sanitize_tree,
)

__all__ = [
    "REPLACEMENT",
    "has_lone_surrogate",
    "sanitize_for_json",
    "sanitize_tree",
]
