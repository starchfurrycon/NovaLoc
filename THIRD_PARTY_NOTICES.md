# 第三方依赖与算法来源声明

本文档说明 NovaLoc 新译（下称"本工具"）使用了哪些第三方代码、模型与字体，
以及**算法层面**参考了哪些项目。

本项目自身以 MIT 授权发布，见 [LICENSE](../LICENSE)。

---

## 0. 一句话结论

- **代码**：只使用宽松许可（Apache-2.0 / MIT / BSD / OFL）的库，直接作为依赖调用。
- **算法**：从若干 **GPL-3.0** 项目里**只借鉴思路、参数与经验数值，没有复制任何代码**。
  GPL-3.0 的传染性由"复制代码"触发，不由"读了别人的文章然后自己实现"触发；
  这一点在下文逐项说明。
- **模型**：全部为 Apache-2.0 或按需由用户自行下载。**明确不使用 NLLB-200**（CC-BY-NC-4.0）。
- **字体**：**默认不随本工具分发任何字体文件**。自动下载仅限可再分发的许可。

---

## 1. GPL-3.0 项目：只取算法、参数与思路，未复制代码

下列项目均为 GPL-3.0（或 AGPL-3.0）。本工具**没有**引用、包含、链接、改写它们的
任何一行源代码，也没有把它们作为依赖安装。它们的 GPL 义务因此**不会**传导到本工具，
也不会传导到本工具产出的汉化产物上。

借鉴的具体内容逐项列明如下。凡涉及具体数值的，都是本工具在自己的代码里**重新测量并
落到常量**的，出处写在对应模块的 docstring 或注释里，便于审计。

### 1.1 manga-image-translator（GPL-3.0）

参考部位：`src/novaloc/images/render.py`、`src/novaloc/images/inpaint.py`。

| 借鉴内容 | 本工具中的落地 | 出处 |
|---|---|---|
| 描边宽度 ≈ 字号 7% | 常量 `STROKE_RATIO = 0.07` | `images/render.py` |
| 字号下限 `(高 + 宽) / 200` | `fit_font_size()` 里的 `floor` | `images/render.py` |
| 掩膜外扩 `mask_dilation_offset` 默认 30 | 概念沿用，但**默认值改为 3**（`build_mask(..., dilate=3)`） | `images/inpaint.py` |
| "检测 → 识别 → 翻译 → 擦字 → 回填"的五段式图像流程 | 贴图阶段的整体结构 | `images/service.py` |

关于 `mask_dilation_offset` **为什么改小**：那个 30 是针对漫画气泡的大面积场景调的，
游戏 UI 的文字框通常只有几十像素宽，外扩 30 px 会把整个按钮吃掉。本工具保留的是
"掩膜必须比文字本身外扩，否则描边和抗锯齿会留一圈黑边"这个判断，数值是自己定的。

### 1.2 BallonsTranslator（GPL-3.0）

参考部位：贴图流程的分组与审校思路。

借鉴内容：把同一张贴图内相邻、同字号、同行的文字块**聚成一个组再整组翻译**，
而不是逐块送去翻译。这是"整句语义完整"的前提 —— 逐块翻译会把一句话切成几段，
每段各自成句，译文必然破碎。

本工具的实现是自写的 `images/textgroup.py`（`group_blocks()` / `allocate_translation()`），
其中 `allocate_translation()` 是**本工具自己的补充**：它负责把一句中文重新切回原来的
几个块，切点位置由标点与汉字边界决定，这部分在上游项目里没有对应实现。

### 1.3 LunaTranslator（GPL-3.0）

参考部位：术语一致性与翻译记忆的产品形态。

借鉴内容：术语表按"源文 → 译文 + 是否区分大小写 + 是否正则"建模，并在提示词里
**强制注入**；同一原文的历史译文要能复用（翻译记忆）。本工具在
`translate/glossary.py` 与 `translate/memory.py` 中自行实现，未参考其代码。

### 1.4 Unity_Font_Replacer（GPL-3.0）

参考部位：`docs/FONTS.md` 中关于 Unity TextMeshPro 字体替换的说明。

借鉴内容：**TMP 走图集（atlas），不是走 TTF 文件**。因此"把 TTF 拷进 StreamingAssets"
对 TMP 完全无效，必须重新烘焙 SDF 图集或替换 `TMP_FontAsset`。本工具据此**没有**实现
盲目替换 Unity 字体的功能（见第 5 节"未实现"），而不是去踩这个坑。

### 1.5 comic-text-detector（GPL-3.0）

参考部位：图像文字检测的预处理经验。

借鉴内容：检测前把长边缩到固定上限、以及"疑似文字像素占比过低就跳过推理"这类
**成本控制**思路。本工具的落地是 `OcrConfig.max_side = 4096` 与
`OcrConfig.skip_if_no_text_ratio = 0.0002`。本工具实际使用的检测器是 PP-OCRv6
（见第 3 节），没有使用 comic-text-detector 的权重或代码。

### 1.6 为什么这样做是干净的

- GPL-3.0 的传染条款（§5）约束的是**基于该程序的作品**（复制、修改、链接、派生）。
- "阅读一个 GPL 项目的文档与源码，理解其参数含义，然后在自己的代码里独立实现"
  **不构成派生作品**。本工具与上述项目之间没有共享代码、没有链接、没有随附分发。
- 为便于审计，本工具把**每一个借来的数值**都写在常量定义处并注明来源，
  而不是散落在逻辑里 —— 这样任何人想核对或移除都只需要改一个地方。
- **如果**将来要把上述项目的任何代码片段搬进本仓库，那么该片段以及由它派生的部分
  就必须按 GPL-3.0 处理。当前仓库不存在这种情况。

---

## 2. 作为库直接使用的依赖

以下全部为宽松许可，通过 `pyproject.toml` 的依赖声明安装，以库的形式调用。

### 2.1 运行时基础依赖

| 依赖 | 许可 | 用途 |
|---|---|---|
| FastAPI | MIT | Web API 框架 |
| uvicorn | BSD-3-Clause | ASGI 服务器 |
| pydantic | MIT | 配置与数据模型校验 |
| python-multipart | Apache-2.0 | 表单/文件上传解析 |
| httpx | BSD-3-Clause | 访问 Ollama REST API、下载字体与模型 |
| fonttools | MIT | 字体解析、子集化、合并、cmap 重建 |
| Pillow | MIT-CMU | 字形光栅化、文字排版 |
| numpy | BSD-3-Clause | 全部数值与图像计算 |
| opencv-python-headless | Apache-2.0 | 图像读写编解码、k-means 取色、仿射/单应变换、inpaint |
| typer | MIT | CLI 框架 |
| rich | MIT | CLI 表格与面板 |
| sqlmodel | MIT | 数据模型（供 SQLite 持久化使用） |
| platformdirs | MIT | 用户配置目录定位 |
| psutil | BSD-3-Clause | 进程与显存/内存探测 |
| PyYAML | MIT | 读写 YAML 配置 |
| tomli-w | MIT | 写 TOML（早期配置格式，见下） |
| scipy | BSD-3-Clause | 数值辅助（连通域、插值等） |
| pyclipper | MIT | 多边形偏移（文字框外扩） |
| shapely | BSD-3-Clause | 几何包含/相交判断（文字块分组） |

### 2.2 可选 extras 拉入的依赖

| 依赖 | 许可 | extra | 用途 |
|---|---|---|---|
| onnxruntime-directml | MIT | `dml` | Windows 上的 DirectML GPU 推理（**默认推荐**） |
| onnxruntime | MIT | `cpu` | 纯 CPU 推理（无独显时的兜底） |
| onnxruntime-gpu | MIT | `gpu` | CUDA 推理（DirectML 不可用时才需要） |
| rapidocr | Apache-2.0 | `ocr` | PP-OCRv6 的推理与模型下载封装 |
| torch | BSD-3-Clause | `inpaint` | 运行 LaMa（TorchScript） |
| scikit-image | BSD-3-Clause | `inpaint` | 图像处理辅助 |
| transformers / accelerate / qwen-vl-utils | Apache-2.0 | `vision` | 视觉大模型兜底（可选，体积大） |
| pytest / pytest-asyncio | MIT / Apache-2.0 | `dev` | 测试 |
| ruff | MIT | `dev` | 静态检查 |

> **重要冲突提示**：`onnxruntime`、`onnxruntime-directml`、`onnxruntime-gpu`
> 三者提供**同一个 `onnxruntime` 包名**，互相覆盖。同时装两个会导致
> `DmlExecutionProvider` 消失、静默退回 CPU。因此它们**故意没有写进基础依赖**，
> 必须由用户通过 extra 三选一。详见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

### 2.3 未随包分发的可选依赖

| 依赖 | 许可 | 状态 |
|---|---|---|
| IOPaint / LaMa | Apache-2.0 | **未内置**。本工具只提供一个 TorchScript 加载器（`images/inpaint.py` 的 `LamaInpainter`），用户把 `lama.pt` 放到 `<data_root>/models/lama/` 才会启用 |
| py7zr | LGPL-2.1 | **未内置**。仅在需要解压 `.7z` 字体包（如更纱黑体）时使用；缺失时回退到系统 `7z`/`7za`/`7zr` |
| UnityPy | MIT | **未内置**。Unity 序列化资源 / AssetBundle 解析尚未实现（见第 5 节） |

> `py7zr` 是 LGPL-2.1。以"独立的可选依赖、不修改、动态导入"的方式使用不触发 LGPL 的
> 传染条款；它**不在**本仓库内，也不随发行版分发。

---

## 3. 模型

### 3.1 PP-OCRv6（检测 / 识别 / 方向分类）

- 主体许可：Apache-2.0（PaddleOCR / PP-OCR 系列）。
- 用途：贴图文字的检测与识别。**这是本工具唯一的文字检测与识别方案**，
  视觉大模型只做极端情况兜底。

| 模型 | 档位 | 体积 |
|---|---|---|
| 检测 det | tiny | 1.8 MB |
| 检测 det | small | 9.9 MB |
| 检测 det | medium | 62.1 MB |
| 识别 rec | tiny | 4.5 MB |
| 识别 rec | small | 21.2 MB |
| 识别 rec | medium | 76.6 MB |
| 方向分类 cls | — | 0.6 MB |

默认档位为 `medium`；`cls` 默认**关闭**（`OcrConfig.use_cls = False`），因为横排游戏 UI
占绝大多数，开着只会多付一份推理开销。

模型文件下载到 `<data_root>/models/rapidocr/`，**不进仓库**（`.gitignore` 已排除 `*.onnx`）。

### 3.2 NLLB-200 —— 明确不使用

**NLLB-200（含 distilled-600M）的许可是 CC-BY-NC-4.0，禁止商业使用。**
因此本工具**不引入、不分发、不默认下载**它。

这不是因为技术原因 —— 研究结论认为它作为"占位符保护型"的预翻译或兜底是合适的
（体积小、成本极低、术语可控、CPU 可跑）。**纯粹是许可原因**：一个许可不干净的默认
依赖会让整个工具的商业化路径断掉。见 [本地翻译层技术调研.md](本地翻译层技术调研.md) 第 1.2 节。

同一原因被排除的还有：`Tower-Plus-9B`、`Sakura-GalTransl-*`（均为 `cc-by-nc-sa-4.0`）。

### 3.3 默认文本翻译模型（通过 Ollama 拉取，非本工具分发）

| 模型 | 许可 | 说明 |
|---|---|---|
| `translategemma:4b` | **Gemma Terms**（非 OSI 开源许可） | 默认文本翻译模型；约 3.3 GB，需要一个 4B 级模型在 8 GB 显存上的余量 |
| `qwen3-vl:4b` | Apache-2.0 | 默认视觉兜底模型（美术字 / 严重变形） |
| `bge-m3` | MIT | 术语表向量检索 |

> **诚实说明**：`translategemma` 的许可不是 OSI 认可的开源许可，而是 Google 的
> Gemma Terms（含使用限制条款）。如果你的使用场景不能接受该条款，请改用
> Apache-2.0 的替代模型（研究报告中提到 `maternion/hy-mt2:7b`）。
> 模型**由 Ollama 从上游拉取到用户本机**，不随本发行版分发。

---

## 4. 字体

### 4.1 总原则

**本工具默认不随发行版分发任何字体文件。**

- 字体在**本机**下载与合并，产物留在用户自己的 `<data_root>/fonts/` 下。
- 交付给用户的是**渲染好的贴图**与**补齐了字形的字体补丁**，不是字体本身。
- 自动下载（`FontService.ensure_font`）**只考虑 `catalog.redistributable()`
  返回的条目**，即同时满足 `bundle_ok=True` **且** 许可属于
  `BUNDLE_OK_LICENSES = {OFL-1.1, Apache-2.0, MIT, Public-Domain}`。

### 4.2 自动下载白名单（许可判定）

只有以下四种许可允许被自动下载/可再分发：`OFL-1.1`、`Apache-2.0`、`MIT`、
`Public-Domain`。常量定义在 `src/novaloc/fonts/catalog.py` 的 `BUNDLE_OK_LICENSES`。

### 4.3 目录内字体的实际许可与 `bundle_ok`（照 `catalog.py` 实录）

| id | family | 许可 | `bundle_ok` | 可自动下载 | 约体积 |
|---|---|---|---|---|---|
| `source-han-sans-sc` | Source Han Sans SC | OFL-1.1 | `True` | 是（整包 zip） | 173.0 MB |
| `lxgw-neo-xihei` | LXGW Neo XiHei | **IPA-1.0** | **`False`** | 是（有直链，但**不进安装包**） | 9.0 MB |
| `sarasa-gothic-sc` | Sarasa Gothic SC | OFL-1.1 | `True` | 是（需要 7z） | 48.0 MB |
| `lxgw-wenkai-screen` | LXGW WenKai Screen | OFL-1.1 | `True` | 是（直链） | 26.0 MB |
| `lxgw-wenkai` | LXGW WenKai | OFL-1.1 | `True` | 是（直链） | 25.0 MB |
| `source-han-serif-sc` | Source Han Serif SC | OFL-1.1 | `True` | 是（整包 zip） | 139.0 MB |
| `smiley-sans` | Smiley Sans | OFL-1.1 | `True` | 是（整包 zip） | 5.8 MB |
| `noto-sans-sc` | Noto Sans SC | OFL-1.1 | `True` | 是（整包 zip） | 50.0 MB |
| `source-han-sans-sc-vf` | Source Han Sans SC VF | OFL-1.1 | `True` | 是（直链） | 10.2 MB |
| `fusion-pixel-12-monospaced` | Fusion Pixel 12px Monospaced SC | OFL-1.1 | `True` | 是（整包 zip） | 25.6 MB |
| `ark-pixel-12-monospaced` | Ark Pixel 12px Monospaced SC | MIT | `True` | 是（整包 zip） | 20.0 MB |

**注意 `lxgw-neo-xihei` 这一行**：它的许可字段是 `IPA-1.0`，而 `IPA-1.0` **不在**
`BUNDLE_OK_LICENSES` 里，所以 `bundle_ok=False`。也就是说：

- ✅ 可以在**本机**下载、渲染、把字形合并进产物（IPA 许可允许使用与嵌入输出）；
- ❌ **不得**把它的字体文件打进安装包或分发给别人。

另外注意：`redistributable()` 同时检查 `bundle_ok` 和许可，所以它**不会**把
`lxgw-neo-xihei` 返回给自动下载流程 —— 即便它 `direct_url` 是合法的。
实际使用时它仍可作为用户本机已安装字体被扫描到。

### 4.4 绝不捆绑的字体

以下字体**一律不允许**出现在任何发行包里。它们要么只授权"免费使用"而非"免费再分发"，
要么带有禁止改作的条款（CC BY-ND）。本工具只会在**扫描用户本机已安装字体**时识别它们
（`NON_REDISTRIBUTABLE_HINTS`），并在日志里给出"可本地使用、不要打包"的提醒。

| 字体 | 禁止捆绑的原因 |
|---|---|
| **MiSans**（小米） | 许可仅授予免费使用，未授予再分发 |
| **Zpix**（最像素） | 商业使用/再分发需另行授权 |
| **全字庫**（TW-Kai / TW-Sung 等） | **CC BY-ND**：禁止改作。子集化与合并属于改作 |
| **HarmonyOS Sans**（华为） | 许可条款限制再分发 |
| **Microsoft YaHei / SimHei / SimSun / NSimSun / FangSong / KaiTi / DengXian** | 微软随系统授权的字体，不得随第三方软件分发 |
| Alibaba PuHuiTi（阿里巴巴普惠体） | 同为"免费使用"授权，不随第三方分发 |

参考实现：`src/novaloc/fonts/catalog.py` 的 `NON_REDISTRIBUTABLE_HINTS`，
以及 `fonts/coverage.py` 的 `non_redistributable_warning()`（只提醒，不阻断本地使用）。

### 4.5 产物不触发 OFL 署名/传染义务

如果本工具**只输出渲染好的贴图**、且**从不随包分发字体二进制**，那么 SIL OFL 1.1
的署名条款（§4）与传染条款（§5）都不触发 —— 因为交付物里不存在字体软件本身。

具体到本工具：

- 贴图是**像素输出**，不是字体程序，不受 OFL 约束；
- 字体补丁（`merge` 策略的产物）是**在用户本机生成**的、写入**用户自己的工作区**的
  文件，不随本工具分发；
- 子集化会让产物成为 OFL 定义的 "Modified Version"，因此 `catalog.py` 提供了
  `safe_subset_family_name()`，把 OFL **保留字体名（RFN）** 替换掉
  （例如 `Source Han Sans SC` → `NovaLoc Sans SC`）。当前 RFN 集合是
  `ALL_RESERVED_FONT_NAMES`，由目录里各条目的 `reserved_font_names` 汇总而来。

> 一句话：**只要你只分发渲染结果、不分发字体文件，就没有 OFL 义务；
> 一旦开始随包分发字体，就必须保留版权声明并遵守 RFN 规则。**

---

## 5. 明确未实现 / 未包含的部分

为避免误解，以下内容**不在**本工具内：

| 项目 | 状态 | 说明 |
|---|---|---|
| Unity 序列化资源（`.assets`）/ AssetBundle / IL2CPP 文本抽取 | **未实现** | 盲写这些二进制几乎必然损坏资源，且失败表现为"游戏打不开"。官方建议用 UABEA / AssetStudio 导出后再走散装文件模式 |
| TMP SDF 图集烘焙 | **未实现** | 需要 Unity 本身或 FreeType SDF 重烘焙，属于独立工作量 |
| UnityPy / IOPaint / py7zr | **未内置** | 见 2.3，均为可选外部依赖 |
| 任何 GPL 代码 | **未包含** | 见第 1 节 |

---

## 6. 更新方式

新增依赖、模型或字体时，请同步更新本文档与 `src/novaloc/fonts/catalog.py`。
判定新字体能否进入白名单的唯一标准是：**许可是否属于 `BUNDLE_OK_LICENSES`**，
且**是否真的允许再分发**（"免费使用"不算）。
