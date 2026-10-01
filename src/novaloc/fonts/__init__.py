"""字体层：字符集审计、多源合并、QA 硬校验。

本层存在的唯一目的，就是让"游戏里不会出现口口口"成为一条**可验证**的保证，
而不是一句愿望：

* :mod:`~novaloc.fonts.charset` —— 收集项目真实字符集并规划覆盖来源；
* :mod:`~novaloc.fonts.merge` —— 把多来源字形按优先级并入游戏原字体；
* :mod:`~novaloc.fonts.qa` —— 用确定性不变量逐字校验产物；
* :mod:`~novaloc.fonts.service` —— 把上面几步串成一条流水线，
  覆盖率不达标就**硬失败**，绝不写出半成品字体。
"""

from __future__ import annotations

from .catalog import (
    BUNDLE_OK_LICENSES,
    FontSpec,
    license_summary,
    local_only,
    redistributable,
    safe_to_merge,
)
from .charset import (
    CJK_CORE_SAMPLE,
    UI_SAFE_CHARS,
    CharsetCoverageError,
    CharsetPlan,
    SourcePick,
    assert_plannable,
    build_charset_tiers,
    build_required_charset,
    plan_charset,
)
from .merge import (
    MergeError,
    MergeReport,
    font_stats,
    merge_fonts,
    merge_fonts_multi,
    replace_with_font,
    subset_font_for_chars,
)
from .qa import FontQAReport, GlyphIssue, batch_verify, verify_font
from .service import FontAudit, FontService, PatchResult

__all__ = [
    # 目录与授权
    "BUNDLE_OK_LICENSES",
    "FontSpec",
    "license_summary",
    "local_only",
    "redistributable",
    "safe_to_merge",
    # 字符集
    "CJK_CORE_SAMPLE",
    "UI_SAFE_CHARS",
    "CharsetCoverageError",
    "CharsetPlan",
    "SourcePick",
    "assert_plannable",
    "build_charset_tiers",
    "build_required_charset",
    "plan_charset",
    # 合并
    "MergeError",
    "MergeReport",
    "font_stats",
    "merge_fonts",
    "merge_fonts_multi",
    "replace_with_font",
    "subset_font_for_chars",
    # QA
    "FontQAReport",
    "GlyphIssue",
    "batch_verify",
    "verify_font",
    # 编排
    "FontAudit",
    "FontService",
    "PatchResult",
]
