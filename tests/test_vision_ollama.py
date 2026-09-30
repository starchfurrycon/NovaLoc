"""视觉兜底适配器的回归测试。

这个适配器曾经**整条链路都没工作过**，而失败表现为"VLM 不可用"，
看起来像是模型或环境问题，实际是三个我们自己的 bug：

1. ``OllamaClient(cfg)`` —— 构造函数收的是 **host 字符串**，不是配置对象，
   于是 ``host.rstrip("/")`` 抛
   ``'OllamaConfig' object has no attribute 'rstrip'``；
2. ``client.ping()`` —— ``OllamaClient`` 上判断服务在跑的方法是
   ``is_running()``，没有 ``ping()``，又是 AttributeError；
3. ``cfg.timeout_s`` —— 配置里的字段名是 ``request_timeout_s``。

以上三个都被 ``available()`` 的 ``except Exception`` 吞成
"Ollama 探测失败：…"，而 ``Doctor`` 只显示"不可用"。用户永远不会
想到去查这几行。

还有一个更隐蔽的（第 4 个）：``_extract_text`` 只认 JSON **对象**，
而 ``VISION_READ_PROMPT`` 明确要求模型"只输出 JSON **数组**"。
数组解析不出来就一路降级到"裸文本"，把**整段原始 JSON 字符串**
当成识别结果返回。这个最危险 —— 文字非空，于是
``_vlm_rescue`` 会把它当作"更可信的读数"**覆盖掉原本正确的 OCR 结果**。

所以本测试锁四件事：构造函数/自检不再抛异常、数组能正确解析、
空数组必须返回空串（而不是 "[]"）、整段 JSON 绝不能被当文字返回。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.translate.vision_ollama import (  # noqa: E402
    OllamaVisionEngine,
    _extract_json_array,
    _extract_text,
)

Q = chr(34)
NL = chr(10)

ARR = (
    "["
    + "{"
    + Q
    + "text"
    + Q
    + ": "
    + Q
    + "Level"
    + Q
    + ", "
    + Q
    + "bbox"
    + Q
    + ": [50,300,170,400]}, {"
    + Q
    + "text"
    + Q
    + ": "
    + Q
    + "Up"
    + Q
    + ", "
    + Q
    + "bbox"
    + Q
    + ": [180,300,250,400]}]"
)


def _engine() -> OllamaVisionEngine:
    return OllamaVisionEngine(Context(config=Config(), events=EventBus()))


# ----------------------------------------------------------------------
# 构造函数 / 自检不得再抛 AttributeError
# ----------------------------------------------------------------------


def test_engine_constructs_without_attribute_error() -> None:
    """``_client()`` 必须能建出客户端（曾经因传错参数类型而抛）。"""
    eng = _engine()
    client = eng._client()
    assert client.host.startswith("http")
    assert not client.host.endswith("/")


def test_host_comes_from_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OLLAMA_HOST`` 应优先于配置文件里的地址。"""
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:59999/")
    eng = _engine()
    assert eng._host == "http://127.0.0.1:59999/"


def test_available_never_raises_attribute_error() -> None:
    """自检只允许返回 (bool, str)，不允许把 AttributeError 吞成"探测失败"。

    这里连不上 Ollama 也没关系 —— 关键是**失败原因里不能出现
    ``has no attribute``**（那说明是代码 bug，不是环境问题）。
    """
    eng = _engine()
    ok, why = eng.available()
    assert isinstance(ok, bool)
    assert isinstance(why, str) and why
    assert "has no attribute" not in why, f"又是代码 bug 被吞成环境错误：{why!r}"


def test_source_has_no_ping_call() -> None:
    """``ping()`` 在 OllamaClient 上不存在 —— 防止将来又被写回来。"""
    src = inspect.getsource(sys.modules["novaloc.translate.vision_ollama"])
    assert ".ping(" not in src, "OllamaClient 没有 ping()，应为 is_running()"


def test_timeout_field_name_is_right() -> None:
    """配置字段是 ``request_timeout_s``，不是 ``timeout_s``。"""
    cfg = Config().ollama
    assert hasattr(cfg, "request_timeout_s")
    assert not hasattr(cfg, "timeout_s"), "字段名写错了"
    src = inspect.getsource(sys.modules["novaloc.translate.vision_ollama"])
    assert "cfg.timeout_s" not in src
    assert "o.timeout_s" not in src


# ----------------------------------------------------------------------
# 输出解析
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "raw", "want"),
    [
        ("数组（标准返回形状）", ARR, "Level" + NL + "Up"),
        ("空数组（图里没文字）", "[]", ""),
        ("围栏包数组", "```json" + NL + ARR + NL + "```", "Level" + NL + "Up"),
        ("数组前带解释", "好的，识别结果如下：" + NL + ARR, "Level" + NL + "Up"),
        ("对象形式", "{" + Q + "text" + Q + ": " + Q + "Continue" + Q + "}", "Continue"),
        (
            "对象的 lines",
            "{" + Q + "lines" + Q + ": [" + Q + "A" + Q + ", " + Q + "B" + Q + "]}",
            "A" + NL + "B",
        ),
        ("纯文本", "Continue", "Continue"),
        ("客套前缀（单层）", "好的，Continue", "Continue"),
        ("客套前缀（叠两层）", "好的，图中文字是：Continue", "Continue"),
        ("数字数组必须挡掉", "[1,2,3]", ""),
        ("空输入", "", ""),
        (
            "数组里混字符串",
            "[" + Q + "OK" + Q + ", {" + Q + "text" + Q + ": " + Q + "Cancel" + Q + "}]",
            "OK" + NL + "Cancel",
        ),
    ],
)
def test_extract_text(label: str, raw: str, want: str) -> None:
    assert _extract_text(raw) == want, label


def test_extract_text_never_returns_raw_json() -> None:
    """最危险的一条：整段 JSON 绝不能被当成识别文字返回。

    否则 ``_vlm_rescue`` 会把这段乱码当作"更可信的读数"
    覆盖掉原本正确的 OCR 结果 —— 静默损坏，且看起来"兜底生效了"。
    """
    for raw in (
        ARR,
        "{" + Q + "bbox" + Q + ": [1,2,3]}",
        "[{" + Q + "unknown_key" + Q + ": 1}]",
        "[" + Q + "a" + Q + ", 1, 2]",
        '{"a": 1}',
    ):
        got = _extract_text(raw)
        # 空串是**正确**且安全的结果（调用方据此保留原 OCR 结果），
        # 所以判据是"要么空，要么不是 JSON 形状"。
        # 直接写 `got[:1] in "[{"` 会因 `"" in "[{"` 为真而把空串误判成失败。
        looks_like_json = bool(got) and got[:1] in "[{" and got[-1:] in "]}"
        assert not looks_like_json, f"返回了原始 JSON：{got!r}"


def test_extract_json_array_distinguishes_none_from_empty() -> None:
    """``None``（没找到数组）必须与 ``[]``（找到了但是空）区分开。

    区分不了的话，"图里没有文字"会被当成"解析失败"继续降级，
    最终把 ``"[]"`` 这两个字符当文字返回。
    """
    assert _extract_json_array("[]") == []
    assert _extract_json_array("hello") is None
    assert _extract_json_array("") is None
    parsed = _extract_json_array(ARR)
    assert isinstance(parsed, list) and len(parsed) == 2
    assert parsed[0]["text"] == "Level"


def test_read_text_returns_empty_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """VLM 不可用时必须返回空串（调用方据此保留原 OCR 结果）。"""
    eng = _engine()
    monkeypatch.setattr(eng, "available", lambda: (False, "测试用：不可用"))
    assert eng.read_text(object()) == ""
