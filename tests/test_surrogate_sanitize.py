"""孤立代理项（lone surrogate）必须在写入 JSON / 网络请求体之前被处理掉。

## 实测：一次真实游戏的翻译在跑到 27 分钟后整个崩掉

`novaloc run <真实 MV 游戏> --stage translate` 在 1343 条上跑了 **27 分 6 秒**
之后失败，日志里是：

    Error serializing to JSON: UnicodeEncodeError: 'utf-8' codec can't
    encode character '\\uddd1' in position 8: surrogates not allowed

## ⚠️ 我第一版的定位是错的，这里记下正确的

看到 `Error serializing to JSON` 时我以为是 **Ollama 服务端**在序列化时炸了
（这句话确实不在本仓库里）。**错了。** 后来拿到完整 traceback 才看清：

    stages.py:416    self.ws.save_entries(list(merged.values()))
    workspace.py:311 _write_jsonl(...)
    workspace.py:89  fh.write(it.model_dump_json())
    PydanticSerializationError: ...

崩点在**产物落盘**（pydantic 的 `model_dump_json()`），不在请求发送。
而且当时 `entries.jsonl.tmp` 里已经躺着 **848 条完成的译文** ——
全部白跑。真正的原因和修法见 `tests/test_entry_persistence_surrogate.py`。

真实的崩溃路径是「**解析→写入条目→落盘**」。请求体那条路（httpx 的
`json=` 会在发请求前编码）**也**会抛同类异常，属于同一个根因的另一个
出口，所以两端都要净化。本文件覆盖的是 `sanitize` 这一层本身的正确性。

## 缺陷 A —— 异常类型没被接住

`_call_batch` / 逐条降级 / 补空三处的 `except` 只列了
`(ProviderError, OllamaError, ModelMissing, OllamaNotRunning)`。
`UnicodeEncodeError` 继承自 `ValueError`，**一个都不匹配**，
于是它穿过整个重试层逃到 `stage_translate`，让整轮翻译作废。

## 缺陷 B —— 没有净化

模型偶尔把 `\\uddd1` 当字面量吐出来，`json.loads` 会**忠实还原**成一个
孤立代理项，然后它被写进条目、被当作下一批的输入发回去 ——
错误会反复发生（实测 批 11/39/36/1 都在报同一个字符）。

## 为什么在"边界"净化而不是"发现就报错"

孤立代理项是**无法表示的字节序列**，没有任何合法 UTF-8 编码。
所以它不是"需要用户决策的数据问题"，而是"必须在编码之前消除的污染"。
做法与 Python 自身的 `errors="replace"` 一致：换成 U+FFFD（`�`），
保留长度与位置，让译文其余部分仍可用。整条丢弃会让玩家看到一个
**完全没翻译**的条目，比一个 `�` 更糟。

## 踩过的坑：源码里不能写 `\\uXXXX` 字面量

本文件第一版在 docstring 里写了单反斜杠的 `\\uddd1`。
CPython 的词法分析会在**编译期**把它解码成真的代理项放进 code object，
于是**这个测试文件自己 import 不进来**（写 `.pyc` 时
`UnicodeEncodeError`），pytest 收集阶段直接报错。
所有"扫字节""扫字符"的检查都说没问题 —— 问题在**编译产物**里。
守卫见 `tests/test_no_source_surrogates.py`。
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

LONE_LOW = chr(0xDDD1)
LONE_HIGH = chr(0xD801)
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

    ⚠️ 这里必须用 `chr()` 构造，**不能**写 `"\\ud83d\\ude00"` 字面量：
    CPython 的词法分析会把 `\\uXXXX` 转义**在编译期**变成一个真的代理项，
    放进 code object 的常量池 —— 随后写 `.pyc` 时
    `UnicodeEncodeError: surrogates not allowed`，**这个测试文件自己都
    import 不进来**。代理项只能存在于运行期字符串里。
    （这不是理论：第一版就是这么写的，pytest 收集阶段直接报错。）
    """
    s = chr(0xD83D) + chr(0xDE00)
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
