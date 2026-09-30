"""NovaLoc 数据模型。

这里的模型是整个流水线的契约：抽取器产出 :class:`TextUnit` 与 :class:`ImageAsset`，
翻译层把前者变成 :class:`TranslationEntry`，渲染层用 :class:`ImageTextBlock` 描述
"贴图里哪块区域是什么文字、什么颜色、什么描边"。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class TextKind(str, Enum):
    """文本的用途，决定翻译提示词与长度约束。"""

    DIALOGUE = "dialogue"        # 对白
    NARRATION = "narration"      # 旁白
    UI_LABEL = "ui_label"        # 界面短标签（有严格宽度限制）
    MENU = "menu"                # 菜单项
    ITEM_NAME = "item_name"      # 道具/技能名
    ITEM_DESC = "item_desc"      # 道具说明
    SYSTEM = "system"            # 系统提示
    NAME = "name"                # 人物/地名
    CREDIT = "credit"            # 制作名单
    UNKNOWN = "unknown"
    # --- 以下为游戏本地化里需要单独给提示的类型 ---
    SKILL = "skill"              # 技能/法术名（要有游戏感）
    QUEST = "quest"              # 任务名与任务目标
    TUTORIAL = "tutorial"        # 教学提示（保留按键与操作名）
    MAP_NAME = "map_name"        # 地图/地点名
    CHARACTER_NAME = "character_name"  # 角色名（全篇必须统一）
    IMAGE_TEXT = "image_text"    # 从贴图里 OCR 出来的文字


class EntryStatus(str, Enum):
    PENDING = "pending"
    TRANSLATED = "translated"
    REVIEWED = "reviewed"
    SKIPPED = "skipped"          # 明确不翻译（数字、代码、占位符等）
    FAILED = "failed"
    LOCKED = "locked"            # 用户手工锁定，重跑不覆盖


class UntranslatedReason(str, Enum):
    EMPTY = "empty"
    NO_LETTERS = "no_letters"
    ALREADY_CHINESE = "already_chinese"
    PLACEHOLDER_ONLY = "placeholder_only"
    CONTROL_CODE = "control_code"
    BLOCKLIST = "blocklist"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# --------------------------------------------------------------------------
# 抽取层
# --------------------------------------------------------------------------


class TextLocation(BaseModel):
    """一条文本在原始文件中的位置，用于回写。"""

    file: str
    """相对于游戏根目录的路径。"""

    pointer: str = ""
    """文件内的定位串。JSON 用 ``/data/Actors/0/name``；二进制用 ``@offset``；
    纯脚本用 ``L120-124``。"""

    line: int | None = None
    byte_offset: int | None = None
    encoding: str = "utf-8"
    raw: bool = False
    """True 表示该串在文件里是转义存储的（如 RPG Maker 的 ``\\n``），写回需再转义。"""


class TextUnit(BaseModel):
    """一条待翻译文本。"""

    uid: str
    source: str
    kind: TextKind = TextKind.UNKNOWN
    context: str = ""
    """给翻译模型的场景提示，例如 "角色：艾莉丝，第一章 酒馆"。"""

    speaker: str | None = None
    max_chars: int | None = None
    """UI 标签的硬性字符上限，超过会溢出界面。"""

    max_bytes: int | None = None
    """写回时的字节预算；None 表示不限。"""

    location: TextLocation
    placeholders: list[str] = Field(default_factory=list)
    """源串里必须原样保留的标记，例如 ``{0}``、``%s``、``<color=#fff>``、``\\V[1]``。"""

    tags: list[str] = Field(default_factory=list)
    engine: str = ""
    adapter: str = ""

    @field_validator("source")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        return v


class ExtractReport(BaseModel):
    adapter: str
    files_scanned: int = 0
    files_matched: int = 0
    units: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    """跳过原因 -> 数量。"""

    errors: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


# --------------------------------------------------------------------------
# 翻译层
# --------------------------------------------------------------------------


class TranslationEntry(BaseModel):
    uid: str
    source: str
    target: str = ""
    status: EntryStatus = EntryStatus.PENDING
    kind: TextKind = TextKind.UNKNOWN
    provider: str = ""
    model: str = ""
    glossary_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    """例如 "placeholder_lost"、"too_long"、"looks_untranslated"。"""

    retries: int = 0
    updated_at: float = Field(default_factory=time.time)
    meta: dict[str, Any] = Field(default_factory=dict)


class GlossaryEntry(BaseModel):
    source: str
    target: str
    case_sensitive: bool = False
    whole_word: bool = True
    note: str = ""
    category: str = "term"


class PlaceholderSpec(BaseModel):
    """占位符规则。``pattern`` 必须整体匹配一个占位符。"""

    name: str
    pattern: str
    description: str = ""

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


# --------------------------------------------------------------------------
# 贴图文字层
# --------------------------------------------------------------------------


class TextBlockStyle(BaseModel):
    """贴图内某块文字的外观参数，用于"把中文按原风格贴回去"。"""

    text_color: tuple[int, int, int] = (255, 255, 255)
    stroke_color: tuple[int, int, int] | None = None
    stroke_width: int = 0
    align: Literal["left", "center", "right"] = "center"
    vertical: bool = False
    line_spacing: float = 1.15
    letter_spacing: float = 0.0
    bold_guess: bool = False
    italic_guess: bool = False
    angle: float = 0.0
    """文字旋转角度（度）。"""

    font_size: int | None = None
    """为 None 时由渲染器按区域高度自动推算。"""

    font_hint: str | None = None
    """匹配到的中文字体家族名。"""


class ImageTextBlock(BaseModel):
    """贴图里的一个文字区域。"""

    id: str
    box: tuple[int, int, int, int]
    """原图坐标系下的 ``(x0, y0, x1, y1)``。"""

    quad: list[tuple[float, float]] | None = None
    """四点多边形，处理斜排文字时使用。"""

    source: str = ""
    detected_lang: str = ""
    confidence: float = 0.0
    ocr_engine: str = ""
    polygon: list[tuple[float, float]] = Field(default_factory=list)
    style: TextBlockStyle = Field(default_factory=TextBlockStyle)
    target: str = ""
    status: EntryStatus = EntryStatus.PENDING
    warnings: list[str] = Field(default_factory=list)


class ImageAsset(BaseModel):
    """一张可能含有文字的贴图。"""

    uid: str
    path: str
    """相对游戏根目录的路径。"""

    kind: Literal["texture", "bitmap_font", "loose_image", "unknown"] = "unknown"
    width: int = 0
    height: int = 0
    has_alpha: bool = False
    container: str | None = None
    """若贴图打包在容器里（如 Unity bundle），记录容器路径。"""

    container_member: str | None = None
    blocks: list[ImageTextBlock] = Field(default_factory=list)
    """检测后填充。"""

    analyzed: bool = False
    ocr_engine: str = ""
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 字体层
# --------------------------------------------------------------------------


class FontCoverage(BaseModel):
    font_id: str
    path: str
    family: str = ""
    style: str = ""
    num_glyphs: int = 0
    cmap_size: int = 0
    covers_cjk: bool = False
    """是否至少覆盖常用汉字（GB2312 一级字表抽样）。"""

    missing: list[str] = Field(default_factory=list)
    """针对当前项目所需字符集缺失的字符（去重排序）。"""

    @property
    def missing_count(self) -> int:
        return len(self.missing)


class ProjectCharset(BaseModel):
    """项目实际需要的字符集 —— 决定字体补丁要做多大。"""

    text_chars: list[str] = Field(default_factory=list)
    ui_chars: list[str] = Field(default_factory=list)
    all_chars: list[str] = Field(default_factory=list)
    total: int = 0


class FontPatchResult(BaseModel):
    font_id: str
    action: Literal["none", "merge", "replace", "regen_atlas", "install_fallback"] = "none"
    output_path: str | None = None
    added_glyphs: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 项目与任务
# --------------------------------------------------------------------------


class PackSource(BaseModel):
    """一个游戏目录或封包。"""

    path: str
    kind: Literal["directory", "archive", "file"] = "directory"
    note: str = ""


class ProjectCreate(BaseModel):
    name: str
    game_dir: str
    target_lang: str = "zh-Hans"
    source_langs: list[str] = Field(default_factory=lambda: ["auto"])
    engine_hint: str | None = None
    notes: str = ""


class Project(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    game_dir: str
    target_lang: str = "zh-Hans"
    source_langs: list[str] = Field(default_factory=lambda: ["auto"])
    engines: list[str] = Field(default_factory=list)
    """检测到的引擎插件名。"""

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    notes: str = ""
    stage: str = "created"

    def bump(self) -> None:
        self.updated_at = time.time()


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageState(BaseModel):
    name: str
    status: StageStatus = StageStatus.PENDING
    progress: float = 0.0
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def stable_uid(*parts: Any) -> str:
    """由内容派生稳定 id —— 保证重跑流水线时 uid 不变，翻译记忆才能命中。"""
    h = hashlib.blake2b(digest_size=10)
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
