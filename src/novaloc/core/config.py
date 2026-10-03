"""配置：默认值 + TOML 持久化 + 环境变量覆盖。

配置只存"小东西"（模型名、路径、开关）；大文件一律走 :mod:`novaloc.core.paths`。
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field

from . import paths

log = logging.getLogger(__name__)


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

    max_output_tokens_per_item: int = 48
    """每条原文允许模型生成多少 token 的**保险丝上限**。

    原先**没有设** `num_predict`，于是 Ollama 用默认值 —— 等于不限制。
    真实游戏实测（19048 句台词）里有若干批次会跑飞：正常批次 6～10 秒，
    个别批次 **384 秒**，有的甚至到 **1500 秒以上**（模型停在
    `done_reason='length'`，撞的是 `num_ctx` 上限）。
    结果是整轮翻译看着像卡死，而它其实在一条条慢慢烧。

    这只是**上限**；实际值由 `max_output_char_factor` 按这批内容的
    字符数算出来（见 `ollama_provider._num_predict`），所以短句不会被
    这个数字拖慢。设成 48 是给"内容异常长"的情况兜底。
    """

    max_output_char_factor: float = 2.2
    """输出 token 上限按"原文总字符数 × 这个系数"计算。

    中文译文一般比原文短，但这批真实数据里混着西语、表情符号和
    RPG Maker 转义序列，实测译文/原文的 token 比接近 1.0～1.5，
    取 2.2 留安全余量。

    ⚠️ 这个值**不能太小**：上限截断会让 JSON 解析失败，然后走
    重试 + 逐条降级（12 条 = 12 次模型调用），比不管还慢。
    实测一刀切 `per_item=48` 时，有的批次反而涨到 153～183 秒。
    """

    max_batch_chars: int = 3000
    """一批里原文总字符数的上限。这是**真正的**"别塞太多"护栏。"""

    max_batch_long_chars: int = 200
    """超过这个字符数的单条文本会被**单独**成批（不与其他条目混批）。

    ## 为什么需要这个门槛，以及为什么不能定小

    "极长的文本单独翻"本身是对的：一条 900 字的旁白和一条 20 字的按钮
    混在一批里，长句的语言风格会传染过去，按钮文字可能变长到溢出 UI 框。

    但门槛**定小了灾难性**：早先硬编码 `> 60`，而真实 MV 游戏
    （剩余 34357 条）实测：

        长度：中位 50  均值 43  90 分位 67  最大 159
        超过 60 字符的：11966 条 = 34.8%

    三分之一的对白被逐条翻译。一条 67 字符的对白和一条 20 字符的
    都占满一次模型往返（约 5 秒），于是整轮要发约 12866 次请求 ≈ 7.4 小时。

    **60 字符根本不"长"**。定成 200 之后只有极少数真正长的条目逐条走，
    请求数降到约 1/9，而 `max_batch_chars` 仍然守着"一批别塞太多字"。

    注意这个字段**不影响质量**：它只决定"和谁一起发"，不决定怎么翻。
    真正防"风格传染"的是上面那条"短标签不与非短标签混批"的规则。
    """

    concurrency: int = 1
    """并发请求数。8 GB 显存建议 1，跑大模型时别并发。"""

    max_loaded_models: int = 1
    """同时驻留的模型数。Ollama 默认 3×GPU 数，在 8 GB 上会导致反复换入换出。
    建议固定为 1（或 2：翻译模型 + embed 模型）。"""

    kv_cache_type: str = Field(
        default="q8_0",
        description=(
            "KV cache 量化。⚠️ 这是 **Ollama 服务器**的环境变量"
            "（OLLAMA_KV_CACHE_TYPE=q8_0），**不是**每个请求能带的参数。"
            "放进请求的 options 会被 Ollama **静默忽略**。"
            "程序里改这个字段**不会有任何效果**，它只用来记下你打算怎么配。"
        ),
    )
    flash_attention: bool = Field(
        default=True,
        description=(
            "Flash Attention 开关。⚠️ 同上 —— 这是 **Ollama 服务器**的环境变量"
            "（OLLAMA_FLASH_ATTENTION=1），不是请求参数。"
            "程序里改这个字段**不会有任何效果**。"
        ),
    )


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

    mask_newlines: bool = True
    """把**换行**也当成占位符屏蔽掉（``\\n`` → ``⟦n⟧``）。

    ## 为什么必须开（实测）

    RPG Maker 的对话有 **48%** 是"一条台词被引擎按显示宽度切成多个 401 指令"，
    适配器把这种组合成一条送翻译，译完再按行拆回各槽位。
    但**模型会看着换行自己切条**。裸调 Ollama 抓到的证据：提示词给的是
    编号 0~3 共 4 条，``translategemma:4b`` 却吐出了 ``"4"`` 这个键 ——
    它把同一条目里的多个换行当成了多个条目。后果是编号整体错位、
    一条 3 行的台词只翻出第 1 行，而两种都**通顺、长度比也不越界**。

    拿"逐行拆开单独翻再拼起来"当内容量参照（它不会丢行），
    20 条真实多行英文文本：

    | 模型 | 现状（合并送） | 屏蔽换行后 |
    |---|---|---|
    | ``translategemma:4b`` | 内容量比中位数 **0.33~0.70**，空 1 条 | **0.87**，快 15% |
    | ``HY-MT1.5-7B`` | 0.75~0.94，但 **5 条空译文** | **1.03**，**0 条空**，快 50% |

    ## 选型结论（40 条英文 + 40 条日文真实样本，两者都开这个开关）

    | 模型 | 内容量比中位 | 空译文 | hint 回声（守卫判死） | 秒 |
    |---|---|---|---|---|
    | ``translategemma:4b`` | 0.77 / 1.00 | **0 / 0** | **0 / 0** | 16 / 12 |
    | ``HY-MT1.5-7B`` | 1.00 / 1.00 | 3 / 5 | **3 / 4** | 37 / 25 |

    ``HY-MT1.5-7B`` 内容更完整，但有 **~10% 的条目会复述提示词**
    （被 :func:`~novaloc.translate.guards.check_hint_echo` 判死 →
    退回原文，玩家看到日文）。
    ``translategemma:4b`` 完全没有这两种毛病，
    所以**默认仍是它**；换 HY-MT 是"更全但更爱复述"的取舍，由用户决定。

    附加好处：换行进了槽位表，"丢行"会被占位符多重集校验抓住，
    从**静默漏译**变成**硬失败**（不留坏译文）。
    """

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

    lang: str = ""
    """识别语言覆盖。留空则跟随 `translate.source_lang`。

    **为什么必须有这个选项**：PP-OCRv6 的识别器不含韩语与西里尔文字
    （官方文档写明），这类语言会被静默读成空字符串。填 `ko` / `ru`
    会切换到 PP-OCRv5 的分语种识别模型（检测仍用 v6）。
    可取：`ko` / `ru` / `be` / `uk` / `bg` / `sr` / `latin` / `en`，
    或留空跟随翻译设置。
    """

    allow_model_download: bool = True
    """缺分语种识别模型时是否自动下载（韩语约 13.5 MB，俄语约 7.9 MB）。

    关掉它则完全离线：缺模型时给出**带下载链接**的明确错误，
    而不是让 RapidOCR 自己去联网（那会在断网时变成一句莫名其妙的失败）。
    """
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
    vlm_fallback: bool = False
    """置信度偏低时是否升级给视觉模型复核。**默认关闭。**

    ## 为什么默认关（两次实测，第二次才可信）

    这个兜底的**动机**是对的：专用 OCR 在定位上碾压 VLM
    （旋转文本 Hmean 93.8 vs 2.1），但偶尔会把文字读错，
    让一个视觉模型复核**内容**是合理的。问题是**代价与收益**。

    ### 第一次测量的结论**不可信** —— 工具坏了三处（v1.0.0 修）

    原先记录的是"抽样 10 个低置信块：0 个变好，1 个持平，2 个更差，
    其余读不出；每次 20～48 秒"。但那次的调用链自身有三个缺陷：

    1. 提示词要求 ``[{"text":…, "bbox":…}]``，而调用方**只用文本、
       丢弃坐标**（VLM 定位精度差两个数量级，不该让它在排版上有话语权）；
    2. 调用方又追加一条**互斥**的 hint「请只输出文字本身」；
    3. 没有 ``num_predict`` 上限 ⇒ 推理模型生成到 ``num_ctx`` 才停。

    实测同一张 389x57 裁剪：**旧组合 21.9 秒**（已超过
    ``vlm_timeout_s`` 默认 20 秒 ⇒ 必然超时返回空串），
    **新组合 15.1 秒且输出干净**。所以"读不出"和"每次 20 秒以上"
    有相当一部分是**测量伪影**，不是模型的真实能力。

    ### 修正后重测：默认依然该关，但理由变成了**它会编造**

    修好工具后重扫真实 DemonsRoots 贴图（40 张 / 295 个块）：

    * 置信度分布 ``<0.6: 9.5%``、``0.6-0.8: 16.9%``、``>=0.99: 38.3%``
      —— 低置信块**确实存在**，"很少触发"这个说法在这份资产上不成立；
    * 对 30 个 ``conf < 0.8`` 的块逐个重读：**变好 0 / 变差 0 /
      持平 3 / 被质量闸门拒 16 / 空结果 11**，平均 **13.4 秒/块**。

    最关键的是一组**对照实验**（同一模型、同一提示词）：

    ==================== ==========================================
    输入                 输出
    ==================== ==========================================
    纯白 200x60          ``'Now Loading...\\n-Demons Roots-'``
    近白噪声 200x60       ``'Now Loading...\\n\\n-Demons Roots-'``
    极小 34x12 纯白       ``'Now Loading...\\n-Demons Roots-'``
    写有 ``GAME OVER``    ``'GAME OVER'``  ← 只有这张是对的
    ==================== ==========================================

    **在完全没有文字的空白图上，它会稳定编造出本游戏加载画面的那行字。**
    也就是说它在"补全一个像样的贴图文字"，而不是在**读**。小图标、
    被 inpaint 擦过一半的区域、低分辨率裁剪都会触发这种编造 ——
    而"低置信块"恰恰就是这类输入，正是兜底要去处理的对象。

    这就是"**绝不能拿生成模型读/写文字**"这条设计红线的实测依据：
    生成模型填的是"先验上像什么"，而真值对不对它**无从判断**。
    所以默认关闭。打开后仍有三道保护（``_vlm_answer_is_usable``
    质量闸门、超时、熔断），但那只是止损，不是可信。
    """

    vlm_threshold: float = 0.6
    """低于该置信度才走视觉模型。PP-OCRv6 实测多在 0.99 以上，很少触发。

    注意"很少触发"在**真实游戏资产**上不成立：图标表/行走图这类
    小图案密集的贴图里，置信度天然偏低（0.5～0.6 很常见）。
    """

    vlm_max_per_image: int = 8
    """单张图最多问 VLM 几次。``0`` 表示不限。

    没有这个上限时，`IconSet.png` 一张图就会问几十次，
    每次都可能是几十秒。
    """

    vlm_timeout_s: float = 20.0
    """单次 VLM 调用的超时（秒）。

    超时后放弃这一块，保留 OCR 原结果 —— 兜底是**可选优化**，
    不该让整条流水线卡住。

    ## 这个值曾经**必然**超时（已修，记录原因）

    早先这里写的是"本机 `qwen3-vl:4b` 实测要 10～48 秒，Ollama 的
    ``think: false`` 对它不生效"。**那句话是错的**，而且错得很有迷惑性：

    * 真实原因是 `ollama.num_predict` **没有上限** ⇒ 推理模型一路生成到
      `num_ctx`(8192) ⇒ 实测 **55 秒**、3009 个 token、`content` 是**空串**
      （内容全在 `message.thinking` 里）；
    * 另外提示词还要求模型输出 ``[{"text":…, "bbox":…}]`` —— 而调用方
      **只用文本、明确丢弃坐标**，于是模型为了填坐标要多想 17.8～21.3 秒；
    * 两件事叠加：每个低置信块都超时 ⇒ 连续 3 次 ⇒ 熔断 ⇒
      **整个视觉兜底被静默关闭**，而日志只说"连续失败 3 次"。

    修法：提示词改纯文本（实测 **3.2 s**）、调用加 ``think: false`` 与
    ``num_predict`` 上限（见 ``vision_max_tokens``）。
    """

    vision_max_tokens: int = 1024
    """视觉读字的 ``num_predict`` 上限。``0`` 或负数用内置默认 1024。

    见 ``vlm_timeout_s`` 的说明：不设上限会让推理模型生成到 ``num_ctx``
    才停（实测 55 秒、`content` 空）。上限只为**兜住异常**，
    正常输出只有几十个 token。
    """

    vlm_fail_limit: int = 3
    """连续失败多少次后熔断，本次运行不再尝试视觉兜底。

    兜底应该是"偶尔帮忙"，不是"把整条流水线拖死"。
    没有熔断时，一个不通的 VLM 会让每张图都白等超时时间。
    """

    min_box_size: int = 6
    """小于该边长的框直接忽略（噪点）。"""

    skip_if_no_text_ratio: float = 0.0002
    """文字框面积占整图比例低于该值就跳过（判定为无文字/误检）。

    实测 `system/Loading.png`（400x100）一个 `Now Loading...` 块就占 44%，
    所以这个阈值只能卡"小到不可能是真文字"的情况，默认 0.02% 是很低的下限。
    """

    cache: bool = True
    """是否把 OCR 结果缓存到磁盘。

    真实游戏上贴图动辄上千张，OCR 是最慢的一步，而且在真实资产上
    **结果不确定**（重跑一次识别出的框/文字会变）。缓存把这一步
    变成"只做一次、每次都一样"，也让崩溃后重跑不必从头再来。
    """

    cache_dir: str = ""
    """OCR 缓存目录；留空用 ``<数据目录>/cache/ocr``。"""


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


class PackConfig(BaseModel):
    """游戏资源解包/回写的行为。"""

    no_unpack: bool = False
    """完全跳过解包，一律按明文目录处理。

    什么时候需要它：游戏目录很大、里面有个几十 GB 的资源包，而用户只想
    改脚本 —— 解包会把磁盘吃满。这时跳过解包，让用户自己解。
    """

    repack: bool = True
    """回写阶段要不要把改动打回原归档。

    默认要。关掉它用于"我只想看 out/ 里的产物，别动我的游戏文件"。
    """

    max_depth: int = 3
    """探测归档时最多往下走几层。"""

    only_suffixes: list[str] = Field(default_factory=list)
    """非空时只解这些后缀（例如只解 `.rpy`/`.png`），能省大量磁盘和时间。"""

    scan_bundles: bool = False
    """抽取阶段要不要**读 UnityFS 包里的文本**。

    ## 为什么默认关（实测数据）

    实测本库 46 个含 UnityFS 的游戏里，**25 个包里根本没有文本**，
    11 个"仅少量"，只有约 10 个有真文本；而有文本的那几个**绝大多数
    已经官方汉化**（Jerez's Arena 的 9619 个槽位组里 8708 个自带中文，
    全游戏只剩 300 条真需翻译）。

    即"打开它"在多数游戏上**只增加耗时、不增加译文**：
    扫一个 188 MB 的包约 **10 s**，而有个游戏有 5857 个包。

    ## 什么时候该打开

    * 你**知道**某个游戏的文本在包里（用 `novaloc bundle-strings` 先探一下）；
    * 或者你不在乎耗时，只想要"尽可能全"。

    ⇒ 默认 `False` 是**为了不让全库批量跑变慢**；需要时显式打开。
    """

    bundle_scan_limit: int = 0
    """限制扫描多少个包（0 = 全部，按文件从大到小）。

    实测文本多在较大的包里，所以"取前 N 大"是有效的降本手段。
    """


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
    pack: PackConfig = Field(default_factory=PackConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    data_root: str = ""
    log_level: str = "INFO"

    # ---------------- 持久化 ----------------

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """读取配置。

        **单一事实来源是 ``<data_root>/config.json``（JSON）**。

        早先这里默认读写 ``<config_dir>/config.toml``（TOML），而 API 层
        （``api/jobs.py``）和 CLI 读写 ``<data_root>/config.json``（JSON）——
        同一份配置有两个位置、两种格式。后果是用户在网页设置里改的项，
        命令行 ``novaloc config show`` 看不到，反之亦然；两边各自保存还会
        互相覆盖（同一个 JSON 路径，一边用 tomli_w 写、一边用
        model_dump_json 写）。

        现在统一到 JSON：
        1. ``<data_root>/config.json`` —— 权威位置；
        2. 找不到时回退读旧的 ``<config_dir>/config.toml``，**并自动迁移**
           成 JSON，老用户不丢配置。
        """
        # 1) 权威：JSON
        j = path if (path is not None and path.suffix == ".json") else paths.config_json()
        if j.is_file():
            try:
                cfg = cls.model_validate_json(j.read_text(encoding="utf-8"))
                cfg.data_root = str(paths.data_root())
                cfg._apply_env()
                return cfg
            except Exception as exc:  # noqa: BLE001
                log.warning("配置文件损坏，改用默认配置：%s", exc)

        # 2) 回退：旧 TOML，读到就迁移
        legacy = path or paths.config_file()
        if legacy.is_file():
            try:
                raw = tomllib.loads(legacy.read_text(encoding="utf-8"))
                cfg = cls.model_validate(raw)
                cfg.data_root = str(paths.data_root())
                cfg._apply_env()
                try:
                    cfg.save()
                    log.info("已把旧配置 %s 迁移为 %s", legacy, paths.config_json())
                except Exception as exc:  # noqa: BLE001
                    log.warning("旧配置迁移失败（不影响本次运行）：%s", exc)
                return cfg
            except Exception:  # 配置坏了不能让程序起不来
                pass

        cfg = cls()
        cfg.data_root = str(paths.data_root())
        cfg._apply_env()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        """写出配置。默认写权威位置 ``<data_root>/config.json``。"""
        p = path or paths.config_json()
        data = self.model_dump(mode="json")
        data.pop("data_root", None)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".json":
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
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
