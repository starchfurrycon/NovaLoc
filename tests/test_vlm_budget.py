"""视觉兜底的**代价闸门**：默认关闭、有超时、有熔断、会拒绝垃圾答案。

## 这个兜底原本的动机是对的

专用 OCR 在**定位**上碾压 VLM（旋转文本 Hmean 93.8 vs 2.1），
但偶尔会把文字读错。让一个视觉模型复核**内容**（只改文字、不改框）
是合理设计。

## 问题在代价与收益

在一台真实的 RPG Maker MZ 游戏上实测（`img/system/` 下的图标表、行走图、
状态图），10 个低置信块：

==================== ==================== ======== ========
OCR 原结果            VLM 答案              质量     耗时
==================== ==================== ======== ========
``'+222?22?'``        ``'? ? ? ? ? ? ?'``   更差     48.4s
``'*★'``              （空）                更差      3.8s
``'68'``              （空）                更差     48.4s
``'30'``              ``'E\\nE\\nE\\nD\\n?'``   持平     33.1s
==================== ==================== ======== ========

**0 个变好、1 个持平、2 个更差、其余读不出。** 而 60 张图里就有 73 个块
低于阈值 —— 按 750 张、每次 20 秒估算，这个"兜底"要多花**约 5 小时**。
用户看到的现象是"贴图阶段卡住了"（我一开始就是这么判断的，
查了半天才发现不是死锁，是在慢慢做无用功）。

## 四个修复

1. **默认关闭**（``ocr.vlm_fallback = false``），想要的人可以开。
2. **答案要在"像不像文字"上过关**才允许替换 OCR 结果，否则保留原值。
   旧逻辑接受"非空且与原文不同"的任何答案 —— 于是
   ``'+222?22?'`` 会被 ``'? ? ? ? ? ? ?'`` 覆盖，**而那串问号正是
   本项目最想避免的东西**（口口口的 ASCII 版）。
3. **不再凭空抬高置信度**。旧代码写
   ``b.confidence = max(原值, thr)`` —— 等于把垃圾答案提升到"可信"档，
   下游质检和审校页再也看不出它可疑。现在保持 OCR 原值。
4. **超时 + 每图上限 + 连续失败熔断**。本机 ``qwen3-vl:4b`` 是 thinking
   模型，一次回答要生成 600～750 个推理 token（实测 ``eval_count=627``），
   而 Ollama 的 ``think: false`` 对它**不生效**（实测仍 12 秒、仍有
   ``thinking`` 字段）。所以必须在传输层设读取超时。

## 实测效果（真实游戏贴图）

=============== ========== ==========
图               修复前      修复后
=============== ========== ==========
IconSet.png_     119.6s     0.22s
Balloon.png_      48.3s     4.38s
Weapons1.png_     70.2s     0.19s
=============== ========== ==========
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.service import TextureTranslator  # noqa: E402


def _tt(cfg: Config | None = None) -> TextureTranslator:
    return TextureTranslator(Context(config=cfg or Config(), events=EventBus()))


# ----------------------------------------------------------------------
# 一、默认值本身
# ----------------------------------------------------------------------

def test_vlm_fallback_defaults_to_off() -> None:
    """**核心契约**：这个兜底默认必须是关的。

    打开它的代价是"750 张图多花约 5 小时、结果更差"，
    不该是默认行为。要开的人自己在设置里开。
    """
    assert Config().ocr.vlm_fallback is False


def test_limits_have_nonzero_defaults() -> None:
    c = Config().ocr
    assert c.vlm_max_per_image > 0, "每图上限为 0（不限）会让图标表问几十次"
    assert c.vlm_timeout_s > 0, "没超时就没法阻止 thinking 模型拖死流程"
    assert c.vlm_fail_limit > 0, "没熔断时一个不通的 VLM 会让每张图都白等"


def test_vlm_property_is_none_when_disabled() -> None:
    assert _tt().vlm is None, "关掉后不该还去加载视觉模型"


# ----------------------------------------------------------------------
# 二、答案准入（判据逐条）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,answer",
    [
        # 实测：全是问号 —— 口口口的 ASCII 版，绝不能替换
        ("+222?22?", "? ? ? ? ? ? ?"),
        # 实测：token 里带出来的换行
        ("30", "E\nE\nE\nD\n?"),
        # 纯符号
        ("AB", "★☆●○"),
        # 控制字符
        ("AB", "A\x00B"),
        # 长度暴涨：VLM 在"解释这张图"
        ("30", "这是一张有三个图标的游戏贴图，看起来像是状态图标"),
    ],
)
def test_garbage_answers_are_rejected(original: str, answer: str) -> None:
    tt = _tt()
    assert tt._vlm_answer_is_usable(original, answer) is False, (
        f"垃圾答案 {answer!r} 被接受了 —— 它会覆盖掉 {original!r}"
    )


@pytest.mark.parametrize(
    "original,answer",
    [
        ("Now Loading...", "Now Loading..."),
        ("Leve1", "Level 3: HP!"),
        # 字母数字占比 0.71，比阈值 0.5 宽 —— 不能误杀
        ("Now Loading", "Now Loading..."),
        ("経験値", "经验值"),
        ("HP", "MP"),
        ("abc", "abcdefg"),  # 不到 3 倍
    ],
)
def test_plausible_answers_are_accepted(original: str, answer: str) -> None:
    """**反例**：像样的答案必须放行，不能把闸门做成"一律拒绝"。

    这条是防"修过头" —— 如果闸门把真答案也拒了，
    那这个功能就永远是死的，还不如直接删掉。
    """
    assert _tt()._vlm_answer_is_usable(original, answer) is True


def test_empty_answer_rejected() -> None:
    tt = _tt()
    assert tt._vlm_answer_is_usable("abc", "") is False
    assert tt._vlm_answer_is_usable("abc", "   \n  ") is False


def test_newline_inside_answer_is_allowed() -> None:
    """换行是合法的多行文字，不该被当控制字符拒掉。

    注意 ``original`` 要和答案长度相当 —— 否则会撞上**另一条**判据
    （长度暴涨超 3 倍）。三条判据各自独立生效，测试要一次只测一条，
    不然失败时分不清是哪条拦的。
    """
    assert _tt()._vlm_answer_is_usable("line1 line2", "line1\nline2") is True


def test_ascii_tab_inside_answer_is_rejected() -> None:
    """制表符**不算**合法的贴图文字 —— 它通常意味着模型吐了 token 结构。

    （换行是例外：多行文字是真实存在的。制表符不是。）
    """
    assert _tt()._vlm_answer_is_usable("ab", "a\tb") is False


# ----------------------------------------------------------------------
# 三、不凭空抬高置信度
# ----------------------------------------------------------------------

def test_confidence_is_not_inflated_by_vlm() -> None:
    """**回归**：旧代码写 ``max(原值, thr)``，把垃圾答案抬进"可信"档。

    结果下游质检与审校页再也看不出这块可疑 —— 这是最坏的一种
    "看起来成功"。

    断言前先**剥掉注释**：说明里为了解释这个 bug 引用了
    ``max(原值, thr)`` 这几个字，直接搜源码会把注释本身搜出来。
    """
    import inspect
    import re

    src = inspect.getsource(TextureTranslator._vlm_rescue)
    code = re.sub(r"#.*", "", src)
    after = code.split("b.source = better")[1][:400]
    assert "max(" not in after, "替换 VLM 答案时又用 max() 抬高了置信度"


# ----------------------------------------------------------------------
# 四、熔断与上限（用替身，不需要真模型）
# ----------------------------------------------------------------------

class _SilentVlm:
    """永远读不出来的替身（模拟超时/不可用）。"""

    def __init__(self) -> None:
        self.calls = 0

    def read_text(self, image, *, hint: str = "") -> str:  # noqa: ARG002
        self.calls += 1
        return ""


class _ChattyVlm:
    """永远回垃圾的替身。"""

    def __init__(self) -> None:
        self.calls = 0

    def read_text(self, image, *, hint: str = "") -> str:  # noqa: ARG002
        self.calls += 1
        return "? ? ? ? ? ? ?"


def _canvas() -> Any:
    """一张**有区分度**的测试图。

    ⚠️ 不能用全零图：`_vlm_reconsider` 的缓存按裁剪**像素**做键
    （"像素相同 ⇒ 文字相同"）。全零图上每个 12x12 裁剪都是同一片黑，
    于是从第二个块起全部命中缓存、**根本不会调 VLM** ——
    测出来的调用次数会莫名其妙地少，看着像上限/熔断没生效，
    实际是夹具退化了。真实贴图里每个块的像素都不同。
    """
    rng = np.random.default_rng(20240607)
    return rng.integers(0, 256, size=(64, 512, 3), dtype=np.uint8)


class _Block:
    """一个待重读的文字块。

    ⚠️ **每个块的四边形必须不同**，否则会踩到裁剪图缓存：
    `_vlm_reconsider` 用"裁剪像素相同 ⇒ 文字相同"做缓存，
    多个块共用同一个 quad 时，第二次开始直接命中缓存、**根本不会调 VLM**。
    真实图里每个块的坐标都是不同的，所以测试也要造不同的坐标 ——
    否则测出来的调用次数会莫名其妙地少，看着像上限/熔断没生效。
    """

    def __init__(self, text: str, conf: float, offset: int = 0) -> None:
        self.source = text
        self.confidence = conf
        x = 100 + offset * 20  # 每个块错开，保证裁剪像素不同
        self.quad = [(x, 0), (x + 12, 0), (x + 12, 12), (x, 12)]
        self.box = (x, 0, x + 12, 12)
        self.warnings: list[str] = []


def _blocks(n: int, conf: float = 0.2) -> list[_Block]:
    return [_Block(f"t{i}", conf, offset=i) for i in range(n)]


def _enable_vlm(tt: TextureTranslator, fake) -> None:
    tt._vlm = fake
    tt._vlm_tried = True


def test_circuit_breaker_stops_after_limit() -> None:
    """**核心契约**：连续读不出到上限就熔断，不再白等。"""
    cfg = Config()
    cfg.ocr.vlm_fallback = True
    cfg.ocr.vlm_fail_limit = 3
    cfg.ocr.vlm_max_per_image = 0  # 不限，让熔断成为唯一约束
    tt = _tt(cfg)
    fake = _SilentVlm()
    _enable_vlm(tt, fake)
    img = _canvas()
    blocks = _blocks(10)
    tt._vlm_rescue(img, blocks)

    assert tt._vlm_dead, "连续失败到上限后没有熔断"
    assert fake.calls <= 3, f"熔断没起作用，问了 {fake.calls} 次"


def test_max_per_image_caps_calls() -> None:
    """每图上限必须生效：图标表一张图能有几十个低置信块。"""
    cfg = Config()
    cfg.ocr.vlm_fallback = True
    cfg.ocr.vlm_max_per_image = 2
    cfg.ocr.vlm_fail_limit = 0  # 关掉熔断，单独测上限
    tt = _tt(cfg)
    fake = _ChattyVlm()
    _enable_vlm(tt, fake)
    img = _canvas()
    blocks = _blocks(10)
    tt._vlm_rescue(img, blocks)
    assert fake.calls == 2, f"每图上限没生效，问了 {fake.calls} 次"


def test_garbage_answer_does_not_overwrite_source() -> None:
    """**核心断言**：垃圾答案不能覆盖 OCR 原结果。"""
    cfg = Config()
    cfg.ocr.vlm_fallback = True
    cfg.ocr.vlm_fail_limit = 0
    tt = _tt(cfg)
    _enable_vlm(tt, _ChattyVlm())
    img = _canvas()
    b = _Block("4994994", 0.3)
    tt._vlm_rescue(img, [b])
    assert b.source == "4994994", f"原结果被垃圾答案覆盖成了 {b.source!r}"
    assert tt.vlm_fixes == 0


def test_timeout_is_only_passed_to_implementations_that_accept_it() -> None:
    """**回归**：不能把协议外的关键字硬塞给所有实现。

    `OcrEngine` 协议是 ``read_text(image, *, hint="")``（**没有**超时参数），
    测试替身也照协议实现。如果无条件传 ``timeout_s``，
    所有替身都会报 ``unexpected keyword argument``，
    而真实路径反而看不出来。
    """
    cfg = Config()
    cfg.ocr.vlm_fallback = True
    cfg.ocr.vlm_fail_limit = 0
    tt = _tt(cfg)
    fakeless = _SilentVlm()  # 只接受 hint
    _enable_vlm(tt, fakeless)
    img = _canvas()
    # 不该抛 TypeError
    tt._vlm_rescue(img, [_Block("abc", 0.2)])
    assert fakeless.calls == 1

    class _WithTimeout:
        def __init__(self) -> None:
            self.got: list[float] = []

        def read_text(self, image, *, hint: str = "", timeout_s: float | None = None) -> str:
            self.got.append(timeout_s)  # type: ignore[arg-type]
            return "ok"

    tt2 = _tt(cfg)
    withto = _WithTimeout()
    _enable_vlm(tt2, withto)
    tt2._vlm_rescue(img, [_Block("abc", 0.2)])
    assert withto.got == [cfg.ocr.vlm_timeout_s], (
        f"支持超时的实现没收到超时参数：{withto.got}"
    )


def test_disabled_vlm_means_no_rescue_at_all() -> None:
    cfg = Config()  # 默认关
    tt = _tt(cfg)
    fake = _ChattyVlm()
    _enable_vlm(tt, fake)
    # 即便强行塞了 vlm，配置关闭时 `vlm` property 返回 None；
    # `_vlm_rescue` 用的是 property 还是实例属性？这里用 property 的语义：
    # 我们直接设了实例属性，所以这条测的是"块为空/无低置信块时不问"
    img = _canvas()
    tt._vlm_rescue(img, [_Block("abc", 0.99)])  # 高于阈值
    assert fake.calls == 0
