"""阶段的"接线一致性"：`STAGES` / CLI `--stage` / API `/api/health` / API 执行分支。

## 为什么要单独测这个

流水线阶段散落在四处：
1. `pipeline/stages.py` 的 `STAGES`（唯一事实来源）；
2. `/api/health` 返回的阶段列表（**从 STAGES 动态生成**，所以永远对）；
3. `api/app.py` `run_stage_job` 里的 `fn_map`（**手写**，会漂移）；
4. CLI 的 `--stage`（从 STAGES 校验，所以永远对）。

新增 `unpack` 阶段时就漏了第 3 处：接口**宣称**支持 9 个阶段，
真去调用第 9 个却报"未知阶段"。这类不一致不会让测试变红，
只会让用户在某个具体按钮上吃一次莫名其妙的错误 ——
所以必须有测试盯着，而且要盯住"手写的那一处"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from novaloc.pipeline.stages import STAGES  # noqa: E402

STAGE_IDS = [s for s, _ in STAGES]
APP_PY = ROOT / "src" / "novaloc" / "api" / "app.py"
CLI_PY = ROOT / "src" / "novaloc" / "cli.py"
STAGES_PY = ROOT / "src" / "novaloc" / "pipeline" / "stages.py"


def _fn_map_keys() -> set[str]:
    """把 `run_stage_job` 里 `fn_map` 的键抠出来。"""
    src = APP_PY.read_text(encoding="utf-8")
    start = src.index("fn_map = {")
    block = src[start : src.index("\n                }", start)]
    return set(re.findall(r'"([a-z_]+)":\s*lambda', block))


def test_stages_are_unique_and_ordered() -> None:
    assert len(STAGE_IDS) == len(set(STAGE_IDS)), f"阶段 id 有重复：{STAGE_IDS}"
    assert STAGE_IDS[0] == "unpack", "解包必须是第一个阶段（后续阶段依赖解包树）"
    assert STAGE_IDS[-1] == "apply", "回写必须是最后一个阶段"


def test_api_fn_map_covers_every_stage() -> None:
    """**这条就是 `unpack` 漏网的原因**。"""
    keys = _fn_map_keys()
    missing = [s for s in STAGE_IDS if s not in keys]
    assert not missing, (
        f"这些阶段在 STAGES 里但 api/app.py 的 fn_map 没有执行分支：{missing}。\n"
        "/api/health 会宣称支持它们，实际调用却报未知阶段。"
    )
    extra = sorted(keys - set(STAGE_IDS))
    assert not extra, f"fn_map 里有 STAGES 中不存在的阶段：{extra}"


def test_every_stage_has_a_public_pipeline_method() -> None:
    """每个阶段都要有 `Pipeline.stage_<id>()` 可调。"""
    src = STAGES_PY.read_text(encoding="utf-8")
    missing = [
        s for s in STAGE_IDS
        if f"def stage_{s}(" not in src
    ]
    assert not missing, f"缺少 Pipeline.stage_*() 方法：{missing}"


def test_every_stage_has_a_label() -> None:
    for sid, label in STAGES:
        assert label and label.strip(), f"{sid} 没有中文标签（UI 会显示英文 id）"
        assert label != sid, f"{sid} 的标签和 id 一样，等于没翻译"


def test_cli_validates_against_stages() -> None:
    """CLI 的 `--stage` 必须从 STAGES 校验（不能自己写一份列表）。"""
    src = CLI_PY.read_text(encoding="utf-8")
    assert "_validate_stage" in src
    i = src.index("def _validate_stage")
    body = src[i : i + 700]
    assert "STAGES" in body, (
        "`_validate_stage` 没有基于 STAGES 校验 —— 手写列表会和流水线漂移"
    )


@pytest.mark.parametrize("stage_id", STAGE_IDS)
def test_each_stage_id_is_a_plain_ascii_key(stage_id: str) -> None:
    """阶段 id 必须是纯 ASCII 小写标识符。

    它们会出现在 CLI 参数、URL、JSON 与前端映射表里，
    混入中文或大写会带来各种匹配问题（前端就是 `stage.toLowerCase()` 后再查表的）。
    """
    assert re.fullmatch(r"[a-z][a-z0-9_]*", stage_id), f"阶段 id 不规范：{stage_id!r}"
