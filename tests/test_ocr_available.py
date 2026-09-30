"""OCR 适配器自检必须真的能反映"离线可用"。

回归的 bug：``available()`` 只 `import rapidocr` 就报可用，不检查模型
文件是否已经下载。于是：

* ``doctor`` 显示 OCR ✅；
* 用户以为环境就绪，开始跑流水线；
* 首次识别时 RapidOCR 发现模型缺失，**联网下载**（本工具承诺全离线），
  断网环境下则直接失败。

对一个"全离线优先"的工具来说这是硬伤 —— 自检的意义就是断网时也成立。
本测试锁死这个契约的两侧：

1. 模型齐全 → 可用；
2. 模型缺失 → **不可用**，且消息里点明缺哪个文件、该放哪里。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rapidocr", reason="需要 rapidocr 才能测 OCR 适配器")

from novaloc.core.config import Config  # noqa: E402
from novaloc.core.events import EventBus  # noqa: E402
from novaloc.core.registry import Context  # noqa: E402
from novaloc.images.ocr_ppocrv6 import PPOcrV6Engine  # noqa: E402


def _engine() -> PPOcrV6Engine:
    return PPOcrV6Engine(Context(config=Config(), events=EventBus()))


def test_missing_models_reported_not_ready() -> None:
    """指向一个空目录 → 必须报不可用，且消息指明缺哪些模型。"""
    eng = _engine()
    with tempfile.TemporaryDirectory() as td:
        eng.model_dir = lambda: Path(td)  # type: ignore[method-assign]
        eng._ready = None

        missing = eng.missing_models()
        assert missing, "空目录下应报出缺失模型"
        # 档位默认 medium，det/rec 两个文件都应被点到
        assert any("det" in m for m in missing), missing
        assert any("rec" in m for m in missing), missing

        ok, msg = eng.available()
        assert ok is False, f"模型缺失却报可用：{msg!r}"
        assert "缺少" in msg and ".onnx" in msg, f"消息没说清缺什么：{msg!r}"
        assert str(Path(td)) in msg or "模型" in msg, f"消息没说要放哪：{msg!r}"


def test_available_does_not_claim_ready_without_models() -> None:
    """要点：`available()` 不能因为"模型缺失"就触发下载后报成功。

    这条用一个不可写的目录模拟：即便 RapidOCR 想下载也会失败，
    而我们的实现应当在**进入 RapidOCR 之前**就返回 False。
    """
    eng = _engine()
    with tempfile.TemporaryDirectory() as td:
        eng.model_dir = lambda: Path(td)  # type: ignore[method-assign]
        eng._ready = None
        ok, _ = eng.available()
        assert ok is False
        # 目录里不应出现任何新下载的文件
        assert list(Path(td).iterdir()) == [], "available() 不应触发任何下载"


def test_cls_model_only_required_when_enabled() -> None:
    """方向分类模型只在开启 use_cls 时才算必需。"""
    eng = _engine()
    with tempfile.TemporaryDirectory() as td:
        eng.model_dir = lambda: Path(td)  # type: ignore[method-assign]
        base = {m for m in eng.missing_models() if m.startswith("PP-OCRv6")}
        assert base, "基础模型应被报缺失"
        # 默认 use_cls=False，不应把 cls 模型算进必需项
        assert not any("cls" in m for m in base), base
