"""JSON / 网络层边界的文本净化：消除**孤立代理项**。

## 为什么需要这个模块

一次真实游戏的翻译跑了 **27 分 6 秒**后整个崩掉，报：

    Error serializing to JSON: UnicodeEncodeError: 'utf-8' codec can't
    encode character '\\uddd1' in position 8: surrogates not allowed

孤立代理项（U+D800–U+DFFF 中**不构成合法代理对**的那些）在 UTF-8 里
**没有任何合法表示**，因此：

* `"\\uddd1".encode("utf-8")` → `UnicodeEncodeError`；
* httpx 的 `json=` 参数会先 `json.dumps(...).encode("utf-8")`，
  于是在**发请求之前**就抛异常（不是 Ollama 服务端的错 ——
  我一开始误判成服务端，实际触发点在客户端）；
* `UnicodeEncodeError` 继承自 `ValueError`，而调用方的重试逻辑只捕获
  `(ProviderError, OllamaError, ...)`，**一个都不匹配**，于是异常穿过
  整个重试层，让整轮翻译作废。

数据本身是干净的（源游戏 `www/data/*.json` 是合法 UTF-8，抽取产物、
术语表、配置全都无代理项）；污染是**运行期从模型回复里进来的** ——
模型偶尔把 `\\uddd1` 当字面量吐出来，`json.loads` 会忠实还原成
一个孤立代理项，然后它被当作下一批的输入发回去，于是错误反复发生。

## 处理策略：替换，不丢弃

换成 U+FFFD（`�`），**保持长度与位置不变**，与 Python 自身
`errors="replace"` 的语义一致。理由：

* 这是**编码污染**，不是"需要用户决策的数据问题"，没有可用的上游信息；
* 整条丢弃会让玩家看到一个**完全没翻译**的条目，比一个 `�` 更糟；
* 替换保留长度，占位符/换行的位置不会漂移。

**只碰孤立代理项**：真正的非 BMP 字符（emoji 如 U+1F600）在 Python 3 里
是**单个码点**，不是代理对，必须原样保留 —— 误伤 emoji 会破坏游戏里的
图标文本。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "has_lone_surrogate",
    "sanitize_for_json",
    "sanitize_tree",
]

REPLACEMENT = "\ufffd"
"""U+FFFD REPLACEMENT CHARACTER，与 `errors="replace"` 一致。"""


def has_lone_surrogate(s: str) -> bool:
    """``s`` 里是否含孤立代理项。"""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def sanitize_for_json(value: Any) -> Any:
    """把字符串里的孤立代理项换成 U+FFFD；非字符串原样返回。

    非字符串（数字、None、bool）直接放行 —— 它们不可能含代理项，
    且保持类型不变对 JSON 语义很重要（`True` 不能变成 `"True"`）。
    """
    if not isinstance(value, str):
        return value
    if not has_lone_surrogate(value):
        return value
    return "".join(
        REPLACEMENT if 0xD800 <= ord(c) <= 0xDFFF else c for c in value
    )


def sanitize_tree(value: Any) -> Any:
    """递归净化 dict / list / tuple / set 里的所有字符串。

    **返回新对象，不就地修改** —— 调用方（例如 provider）可能在净化后
    还要用原对象做别的判断，就地改会制造难以追踪的耦合。

    字典的键也一并净化：键同样要编码进 JSON。
    """
    if isinstance(value, str):
        return sanitize_for_json(value)
    if isinstance(value, dict):
        return {
            sanitize_for_json(k) if isinstance(k, str) else k: sanitize_tree(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(sanitize_tree(v) for v in value)
    if isinstance(value, set):
        return {sanitize_tree(v) for v in value}
    return value
