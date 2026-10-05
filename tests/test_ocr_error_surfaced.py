r"""★★ OCR 层系统性失败必须**响亮地**报出来（不能静默成"这些图没文字"）。

## 实测缺陷

我在**隔离数据根**（`D:\NovaLoc_probe`，那里**没有 OCR 模型**）里跑
`images_localize`：

```
images_localize: ok=True 0/243 张贴图已汉化（0 处文字）
localize.json:   243 条，全部 ok=False changed=False warnings=[]
OCR 引擎内部:     error='缺少 2 个离线模型：PP-OCRv6_det_medium…'
```

⇒ **"OCR 引擎起不来"被表现成"这些图没有文字"**。
`stages.py` 只把 `res.error` 写进 `localize.json`，
**从不记日志、从不统计** ⇒ 用户看到的是"贴图都没字"。

这属于**配置/依赖缺失**，必须响亮地报出来 —— 否则整个贴图汉化会
"看起来正常但无事可做"。

## 修复

`stage_images_localize` 收集 `res.error`，并**按比例**判断：

* ≥ 50% 报错 ⇒ `Severity.ERROR`，消息里点明"通常意味着 OCR 模型缺失
  或引擎起不来"，并附 3 条样本；
* 少量报错 ⇒ `Severity.WARN`（个别图损坏/加密很常见）；
* 无论哪种，`StageResult.message` 与 `stats["ocr_errors"]` 都带上计数。

## 为什么用比例而不是绝对数

个别图读不了很正常（损坏、超尺寸、非图片）。**过半**报错才是系统性问题。
用绝对数会把"偶尔一张坏图"误报成故障。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novaloc.models import Severity  # noqa: E402


def _ratio_rule(n_err: int, n_total: int) -> str:
    """复刻 `stage_images_localize` 里的判级逻辑（保持与源码一致）。

    ⚠️ 这里**有意复刻**而不是导入内部函数：那段逻辑写在闭包里，
    没有可导入的入口。复刻的风险是"改了源码忘了改测试"，
    所以下面还有一条**读源码**的结构性守卫（见最后一个用例）。
    """
    if n_err == 0:
        return "none"
    ratio = n_err / max(1, n_total)
    return "error" if ratio >= 0.5 else "warn"


# ----------------------------------------------------------------------
# 判级
# ----------------------------------------------------------------------
def test_all_errors_is_error_level() -> None:
    """全部报错 ⇒ ERROR（这就是实测那个"缺少 OCR 模型"的情形）。"""
    assert _ratio_rule(243, 243) == "error"


def test_half_errors_is_error_level() -> None:
    """恰好一半 ⇒ ERROR（阈值取 `>= 0.5`）。"""
    assert _ratio_rule(50, 100) == "error"


def test_few_errors_is_warn_level() -> None:
    """少量报错 ⇒ WARN（个别图损坏很常见，不该报成故障）。"""
    assert _ratio_rule(3, 243) == "warn"
    assert _ratio_rule(49, 100) == "warn"


def test_no_errors_is_silent() -> None:
    """没有报错 ⇒ 不额外记日志。"""
    assert _ratio_rule(0, 243) == "none"


def test_zero_total_does_not_crash() -> None:
    """总数为 0 时不该除零。"""
    assert _ratio_rule(0, 0) == "none"


# ----------------------------------------------------------------------
# 结构性守卫：源码里确实有这么一段
# ----------------------------------------------------------------------
def test_stage_collects_ocr_errors() -> None:
    r"""★★ 结构性守卫：`stages.py` 必须**收集并报告** OCR 报错。

    复刻判级的风险是"改了源码忘了改测试"。这条直接读源码，
    检查关键片段还在：

    * 收集变量 `ocr_errors`；
    * 按比例判级的表达式；
    * `Severity.ERROR` 的响亮报告；
    * `stats` 里暴露 `ocr_errors` 计数。
    """
    src = (ROOT / "src" / "novaloc" / "pipeline" / "stages.py").read_text(encoding="utf-8")
    for needle in (
        "ocr_errors",
        "if res.error:",
        "err_ratio >= 0.5",
        "Severity.ERROR",
        '"ocr_errors": len(ocr_errors)',
    ):
        assert needle in src, f"stages.py 里找不到 {needle!r} —— OCR 报错可能又被静默了"


def test_severity_error_exists() -> None:
    """`Severity.ERROR` 必须存在（响亮报告要用它）。"""
    assert Severity.ERROR is not None
