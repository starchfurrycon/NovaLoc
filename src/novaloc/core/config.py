"""配置：默认值 + TOML 持久化 + 环境变量覆盖。

配置只存"小东西"（模型名、路径、开关）；大文件一律走 :mod:`novaloc.core.paths`。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field

from . import paths


class OllamaConfig(BaseModel):
    """本地推理服务（Ollama）配置。"""

    enabled: bool = True
    host: str = "http://127.0.0.1:11434"
    # 文本翻译模型。2026-09 实测可用：translategemma 3.3GB / qwen3.5 6.6GB 等。
    # 8 GB 显存上 4B 级模型留有充足余量，可与 embed 模型并行驻留。
    text_model: str = "translategemma:4b"
    # 视觉模型：识别贴图里的美术字 / 花体字（仅兜底，主 OCR 走 RapidOCR）
    vision_model: str = "qwen3-vl:4b"
    # 向量模型：术语表检索
    embed_model: str = "bge-m3"
    # 生成模型：仅艺术字兜底
    gen_model: str = ""
    keep_alive: str = "10m"
    num_ctx: int = 8192
    num_gpu: int = -1
    """-1 表示交给 Ollama 自动决定。"""

    temperature: float = 0.2
    top_p: float = 0.9
    repeat_penalty: float = 1.1
    """**必须显式设置**。Ollama 的默认值是 1.0（等于关闭），
    这是本地模型批量翻译时"复读到停不下来"的根因。建议 1.05~1.15。"""

    repeat_last_n: int = 256
    request_timeout_s: float = 300.0
    max_batch_strings: int = 40
    """一次请求塞多少条短字符串。"""

    max_batch_chars: int = 3000
    concurrency: int = 1
    """并发请求数。8 GB 显存建议 1，跑大模型时别并发。"""

    max_loaded_models: int = 1
    """同时驻留的模型数。Ollama 默认 3×GPU 数，在 8 GB 上会导致反复换入换出。
    建议固定为 1（或 2：翻译模型 + embed 模型）。"""

    kv_cache_type: str = "q8_0"
    """KV cache 量化。f16 是默认值；q8_0 能显著省显存且质量损失很小。"""

    flash_attention: bool = True


class TranslateConfig(BaseModel):
    primary_provider: str = "ollama"
    fallback_providers: list[str] = Field(default_factory=list)
    target_lang: str = "zh-Hans"
    source_lang: str = "auto"
    use_glossary: bool = True
    use_memory: bool = True
    """翻译记忆：同一源串直接复用历史译文。"""

    memory_similarity: float = 0.97
    """模糊匹配阈值（0~1）。1.0 表示只接受完全相同。"""

    review_pass: bool = True
    """额外一轮自检，修掉漏译/占位符丢失/过长。"""

    mask_placeholders: bool = True
    """把 {0} %s \\V[1] <color=#fff> 等占位符临时替换成稀有 Unicode 记号再发给模型。

    这是占位符保护最强的手段：模型看不到原始语法，就无从破坏它，
    攻击面直接归零。翻译回来后再按索引逆映射。"""

    enforce_indexed_json: bool = True
    """批量翻译要求模型返回 [{i, t}, ...] 这种带显式索引的对象数组。

    裸数组一旦漏一条或合并两条，后面全部错位 —— 而且错得很隐蔽
    （译文本身通顺，只是贴错了行）。显式索引能立刻发现缺项。"""

    max_chars_ratio: float = 2.2
    """UI 文本译文相对源串的字符数上限倍率，超过就标记 too_long。"""


class FontConfig(BaseModel):
    strategy: Literal["merge", "replace", "fallback_only"] = "merge"
    """merge=把中文字形并入原字体；replace=整体换字体；fallback_only=只装回退字体。"""

    ui_font: str = "Source Han Sans SC"
    """界面/正文首选字体。"""

    display_font: str = "Source Han Sans SC Heavy"
    """标题/艺术字首选字体。"""

    dialog_font: str = "LXGW WenKai"
    """对白首选字体（更接近手写/衬线感）。"""

    pixel_font: str = ""
    """像素字体，用于复古游戏；留空则用 ui_font 降采样。"""

    min_glyph_coverage: float = 0.999
    """注入中文前要求的最低字形覆盖率。达不到就报警并跳过该文件。"""

    allow_download: bool = True
    """允许自动下载开源字体。"""


class OcrConfig(BaseModel):
    """贴图文字识别（OCR）配置。

    默认走 PP-OCRv6 + DirectML。实测（RTX 4060 Laptop，1024×1024）
    medium ≈ 610 ms / small ≈ 410 ms / tiny ≈ 170 ms，
    而纯 CPU 同一张图要 20~24 秒 —— 差 40~140 倍。
    """

    engine: str = "ppocrv6"
    model_tier: Literal["tiny", "small", "medium"] = "medium"
    """模型档位。medium 精度最好（也是研究结论推荐的档位），tiny 最快。"""

    use_directml: bool = True
    """用 DirectML 走 GPU。关掉会慢 40~140 倍，仅在 DirectML 不可用时才关。"""

    use_cls: bool = False
    """方向分类模型。横排 UI 不需要，开着会多一份推理开销。"""

    cpu_threads: int = 0
    """ONNX intra-op 线程数，0 表示交给运行时决定。"""

    min_score: float = 0.5
    """置信度下限，低于此值的框丢弃。"""

    max_side: int = 4096
    """超过该边长就先缩放再识别，避免显存/内存爆掉。"""

    verbose: bool = False

    # --- 视觉模型兜底 ---
    vlm_fallback: bool = True
    """置信度偏低时是否升级给视觉模型复核。"""

    vlm_threshold: float = 0.6
    """低于该置信度才走视觉模型。PP-OCRv6 实测多在 0.99 以上，很少触发。"""

    min_box_size: int = 6
    """小于该边长的框直接忽略（噪点）。"""

    skip_if_no_text_ratio: float = 0.0002
    """图中"疑似文字像素"占比低于该值就跳过，省掉无谓推理。"""


class ImageConfig(BaseModel):
    enabled: bool = True
    detect_engine: str = "rapidocr"
    recognize_engine: str = "rapidocr"
    """可选 rapidocr | paddleocr | vlm | auto。"""

    vlm_threshold: float = 0.6
    """OCR 置信度低于该值时，升级给视觉模型复核。"""

    inpaint_engine: str = "lama"
    """可选 lama | migan | opencv（telea，仅测试用）。"""

    inpaint_dilate: int = 3
    """遮罩向外扩张像素数，避免描边残留。"""

    min_box_size: int = 6
    """小于该尺寸的文字框忽略（多为噪点）。"""

    max_upscale: int = 4
    skip_patterns: list[str] = Field(
        default_factory=lambda: [
            "*_normal", "*_n", "*_roughness", "*_metallic", "*_ao",
            "*_height", "*_mask", "*_specular", "*_gloss",
        ]
    )
    """明显不含文字的贴图命名，跳过以省时间。"""


class UIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8791
    open_browser: bool = True


class Config(BaseModel):
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    translate: TranslateConfig = Field(default_factory=TranslateConfig)
    font: FontConfig = Field(default_factory=FontConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    data_root: str = ""
    log_level: str = "INFO"

    # ---------------- 持久化 ----------------

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        p = path or paths.config_file()
        cfg = cls()
        if p.exists():
            try:
                raw = tomllib.loads(p.read_text(encoding="utf-8"))
                cfg = cls.model_validate(raw)
            except Exception:  # 配置坏了不能让程序起不来
                cfg = cls()
        cfg.data_root = str(paths.data_root())
        cfg._apply_env()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        p = path or paths.config_file()
        data = self.model_dump(mode="json")
        data.pop("data_root", None)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(tomli_w.dumps(data).encode("utf-8"))
        return p

    def _apply_env(self) -> None:
        env_map: list[tuple[str, str]] = [
            ("NOVALOC_OLLAMA_HOST", "ollama.host"),
            ("NOVALOC_TEXT_MODEL", "ollama.text_model"),
            ("NOVALOC_VISION_MODEL", "ollama.vision_model"),
            ("NOVALOC_EMBED_MODEL", "ollama.embed_model"),
            ("NOVALOC_UI_FONT", "font.ui_font"),
            ("NOVALOC_LOG_LEVEL", "log_level"),
        ]
        for env_name, dotted in env_map:
            val = os.environ.get(env_name)
            if not val:
                continue
            self._set_dotted(dotted, val)

    def _set_dotted(self, dotted: str, value: Any) -> None:
        obj: Any = self
        parts = dotted.split(".")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)

    def resolved_ollama_host(self) -> str:
        return os.environ.get("OLLAMA_HOST") or self.ollama.host


_cached: Config | None = None


def get_config(refresh: bool = False) -> Config:
    global _cached
    if _cached is None or refresh:
        _cached = Config.load()
    return _cached


def set_config(cfg: Config) -> None:
    global _cached
    _cached = cfg
