r"""★ 配置字段分两类：**真的会发出去**的，和**只是记下来**的。

## 事故经过

`OllamaConfig` 里有 `flash_attention` 与 `kv_cache_type` 两个字段，
而 `_options()` **从来不读它们** —— 更糟的是，早先代码在
`return opts` **之后**还留了一段（不可达）代码，显然是想把它们
塞进 `options` 但没写完。

**留着比删掉更糟**：读代码的人会以为设置生效了，
调参调半天没变化，最后怀疑是模型问题。

## 为什么它们**不该**被发出去

这两个是 **Ollama 服务器**的环境变量：

* `OLLAMA_FLASH_ATTENTION=1`
* `OLLAMA_KV_CACHE_TYPE=q8_0`

**不是** `/api/chat` 的请求参数。放进 `options` 会被 Ollama
**静默忽略** —— 又是一次"看起来设置了、其实没有"。

所以正确做法是：

1. 请求参数里**只放** Ollama 真正认识的键；
2. 这两个字段保留（用来"记下你打算怎么配"），但 docstring
   必须写明**改它不会有效果**；
3. 用测试把这条边界钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.translate.ollama_provider import OllamaTranslationProvider  # noqa: E402


def _provider() -> OllamaTranslationProvider:
    return OllamaTranslationProvider(Context(config=Config(), events=EventBus()))


#: Ollama `/api/chat` 的 `options` 里**真正认识**的键。
#: 只列本工具会用到的那几个。
_KNOWN_OLLAMA_OPTIONS = {
    "temperature",
    "top_p",
    "num_ctx",
    "repeat_penalty",
    "repeat_last_n",
    "num_predict",
    # 下面这些 Ollama 也认，将来用到时再加进来
    "seed",
    "top_k",
    "min_p",
    "stop",
    "num_gpu",
    "num_thread",
    "tfs_z",
    "typical_p",
    "presence_penalty",
    "frequency_penalty",
    "mirostat",
    "mirostat_tau",
    "mirostat_eta",
    "penalize_newline",
    "num_keep",
    "num_batch",
}


def test_only_known_ollama_options_are_sent() -> None:
    """★ 发出去的键必须**全部**在 Ollama 认识的集合里。

    这是防"声明了但发不出去"的通用闸门：将来有人往
    `_options()` 里加字段时，这条会立刻拦住拼错的键
    （或被静默忽略的键）。
    """
    opts = _provider()._options(n_items=40, src_chars=3000)
    unknown = set(opts) - _KNOWN_OLLAMA_OPTIONS
    assert not unknown, (
        f"这些键不在 Ollama 的 options 里，会被静默忽略：{sorted(unknown)}"
    )


@pytest.mark.parametrize("dead", ["flash_attention", "kv_cache_type"])
def test_server_level_settings_are_not_sent_per_request(dead: str) -> None:
    """★ 服务器级设置不许出现在请求参数里。

    它们放进去会被**静默忽略** —— 用户会以为生效了。
    """
    opts = _provider()._options(n_items=1, src_chars=100)
    assert dead not in opts, (
        f"{dead} 是 Ollama **服务器**的环境变量，不是请求参数；"
        "放进 options 会被静默忽略"
    )


@pytest.mark.parametrize("dead", ["flash_attention", "kv_cache_type"])
def test_server_level_settings_are_documented_as_dead(dead: str) -> None:
    """★ 字段的 docstring 必须写明"改它不会有效果"。

    光把字段从请求里拿掉还不够 —— 用户看到配置里有这一项，
    就会去改它，然后奇怪为什么没变化。
    """
    from novaloc.core.config import OllamaConfig

    field = OllamaConfig.model_fields[dead]
    doc = (field.description or "") + "\n" + (
        getattr(OllamaConfig, "__doc__", "") or ""
    )
    assert "环境变量" in doc, f"{dead} 的说明里必须点明它是服务器环境变量"
    assert "不会" in doc or "静默" in doc, (
        f"{dead} 的说明里必须点明改它没效果/会被静默忽略"
    )


def test_num_predict_is_always_set() -> None:
    """`num_predict` 必须**永远**被设置。

    不设它 Ollama 就不限制输出长度，真实游戏上撞过
    384 秒、1500 秒以上的批次（模型停在 `done_reason='length'`）。
    """
    opts = _provider()._options(n_items=1, src_chars=10)
    assert "num_predict" in opts
    assert isinstance(opts["num_predict"], int)
    assert opts["num_predict"] > 0


def test_repeat_penalty_is_always_set() -> None:
    """`repeat_penalty` 必须被设置 —— Ollama 默认 1.0 等于关闭，批量必复读。"""
    opts = _provider()._options(n_items=1, src_chars=10)
    assert opts.get("repeat_penalty", 1.0) != 1.0


def test_no_unreachable_code_after_return() -> None:
    """★ `_options()` 的 `return` 之后不许有代码。

    早先正是"想加两个字段但没写完、留在 return 之后"
    造成了"看起来设置了、其实没有"。不可达代码是**误导**，不是无害。
    """
    import ast
    import inspect

    src = inspect.getsource(OllamaTranslationProvider._options)
    fn = ast.parse(src.lstrip()).body[0]
    ret_idx = next(
        i for i, n in enumerate(fn.body) if isinstance(n, ast.Return)
    )
    tail = fn.body[ret_idx + 1 :]
    # 只允许注释（注释不进 AST），所以尾部必须是空的
    assert not tail, (
        "`_options()` 在 return 之后还有代码（不可达），"
        f"会让人以为设置生效了：{[ast.dump(n)[:60] for n in tail]}"
    )
