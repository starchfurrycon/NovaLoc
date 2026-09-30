"""输出 token 上限：本地模型批处理**必须**显式设，且不能一刀切。

## 真实事故

真实游戏（19048 句台词）跑 `translate` 看着**卡死**：进程活着、
只有 3 秒 CPU、Ollama 那侧 GPU 空转、`entries.jsonl` 十几分钟不动。
杀掉重来，**卡在同一个位置**。

逐块计时后真相出来：

==================== ==========
块                    耗时
==================== ==========
块 0                  6.3s
**块 1**              **384.3s**
块 2                  6.1s
**块 3**              **153.3s**
==================== ==========

根因：全代码库**没有一处**设 `num_predict`。Ollama 在这种情况下
不限制输出长度，于是模型一直生成、撞到 `num_ctx`（8192）才停
（`done_reason='length'`）。代价是整轮翻译从 2.5 小时变成 50 小时以上。

## 第一版修错了（本文件一半的测试在钉这一点）

先用的是一刀切 `48 × 条数`。跑飞那批从 384 秒降到 17.6 秒，
但另一些批次**反而涨到 153～183 秒** —— 上限太紧，正常 JSON 被截断，
解析失败后走"重试 → 逐条降级"（12 条 = 12 次调用），比不管还慢。

所以本文件同时钉住两件事：

* **必须有上限**（否则跑飞）；
* **上限要贴着内容**（否则截断更慢）——
  即"长批的上限必须明显大于短批"，而不是一个常数乘以条数。
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


# ----------------------------------------------------------------------
# 一、必须设：不设就是"跑飞"
# ----------------------------------------------------------------------

def test_num_predict_is_always_set() -> None:
    """**核心回归**：`num_predict` 必须出现在 options 里。

    漏了它就是回到"不限制输出"，也就是 384 秒／1500 秒那类批次。
    """
    p = _provider()
    for n in (1, 2, 12, 25, 40):
        opts = p._options(n_items=n, src_chars=100 * n)
        assert "num_predict" in opts, f"n_items={n} 时没设 num_predict"
        assert isinstance(opts["num_predict"], int)
        assert opts["num_predict"] > 0


def test_num_predict_never_zero_or_negative() -> None:
    """0 或负数会让 Ollama 行为异常（0 等于生成 0 个 token）。"""
    p = _provider()
    for n, chars in [(1, 0), (1, 1), (12, 0), (40, 0)]:
        assert p._num_predict(n, chars) > 0


def test_num_predict_bounded_by_ctx() -> None:
    """上限不得超过上下文窗口的一部分，否则等于没限制。"""
    p = _provider()
    ctx = p.cfg.ollama.num_ctx
    # 用极长内容逼出上限
    assert p._num_predict(40, 10_000_000) <= ctx // 3


# ----------------------------------------------------------------------
# 二、要贴着内容：长批的上限必须明显更大
# ----------------------------------------------------------------------

def test_longer_content_gets_higher_cap() -> None:
    """**回归（第一版修错的地方）**：上限必须随内容增长。

    一刀切 `48×条数` 时，12 条短对话和 12 条长对话拿到同一个上限，
    长的那批被截断 → 解析失败 → 重试 + 逐条降级 → 反而更慢（153～183 秒）。
    """
    p = _provider()
    short = p._num_predict(12, 150)     # 12 条短对话（真实数据里很常见）
    long_ = p._num_predict(12, 800)     # 12 条长对话
    assert long_ > short, (
        f"长内容的上限没有更大（短 {short} vs 长 {long_}）—— "
        "一刀切会让长批被截断"
    )


def test_cap_grows_with_item_count() -> None:
    p = _provider()
    caps = [p._num_predict(n, 40 * n) for n in (1, 5, 12, 25, 40)]
    assert caps == sorted(caps), f"上限没有随条数单调增长：{caps}"
    assert caps[-1] > caps[0]


def test_real_batch_sizes_get_enough_room() -> None:
    """用**真实数据**的规模验算：12 条短对话实际需要 ~460 token。

    实测 `eval_count` 在 460 左右（12 条短句）。上限必须高于它，
    否则就是在截断正常输出。
    """
    p = _provider()
    # 真实块：12 条，原文总长 150～400 字符
    for chars in (150, 250, 400):
        cap = p._num_predict(12, chars)
        assert cap >= 460, (
            f"12 条 / {chars} 字符的上限只有 {cap} token，低于实测所需的 460 —— 会截断"
        )


def test_fuse_limits_pathological_content() -> None:
    """保险丝：内容异常长时也不能无上限。"""
    p = _provider()
    per_item = p.cfg.ollama.max_output_tokens_per_item
    cap = p._num_predict(12, 1_000_000)
    assert cap <= per_item * 12 + 64, f"保险丝失效：{cap}"


# ----------------------------------------------------------------------
# 三、配置项本身
# ----------------------------------------------------------------------

def test_config_defaults_exist_and_are_sane() -> None:
    cfg = Config()
    assert cfg.ollama.max_output_tokens_per_item > 0
    assert cfg.ollama.max_output_char_factor > 1.0, "系数至少得大于 1"


def test_factor_can_be_tuned() -> None:
    """系数是可调的 —— 用户可能用别的模型（输出更长/更短）。"""
    cfg = Config()
    cfg.ollama.max_output_char_factor = 5.0
    ctx = Context(config=cfg, events=EventBus())
    p = OllamaTranslationProvider(ctx)
    big = p._num_predict(12, 400)
    assert big > 0


@pytest.mark.parametrize("n_items", [1, 2, 12, 25, 40])
def test_options_include_all_required_keys(n_items: int) -> None:
    """`num_predict` 加进来时别把原有选项弄丢（repeat_penalty 尤其重要：
    不设它 Ollama 用 1.0，批量翻译必然复读）。"""
    opts = _provider()._options(n_items=n_items, src_chars=50 * n_items)
    for key in ("temperature", "top_p", "num_ctx", "repeat_penalty", "repeat_last_n", "num_predict"):
        assert key in opts, f"options 里缺 {key}"
