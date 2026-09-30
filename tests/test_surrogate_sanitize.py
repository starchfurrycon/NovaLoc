"""孤立代理项（lone surrogate）必须在进入 HTTP 请求体之前被处理掉。

## 实测：一次真实游戏的翻译在跑到 27 分钟后整个崩掉

`novaloc run <真实 MV 游戏> --stage translate` 在 1343 条上跑了 **27 分 6 秒**
之后失败，报：

    Error serializing to JSON: UnicodeEncodeError: 'utf-8' codec can't
    encode character '\\uddd1' in position 8: surrogates not allowed

## 定位过程（结论和直觉不一样，所以记下来）

1. `Error serializing to JSON` **不在本仓库任何代码里**（全仓库搜不到），
   所以它来自 Ollama 服务端的响应文本 —— 一开始以为是服务端的锅。
2. 实际复现出来的是客户端异常：`OllamaClient._request` 把 `json=payload`
   交给 httpx，httpx 在**发请求之前**就 `json.dumps(...).encode("utf-8")`，
   于是 `UnicodeEncodeError` 在**我们这边**抛出。
   （我一开始把这条归因给 Ollama，是错的。）
3. 触发条件是：请求体里任何字符串含孤立代理项（U+D800–U+DFFF）。
   实测最小复现：源文本 `"Rending Claw\\uddd1 deals damage."`。
4. 源数据是**干净的**：重新抽取 1343 条、扫工作区所有 JSON、
   扫内置术语表，都没有代理项；游戏 `www/data/*.json` 也是合法 UTF-8。
   所以代理项是**运行期从模型回复里进来的**（模型偶尔会把 `\\uddd1`
   当字面量吐出来，`json.loads` 忠实还原成孤立代理项）。

## 为什么它致命（两个独立缺陷）

**缺陷 A —— 异常类型没被接住。** `_call_batch` / 逐条降级 / 补空
三处的 `except` 只列了 `(ProviderError, OllamaError, ModelMissing,`
`OllamaNotRunning)`。`UnicodeEncodeError` 继承自 `ValueError`，
**一个都不匹配**，于是它穿过整个重试层逃到 `stage_translate`，
让整轮翻译作废。

**缺陷 B —— 没有输入净化。** 模型回复里带回的孤立代理项被原样用作
下一批的输入（也写进了条目），于是错误会**反复**发生：
`批 11/39/36/1` 都在报同一个字符。

## 为什么在"边界"净化而不是"发现就报错"

孤立代理项是**无法表示的字节序列**，没有任何合法 UTF-8 编码。
所以它不是"需要用户决策的数据问题"，而是"必须在写入网络层之前
消除的编码污染"。做法与 Python 自身的 `errors="replace"` 一致：
换成 U+FFFD（`�`），保留长度与位置，让译文其余部分仍可用。
整条丢弃会让玩家看到一个**完全没翻译**的条目，比一个 `�` 更糟。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from novaloc.translate.sanitize import (  # noqa: E402
    has_lone_surrogate,
    sanitize_for_json,
    sanitize_tree,
)

LONE_LOW = "\uddd1"
LONE_HIGH = "\ud801"
REPLACEMENT = "\ufffd"


# ----------------------------------------------------------------------
# 基本净化
# ----------------------------------------------------------------------

def test_lone_surrogate_is_replaced() -> None:
    out = sanitize_for_json(f"Rending Claw{LONE_LOW} deals damage.")
    assert LONE_LOW not in out
    assert out == f"Rending Claw{REPLACEMENT} deals damage."
    # 长度不变，位置不变 —— 译文里其余内容仍然对得上
    assert len(out) == len(f"Rending Claw{LONE_LOW} deals damage.")


def test_clean_text_is_untouched() -> None:
    s = "正在加载... / Now Loading... ① 「Attack Up」 \\C[6] {name}"
    assert sanitize_for_json(s) == s


def test_normal_emoji_and_astral_chars_survive() -> None:
    """**关键**：真正的非 BMP 字符（emoji）不能被杀掉。

    Python 里 emoji 是**单个**码点（U+1F600），不是代理对；
    只有**孤立**代理项才是非法。净化逻辑必须只碰后者 ——
    误伤 emoji 会让游戏里的图标/表情文本被破坏。
    """
    s = "You got a 🗡 and 😀!"
    assert sanitize_for_json(s) == s
    assert not has_lone_surrogate(s)


def test_valid_surrogate_pair_as_two_chars_is_still_lone() -> None:
    """两个**相邻的孤立代理项**在 Python 字符串里是两个非法字符。

    注意这和"一个 emoji"不同：emoji 在 Python 3 里就是 U+1F600 一个码点。
    有人可能以为 `"\\ud83d\\ude00"` 是 emoji —— 它不是，那是两个代理项，
    无法编码，必须净化。
    """
    s = "\ud83d\ude00"
    out = sanitize_for_json(s)
    assert out == REPLACEMENT * 2


def test_both_high_and_low_are_handled() -> None:
    assert LONE_HIGH not in sanitize_for_json(f"a{LONE_HIGH}b")
    assert LONE_LOW not in sanitize_for_json(f"a{LONE_LOW}b")


def test_non_str_passthrough() -> None:
    assert sanitize_for_json(42) == 42
    assert sanitize_for_json(None) is None
    assert sanitize_for_json(True) is True


def test_has_lone_surrogate_detects() -> None:
    assert has_lone_surrogate(f"x{LONE_LOW}y")
    assert not has_lone_surrogate("x😀y")
    assert not has_lone_surrogate("普通中文")


# ----------------------------------------------------------------------
# 树净化（请求体）
# ----------------------------------------------------------------------

def test_sanitize_tree_walks_nested_payloads() -> None:
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "ok"},
            {"role": "user", "content": f"bad{LONE_LOW}here"},
        ],
        "options": {"stop": [f"a{LONE_HIGH}", "b"]},
    }
    out = sanitize_tree(payload)
    assert out["messages"][1]["content"] == f"bad{REPLACEMENT}here"
    assert out["options"]["stop"][0] == f"a{REPLACEMENT}"
    assert out["messages"][0]["content"] == "ok"


def test_sanitize_tree_handles_tuples_and_dict_keys() -> None:
    out = sanitize_tree({"k": (f"a{LONE_LOW}", {"n": f"b{LONE_HIGH}"})})
    assert out["k"][0] == f"a{REPLACEMENT}"
    assert out["k"][1]["n"] == f"b{REPLACEMENT}"


def test_sanitize_tree_does_not_mutate_input() -> None:
    payload = {"messages": [{"content": f"x{LONE_LOW}"}]}
    sanitize_tree(payload)
    assert payload["messages"][0]["content"] == f"x{LONE_LOW}", "净化不该就地改调用方的对象"


# ----------------------------------------------------------------------
# 端到端：净化后的请求体真的能编码出去
# ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [LONE_LOW, LONE_HIGH])
def test_sanitized_payload_is_encodable(bad: str) -> None:
    """**这就是那个 27 分钟的崩溃**：未净化时 httpx 直接抛异常。"""
    payload = {
        "model": "translategemma:4b",
        "messages": [{"role": "user", "content": f"Rending Claw{bad} deals damage."}],
        "stream": False,
        "format": "json",
    }
    # 先确认未净化确实会炸（否则这个测试就失去意义了）
    with pytest.raises(UnicodeEncodeError):
        httpx.Request("POST", "http://127.0.0.1:11434/api/chat", json=payload).read()

    # 净化后必须能正常构造请求
    req = httpx.Request(
        "POST", "http://127.0.0.1:11434/api/chat", json=sanitize_tree(payload)
    )
    assert req.read()


def test_client_chat_sanitizes_before_encoding() -> None:
    """`OllamaClient.chat` 必须在编码前净化，而不是把异常抛给调用方。"""
    from novaloc.translate.ollama_client import OllamaClient

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"message": {"content": "ok"}, "model": "m"}

    class _FakeHttp:
        def request(self, method: str, url: str, **kw: object) -> _FakeResp:
            captured.update(kw)
            return _FakeResp()

    client = OllamaClient(host="http://127.0.0.1:11434")
    # `client` 是只读 property，所以替换底层的 `_client`
    client._client = _FakeHttp()  # type: ignore[assignment]
    # 含孤立代理项也不该抛 UnicodeEncodeError
    res = client.chat("m", [{"role": "user", "content": f"a{LONE_LOW}b"}])
    assert res.text == "ok"
    sent = captured.get("json")
    assert isinstance(sent, dict)
    assert LONE_LOW not in str(sent["messages"])
