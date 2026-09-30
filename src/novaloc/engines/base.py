"""引擎适配器基类。

每个适配器负责一种游戏引擎的"读文本、读贴图、找字体、写回去"。
所有适配器共用同一套契约，上层流程（抽取 → 翻译 → 字体 → 回写）不关心
具体是哪个引擎。

**只读约束**：``extract_*`` 只允许读；只有 :meth:`EngineAdapter.apply` 会写，
而且写的是**工作区里的副本**（``ws.out_dir``），原始游戏目录永远不动。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..core.registry import Context
from ..models import ExtractReport, FontCoverage, ImageAsset, TextUnit

log = logging.getLogger(__name__)


@dataclass
class EngineInfo:
    """引擎识别结果。"""

    engine_id: str
    display_name: str
    confidence: float = 0.0
    """0~1。多个适配器都匹配时取最高的。"""

    evidence: list[str] = field(default_factory=list)
    """判定依据（给用户看，便于排查误判）。"""

    root: Path | None = None
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.confidence > 0 and self.engine_id != "unknown"


@dataclass
class ApplyResult:
    """回写结果。"""

    ok: bool = False
    files_written: int = 0
    files_skipped: int = 0
    out_dir: Path | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)


class EngineAdapter:
    """引擎适配器基类。"""

    id: str = "base"
    display_name: str = "基类"
    #: 优先级，越小越先被检查
    priority: int = 100

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.config

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        """判断 ``game_dir`` 是不是本引擎的游戏。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 抽取（只读）
    # ------------------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        """抽取待翻译文本。"""
        raise NotImplementedError

    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]:
        """发现可能含文字的贴图。默认不发现（文本型引擎返回空）。"""
        return [], ExtractReport(adapter=self.id, errors=["该引擎不支持贴图抽取"])

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        """找出游戏自带的字体文件。"""
        return []

    def wire_fonts(self, out_dir: Path, installed: dict[str, str]) -> list[str]:
        """让引擎**真正用上**已经放进目录里的字体。

        ``installed`` 是 ``{原字体相对路径: 新字体相对路径}``。

        **为什么必须有这一步**：把补好的字体文件复制进 ``fonts/``
        并不会让引擎去用它 —— 每个引擎都有自己的"字体指向"配置：

        * Ren'Py 要在 ``gui.rpy`` 里改 ``gui.text_font``；
        * RPG Maker MV/MZ 要在 ``fonts/gamefont.css`` 里改 ``@font-face``
          的 ``src``；
        * Unity 的 TextMeshPro 走图集，情况复杂。

        少了这一步，用户会看到"字体文件确实被替换了，但游戏里还是口口口"，
        而且完全查不出原因。返回人类可读的操作说明（供日志与报告）。
        """
        return []

    # ------------------------------------------------------------------
    # 回写
    # ------------------------------------------------------------------

    def apply(
        self,
        game_dir: Path,
        out_dir: Path,
        units: list[TextUnit],
        translations: dict[str, str],
        *,
        font_patches: dict[str, Path] | None = None,
        rebuilt_images: dict[str, Path] | None = None,
    ) -> ApplyResult:
        """把译文写进 ``out_dir``（``game_dir`` 的副本），**不碰原目录**。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 共用工具
    # ------------------------------------------------------------------

    @staticmethod
    def prepare_out(game_dir: Path, out_dir: Path, *, overwrite: bool = True) -> None:
        """把游戏目录复制到输出目录。

        默认整目录复制而不是"只复制要改的文件"：游戏运行时可能依赖
        大量未被修改的资源，少复制一个就可能启动失败。
        """
        out_dir = Path(out_dir)
        if out_dir.exists() and overwrite:
            shutil.rmtree(out_dir)
        if not out_dir.exists():
            out_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(game_dir, out_dir, symlinks=True)

    def _rel(self, game_dir: Path, path: Path) -> str:
        try:
            return path.relative_to(game_dir).as_posix()
        except ValueError:
            return path.name


__all__ = ["ApplyResult", "EngineAdapter", "EngineInfo"]
