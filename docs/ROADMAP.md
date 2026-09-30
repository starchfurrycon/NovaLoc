# 路线图与当前状态

本文档诚实记录 NovaLoc 新译**已经能做什么**、**还不能做什么**，以及每项缺口的态度
（规划中 / 调研中 / 不计划）。目的是让使用者不会误以为某个功能存在。

状态标记的含义：

| 标记 | 含义 |
|---|---|
| ✅ **已完成** | 代码在仓库里，可以跑通 |
| 🔜 **规划中** | 方向明确、工作量可估，但还没有代码 |
| 🔍 **调研中** | 技术路线或收益还不确定，需要先做验证 |
| ❌ **不计划** | 有意不做，理由见各项 |

---

## 1. 已经完成的

### 1.1 九个流水线阶段（全部）

`src/novaloc/pipeline/stages.py` 里的 `STAGES` 九个阶段都有实现，可以整条跑
（`novaloc run <id>`）也可以单独跑（`--stage <name>`）：

| # | 阶段 | 状态 | 说明 |
|---|---|---|---|
| 1 | `unpack` | ✅ | 探测并解包 `.rpa`/`.zip`/`.tar*`/`.7z`，产出解包树供后续阶段读取 |
| 2 | `detect` | ✅ | 四个适配器并行探测，取置信度最高者；给出 evidence 供排查误判 |
| 3 | `extract` | ✅ | 按引擎格式抽取文本，含地图事件指令 |
| 4 | `images_scan` | ✅ | 发现候选贴图并按路径去重 |
| 5 | `translate` | ✅ | 按 `kind` 分批、占位符屏蔽、术语表注入、翻译记忆、逐批落盘 |
| 6 | `fonts` | ✅ | 字符集规划 → 覆盖审计 → 多源贪心合并 → QA 不变量校验 |
| 7 | `images_localize` | ✅ | OCR → 分组 → 翻译 → 三档去字 → 真实字体重绘 → 单应贴回 |
| 8 | `qa` | ✅ | 占位符 / 漏译 / 长度 / 术语一致性（正向+反向）/ 字体缺字 / 贴图问题 |
| 9 | `apply` | ✅ | 整目录复制到 `out/` 再写入；原始游戏目录只读；末尾回写归档 |

每个阶段幂等，可通过工作区文件断点续跑。

### 1.2 四个引擎适配器

| 引擎 | 状态 | 覆盖内容 | `wire_fonts` |
|---|---|---|---|
| RPG Maker MV / MZ | ✅ 完整 | `data/*.json` 全库（角色/道具/技能/敌人/状态/图块/系统术语）+ 地图事件指令（对白、选项、说话人名） | ✅ 改写 `fonts/gamefont.css` 的 `@font-face` |
| Ren'Py | ✅ 完整 | `.rpy` 对白、菜单选项、角色名、`config.name` 等 | ✅ 生成 `game/novaloc_fonts.rpy` 覆盖文件 |
| Unity | ⚠️ 部分 | `StreamingAssets` 与 `*_Data` 下的明文 json/csv/txt/xml/po/ini | ❌ **未覆写**（需要处理 TMP，见第 2.3 节） |
| 散装文件 | ✅ 完整 | 任意目录下的图片或文本，任何引擎的兜底方案 | ❌ 未覆写（按相对路径复制） |

### 1.3 字体服务

`src/novaloc/fonts/` 五个模块全部可用：

- `charset.py` —— 项目字符集汇总、`UI_SAFE_CHARS`、贪心集合覆盖规划、
  `CharsetCoverageError` 硬失败；
- `coverage.py` —— 字体解析、cmap 审计、系统字体发现、风格猜测；
- `merge.py` —— `merge` / `merge_multi` / `replace` 三种产物生成，
  cmap 重映射、UPEM 缩放、字形重命名、表集合归一化、垂直度量修正、
  OS/2 CJK 区段位；
- `qa.py` —— 五个确定性不变量（cmap / 有墨迹 / 非 `.notdef` / 无 glyph id 冲突 /
  可加载）；
- `catalog.py` + `downloader.py` —— 11 个条目的字体目录、许可白名单、
  多镜像下载（httpx / curl 回退、GitHub 代理、zip/tar.gz/7z 解压）；
- `service.py` —— 审计 → 规划 → 合并/替换 → QA 的编排器。

详见 [docs/FONTS.md](FONTS.md)。

### 1.4 贴图管线

`src/novaloc/images/` 全部模块可用：

- `ocr_ppocrv6.py` —— PP-OCRv6 三档（tiny/small/medium）、DirectML、
  四点规整（`order_quad`）、几何推算（`quad_geometry`）；
- `rec_models.py` —— **按语种路由识别模型**（见下），含按需下载 + SHA256 校验。

**PP-OCRv6 的识别器不含韩语与西里尔文字**（两侧官方文档都写明：
PaddleOCR 说识别支持 50 种语言 = 中/繁/英/日 + 46 种拉丁；RapidOCR 的
模型清单原话是"**不包括韩语**、阿拉伯语、藏语、彝族等语言"）。
对这类语言它不是"读得差"而是**读成空字符串** —— 与"这张图本来没有文字"
在结果上完全一样，流水线不报错、质检不报错，用户只会觉得"这游戏汉化不了"。
属于最难排查的一类问题。

实测（本机，DirectML），同一张写着 `게임 시작` 的图：

| 识别器 | 读到 |
| --- | --- |
| 默认 PP-OCRv6 multi | `''`（空） |
| 按韩语路由到 PP-OCRv5 korean | `'게임'`、`'시작'` ✅ |

修法：**检测不变**（始终 PP-OCRv6 medium，旋转文本 Hmean 93.8），
**识别按语种换模型**。中日英拉丁留在 v6 —— 特别注意**日文必须留在 v6**
（官方 90.5% vs v5 73.7%，差 16.8 个点），有测试守着不许改错方向。

| 语族 | 模型 | 大小 |
| --- | --- | --- |
| 中/日/英/拉丁 | PP-OCRv6 multi | 已含 |
| 韩语 | `korean_PP-OCRv5_rec_mobile` | 13.5 MB |
| 东斯拉夫（ru/be/uk） | `eslav_PP-OCRv5_rec_mobile` | 7.9 MB |
| 其他西里尔 | `cyrillic_PP-OCRv5_rec_mobile` | 8.1 MB |

**实现上两个坑**：

1. v5 模型**无法通过 RapidOCR 的常规配置路径到达**。读
   `rapidocr/utils/model_resolver.py` 才确认 `MODEL_ROUTES` **只注册了
   PPOCRV6**，所以传 `Rec.ocr_version=PPOCRV5` 会让 `resolve_model_key`
   返回 None，随后查不到 model_dict，直接抛 "Invalid OCR configuration."。
   解法：用 `Rec.model_path` 直接指到文件。
2. **不需要字典文件**：ONNX 内嵌了字符集（实测韩语模型 metadata
   `character` 有 23890 项，以谚文字母开头），RapidOCR 走 `have_key()`
   分支。若误以为需要字典，它会去下默认的 `ppocr_keys_v1.txt`（中文），
   结果是"识别出满屏乱码"而**不是**报错 —— 又一个静默失败。
- `textgroup.py` —— 相邻文字块聚合（避免把一句话逐块翻译）+ 译文按原始块数回切；
- `inpaint.py` —— 三档降级（纯色填充 / OpenCV Telea-NS / LaMa）；
- `render.py` —— 2-means 取前景背景色、描边色估计、字号二分收缩、CJK 混排折行、
  超采样 + BOX 降采样、单应变换贴回（支持旋转/透视）；
- `io.py` —— 非 ASCII 路径安全的图像读写；
- `annotate.py` —— 生成带文字框标注的可视化图，供 Web 审校页显示；
- `service.py` —— 整图编排（`process` / `process_many`）。

### 1.5 翻译层

- Ollama 提供者（`ollama_provider.py`）与客户端（`ollama_client.py`：
  `/api/chat`、`/api/tags`、`/api/pull` 的 NDJSON 流式解析）；
- `ollama_setup.py` —— 安装探测、winget 安装提示、模型状态快照、
  推荐模型检查、`OLLAMA_MODELS` 环境变量建议、`ollama serve` 就绪等待；
- `placeholders.py` —— 20 条正则覆盖 RPG Maker / Ren'Py / 富文本 / printf / .NET /
  Python / shell / 反引号代码 / 具名尖括号；`⟦i⟧` 屏蔽 + 三层还原校验
  （多重集 / 剩余记号 / 出现顺序）；
- `prompts.py` —— 批量提示、单条提示、复核提示、术语表注入块；
- `json_parse.py` —— 宽容 JSON 解析（剥代码围栏、括号平衡提取、修复常见错误、
  定界符回退、索引对齐）；
- `guards.py` —— 清洗、占位符检查、长度检查、未翻译检测、假名/源语言残留检测、
  重复 n-gram 检测；
- `memory.py` —— 翻译记忆（完全匹配 + 模糊匹配，默认 `memory_similarity=0.97`）；
- `glossary.py` —— 术语匹配（大小写敏感 / 全词 / 正则）+ 术语候选挖掘 +
  64 条内置游戏术语表（**不默认注入**，理由见下）。

**两处实测教训**（都影响"译文质量"这类看不见的问题）：

1. **内置术语表不默认注入**。曾试过在流水线里自动并入 64 条游戏术语
   （修 `MP` → "魔法值"这类缩写误译）。实测是**净亏**：同一批 27 条
   真实游戏字符串跑 3 遍，漏译从 0 升到 2.67/遍，还出现
   `Load Game` → "重新开始"（应为"读档"）的污染 —— 提示词把
   `Restart`/`Save`/`Load` 一起注入，4B 模型在小批量里会串。而
   "过滤掉源词已在文本里的术语"这条安全规则会**正好**滤掉
   `HP`/`MP`/`Gold`，也就是唯一受益的条目，等于自己取消自己。
   现在 `builtin_entries()` 只作为用户可查可抄的起点。

2. **缩写误译改由质检兜住**。`MP` → "生命值" 这类错误，不是漏译、
   不是占位符丢失、源文也各不相同，传统规则**完全抓不到**。
   `qa.py` 因此新增"反向碰撞"检查：不同的**短标签**（无空格、≤4 字符）
   映射到同一译文 → 报 **ERROR**。只查短标签是因为长句撞词可能是
   合理的同义表述（"我明白了"/"原来如此"），用规则去压会伤害翻译自由度。

3. **尾部占位符被删除时不再丢弃整条译文**。实测
   `Gold: {gold}` 被模型译成 `'黄金'`（占位符被整段删掉）；
   `repair_dropped_masks()` 因纯汉字里没有词边界可用而放弃，导致整条
   `placeholder_broken`、**完全不产出译文**。现在若该记号是原文最后一个
   记号且其后无非空白内容，则确定性地补到译文末尾（不可能劈开中文词、
   也不会造成 `%dgold` 粘连）。

设计依据与实测数据见 [本地翻译层技术调研.md](../本地翻译层技术调研.md)，
本文档不重复。

### 1.6 CLI、API 与 Web UI

**CLI**（`novaloc`，Typer）：`doctor` / `scan` / `project new|list|show|delete` /
`run`（含 `--stage`）/ `text export|import`（CSV / JSON / PO）/
`fonts list|audit` / `ollama status|pull|env` / `qa` / `serve`。

**API**（FastAPI，`api/app.py`）：

| 方法 | 路径 |
|---|---|
| GET | `/api/health` |
| GET / PUT | `/api/settings` |
| GET / POST | `/api/projects` |
| GET / DELETE | `/api/projects/{pid}` |
| POST | `/api/projects/{pid}/detect\|extract\|translate\|fonts\|images\|qa\|apply\|run` |
| GET / PATCH | `/api/projects/{pid}/text` |
| GET / PATCH | `/api/projects/{pid}/images` |
| GET | `/api/projects/{pid}/images/{uid}/annotated` |
| GET | `/api/projects/{pid}/fonts` |
| GET | `/api/projects/{pid}/qa` |
| GET / DELETE | `/api/jobs`, `/api/jobs/{job_id}` |
| WS | `/ws/jobs/{job_id}` |
| GET | `/` 与 SPA 回退（挂载 `novaloc/web_dist`） |

API 文档页：`/api/docs`。

**Web UI**（React 19 + Vite + Tailwind 4 + Arwes）：

| 页面 | 功能 |
|---|---|
| `Projects` | 项目列表、新建、删除 |
| `Scan` | 引擎识别 |
| `Fonts` | 字体目录、覆盖审计、补丁结果 |
| `TextReview` | 译文逐条审校（配合 `text export/import`） |
| `Images` | 贴图审校（带标注可视化图） |
| `Qa` | 质检报告 |
| `Job` | 任务进度（WebSocket 实时日志流） |
| `Settings` | 配置编辑 |

前端构建产物 `src/novaloc/web_dist/` **随仓库提交**，所以运行时不依赖 Node。（放在包内是为了让 wheel 也带上它，见第 3 节 #13）

### 1.7 测试现状（重要）

**测试代码在 `tests/` 下。** 共 21 个套件，覆盖
占位符、JSON 解析、字体合并、字体接线、字符集合并、
贴图管线、贴图服务、翻译集成、四个引擎的抽取、端到端 API（含 WebSocket）、
流水线端到端，以及**用真实字体**验证字体合并的 `test_font_real.py`。

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q          # 全量 57/57
.\.venv\Scripts\python.exe tests\test_font_real.py     # 单跑，输出实测数字
```

（早期这些脚本散在 `.scratch/` 下，而 `pyproject.toml` 的
`testpaths = ["tests"]` 指向一个不存在的目录 —— pytest 会以
"no tests ran"（退出码 5）静默通过，看起来是绿的其实一条没跑。
现已修正，详见 [tests/README.md](../tests/README.md)。）

CI 现在会跑**不需要模型与 GPU** 的那部分：

```
pytest tests -q -m "not needs_fonts and not needs_gpu and not needs_models"
```

需要模型 / GPU / 系统字体的套件在本地跑。**所以"CI 绿了"仍然不等于
"功能正确"** —— 字体合并、OCR、贴图重绘这些核心环节只有在本机才能验证。

---

## 1.8 字体合并的垂直度量必须按 UPEM 换算（已修，记录以备回归）

实测数据（`tests/test_font_real.py`）：

| 字体 | unitsPerEm | hhea.ascender | asc/upem |
| --- | --- | --- | --- |
| `simhei.ttf`（基准） | 256 | 220 | 0.86 |
| `lxgw-wenkai-screen.ttf`（补充） | 2048 | 1900 | 0.93 |
| 合并产物（**修复前**） | 256 | **2210** | **8.63** ← 错 |
| 合并产物（修复后） | 256 | 238 | 0.93 |

根因：`_snapshot_metrics()` 在补充字体**自己的 UPEM 坐标系**下抓度量，
随后被原样写进 UPEM=256 的合并字体；`fontTools` 的 `Merger` 会把字形
缩放到 `base_upem`，但度量没人管。

后果比缺字形更糟：Pillow 把文字排到 y=399（48px 画布之外），
游戏里表现为**文字彻底不可见**，而所有静态检查（cmap、轮廓数、
水平度量、FreeType 可加载）**全部通过** —— 也就是"QA 说没问题，
游戏里什么都看不到"。这正是本项目最想避免的那类假成功。

现在的判据：`ascender / unitsPerEm` 的比值必须与基准字体同量级。
静态不变量查不出这类错误，必须**实际渲染并检查墨迹**。

---

## 2. 当前的缺口

### 2.1 Unity 序列化资源 / AssetBundle / IL2CPP —— 🔜 规划中

**现状**：不支持。`UnityAdapter` 只处理 `StreamingAssets` 与 `*_Data` 下的
**明文** json/csv/txt/xml/po/ini。

**缺口**：

| 文本形态 | 需要的技术 |
|---|---|
| `.assets` / `.unity3d` 序列化资源 | Unity 序列化格式解析（UnityPy 已提供） |
| AssetBundle | 同上 + bundle 容器格式与压缩 |
| IL2CPP（`global-metadata.dat`） | 元数据解析 + **定长字符串替换**（改长度要搬移所有偏移） |

**为什么不做成"先做最简版本"**：README 已明确表态 ——

> 我们不会去盲写这些二进制格式 —— 盲写几乎必然破坏资源，
> 而且失败时表现为"游戏打不开"，用户根本查不出原因。

**当前给用户的替代路径**：用 [UABEA](https://github.com/nesrak1/UABEA) 或
[AssetStudio](https://github.com/Perfare/AssetStudio) 导出 `TextAsset`，
再用散装文件模式处理。`UnityAdapter` 会在 evidence 里报告检测到的托管后端类型，
并在发现未处理的序列化资源时给出这个建议。

**态度的理由**：这个功能的**风险收益比**很不对称 ——
做对了只是"多支持一类游戏"，做错了是"用户游戏打不开且不知道为什么"。
所以要做就必须配一套"回写后校验"机制（重新解析产物确认结构完好），
工作量远超解析本身。

### 2.2 视觉 VLM 兜底 —— ✅ 已完成（**只做内容纠错，不做检测**）

**现状**：已经接通。

- `translate/vision_ollama.py` 提供 `OllamaVisionEngine`，注册为
  `@register("vision", "ollama")`；实测 `registry_snapshot()` 里
  `'vision': ['ollama']`。
- `images/service.py::_vlm_rescue()` 在检测之后、翻译之前执行：
  挑出 `confidence < vlm_threshold`（默认 0.6）的块，按四边形做透视校正裁出正立区域
  （`_crop_quad`），交给 VLM 重读**内容**。
- `_vlm_reconsider()` 的 docstring 明确了边界：

  > **只用来纠正文字，绝不用来改框**：VLM 的定位精度比专用 OCR 差两个数量级
  > （实测旋转文本 Hmean 2.1 vs 93.8），一旦让它决定位置，排版就毁了。
  > 所以这里只取文本，四边形坐标原样保留。

- 替换条件很保守：**只在 VLM 给出非空、且归一化后与 OCR 原文不同的结果时才替换**，
  读不出来就保留 OCR 原结果（避免"兜底把好结果搞坏"）。
  被改过的块带上 `vlm_reread` 警告，并计入 `vlm_reads` / `vlm_fixes`。
- 关闭方式：`OcrConfig.vlm_fallback = False`，或者干脆不起 Ollama
  （`vlm` 属性会 `available()` 失败并返回 `None`，只打一行 info 日志）。

**仍然存在的边界 —— 这是最重要的一点**：

**检测永远不由 VLM 做。** 实测旋转文本检测的 Hmean：

| 方案 | 旋转文本 Hmean |
|---|---|
| PP-OCRv6 small | 93.7 |
| PP-OCRv6 medium | 93.8 |
| Qwen3-VL-235B | 2.1 |
| GPT-5.5 | 10.0 |

差一个数量级。VLM 能"读懂图里写了什么"，但不输出稳定的文字框。
所以如果 PP-OCRv6 **一个框都没检出来**，就没有低置信度的块可供兜底，
VLM 也不会被调用 —— 这时问题在检测能力，兜底帮不上。

> **注意**：上述四个数字不在 `本地翻译层技术调研.md` 里
> （那份报告讨论的是文档解析方向的 OCR，建议的是 PaddleOCR PP-OCRv5 + qwen3-vl 兜底）。
> 这组对比来自本项目自己的验证记录。

**剩余可做的事 —— 🔜 规划中**：

- 兜底**没有缓存**，同一段文字在不同贴图上重复出现会重复问模型；
  `_vlm_reconsider` 也没有按"已成功重读过的文字"做记忆。
  贴图里重复出现的 "OK" / "Cancel" 很常见，加一层缓存能省大量时间。
- `describe_style()` 已定义在 `VisionEngine` 协议里，但 `OllamaVisionEngine`
  是否实现了它、以及有没有被 `render.py` 用上，需要单独确认；
  **风格描述**（字重、是否艺术字、描边粗细）本可以用来改善重绘的观感。


### 2.3 TMP 静态图集 SDF 烘焙 —— 🔜 规划中

**现状**：不支持。这是 Unity 字体替换的**真正难点**。

**问题本质**：TextMeshPro 在运行时查的是 `TMP_FontAsset` 里的
`characterLookupTable` 与配套的 **SDF 图集纹理**。
**把 `.ttf` 拷进 `StreamingAssets` 对 TMP 完全无效** —— 它根本不读那个文件。

**要做的工作**：

1. 从 `TMP_FontAsset` 的序列化数据里读出图集尺寸、padding、`faceInfo` 等参数；
2. 用 FreeType 的 SDF 模式（或自己实现有符号距离场生成）为新增字符生成 SDF 位图；
3. 把新字形**塞进已有图集**（如果没有空位就要扩图集并更新所有 UV 矩形）；
4. 重建 `characterLookupTable` 与 `glyphTable`；
5. 重新序列化 `.asset` 并把图集 PNG 写回。

**为什么标注"规划中"而不是"调研中"**：技术路径是清楚的
（Unity_Font_Replacer 已经验证过可行性），只是工作量大且需要
Unity 序列化格式的读写能力 —— 而后者与第 2.1 节是同一块基础设施。
**所以这两项的优先级应该绑定：先做序列化格式读写，再做 SDF 烘焙。**

**相关法务提示**：Unity_Font_Replacer 是 GPL-3.0。本工具只借鉴"必须重新烘焙图集"
这个结论，**不会复制其代码**。见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 第 1.4 节。

### 2.4 归档解压与回写 —— ✅ 已完成（`src/novaloc/archives/`）

**现状**：独立成包的归档层，作为流水线的**第一个阶段**（`unpack`）。

| 格式 | 读 | 写 | 实现 |
| --- | --- | --- | --- |
| `.rpa`（Ren'Py，v2.0/v3.0/v3.2） | ✅ | ✅ | 自研，含 XOR 前缀与受限反序列化 |
| `.zip` | ✅ | ✅ | `zipfile` |
| `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tar.xz` | ✅ | ✅ | `tarfile` |
| `.7z` | ✅ | ❌ **故意只读** | `py7zr`，退回 `7z`/`7za`/`7zr` CLI |

7z **不回写**是刻意的：写不回去时宁可明确拒绝，也不要产出一个半坏的归档。
`probe()` 会把它标成 `writable=False`，回写阶段据此拒绝并提示用户手工打包。

**工作方式**（这是设计上最容易出错的地方）：

```
unpack 阶段   解包到 <workspace>/unpacked/<归档名>/
各阶段        读的是「归档解包后的目录」（effective_source），不是归档文件
apply 阶段    把产物写到 out/，**不动**原目录与解包树（延续"原游戏只读"）
apply 末尾    repack(changes_from=out/) 把改动打回原归档，原文件备份为 *.novaloc.bak
```

注意 `changes_from=out/` 这个参数：流水线**不修改解包树**，如果 `repack`
去比对解包树，会得到"没有变化" → `repacked: 0` → 所有阶段都报 ok、
归档里却还是原文。这是本项目踩过的最阴的一类 bug（统计上完全成功、
产物是旧的），所以 `repack()` 的文档里把它写清楚了。

**安全三重**（读不可信归档时）：

1. 路径穿越 —— `safe_member_path()` 拒绝绝对路径、`..`、UNC、盘符、控制字符；
2. 解压炸弹 —— `MAX_ENTRY_BYTES` 256 MiB / `MAX_TOTAL_BYTES` 2 GiB /
   `MAX_ENTRIES` 20 万，**在读之前**按自报大小检查；
3. 恶意 pickle —— `.rpa` 索引是 pickle，用 `_RestrictedUnpickler` 白名单反序列化。
   白名单**必须**包含 `_codecs.encode`（pickle protocol 2 用它编码 bytes），
   否则所有带 XOR 前缀的正常归档都会被拒。

**还没做**：`.xp3`（KiriKiri）、Wolf RPG 的 `.wolf`、Unity `resources.assets`
的自定义容器 —— 见 2.1 与 2.5。加密/私有容器格式**明确不做**，那是无底洞。

### 2.5 更多引擎 —— 🔜 规划中

按"需求密度 / 实现难度"排序：

| 引擎 | 状态 | 难度 | 理由 |
|---|---|---|---|
| **Godot** | 🔜 规划中 | 中 | `.tscn` / `.tres` 是**纯文本**，解析风险低；`.pck` 需要解包。变现路径清楚 |
| **RPG Maker 旧版本**（VX / VX Ace / XP） | 🔜 规划中 | **低** | 与 MV/MZ 的 `data/` JSON 结构高度相似，`RPG Maker VX Ace` 用 `.rxdata`（Ruby Marshal 格式），需要额外解析器；XP 更老。**投入产出比最高的一项** |
| **Wolf RPG** | 🔍 调研中 | 中 | `.wolf` / 数据文件是私有二进制格式；社区有工具但格式未完全文档化 |
| **KiriKiri**（`.xp3`） | 🔍 调研中 | 中高 | `.ks` 脚本是纯文本（容易），但 `.xp3` 容器与加密变体是难点。且这个生态的文本量大、翻译价值高 |
| **NScripter / ONScripter** | 🔍 调研中 | 低 | `.txt` / `.dat` 格式简单，但生态老旧 |
| **Unreal Engine** | ❌ **不计划** | 很高 | 文本在 `.pak` + `.uasset` 二进制里，且格式随引擎版本变化。且 Unreal 游戏通常自带完善的本地化系统，用官方流程更合适 |
| **专有引擎**（各种自研） | ❌ **不计划** | — | 无法泛化。这类需求应该用**散装文件模式**兜底 |

**新增引擎的接口**已经有清晰的骨架，见
[docs/ARCHITECTURE.md](ARCHITECTURE.md) 第 7.3 节的完整示例。
需要动的只有两处：新建 `src/novaloc/engines/<name>.py`，以及在
`engines/__init__.py` 的 `_CLASSES` 与 `_resolve()` 里登记。

### 2.6 `review.py` 接触印样 —— 🔜 规划中

**现状**：不存在这个模块。

**需求**：批量贴图审校时，逐张打开图片效率很低。需要生成"接触印样"
（contact sheet）—— 把 N 张重绘后的贴图拼成一张大图（带编号），
让用户一眼看出哪几张需要处理。

**已有的基础**：`images/annotate.py` 已经有 `annotate_blocks()`，
能在图上画出文字框、角标与序号，产出"带标注的可视化图"；
API 也已经有 `GET /api/projects/{pid}/images/{uid}/annotated`。
所以 `review.py` 要做的是**在它之上做拼版**：

```
读 images/localize.json 找 needs_review 的图
  → 逐张 annotate_blocks()
  → 缩放到统一单元格尺寸
  → 按网格拼成一张大图
  → 每格左上角写编号 + 文件名
  → 输出到 images/analyzed/contact_sheet_N.png
```

**为什么要做**：当前 `needs_review` 的判定已经存在
（`overflow` / `too_small` / `warnings`），QA 报告也会统计
`images_review` 的数量，但**用户没有高效的方式去看这些图**。
缺口不在检测，在呈现。

**优先级**：中。它不增加能力，只提升可用性，所以排在第 2.1–2.5 之后。

---

## 3. 已知的结构性问题（不是新功能，是需要修的）

这些在 [docs/ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节有更完整的技术描述。
列在这里是因为它们**影响正确性**，不只是体验。

| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| 1 | ~~`images/service.py::_pick_font()` 用硬编码路径列表找中文字体；`FontSpec.local_path` 属性不存在，所以目录查找分支实际不生效~~ | **已修**：改走 `FontService`（系统字体索引 → 缓存 → 可再分发下载）。顺带发现 `_find_system_font()` 每次要 rglob 并逐个解析整个字体目录（实测 0.3~1.5 秒/次），而 `_pick_font` 会按十几个家族名各找一次 —— 每张贴图白花十几秒。已加进程级索引缓存（首次 2.2 秒，之后 0.000 秒） | ✅ 已完成 |
| 2 | ~~`pipeline/stage_apply` 只把 `action == "merge"` 的字体补丁传给适配器~~ | **已修**。这里有两个叠加的错：`stage_fonts` 把 `action` 硬编码成 `"merge"`（与 `cfg.font.strategy` 无关），`stage_apply` 又按 `action == "merge"` 过滤 —— 于是 `replace` / `fallback_only` 的产物虽然 `ok=True`、`out_path` 也有值，却**永远不会回写**，文件躺在工作区里而游戏毫无变化，且不报错。现在 `stage_fonts` 记录真实的 `pr.merge.method`，`stage_apply` 改用 `font_patch_records()` 按"注入成功且有产物"判断（只排除 `none`）。`tests/test_pipeline_e2e.py` 第 7a 节直接喂四种 action 守住它 | ✅ 已完成 |
| 3 | ~~`engines/unity.py` 与 `engines/loose.py` **没有覆写 `wire_fonts()`**~~ | **已修**：两个适配器都覆写了 `wire_fonts()`，并抽出公共的 `EngineAdapter.copy_fonts_into()`。**但刻意如实说明限制**：Unity 的 UI 文字走 TextMeshPro，渲染用的是**预烘焙图集**（`m_AtlasTextures` 指向一张贴图，字形是位图块），换 TTF 对已烘焙资源**没有任何影响** —— 重新烘焙需要解析 `TMP_FontAsset` 序列化格式、按原字号重新光栅化并重算 `m_GlyphTable`/`m_CharacterTable`/`m_FaceInfo`，且必须用 Unity 自身的排版度量才能与游戏一致，是独立的大工程，本版**未实现**。所以 Unity 的处理是：字体复制到 `*_Data/StreamingAssets/_novaloc_fonts/`（StreamingAssets 会被原样打进构建，是运行时最可能读到的位置）+ 检测到 TMP 资源时明确输出「需要重新烘焙」与 Font Asset Creator 的具体步骤 + 提到动态（Dynamic）TMP 资源这一真实例外（那种情况换 StreamingAssets 里的字体确实有效）。散装模式则是：就地替换同名文件 + 集中放到 `_fonts/` + 说明无法自动改字体指向。**宁可明确告知需要人工介入，也不产出「看着成功、实际没用」的结果。** `tests/test_font_wiring_engines.py` 13 项守住（含「必须覆写」与「必须如实说明」） | ✅ 已完成 |
| 4 | ~~`translate` 包没有 `__init__.py`~~ | 已修：`src/novaloc/translate/__init__.py` 已加入。它刻意**不在 `__init__` 里即时导入子模块**（避免循环依赖与"一 import 就注册 provider"的副作用），改用 `__getattr__` 做懒加载 | ✅ 已完成 |
| 5 | ~~配置有两套格式：`<data_root>/config.json` 与 `<config_dir>/config.toml`~~ | **已修**：统一到 `<data_root>/config.json`。`Config.load/save` 是唯一实现，API 与 CLI 都委托给它；旧 TOML 只在 JSON 缺失时作只读回退并**自动迁移**，老用户不丢配置 | ✅ 已完成 |
| 6 | ~~`tests/` 目录不存在，而 `testpaths = ["tests"]`~~ | **已修**：套件搬进 `tests/`，`pytest tests -q` 全量 57/57。CI 也改为按 marker 跑无需模型/GPU 的 12 个（之前 CI **完全不跑 pytest**，纯逻辑回归只能靠人肉发现） | ✅ 已完成 |
| 7 | ~~`ocr_ppocrv6.py::available()` 只检查 `import rapidocr`，不检查模型是否已下载~~ | **已修**：新增 `missing_models()` 按档位检查 `PP-OCRv6_{det,rec}_{tier}.onnx` 是否在 `model_root_dir` 下，`available()` 在**进入 RapidOCR 之前**就据此返回，并点明缺哪个文件、该放哪里。这条对「全离线优先」是硬伤：自检显示 ✅ 但首次识别才联网下载（断网则直接失败），用户完全无法自查。`tests/test_ocr_available.py` 3 项守住，其中一项断言`available()` **不产生任何下载文件** | ✅ 已完成 |
| 8 | `stage_images_localize` 逐图串行 | 贴图多的大项目耗时长。并发会争抢 GPU，所以是有意保守，但可以做成"OCR 与重绘流水线化" | 🔍 调研中 |
| 9 | ~~视觉兜底**逐块串行、无缓存**~~ | **已修（缓存部分）**：新增裁剪图缓存 `_vlm_cache`，键为裁剪像素的 sha1。键用像素而非文字，因为这里的问题恰恰是「我们还不知道文字是什么」；而「像素完全相同 ⇒ 文字相同」是充分条件且可精确判定。缓存**跨图片**存活（与图片无关），因为同一套 UI 图形在不同图里反复出现。空结果也缓存 —— 读不出来时重问通常还是读不出来，而重复的失败重问正是最浪费的部分。实测一张图里三处相同按钮：VLM 调用 3 次 → 1 次。**串行部分保留**：并发会争抢 GPU，与 #8 一起属于有意保守，见该项 | 🟡 部分完成（缓存已加，并发仍串行） |
| 10 | `novaloc serve --reload` 曾是空操作 | **已修**：uvicorn 的 reload 需要可导入字符串才能起子进程；传 app 实例时它只打一行 warning 然后静默忽略。已改为 `"novaloc.api.app:create_app"` + `factory=True` | ✅ 已完成 |
| 11 | `Providers.diagnostics()` 把四个引擎全报成不可用 | **已修**：对 `kind == "engine"` 也调 `available()`，而 `EngineAdapter` 没有该方法，于是 detail 是 `'RpgMakerAdapter' object has no attribute 'available'`。用户看到"四个引擎全部不可用"会以为工具坏了 | ✅ 已完成 |
| 12 | ~~`translate/vision_ollama.py` 的视觉兜底**从来没有工作过**（四个叠加的 bug）~~ | **已修**。四个 bug 全被 `available()` 的 `except Exception` 吞成「Ollama 探测失败：…」，而 `doctor` 只显示「不可用」，看起来像环境问题：① `OllamaClient(cfg)` —— 构造函数收的是 **host 字符串**，不是配置对象，于是 `host.rstrip('/')` 抛 `'OllamaConfig' object has no attribute 'rstrip'`；② `client.ping()` —— 方法是 `is_running()`，没有 `ping()`；③ `cfg.timeout_s` —— 字段名是 `request_timeout_s`；④ **最隐蔽的一个**：`_extract_text()` 只认 JSON **对象**，而 `VISION_READ_PROMPT` 明确要求模型「只输出 JSON **数组**」。数组解析不出来就一路降级到「裸文本」，把**整段原始 JSON 字符串**当识别结果返回 ——文字非空，于是 `_vlm_rescue` 会把它当作「更可信的读数」**覆盖掉原本正确的 OCR 结果**（静默损坏，且日志显示「兜底生效」）。顺带修了客套前缀剥离「试一遍就结束」的问题：前缀会叠着出现（`好的，图中文字是：…`），已改为循环到收敛。`tests/test_vision_ollama.py` 20 项守住（含「源码里不得再出现 `ping(`」与「绝不返回原始 JSON」），`tests/test_vlm_cache.py` 里另有 4 项跑通完整兜底链路并断言**文字框不被改动** | ✅ 已完成 |
| 13 | ~~wheel 里没有前端；版本号写了三份；`import novaloc.api.app as m` 拿到的是 FastAPI 实例~~ | **已修**。三个都是"能跑但结果是错的"：① 前端产物在仓库根的 `web/dist`，而 hatchling 只打包 `src/novaloc`，于是 `pip install` 出来的界面**根路径 404**、构建时零警告（已挪进 `src/novaloc/web_dist/`，并加了 CI 断言，见 #14）；② 版本号在 `pyproject.toml` / `__init__.py` / `api/app.py` 各写一份，升 0.2.0 时漏改第三处，`/api/health` 报 0.1.0（已改 `dynamic = ["version"]` 单一来源）；③ 模块里的 `app = create_app()` **遮蔽同名子模块**，`import novaloc.api.app as m` 得到 FastAPI 实例而非模块（`from ... import` 反而正常，所以只在特定写法下炸），已改用 PEP 562 模块级 `__getattr__` | ✅ 已完成 |
| 14 | ~~CI **完全不跑打包断言**，wheel 少了前端也没人知道~~ | **已修**：新增 job `wheel-contains-frontend`，两步 —— 先直接看 wheel 的 zip 内容，再**真装一遍并 `cd` 到仓库外导入**确认 `_web_dist()` 在安装位置找得到。第二步的 `cd` 不能省：在仓库里导入会命中源码树那份，测了等于没测（第一次就是这么被骗过去的）。写这条 CI 时自己也踩了一次：直接用临时目录 import，绕过依赖安装 → `ModuleNotFoundError: No module named 'fastapi'`；加了 `.scratch/_ci_clean_venv.py`（建**干净 venv** 复现 runner 条件）后才推 | ✅ 已完成 |
| 15 | ~~`engines/loose.py::wire_fonts()` 的"同名就地替换"在 Linux 上永远不生效~~ | **已修**。`copy_fonts_into()` 先把自己补好的字体拷到 `out/_fonts/game.ttf` —— 它的**文件名就是游戏原字体的名字**（"同名替换"的前提）。随后扫描 `out_dir` 找同名文件时若把这个副本也扫进来，`by_name` 命中它，`dst.resolve() == src.resolve()` 判定"同一个文件"而 `continue`，**真正的 `out/fonts/game.ttf` 永远不被替换**。而扫到什么取决于文件系统顺序：下划线 ASCII 0x5f 小于字母，Linux 上 `_fonts` 排在 `fonts` **前面**（WSL 实测 `rglob` 顺序为 `_fonts, fonts, _fonts/game.ttf, fonts/game.ttf`），Windows 恰好相反 —— 所以这个 bug 在开发机上一直是绿的，只在 CI 上红。修法是**显式排除 `_fonts/`**，不去赌枚举顺序。`tests/test_font_wiring_engines.py` 新增 2 项守住，其中一项用 monkeypatch **把 Linux 的（不利）顺序固定进测试**，否则它在 Windows 上照样抓不到 | ✅ 已完成 |
| 16 | ~~测试套件之间靠**执行顺序**隐式耦合，且测试写死系统字体路径~~ | **已修**。两类问题：① `test_api_e2e` / `test_pipeline_e2e` / `test_engines_all` 都读 `tests/fixtures/rpgmaker_game`，但其中 `data/` 被 `.gitignore` 的通用 `data/` 规则忽略，**只有 `test_engine_rpgmaker` 跑过才会有** —— 单独跑必红，跑全量永远绿；`test_engines_all` 更糟，它那句 `if not path.exists(): continue` 会让检查**静默跳过**（看着是绿的，其实这条断言从没在本文件里执行过）。已改为会话级临时目录 + 共享构造函数（`tests/_fake_game.py` + `conftest.py::_fake_game_dir`）。② `test_engine_rpgmaker` 写死 `C:\Windows\Fonts\arialbd.ttf`，Linux 上 `OSError: cannot open resource`。已改用 `tests/_minimal_font.py` 自造字体。**这个自造字体本身也踩过一次**：第一版给每个字符画同一个**方块**，PIL 能加载、有墨迹、静态检查全绿，但 OCR 完全认不出（40px 时"墨迹"占 88% 像素 = 一整块实心矩形），贴图汉化报告"0 处文字"。现在改为**从 Pillow 内置字体（Aileron）抽出真实字形轮廓重新组装**，OCR 实测能正确读出 `NEW GAME`。另附 `.scratch/_run_each_alone.py` 逐个文件单独跑，防止这类耦合复发 | ✅ 已完成 |
| 17 | ~~OCR 把一行文字切成**互相重叠**的两块时，重绘产物是「中文 + 残留外文」的花字~~ | **已修**。这条是**端到端实跑**抓到的，不是读代码想出来的：对一张真实游戏贴图（`img/system/Window.png`），PP-OCRv6 medium 把 `NEW GAME` 切成 `'NEW' box=(13,29,123,76)` + `'V GAME' box=(99,27,253,78)` —— **横向重叠 24 px**（它把 `W` 读成了 `V`）。而重绘是"擦掉本块的框 → 写本块的译文"，于是擦 A 连带擦掉 B、两块译文又互相覆盖，**产物 OCR 回读得到 `'新V GAME'`**。最糟的是流水线当时报告 `ok=True / translated=2 / changed=True` —— 从统计看完全成功。修法两层：① **几何层**：横向重叠 ≥ 较小块宽度 15% 时**强制合并成一组**（`OVERLAP_FORCES_MERGE`），越过 `MIN_MERGE_CHARS` 等所有保守判据，因为这里的后果不是排版难看而是文字互相覆盖；② **文本层**：合并后**不能**用拼接文本翻译 —— 重叠区文字会数两遍（`'NEW'`+`'V GAME'` → 幻影 `'NEW V GAME'`），必须对整组包围盒**重读**一次，并整组一次性重绘、整句译文交给首块。实测修复后同一张图产出 `'全新游戏'`（conf 1.000）。测试入口 `test_textgroup_overlap.py`（12 项，纯几何，CI 里跑）+ `test_texture_overlap.py`（4 项，注入**实测坐标**）。**验证过 7/16 项在退掉修复后会红**（其余 9 项是"不该变"的保护项，理应仍然绿） | ✅ 已完成 |

---

## 4. 明确不计划做的事

写出来是为了节省沟通成本。

| 事项 | 理由 |
|---|---|
| **用生成式模型"画"贴图文字** | README 已经说明了取舍：生成模型不保证笔画正确、不需要字真的存在，结果是"看起来像中文但是错字"，而且尺寸字重描边每次都不同。**宁可风格略有差异，也不能画出错字**。这条不会变 |
| **让视觉大模型做文字检测** | 实测 Hmean 差一个数量级（PP-OCRv6 93.7/93.8 vs Qwen3-VL-235B 2.1、GPT-5.5 10.0），且 VLM 不输出稳定文字框 |
| **引入 NLLB-200 或任何 CC-BY-NC 模型** | 许可为 CC-BY-NC-4.0，禁止商业使用。技术上是合适的兜底方案，但许可不干净 |
| **随发行版分发字体文件** | 会让 OFL / IPA / 商业授权的义务全部触发。默认 `bundle_fonts=False` |
| **修改只有 `.rpyc` 的 Ren'Py 发行版** | 编译产物改了游戏也不认，且这是作者明确的意图。工具明确报告失败而不是绕过 |
| **支持加密/私有归档容器** | 无底洞。用户可以先自己解包，再用散装文件模式 |
| **绕过游戏的反调试/反篡改** | 与本地化的目标无关 |
| **联网翻译 API（OpenAI / DeepL 等）** | 本工具的核心卖点之一是**完全离线** —— 不把游戏文本发到任何服务器。`registry` 的 `translate` 类别在架构上支持云 API 适配器，但**本仓库不会内置** |
| **为个别游戏写专用适配器** | 无法泛化。这类需求走散装文件模式 |

---

## 5. 优先级建议

如果要在上述缺口里排一个顺序，按"能解锁多少用户 / 风险多低"：

| 顺序 | 事项 | 理由 |
|---|---|---|
| 1 | ~~修第 3 节的第 6 项（`testpaths` 失配）~~ **已完成** | 现在有 21 个套件 / 81 个用例，CI 也按 marker 跑其中的纯逻辑部分 |
| 2 | ~~修第 3 节的第 1、2 项（`_pick_font`、`stage_apply` 的补丁过滤）~~ **已完成** | 两项都是正确性 bug，已修并有回归测试守住 |
| 3 | ~~修第 3 节的第 9 项（视觉兜底加缓存）~~ **已完成** | 裁剪图缓存已加（键为像素 sha1，跨图存活）。同时发现并修掉视觉适配器四个叠加的 bug —— 该链路**此前从未工作过**（见第 3 节 #12）。并发仍串行（有意保守，见 #8） |
| 4 | 第 2.5 节的 **RPG Maker VX Ace / XP** | 与现有 MV/MZ 代码高度复用，解锁一整类游戏 |
| 5 | 第 2.6 节 `review.py` 接触印样 | 纯提升可用性，工作量小，直接缓解"审校效率低" |
| 6 | 第 2.4 节通用归档解压 | 复用已有的 `extract_archive()`，中等工作量 |
| 7 | 第 2.1 节 Unity 序列化格式读写 | 高风险高投入，但它是第 2.3 节的前置 |
| 8 | 第 2.3 节 TMP SDF 图集烘焙 | 依赖第 7 项 |
| 9 | 第 2.5 节的 Wolf RPG / KiriKiri | 格式未完全文档化，不确定性高 |

---

## 6. 怎么跟踪

- 各缺口的**代码位置与实现细节**见 [docs/ARCHITECTURE.md](ARCHITECTURE.md)；
- 字体相关缺口的**技术背景**见 [docs/FONTS.md](FONTS.md)；
- 翻译层的**调研数据与取舍依据**见 [本地翻译层技术调研.md](../本地翻译层技术调研.md)；
- 第三方代码与算法来源见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

本文档应随代码变更同步更新。**如果你发现某处描述与代码不符，以代码为准并修本文档。**
