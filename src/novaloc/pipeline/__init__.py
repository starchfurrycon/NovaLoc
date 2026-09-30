"""流水线包：阶段编排与质检。"""

from .qa import run_qa
from .stages import STAGE_LABELS, STAGES, Pipeline, PipelineError, StageResult

__all__ = [
    "Pipeline",
    "PipelineError",
    "STAGES",
    "STAGE_LABELS",
    "StageResult",
    "run_qa",
]
