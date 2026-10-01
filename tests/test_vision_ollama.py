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
# ★ 视觉兜底"每次都超时"的真正原因：三条互相冲突的指令
# ----------------------------------------------------------------------


def test_hint_does_not_impose_an_output_format() -> None:
    r"""★ `hint` 只能补充**场景**，不能规定**输出格式**。

    ## 实测证据（`qwen3-vl:4b`，同一张 `Loading.png`）

    ====================== ========= ======== ============ ==================
    调用方式                耗时      eval      done_reason  content
    ====================== ========= ======== ============ ==================
    不带 hint               **8.1s**  441      stop         ``'Now Loading…'``
    带 hint（旧行为）        **17.9s** **1024** length       **``''``**
    ====================== ========= ======== ============ ==================

    三次重复完全一致。旧 hint 是
    ``"这是游戏贴图里的文字，请只输出文字本身"``，而提示词要求
    "逐行原样列出" —— 两条互斥的指令让模型陷入思考循环，
    `num_predict` 全部烧在 `thinking` 里，`content` 是空串。

    而 `ocr.vlm_timeout_s` 默认 20 秒 ⇒ 每次都在超时边缘 ⇒
    连续 3 次失败即熔断 ⇒ **整个视觉兜底被静默关闭**，
    日志只说"连续失败 3 次"，看不出真正原因。

    所以这里钉住的是**形状**：hint 被包在场景说明里，
    而不是用"补充线索：<命令>"直接追加。
    """
    src = inspect.getsource(sys.modules["novaloc.translate.vision_ollama"])
    # 旧写法（把 hint 当成追加的命令）不许回来
    assert "补充线索：" not in src, (
        "hint 又被当成了一条规定输出格式的命令 —— 这会让模型陷入"
        "思考循环、content 变空串，实测 17.9 秒零结果"
    )
    # 新写法必须把 hint 明确标成"场景"
    assert "（场景：" in src

    # 调用方传来的 hint 也不许再规定输出格式。
    # 注意：注释里会引用那句旧 hint 作为反例，所以只看**代码行**，
    # 不能对整份源码做子串匹配（第一版就是这么误报的）。
    from novaloc.images import service as img_service

    code_lines = [
        ln.split("#", 1)[0]
        for ln in inspect.getsource(img_service).splitlines()
    ]
    code_only = "\n".join(code_lines)
    assert "请只输出文字本身" not in code_only, (
        "贴图兜底的 hint 又不能规定输出格式了（见本测试的实测数据）"
    )


def test_vision_call_caps_num_predict_and_disables_thinking() -> None:
    r"""★ 视觉调用必须有 ``num_predict`` 上限并关掉思考。

    不设上限时，推理模型会一路生成到 ``num_ctx``：实测
    **55 秒 / 3009 token / ``done_reason='length'`` / ``content`` 是空串**。
    配上 20 秒超时 ⇒ 必然超时 ⇒ 熔断。
    """
    from novaloc.translate.vision_ollama import OllamaVisionEngine

    src = inspect.getsource(OllamaVisionEngine._chat_vision)
    assert '"num_predict"' in src, "视觉调用没有 num_predict 上限"
    assert '"think": False' in src, "视觉调用没有关掉思考"

    # 上限可配，且默认值必须**存在**（0/负数回落到内置默认）
    cfg = Config()
    assert hasattr(cfg.ocr, "vision_max_tokens"), "缺少 ocr.vision_max_tokens 配置项"
    assert cfg.ocr.vision_max_tokens > 0
    engine = OllamaVisionEngine(Context(config=Config(), events=EventBus()))
    assert engine._vision_max_tokens() == cfg.ocr.vision_max_tokens

    # 显式设 0 时回落到内置默认（不能变成"无上限"）
    bad = Config()
    bad.ocr.vision_max_tokens = 0
    e2 = OllamaVisionEngine(Context(config=bad, events=EventBus()))
    assert e2._vision_max_tokens() == 1024


@pytest.mark.parametrize(
    "answer",
    [
        "（图片中无文字，按要求输出空）",
        "图片中没有显示任何文字内容",
        "There is no text in the image.",
        "（无文字）",
        "no text",
        "[]",
    ],
)
def test_no_text_meta_answers_are_rejected(answer: str) -> None:
    r"""★ 模型"图里没文字"时的元回答**不能**写进贴图。

    模型不会老实输出空串，而会回一句"（图片中无文字…）"。危险在于
    这句的**字母数字占比 0.67、长度 15 字符**，
    前三条判据（控制字符 / 字母数字占比 / 长度暴涨）**全部放行** ——
    于是会被当成"更可信的读数"覆盖掉原本正确的 OCR 结果。
    """
    from novaloc.images.service import TextureTranslator, _is_no_text_marker

    assert _is_no_text_marker(answer), f"没认出元回答：{answer!r}"
    t = TextureTranslator(Context(config=Config(), events=EventBus()))
    assert not t._vlm_answer_is_usable("Now Loading...", answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Now Loading...\n-Demons Roots-",
        "NEW GAME",
        "Level 3: HP!",
        "Continue",
    ],
)
def test_real_text_answers_are_still_accepted(answer: str) -> None:
    """反向：真的贴图文字不许被元回答过滤器误杀。"""
    from novaloc.images.service import _is_no_text_marker

    assert not _is_no_text_marker(answer), f"误杀了真实文字：{answer!r}"


def test_no_text_marker_only_fires_on_short_answers() -> None:
    """长回答里出现 "no text" 多半是在**解释**，不该整个丢掉。"""
    from novaloc.images.service import _is_no_text_marker

    long_one = (
        "There is no text in the image itself, but the small print at "
        "the bottom reads Game Over and the button says Continue."
    )
    assert not _is_no_text_marker(long_one)


def test_vlm_fallback_stays_off_by_default() -> None:
    r"""★ 视觉兜底默认**关**，理由必须留在配置文档里。

    ## 这条测试守的是"结论"，不是"代码"

    这个默认值被改过两次，每次都因为**测量工具本身坏了**：

    * 第一次的结论是"0 变好 / 1 持平 / 2 更差 / 其余读不出，每次 20～48 秒"
      —— 但那次调用链有三个缺陷（JSON+bbox 提示词、互斥 hint、
      没有 ``num_predict`` 上限），实测旧组合 **21.9 秒**已经超过
      默认超时 20 秒 ⇒ **必然**返回空串。"读不出"是测量伪影。
    * 修好工具后重测（真实 DemonsRoots，40 张 / 295 块）：
      低置信块占 **26.4%**（``<0.8``），对 30 个块逐块重读得到
      **变好 0 / 变差 0 / 持平 3 / 闸门拒 16 / 空 11**，平均 13.4 秒/块。

    **真正的理由是它会编造**：对照实验里，纯白 200x60、近白噪声、
    极小 34x12 纯白都稳定输出 ``'Now Loading...\n-Demons Roots-'``
    —— 那是本游戏加载画面上的字，**输入里根本没有**。只有真的写了
    ``GAME OVER`` 的那张读对了。

    所以：默认关，且文档里必须留着"它会编造"这个理由，
    不能只留一句"效果不好"（那会让人以为调调参数就能救）。
    """
    from novaloc.core.config import Config

    cfg = Config()
    assert cfg.ocr.vlm_fallback is False, "视觉兜底被默认打开了"

    # 理由必须写在**源码文档**里。
    # 注意不能用 `model_fields[...].description` —— pydantic 不会把
    # 字段的 docstring 搬进 description（实测拿到的是空串），
    # 所以直接读源码。
    from novaloc.core import config as config_mod

    src = inspect.getsource(config_mod)
    seg = src[src.index("vlm_fallback: bool") : src.index("vlm_threshold: float")]
    assert "编造" in seg, "配置文档里没写清真正原因（它会编造文字）"
    assert "Now Loading" in seg, "配置文档里没有留下那条关键对照实验"
    # 也要留下"第一次测量不可信"的记录，避免以后又照着旧数据下结论
    assert "不可信" in seg or "测量伪影" in seg
    # 预算类配置必须都在（它们和 vlm_timeout_s 是一组）
    for field in ("vlm_threshold", "vlm_timeout_s", "vlm_max_per_image",
                  "vlm_fail_limit", "vision_max_tokens"):
        assert field in type(cfg.ocr).model_fields, f"缺少 {field}"


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
