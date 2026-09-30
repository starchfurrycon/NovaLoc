# 排错手册

按**症状**组织。每条都给出：怎么确认、原因是什么、怎么修。

---

## 目录

- [1. DirectML 没有生效，OCR 慢得离谱](#1-directml-没有生效ocr-慢得离谱)
- [2. Ollama 连不上 / 模型缺失](#2-ollama-连不上--模型缺失)
- [3. 路径含中文导致图片读不出来](#3-路径含中文导致图片读不出来)
- [4. 游戏里仍然有口口口](#4-游戏里仍然有口口口)
- [5. 贴图文字没被识别](#5-贴图文字没被识别)
- [6. 去字之后留下痕迹](#6-去字之后留下痕迹)
- [7. Unity 的文本没抽到](#7-unity-的文本没抽到)
- [8. Ren'Py 只有 .rpyc](#8-renpy-只有-rpyc)
- [9. 翻译长度溢出与短标签问题](#9-翻译长度溢出与短标签问题)
- [10. 占位符 / 转义码被破坏](#10-占位符--转义码被破坏)
- [11. 可以忽略的日志噪音](#11-可以忽略的日志噪音)
- [12. 其他常见问题](#12-其他常见问题)

---

## 1. DirectML 没有生效，OCR 慢得离谱

### 症状

一张 720×300 的贴图要跑 20 秒以上；或者 `novaloc doctor` 里 DirectML 显示为
`⚠ 不可用（OCR 将退回 CPU，慢 40~140 倍）`。

### 先确认

```powershell
novaloc doctor
```

只看两行：

```
② 计算设备（ONNX Runtime）
  DirectML              ✅ 可用 / ⚠ 不可用
  可用执行提供器        DmlExecutionProvider, CPUExecutionProvider
  ONNX Runtime          <版本号>
```

`--json` 模式下对应 `gpu.directml` 与 `gpu.providers`：

```powershell
novaloc doctor --json | Select-String -Pattern 'directml|providers' -Context 0,3
```

### 原因与修法

**（a）最常见：`onnxruntime` 和 `onnxruntime-directml` 同时装了。**

这两个包**提供同一个 `onnxruntime` 包名**，会互相覆盖。
谁后装谁的 `__init__.py` 和 `capi` 就赢 —— 如果你先装 directml 再装普通的
`onnxruntime`，`DmlExecutionProvider` 就**静默消失**了，
没有任何报错，只是慢 84 倍。

确认：

```powershell
.\.venv\Scripts\python.exe -m pip list | Select-String onnxruntime
```

如果输出里同时出现 `onnxruntime` 与 `onnxruntime-directml`（或 `onnxruntime-gpu`），
就是这个问题。

修：

```powershell
# 先把三个都卸干净
.\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime onnxruntime-directml onnxruntime-gpu

# 再只装一个
.\.venv\Scripts\python.exe -m pip install "onnxruntime-directml>=1.18"

# 或者直接重装本包的 extra（推荐，会一并处理依赖）
.\.venv\Scripts\python.exe -m pip install -e ".[dml]" --force-reinstall onnxruntime-directml
```

验证：

```powershell
.\.venv\Scripts\python.exe -c "import onnxruntime as ort; print(ort.__version__); print(ort.get_available_providers())"
```

期望输出里能看到 `'DmlExecutionProvider'`。

> `scripts/setup.ps1` 在装完之后会主动检查这个冲突，发现就停下并打印修复命令。

**（b）配置里关掉了 DirectML。**

`OcrConfig.use_directml` 默认是 `True`，但如果被改成了 `false`，
就会走 CPU。检查 `<data_root>/config.json` 里的 `ocr.use_directml`。

临时覆盖（不改配置文件）：

```powershell
novaloc doctor --json   # 先看有没有生效
```

**（c）DirectML 版本与 ONNX Runtime 版本不匹配。**

`onnxruntime-directml` 对 Windows 版本和显卡驱动有要求。
DirectML 需要 Windows 10 1903+ 与较新的 GPU 驱动。
更新显卡驱动通常能解决。

**（d）机器上确实没有可用的 GPU。**

那 DirectML 就是不可用。老实装 CPU 版：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[cpu]"
```

代价是速度。实测（RTX 4060 Laptop，PP-OCRv6 medium，720×300 一张图）：

| 后端 | 耗时 |
|---|---|
| CPU | 23954 ms |
| **DirectML** | **284 ms** |

**84 倍差距**。CPU 路径能跑通，但贴图多的项目基本不可用。

### 为什么会被设计成这样

`pyproject.toml` 里有一段注释，说明了这个"不把 onnxruntime 放进基础依赖"的决定：

> 注意：`onnxruntime` **不在这里**。基础依赖里放 `onnxruntime` 会和
> `onnxruntime-directml` 冲突（两者提供同一个 `onnxruntime` 包名），
> 用户装完会发现 DirectML provider 消失、退回 CPU 慢 84 倍。
> 所以推理后端必须由用户通过 extra 显式选择：`dml` / `gpu` / `cpu` 三选一。

---

## 2. Ollama 连不上 / 模型缺失

### 症状

- 流水线在 `translate` 阶段报"没有可用的翻译适配器"；
- `novaloc doctor` 的"③ 本地推理服务（Ollama）"显示 `⚠ 未运行`；
- 翻译全批失败，`entries.jsonl` 里全是 `status: failed`。

### 先确认

```powershell
novaloc ollama status
```

输出四行关键信息：可执行文件路径、服务地址、服务状态、模型目录。
`--json` 可以拿到完整快照：

```powershell
novaloc ollama status --json
```

### 情况 A：没装 Ollama

`novaloc doctor` 会给出可直接粘贴的命令。用 winget 装（任选一条）：

```powershell
winget install Ollama.Ollama
winget install Ollama.Ollama.Portable
```

**装完务必把模型目录重定向到空间充足的分区。** 三个推荐模型加起来接近 8 GB，
不重定向的话 C 盘会被吃掉一大块：

```powershell
setx OLLAMA_MODELS "D:\NovaLoc\ollama-models"
```

`setx` 只对新开的终端生效，所以**必须重开一个终端**。
`novaloc ollama env` 会直接打印适合本机的环境变量：

```powershell
novaloc ollama env
```

它会输出类似：

```
setx OLLAMA_MODELS "D:\...\NovaLoc\ollama-models"
setx OLLAMA_MAX_LOADED_MODELS "1"
...
```

### 情况 B：装了但服务没起

Ollama 是个**后台服务 + CLI** 的组合。只装了 CLI 不会自动起服务。

```powershell
ollama serve
```

装了桌面版的话，直接启动 Ollama 应用也可以。

验证服务在线：

```powershell
ollama list
```

### 情况 C：服务在线但模型没拉

`novaloc doctor` 的"推荐模型"一栏会显示 `✅` / `⬜`。
缺失的模型用：

```powershell
novaloc ollama pull
```

它会拉取推荐模型并显示进度（NDJSON 流式输出，逐行解析）。
也可以指定单个模型：

```powershell
novaloc ollama pull translategemma:4b
```

配置里默认的三个模型（`OllamaConfig`）：

| 用途 | 默认值 | 说明 |
|---|---|---|
| `text_model` | `translategemma:4b` | 文本翻译主模型，约 3.3 GB |
| `vision_model` | `qwen3-vl:4b` | 贴图美术字兜底 |
| `embed_model` | `bge-m3` | 术语表向量检索 |

### 情况 D：显存不够，模型反复换入换出

症状是翻译一开始很快，然后突然卡住几十秒、再快一阵。

原因是 Ollama 默认允许同时驻留 `3 × GPU 数` 个模型。8 GB 显存上这会导致
模型被反复换入换出。相关配置项（`OllamaConfig`）：

| 配置 | 默认 | 为什么 |
|---|---|---|
| `max_loaded_models` | `1` | 建议固定为 1（或 2：翻译模型 + embed 模型） |
| `concurrency` | `1` | 8 GB 显存建议 1，跑大模型时别并发 |
| `kv_cache_type` | `q8_0` | f16 是 Ollama 默认；q8_0 能显著省显存且质量损失很小 |
| `keep_alive` | `10m` | 模型驻留时长 |

另外 **`repeat_penalty` 必须显式设置**。Ollama 的默认值是 `1.0`（等于关闭），
这是本地模型批量翻译时"复读到停不下来"的根因。本工具默认 `1.1`
（建议范围 1.05~1.15）。

### 情况 E：`OLLAMA_HOST` 覆盖了配置

`Config.resolved_ollama_host()` 的逻辑是：

```python
return os.environ.get("OLLAMA_HOST") or self.ollama.host
```

所以**环境变量优先于配置文件**。如果你设过 `OLLAMA_HOST` 但服务在别处，
就会出现"配置里写着 127.0.0.1:11434，但实际连的是别处"。

```powershell
$env:OLLAMA_HOST
```

清掉（当前会话）：`Remove-Item Env:OLLAMA_HOST`
永久清掉：`setx OLLAMA_HOST ""`（注意 `setx` 不能真正删除变量，需要用注册表编辑器，
或者干脆给它设成正确的地址）。

---

## 3. 路径含中文导致图片读不出来

### 症状

日志里出现：

```
无法读取图片：D:\小工具\某游戏\assets\title.png
```

而且**只有一行 WARN，贴图阶段就直接跳过了那张图**。

### 原因

**OpenCV 的 `imread` / `imwrite` 不支持非 ASCII 路径。**

在含中文（或任何非 ASCII 字符）的路径上：

- `cv2.imread()` 返回 `None`；
- `cv2.imwrite()` 返回 `False`；
- 除此之外**只打一行 WARN**，不抛异常。

这个失败模式特别难查，因为它不报错、不中断、只是"什么都没做"。
本工具的工作目录 `D:\personal tasks\小工具\翻译工具` 本身就带中文，
游戏安装目录也常常带中文（`D:\游戏\...`），所以这个问题**必然会发生**。

### 修法：本工具已经处理了

**所有图像 I/O 都走 `src/novaloc/images/io.py`**，就是这个原因。
`io.py` 的模块 docstring：

> 必须存在的原因：**OpenCV 的 `imread`/`imwrite` 不支持非 ASCII 路径。**
> 本工具的工作目录、游戏安装目录经常带中文……
> 直接用 `cv2.imwrite` 会静默失败（只打一行 WARN，返回 `False`），
> 排查起来非常费时间。所以统一走这里的包装函数：
>
> * 读：先用 `np.fromfile` 把字节读进来，再 `cv2.imdecode`；
> * 写：先 `cv2.imencode` 编码，再用 `Path.write_bytes` 落盘。

所以**如果你看到"无法读取图片"，那多半不是路径问题，而是文件真的不存在或已损坏**。
`imread_bgr()` 会先 `p.is_file()` 判断，文件不存在时打的是 debug 日志
（`文件不存在：...`），而不是 "读取图片失败"。

### 如果你在扩展本工具

**不要在任何地方直接调用 `cv2.imread` / `cv2.imwrite`。** 一律用：

```python
from ..images.io import imread_bgr, imread_rgba, imwrite_bgr, alpha_from

img = imread_bgr(path)      # 返回 BGR ndarray 或 None
imwrite_bgr(dst, image)     # 返回 bool
```

OCR 模块也遵守这个约束 —— `as_rgb_array()` 里有注释：

> 不能交给 RapidOCR 自己读路径：它内部用 `cv2.imread`，
> 而 OpenCV 不支持非 ASCII 路径。

如果你在第三方库里遇到这个问题（例如某个库自己读路径），
**先自己用 `imread_bgr` 读成 ndarray 再传给那个库。**

---

## 4. 游戏里仍然有口口口

### 第一步：看 QA 报告的 `font_missing`

```powershell
novaloc qa <项目id>
```

输出里有两行是重点：

```
字体缺字        0        ← 这就是 font_missing
项目字符集      3847
```

`--json` 模式下在 `stats.font_missing` 与 `stats.font_ok`：

```powershell
novaloc qa <项目id> --json
```

问题清单里字体相关的问题长这样：

```
[error]  fonts   字体 SimHei 缺少 4 个字符，游戏内会显示为口口口：✔✕✖❤
[error]  fonts   字体 fonts/gamefont.ttf 补丁失败：覆盖率 97.941% 低于要求的 99.900%
[error]  fonts   字体 fonts/gamefont.ttf 补丁后覆盖率仅 97.9%，仍有缺字风险
```

### 第二步：看每个字体具体缺什么

```powershell
novaloc fonts audit <项目id>
```

它会用**项目实际字符集**审计游戏自带字体，逐个打印覆盖率与缺字。
`--limit` 控制每个字体最多打印多少个缺字（默认 60）。

也可以直接看工作区文件：

```
<data_root>/workspaces/<项目id>/fonts/analysis.jsonl   ← 每个字体的审计结果
<data_root>/workspaces/<项目id>/fonts/patches.json     ← 补丁结果（含 before/after）
<data_root>/workspaces/<项目id>/fonts/charset.json     ← 项目字符集
```

`analysis.jsonl` 里每条 `FontCoverage` 的 `missing` 字段就是缺的字符列表。

### 第三步：按原因处理

**（a）缺的是常见符号（`✔✕✖❤↔♪` 之类）—— 补充字体候选不够。**

这是最常见的情况。原因是本机没有足够多的中文字体可供合并。
解决：装一个覆盖好的开源字体，或者让工具下载。

```powershell
novaloc fonts list              # 看目录里的字体与许可
novaloc fonts list -r           # 只看可自动下载/可再分发的
```

如果本机 hosts 屏蔽了 GitHub（本工具的 `FontService` 注释里提到过这个情况），
自动下载会失败。`FontService` 会缓存失败结果（`self._failed`），
所以不会反复重试刷屏，但也不会成功。

替代做法：**手动下载字体文件，放到工具会扫描的目录**：

```
<data_root>/fonts/cache/          ← 扫描顺序里的第二个位置
```

`FontService.ensure_font()` 的查找顺序是：
**已下载缓存 → 随包字体 → 系统已安装 → 联网下载**。

**（b）缺的是生僻字 —— 可能是真的没有字体能覆盖。**

如果 `still_missing` 里是几个极生僻的字（罕见人名用字、古籍用字），
那可能确实没有候选字体能提供。两条路：

- 在设置里增加字体来源（装一个更大的字体，如思源黑体全量版）；
- 那些字符所在的文本本来就应该进忽略列表（`check_language_residue` /
  `UntranslatedReason` 机制），或者接受这一处缺字。

**（c）字体补丁部分失败。**

`stage_fonts` 只要求"**需要补丁的字体**"全部失败才硬失败。
如果 3 个字体里 1 个成功 2 个失败，阶段会**通过**，但失败的那 2 个会
在 `patches.json` 里 `ok: false`，QA 阶段会报 ERROR。

处理：

```powershell
novaloc run <项目id> --stage fonts    # 重跑字体阶段（幂等）
```

如果反复失败，检查：

- 原字体是否被其它进程占用（游戏正在运行？）；
- 原字体是否是 CFF2 可变字体 —— `merge.py` 的 docstring 提到这种结构
  "怪异"，需要改用 `replace` 策略；
- 提高 `max_sources`（默认 4），让规划器用更多补充字体。

**（d）字体补丁成功了，但游戏里还是口口口。**

这时候问题不在字体文件本身，而在**引擎没有去用那份文件**。
`EngineAdapter.wire_fonts()` 的 docstring 解释了这个坑：

> 把补好的字体文件复制进 `fonts/` 并不会让引擎去用它 ——
> 每个引擎都有自己的"字体指向"配置。

检查：

| 引擎 | 去看什么 |
|---|---|
| RPG Maker | `fonts/gamefont.css` 的 `@font-face { src: url(...) }` 是否指向新字体，`fontFamily` 是否还是 `GameFont` |
| Ren'Py | 输出目录里是否生成了 `game/novaloc_fonts.rpy` |
| Unity | 见 [第 7 节](#7-unity-的文本没抽到) 与 `docs/FONTS.md` §8.3（TMP 走图集，拷 TTF 无效） |
| 散装文件 | 没有 `wire_fonts`，需要手工处理字体指向 |

**（e）某个界面口口口，但 QA 说字体没问题。**

那个界面用的字体**可能不在游戏的 `fonts/` 目录里** ——
比如引擎内置字体、或者被编译进二进制的字体。
`discover_fonts()` 只能发现文件形式的字体。

确认方法：检查 `fonts/analysis.jsonl` 里有几条记录。
如果只有 1 条，而游戏有多个界面字体，那就是漏了。

---

## 5. 贴图文字没被识别

### 症状

贴图上的文字明明存在，但 `images/localize.json` 里那张图 `blocks` 为空，
输出图与原图一致。

### 5.1 先提高 OCR 档位

`OcrConfig.model_tier` 有三个档位，默认 `medium`：

| 档位 | 能识别什么 | 实测耗时（1024×1024，DirectML） |
|---|---|---|
| `tiny` | 大号、高对比度的字 | ≈ 170 ms |
| `small` | 中等字号 | ≈ 410 ms |
| **`medium`（默认）** | 小字号、低对比度、密集文本 | ≈ 610 ms |

**`medium` 明显能找出比 `tiny` 更多的文字。** 用 `tiny` 换速度的代价是漏检。

改了档位要重跑贴图阶段：

```powershell
novaloc run <项目id> --stage images_scan
novaloc run <项目id> --stage images_localize
```

### 5.2 检查最小框尺寸

`OcrConfig.min_box_size` 默认 `6`。小于这个边长的检测框会被丢弃：

```python
min_box = int(getattr(self.cfg.ocr, "min_box_size", 6))
blocks = [b for b in blocks
          if (b.box[2] - b.box[0]) >= min_box and (b.box[3] - b.box[1]) >= min_box]
```

如果你的目标文字非常小（例如 5 px 高的像素字），把它调小。
代价是噪点会被当文字，误检变多。

`ImageConfig.min_box_size` 也是 `6`（贴图资产的过滤）。

### 5.3 检查跳过规则

`OcrConfig.skip_if_no_text_ratio` 默认 `0.0002`：
图中"疑似文字像素"占比低于这个值就**跳过推理**。
`ImageConfig.skip_patterns` 则按文件名跳过明显不含文字的贴图：

```
*_normal *_n *_roughness *_metallic *_ao *_height *_mask *_specular *_gloss
```

如果你的贴图正好叫 `button_n.png`，它会被跳过。改配置或重命名文件。

### 5.4 视觉大模型**绝不能**用来做检测

**这是本工具的一条硬性架构决定，不是偏好。**

实测数据（旋转文本检测的 Hmean）：

| 方案 | 旋转文本 Hmean |
|---|---|
| PP-OCRv6 small | **93.7** |
| PP-OCRv6 medium | **93.8** |
| Qwen3-VL-235B | **2.1** |
| GPT-5.5 | **10.0** |

**差一个数量级。** 视觉大模型擅长"看懂图里写了什么"，
但它**不输出稳定的文字位置框**。而贴图汉化必须知道每行字在哪里，
才能去字、重绘、贴回。

所以 `ocr_ppocrv6.py` 的 docstring 写得很直白：

> 在旋转文本检测上 PP-OCRv6 的 Hmean 是 93.7~93.8，而 Qwen3-VL-235B 只有 2.1、
> GPT-5.5 只有 10.0 —— 差一个数量级。所以**检测与识别一律走 PP-OCRv6**，
> 视觉模型只在极端情况（艺术字、严重变形、手写）做兜底。

> **注意**：这组数字不在 `本地翻译层技术调研.md` 里（那份报告讨论的是
> 文档解析方向的 OCR，建议的是 PaddleOCR PP-OCRv5 + qwen3-vl 兜底）。
> 这组对比来自本项目自己的验证记录。

**视觉兜底的实际作用范围**：
`OcrConfig.vlm_fallback = True`（默认开）与 `vlm_threshold = 0.6` 控制一条**只改文字、
不改框**的兜底路径，实现在 `images/service.py::_vlm_rescue` 与
`translate/vision_ollama.py`：

- 逐块检查 `confidence`，只挑出**低于阈值**的块；
- 按四边形做透视校正裁出**正立的**文字区域（`_crop_quad`），再交给 Ollama 的视觉模型重读；
- **只在 VLM 给出非空且归一化后不同的结果时**替换 `block.source`，
  读不出来或内容相同就保留 OCR 原结果（避免"兜底把好结果搞坏"）；
- **四边形坐标（`quad`）与 `box` 原样保留** —— 这是关键：VLM 的定位精度差两个数量级，
  一旦让它决定位置，排版就毁了；
- 被改过的块会加上 `vlm_reread` 警告，并计入 `TextureTranslator.vlm_reads` /
  `vlm_fixes` 两个计数器。

所以如果你看到"文字被改成了另一个（更对的）词"，那是兜底生效了，不是 OCR 出错。

**但兜底不改变本节的结论**：检测永远是 PP-OCRv6 做的。
如果 PP-OCRv6 **一个框都没检出来**，就没有低置信度的块可供兜底，VLM 也不会被调用。
预览里完全看不到框的时候，问题在检测，不在识别。

### 5.5 有些文字**本来就应该识别失败**

这是诚实的限制，不是 bug：

| 类型 | 为什么失败 |
|---|---|
| **手写体** | PP-OCRv6 训练数据以印刷体为主 |
| **极度装饰化的美术字** | 字形结构被破坏到无法匹配（例如整个字由火焰纹理组成） |
| **文字与背景对比度过低** | 检测器找不到文本区域 |
| **文字被严重遮挡/裁切** | 不完整的字无法识别 |
| **纯图形化 Logo** | 那已经不是一个"字"了 |

这种情况下工具的行为是：**保留原图并在报告里标出来，不会乱画**（README 的
"已知限制"一节）。`images/localize.json` 里那张图的 `changed` 会是 `false`。

`README.md` 还提到一个具体的例子：

> OCR 会把紧凑的英文标题切碎（例如 `NEW GAME` 可能被切成 `NEW` 和 `V GAME`）。
> 译文长度与分块是按识别结果处理的，个别情况下需要人工在贴图审校页调整。

### 5.6 检查 OCR 模型是否下载完整

```powershell
novaloc doctor
```

"④ OCR 模型（RapidOCR / PP-OCRv6）"会列出模型文件与大小。
如果显示"（未发现）"，说明模型还没下载。

模型位置：`<data_root>/models/rapidocr/`。
首次调用 OCR 时 RapidOCR 会自动下载，也可以参考 `novaloc doctor` 的提示手动放置。

如果 OCR 适配器 `available()` 返回不可用：

```
novaloc doctor --json    →  找 adapters 里 kind == "ocr" 的那条
```

它会告诉你 `未安装 rapidocr` 还是 `PP-OCRv6 初始化失败：...`。

---

## 6. 去字之后留下痕迹

### 症状

原文字被抹掉了，但那个位置留下：

- 一圈黑色/白色轮廓（残影）；
- 一块颜色不对的斑块；
- 在渐变或照片背景上出现糊掉的涂抹痕迹。

### 原因：inpaint 是分三档降级的

`images/inpaint.py` 的 docstring 说明了这个设计：

| 档位 | 触发条件 | 效果 | 依赖 |
|---|---|---|---|
| **1. 纯色填充** | 文字周围背景标准差 < `SOLID_STD_THRESHOLD = 12.0` | 最干净，快到可以忽略 | 无 |
| **2. OpenCV Telea / NS** | 背景有渐变或简单纹理 | 一般 | 无 |
| **3. LaMa** | 背景复杂（照片、手绘纹理） | 最好 | **需要 `torch`** |

而且它刻意**逐框判断**：

> 策略：逐框判断背景是否纯色 —— 纯色就单独填，其余区域统一交给 inpaint 算法。
> 这样一张图里"纯色按钮 + 渐变横幅"能各自用最合适的方法。

### 修法 A：装 torch，启用 LaMa

LaMa 是唯一能处理复杂背景的方案。它需要 `torch`：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[inpaint]"
```

这个 extra 会装 `torch>=2.4` 与 `scikit-image>=0.24`。

**但装 torch 只是第一步 —— 还需要模型文件。**
`TextureTranslator.lama` 属性找的是：

```
<data_root>/models/lama/lama.pt
```

找不到就 `log.info("未找到 LaMa 模型文件，跳过（将使用纯色/OpenCV 修补）")`
并返回 `None`。所以你必须自己把 LaMa 的 TorchScript 文件放到那个位置。

`LamaInpainter` 的实现在 `images/inpaint.py`，用的是 `torch.jit.load()`。
它会把输入尺寸对齐到 8 的倍数（LaMa 的 U-Net 要求）。

**LaMa 不可用时的日志**：

```
未安装 torch，跳过 LaMa（将使用纯色/OpenCV 修补）
未找到 LaMa 模型文件，跳过（将使用纯色/OpenCV 修补）
LaMa 加载失败：...
```

### 修法 B：没有 LaMa 时能做什么

**（a）加大掩膜外扩量。**

`ImageConfig.inpaint_dilate` 默认 `3`。残影多半是描边或抗锯齿边缘没被吃进去 ——
文字的抗锯齿边缘和描边通常比 OCR 框大 1~3 像素。加大到 `4`~`6` 试试。

注意 `inpaint.py` 里的注释：

> `manga-image-translator` 的 `mask_dilation_offset` 默认 30 是针对漫画气泡的
> 大场景，游戏 UI 用 3 左右就够。

**不要**盲目调到 30 —— 游戏按钮只有几十像素宽，外扩 30 px 会把整个按钮吃掉。

**（b）换 inpaint 算法。**

`ImageConfig.inpaint_engine` 可选 `lama | migan | opencv`。
`opencv` 走 Telea（`cv_method="telea"`），对渐变背景比 NS 好一些。
在 `inpaint_boxes()` 里对应：

```python
flag = cv2.INPAINT_NS if cv_method == "ns" else cv2.INPAINT_TELEA
radius = max(3, dilate + 2)
```

**（c）接受它。** 对于照片背景上的文字，没有 LaMa 就是会有痕迹。
这是 README 明确写出的已知限制：

> **复杂背景的去字依赖 LaMa**（可选，需要 torch）。没装 torch 时
> 退化为纯色填充 / Telea 修补，在渐变或照片背景上会留痕。

### 判断某张图实际用了什么方法

看 `images/localize.json` 里每条记录的 `inpaint_method` 字段：

```json
{ "path": "img/title.png", "ok": true, "inpaint_method": "solid+telea", ... }
```

可能的值：`solid` / `telea` / `ns` / `lama` / `solid+telea` / `none`。
混合值（`solid+telea`）表示一张图里两种方法都用上了。

---

## 7. Unity 的文本没抽到

### 症状

`novaloc scan` 正确识别出 Unity，但 `novaloc run <id> --stage extract` 报
"没有抽到任何文本"，或者抽到的条数远少于游戏里的文本量。

### 原因：Unity 没有统一的文本存放位置

文本可能在四个地方，本工具只支持其中一个：

| 位置 | 本工具支持 | 说明 |
|---|---|---|
| `StreamingAssets` 与 `*_Data` 下的明文 `json/csv/txt/xml/po/ini` | ✅ | 这是唯一被支持的路径 |
| **序列化资源**（`.assets` / `.unity3d`） | ❌ | 二进制序列化格式 |
| **AssetBundle** | ❌ | 打包资源 |
| **`Assembly-CSharp.dll` / IL2CPP** | ❌ | 编译进二进制 |

**为什么不做后面三个**：README 的"关于 Unity 的诚实说明"写得很清楚：

> 我们不会去盲写这些二进制格式 —— 盲写几乎必然破坏资源，而且失败时表现为
> "游戏打不开"，用户根本查不出原因。

### 正确做法：先导出，再用散装文件模式

1. 用 [UABEA](https://github.com/nesrak1/UABEA) 或
   [AssetStudio](https://github.com/Perfare/AssetStudio) 打开游戏资源；
2. 把 `TextAsset` **导出**成普通文件（`.txt` / `.bytes` / `.json`）；
3. 导出到一个**独立目录**（不要混在游戏目录里）；
4. 用 NovaLoc 的散装文件模式处理那个目录。

```powershell
novaloc scan "D:\Unity导出\TextAssets"
novaloc project new --name "某Unity游戏" --game "D:\Unity导出\TextAssets"
novaloc run <项目id>
```

### 工具其实会提示你

`UnityAdapter.detect()` 会检查后端类型并写进 evidence：

```
存在 Managed/（.NET 程序集）
Mono 后端（有 Assembly-CSharp.dll）
IL2CPP 后端（文本多编译进二进制，抽取能力有限）
```

`README.md` 也说明：

> NovaLoc 在扫描 Unity 工程时会明确告诉你检测到了多少个未处理的序列化资源，
> 并给出这个建议 —— 而不是假装全都翻好了。

### IL2CPP 的额外麻烦

IL2CPP 把 C# 编译成了原生代码。字符串字面量在 `global-metadata.dat` 里，
**修改它需要重新计算偏移**，而且同一长度的替换是唯一安全的做法。
这不是本工具的目标场景（见 `docs/ROADMAP.md`）。

---

## 8. Ren'Py 只有 .rpyc

### 症状

`novaloc scan` 识别出 Ren'Py，但置信度偏低，evidence 里有：

```
⚠️ 只有 .rpyc 没有 .rpy，无法安全修改文本
```

抽取阶段抽到 0 条文本。

### 原因

`.rpyc` 是 Ren'Py 的**编译产物**（Python 字节码 + 序列化的脚本数据）。
`.rpy` 才是源脚本。

一个只发布 `.rpyc` 的发行版意味着作者**刻意**不提供源脚本。
这种情况下：

1. **改了 `.rpyc` 游戏也不认** —— Ren'Py 有版本校验与字节码格式校验；
2. 即使暴力改成功，不同 Ren'Py 版本的 `.rpyc` 格式不一样，
   今天能改明天就崩；
3. 这是**作者明确的意图**，绕过它涉及法律与道德问题。

所以本工具的选择是：**明确报告而不是假装成功**。

README 的"已知限制"：

> **只有 `.rpyc` 没有 `.rpy` 的 Ren'Py 发行版无法处理** ——
> 编译产物改了游戏也不认。工具会明确报告而不是假装成功。

### 代码层面的表现

1. `RenPyAdapter.detect()` 里有一条专门的降权：

   ```python
   if not rpy and rpyc:
       info.confidence *= 0.5
       info.evidence.append("⚠️ 只有 .rpyc 没有 .rpy，无法安全修改文本")
   ```

   乘 0.5 是为了让它**在与其他适配器竞争时更容易输掉** ——
   如果这个目录同时像别的引擎，那别的引擎更可能是对的。

2. `extract_text()` 只扫 `*.rpy`：

   ```python
   files = sorted(p for p in game.rglob("*.rpy") if p.is_file())
   ```

3. 抽到 0 条文本时，`stage_extract` 返回失败：

   ```
   没有抽到任何文本。可能该引擎的文本存放方式尚未支持，
   请查看抽取报告中的说明。
   ```

### 你能做什么

- **联系作者要源脚本**，或找有 `.rpy` 的版本；
- 检查发行版里是否有 `game/` 之外的 `.rpy`（有些打包会把脚本放在别处）；
- 检查是否是 `.rpa` 归档 —— 如果是，先解包；
- **不要**尝试直接编辑 `.rpyc`。本工具不会帮你做这件事，也不会为它做适配。

---

## 9. 翻译长度溢出与短标签问题

### 9.1 一个反直觉的事实

**中文字符数比英文少约 50%，但渲染宽度几乎不变。**

原因：一个汉字是**全宽**的（1 em），一个拉丁字母平均只有 0.5 em 左右。
所以：

| 原文 | 字符数 | 大致渲染宽度 |
|---|---|---|
| `Options` | 7 | 7 × 0.5em ≈ 3.5em |
| `选项设置` | 4 | 4 × 1em = **4em** |

字符数从 7 降到 4（−43%），但宽度反而**变宽了**。

`render.py` 里 `render_text_block_checked()` 的 docstring 直接点出了这个陷阱：

> 为什么单独有这个函数：中文普遍比英文短（字符数少一半），
> 但**渲染宽度并不缩短** —— "OK" 变成"确定"宽度反而翻倍。

### 9.2 真正的风险是**短字符串**

这是最容易被忽视的一点：

| 原文 | 字符数 | 译文 | 字符数 | 宽度变化 |
|---|---|---|---|---|
| `OK` | 2 | `确定` | 2 | **2 → 2 em，翻倍** |
| `No` | 2 | `否` | 1 | 2 × 0.5em → 1em，变小 |
| `Cancel` | 6 | `取消` | 2 | 3em → 2em，变小 |
| `Continue` | 8 | `继续游戏` | 4 | 4em → 4em，基本不变 |

所以：**长文本通常变短，短文本经常翻倍。**

短标签（按钮、菜单项、表头）有**严格的宽度预算** ——
一个按钮的宽度是美术定的，不会因为中文变宽而变宽。
`OK → 确定` 这种翻倍就会顶出按钮边界，或者把旁边的元素挤走。

### 9.3 工具怎么检测

**（a）QA 阶段的长度检查**（`pipeline/qa.py`）：

```python
SHORT_KIND_MAX_RATIO = 2.2
SHORT_KINDS = {
    TextKind.UI_LABEL, TextKind.MENU, TextKind.ITEM_NAME,
    TextKind.CHARACTER_NAME, TextKind.MAP_NAME, TextKind.SKILL,
}

if kind in SHORT_KINDS and len(src) >= 4:
    ratio = len(target) / max(1, len(src))
    if ratio > SHORT_KIND_MAX_RATIO:
        issues.append(_issue(Severity.WARN, "length",
            f"短标签译文偏长（{len(target)}/{len(src)} 字符）：{src[:30]} → {target[:30]}"))
```

注意 `SHORT_KIND_MAX_RATIO = 2.2` 与 `TranslateConfig.max_chars_ratio = 2.2` 是一致的。

**（b）贴图阶段的逐块溢出检测**（`images/render.py`）：

```python
fitted = fit_font_size(text, w, h, font_path, ...)
fw, fh = _measure(fitted)
overflow = bool(fw > w or fh > h)
```

`BlockRender` 有两个属性：

```python
@property
def too_small(self) -> bool:
    return 0 < self.font_size < self.min_readable_size   # 默认 9

@property
def needs_review(self) -> bool:
    return self.overflow or self.too_small
```

溢出的块会进 `TextureResult.needs_review`，
写进 `images/localize.json` 的 `blocks[].overflow`，
QA 报告里统计成 `images_review`：

```
[info] images  3 处贴图文字需要人工复核（过长/过小/有警告）
```

**（c）字号自动收缩 —— 但有下限。**

`fit_font_size()` 用二分找"能塞进框的最大字号"，并且有一个经验下限：

```python
# manga-image-translator 的经验下限，避免极窄区域算出 0 号字
floor = max(min_size, int((box_h + box_w) / 200))
```

**为什么要下限**：如果允许无限缩小，一个特别窄的区域会算出 0 号字或 1 号字，
渲染出来是一团黑点 —— 那比溢出更糟，因为它看起来像渲染 bug。
所以宁可溢出并明确报告。

代码里的判断很直白：

> **`overflow` 的语义**：字号已缩到下限仍塞不下。应**标出来交给用户决定**，
> 而不是硬画 —— 溢出到按钮外的中文比不翻译更糟。

### 9.4 你能做什么

1. **看 QA 报告**，筛出 `[length]` 和 `[images]` 的问题；
2. **导出译文人工编辑**：

   ```powershell
   novaloc text export <项目id> --format csv --out 译文.csv
   # 改完之后
   novaloc text import <项目id> --file 译文.csv
   ```

3. **改短**。短的 UI 标签优先用简短译法（`Options` → `选项` 而不是 `选项设置`）；
4. **调整贴图重绘**：在贴图审校页（`GET /api/projects/{pid}/images/{uid}/annotated`
   提供了带标注的可视化图）看到溢出后，手工调整译文；
5. **放宽 `max_chars_ratio`**（如果你确认那个界面对宽度不敏感）。

---

## 10. 占位符 / 转义码被破坏

### 10.1 为什么这是最危险的一类问题

游戏文本里混着大量**不是自然语言**的记号：

| 引擎/格式 | 记号 |
|---|---|
| RPG Maker MV/MZ | `\V[1]` `\N[2]` `\C[3]` `\I[5]` `\{` `\}` `\G` `\.` `\|` |
| Ren'Py | `{color=#fff}{/color}` `[variable]` `{w}` `{nw}` `{size=+2}` |
| 富文本 | `<color=#FF0000>` `</size>` `<b>` |
| 格式化 | `%s` `%1$s` `{0}` `{name}` `${var}` |
| 其它 | `&nbsp;` `\n` `\t` |

把它们原样发给模型，它**一定会**偶尔改坏：

```
%s        →  % s
{0}       →  { 0 }
\V[1]     →  V[1]        （少个反斜杠）
<color=#fff>  →  < color = #fff >
```

后果是**游戏运行时崩溃或显示错字**，而且这种 bug 极难定位 ——
因为译文本身读起来完全通顺。

### 10.2 解决办法：屏蔽成 `⟦i⟧`

`translate/placeholders.py` 的模块 docstring：

> 翻译前把每个占位符换成一个**稀有 Unicode 记号**（形如 `⟦0⟧`，
> U+27E6/U+27E7 数学白方括号，自然文本里几乎不会出现），
> 翻译后再按索引还原。模型看到的就是一串"普通词"，无处可改。

**为什么选数学白方括号**（代码注释）：

> 1. 出现在正常游戏文本里的概率极低；
> 2. 大多数 BPE 词表会把它切成独立 token，不容易被模型拆开或吞掉。

这个策略在 `TranslateConfig.mask_placeholders = True` 下默认启用，
配置说明写得很直接：

> 这是占位符保护最强的手段：模型看不到原始语法，就无从破坏它，
> **攻击面直接归零**。翻译回来后再按索引逆映射。

### 10.3 三层校验

`verify_restored()` 做三层检查，docstring 说"缺一不可"：

| 层 | 检查内容 | 抓什么 |
|---|---|---|
| 1 | **多重集**（`Counter`） | 数量与内容必须一致 —— `\V[1]` 出现两次就必须还原两次 |
| 2 | **剩余记号** | 还原不掉的自造记号（模型编了个 `⟦9⟧` 出来） |
| 3 | **出现顺序** | `⟦0⟧ ⟦1⟧` 不能被写成 `⟦1⟧ ⟦0⟧` |

**第 3 层是最容易漏的，也是最危险的。** 代码注释原文：

> 成对标签（`<color>` / `</color>`）一旦被模型交换，还原后会得到
> `</color>警告！<color=#f00>` —— **多重集完全正确、文本看着也通顺，
> 但游戏渲染必然出错**。这类"沉默的损坏"比丢字更危险。

这就是为什么**条目顺序很重要**。举个具体的例子：

```
原文:   <color=#f00>警告</color> 
屏蔽后: ⟦0⟧警告⟦1⟧
slots:  ["<color=#f00>", "</color>"]
```

如果模型返回 `⟦1⟧警告⟦0⟧`，还原后是 `</color>警告<color=#f00>`：

- 多重集检查：**通过**（两个记号都在，数量对）；
- 剩余记号检查：**通过**（没有残留）；
- 文本可读性：**通过**（读起来就是"警告"两个字）；
- 只有**顺序检查**能抓到它。

而游戏渲染这句话时会**先关一个没开的标签**，行为未定义 ——
在 Ren'Py 里可能是引擎警告，在 RPG Maker 里可能是整句不显示。

**为什么用 `Counter` 而不是 `set`**（`compare_placeholders` 的注释）：

> `\V[1]` 出现两次就必须还原出两次，少一次同样是 bug。

### 10.4 批量翻译的特殊处理

`mask_batch()` 有一个**反直觉的设计**：每条文本**各自从 `⟦0⟧` 开始编号**，
而不是全批共用一个索引空间。

注释解释了为什么（这个坑很微妙）：

> 为什么不用"全批共用一个索引空间"（看起来更严格）：
> 模型是**逐条**返回的，每条译文的记号只能对着**那一条自己的** slots 还原。
> 若全批共用索引，第 2 条的记号是 `⟦2⟧` `⟦3⟧`，
> 而还原时拿到的 slots 列表只有 2 个元素，索引直接越界 ——
> **好译文会被误判成"占位符被破坏"**。所以必须逐条局部编号。

局部编号的代价是"跨条抄写记号"抓不到了（每条都是 `⟦0⟧` 起步）。
所以有一个专门的补偿检查 `_detect_cross_item_leak()`：

> 某条译文里出现了**超出该条自身槽位数**的记号下标，就是抄错了。

### 10.5 QA 报了 `placeholder_lost` 怎么办

QA 报告里会是这样（`pipeline/qa.py`）：

```
[error] placeholder  占位符校验未通过（回写会破坏游戏文本）：<原文前40字>
```

**回写阶段会拒绝这条**（`stage_apply`）：

```python
if not e.placeholder_ok:
    risky.append(u.uid)
    continue          # 不回写
```

日志：

```
N 条译文占位符校验未通过，已**拒绝回写**（避免游戏内文本错乱）。
请在文本审校页修正后重试。
```

也就是说：**坏的那几条不会进游戏**，游戏里会显示原文。这是安全的默认行为。

处理步骤：

1. 找到那几条：

   ```powershell
   novaloc qa <项目id> --json
   ```

   在 `issues` 里找 `stage == "placeholder"` 的项，看 `uid` 与 `warnings`。

2. 导出译文，手工修好占位符：

   ```powershell
   novaloc text export <项目id> --format csv --out 译文.csv
   ```

3. 导回：

   ```powershell
   novaloc text import <项目id> --file 译文.csv
   ```

4. 重跑 `qa` 与 `apply`：

   ```powershell
   novaloc run <项目id> --stage qa
   novaloc run <项目id> --stage apply
   ```

### 10.6 如果占位符问题**频繁**出现

按可能性排序：

**（a）模型太小或提示词不对。** 换一个更大的 `text_model`，
或者检查 `translate/prompts.py` 里有没有把占位符说明塞进系统提示。

**（b）`mask_placeholders` 被关掉了。** 检查配置。关掉之后模型直接看到原始语法，
破坏率会显著上升。**除非在调试，否则不要关。**

**（c）新增的占位符形态没被识别。** `PLACEHOLDER_PATTERNS` 里有一长串正则，
每条都有注释说明它为什么那样写。例如：

```python
# 单个反斜杠命令：\G \. \| \! \> \< \^ \{ \} \\
# 注意**不能**写成 \\[G.$|!><^{}] —— 字符类里的 `.` 会匹配任意字符，
# 结果把 `\n` 也吃掉。`\n` 要留给下面专门的"转义换行"规则处理。
```

```python
# RPG Maker 插件元数据标签：`<CustomEffect:heal:500>` `<PassiveSkill:5>`
# 这类标签的**内部**是插件读取的参数，翻译了插件就认不出来，
# 游戏行为直接出问题。上面的 HTML 规则匹配不到它（标签名后面
# 跟的是 `:` 而不是 `>`），所以必须单独一条。
```

如果你的游戏有一种新的记号形态（某个插件自定义的），
把它加进 `PLACEHOLDER_PATTERNS`，并加一条针对它的测试。

**（d）注意 `_NEVER_MASK` 与重叠消解的顺序。** `_placeholder_spans()` 的注释：

> 1. **必须先把 `_NEVER_MASK` 里的候选剔除，再做重叠消解。**
>    否则 `100% complete` 这类文本里，被排除的候选会挡住真正该保护的匹配。
> 2. `%%` 是转义后的百分号，不是格式说明符，必须整体忽略；
>    否则单个 `%` 规则会把它拆开。

修改这一块时要小心：**误屏蔽正常文本**的后果是译文被切碎
（"100% complete" 变成三段），比漏保护更难发现。

---

## 11. 可以忽略的日志噪音

以下日志**都是正常的**。它们看着吓人，但都不表示出错。

### 11.1 字体合并时的表丢弃

```
LTSH NOT subset; don't know how to subset; dropped
PCLT NOT subset; don't know how to subset; dropped
meta NOT subset; don't know how to subset; dropped
```

**原因**：`fontTools.subset` 在遇到它不知道如何子集化的表时会打这条日志，
然后丢弃那张表。`merge.py` 的 `_DROP_ALWAYS` 里**明确列了这三张表要丢**：

| 表 | 是什么 | 丢掉有影响吗 |
|---|---|---|
| `LTSH` | 线性阈值表（Linear Threshold） | 只对老式 GDI 灰度渲染有影响，现代渲染器忽略 |
| `PCLT` | PCL 5 打印机度量表 | 打印机时代的产物，与屏幕渲染无关 |
| `meta` | 元数据表 | 与字形渲染无关 |

同一批被丢的还有 `DSIG`（签名，合并后必然失效）、`GSUB`/`GPOS`（Merger 不支持
合并布局表）以及一批位图/彩色字形表（详见 `docs/FONTS.md` §4.4）。

**怎么确认这确实是预期的**：看 `merge.py` 的 `_DROP_ALWAYS` 列表。
只要表名在里面，这条日志就是预期的。

### 11.2 时间戳警告

```
'created' timestamp seems very low; interpreting as seconds since 1970
```

**原因**：某些字体（尤其是从 `.ttc` 提取出来的 face，或经过某些工具处理的字体）
的 `head.created` 字段是一个不合理的值。fontTools 会猜测它是不是"从 1970 起的秒数"。

**影响**：无。时间戳不参与渲染。
`subset_font()` 里 `opts.recalc_timestamp = False` 就是**故意不重算时间戳** ——
避免每次都把字体标记成"刚改过"，影响增量构建缓存。

### 11.3 Windows 的 `mstmc.ttf` 不是字体

```
无法读取字体 C:\Windows\Fonts\mstmc.ttf：Not a TrueType or OpenType font (bad sfntVersion)
```

**原因**：`C:\Windows\Fonts\mstmc.ttf` 虽然扩展名是 `.ttf`，但它**不是字体文件**
（它是 Windows 的"Microsoft 系统字体度量"数据文件）。
`coverage.py` 的 `scan_font_files()` 按扩展名扫描，会扫到它。

**处理**：`load_font_info()` 捕获 `TTLibError` 后返回 `None` 并记一条 warning，
**不会中断流水线**：

```python
except (TTLibError, OSError, ValueError) as exc:
    log.warning("无法读取字体 %s：%s", path, exc)
    return None
```

**你想让它闭嘴的话**：它只是 warning，不影响任何结果。忽略即可。

### 11.4 RapidOCR 的空检测结果

```
[ WARNING] [RapidOCR] main.py:132: The text detection result is empty
```

**原因**：那张图里确实**没有检测到文字**。

**这不是错误**，`TextureTranslator.process()` 对这种情况有明确处理：

```python
if not blocks:
    # 没文字，原图返回，不算失败
    result.image = raw
    result.total_ms = (time.time() - t_start) * 1000
    return result
```

原图会被原样返回，图不会被标记为 `changed`。
一张纯背景（如 `bg_sky.png`）触发这条日志是完全正常的。

### 11.5 其他常见但正常的信息

| 日志 | 含义 |
|---|---|
| `未安装 torch，跳过 LaMa（将使用纯色/OpenCV 修补）` | 预期行为，见第 6 节 |
| `未找到 LaMa 模型文件，跳过（将使用纯色/OpenCV 修补）` | 同上 |
| `视觉兜底不可用，跳过：...` | info 级。`vlm_fallback` 开着但 Ollama 没起 / 没装视觉模型，于是只用 OCR 结果。不影响流程，见第 5.4 节 |
| `视觉兜底初始化失败，跳过：...` | 同上，只是原因是异常而不是 `available()` 返回不可用 |
| `有 N/M 块置信度低于 0.60，尝试视觉兜底` | debug 级。说明确实触发了兜底重读，不是错误 |
| `字体 X 不在本机且不允许下载` | debug 级，候选字体不可用 |
| `字体 X 下载失败（本次运行不再重试）：...` | GitHub 被屏蔽时的正常表现，已缓存失败避免刷屏 |
| `OCR 语言提示 zh（当前模型已支持中英混排，忽略）` | debug 级，PP-OCRv6 不需要按语言换模型 |
| `基础字体已完整覆盖所需字符，未做合并（已导出为独立 TTF）` | 无需补丁，仍产出一份文件保证路径恒定存在 |
| `游戏没有自带可替换的字体文件。引擎将回退到系统/内置字体` | 见第 4 节情况 (e) |
| `X 已全覆盖，跳过` | 该字体不需要补丁 |
| `图像过大，缩放至 X% 再识别` | debug 级，`max_side` 生效 |

### 11.6 怎么提高日志详细度

看更多细节：

- **OCR 详细日志**：`OcrConfig.verbose = True`（会把 RapidOCR 的日志级别从
  `warning` 提到 `info`）。
- **日志级别**：`novaloc` 的全局 `log_level` 配置，或环境变量
  `NOVALOC_LOG_LEVEL`（映射到 `Config.log_level`）。
- **单张图的 OCR 耗时**：`images/localize.json` 里每条记录有 `ocr_ms` 与 `total_ms`。

---

## 12. 其他常见问题

### 12.1 项目建不了 —— "游戏目录不存在"或"不是目录"

`Workspace.create()` 会检查目标路径。注意 **要选游戏根目录**，
即**包含** `data/`（RPG Maker）或 `game/`（Ren'Py）的那一层，而不是它们本身。

```powershell
novaloc scan "D:\Games\SomeGame"     # 先确认识别正确
```

### 12.2 数据目录在 C 盘，把系统盘塞满了

`core/paths.py` 的设计是通过 `_pick_big_disk_base()` **自动挑可用空间最大的分区**
（Windows 上遍历 `D:`~`H:`）。但如果你显式设过 `NOVALOC_DATA_ROOT`，
或者只有 C 盘，就会落在 C 盘。

```powershell
# 看当前的数据根目录
novaloc doctor --json | Select-String data_root

# 改到 D 盘（永久，需要重开终端）
setx NOVALOC_DATA_ROOT "D:\NovaLoc"
```

注意：`data_root` 是**启动时解析**的，改环境变量后要重开终端。

### 12.3 流水线跑到一半断了，要重头来吗

**不用。** `pipeline/stages.py` 的设计原则之一就是：

> **阶段之间只通过工作区文件交换数据**，不靠内存里的对象。
> 这样进程被杀掉（大型游戏汉化动辄几小时）也不会前功尽弃。

继续跑同一条命令即可 —— 每个阶段都是幂等的：

```powershell
novaloc run <项目id>
```

`stage_translate(only_pending=True)` 会跳过已有译文的条目。
`stage_fonts` 会跳过已全覆盖的字体。

### 12.4 我改了译文，要全部重跑吗

不用。字体补丁依赖译文用到的字符集，所以**从 `fonts` 开始往后跑**：

```powershell
novaloc run <项目id> --stage fonts
novaloc run <项目id> --stage images_localize
novaloc run <项目id> --stage qa
novaloc run <项目id> --stage apply
```

README 里也给了这个建议。

### 12.5 手工改的译文被流水线覆盖了

不应该发生。`stage_translate` 合并时用的是：

```python
# 已有条目要合并保留：用户手工改过的译文不能被流水线覆盖
merged = {**existing, **{e.uid: e for e in entries}}
```

`existing` 在后面，所以**已有条目获胜**。而且如果某个条目已有译文，
`only_pending=True` 时它根本不会被送去翻译。

如果你希望某个条目**永不**被覆盖，把它的 status 设为 `locked`
（`EntryStatus.LOCKED` 的注释："用户手工锁定，重跑不覆盖"）。

### 12.6 术语不统一

QA 会检测这个：

```
[warn] consistency  12 条原文存在多种译法（术语可能不统一）
```

处理：建立术语表。

```powershell
novaloc text export <项目id> --format csv --out 译文.csv   # 里面含 glossary 的列
```

术语表文件位置：`<data_root>/workspaces/<项目id>/translations/glossary.jsonl`。
`GlossaryEntry` 的字段：`source` / `target` / `case_sensitive` / `whole_word` /
`note` / `category`。

术语表在提示词里会被**强制注入**（`prompts.py::build_glossary_block`）。

### 12.7 提交了前端构建产物是不是错了

**不是。** 这是有意的：前端构建产物随包提交，
这样工具**不需要 Node 就能运行**。

产物路径是 **`src/novaloc/web_dist/`**，也就是**放在 Python 包内部**。
这一点是踩过坑才定下来的：早先放在仓库根的 `web/dist`，而
`pyproject.toml` 里写的是

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/novaloc"]
```

hatchling 只收 `src/novaloc`，于是 **wheel 里根本没有前端**。
GitHub 自动生成的 wheel 不会因此报错，`pip install nova-loc`
装完接口全都正常、只有根路径 404 —— 典型的"静默残废"。
放进包内后，`pip install` 与 `pip install -e .` 用的是同一批产物。

#### 关于 `.gitignore` 的一个陷阱

网上常见的写法是"先忽略所有 `dist/`、再用 `!` 放行前端"，**那是无效的**：

```
**/dist/          # 忽略任意层级的 dist
!web/dist/        # ← 完全不起作用
!web/dist/**      # ← 也不起作用
```

**Git 不会进入被忽略的目录**，所以目录一旦被忽略，内部的 `!` 例外
再怎么写都无效，而 `git status` 也不报错 —— 前端产物会静默地不被提交。

本仓库的做法是**只忽略仓库根的 `dist/`**（模式 `dist/` 不递归），
而前端产物在 `src/novaloc/web_dist/`，路径里根本没有 `dist` 段，
所以既不需要任何 `!` 例外，也不会被误忽略。
**不要把 `web_dist/` 加进忽略列表。**

### 12.8 同时跑两个项目会不会更快

不会，而且更慢。

`JobManager` 默认 `max_workers=1`，理由写在代码里：

> 默认单 worker：OCR 与模型推理争抢同一块 GPU，
> 并行跑多个任务只会互相拖慢，还容易把显存打爆。

### 12.9 `novaloc serve` 的端口是多少

默认是配置里的 `ui.port`，即 **8791**（`UIConfig.port = 8791`，`host = "127.0.0.1"`）。

```powershell
novaloc serve                      # 用配置里的端口
novaloc serve --port 8000          # 指定端口
novaloc serve --port 8000 --reload # 开发模式
```

`README.md` 里"图形界面（推荐）"一节写的是 `http://127.0.0.1:8000` ——
这是**前端 dev server 的约定端口**（`web/vite.config.ts` 把 `/api` 与 `/ws`
代理到 `http://127.0.0.1:8000`），与 CLI 默认值不同。
用 `scripts/dev.ps1` 时会让后端监听 8000 以配合 Vite。

### 12.10 怎么完全卸载

- 删掉工作区：`novaloc project delete <项目id>`；
- 删掉数据目录（模型、字体、所有工作区）：`<data_root>` 整个删掉；
- 删掉虚拟环境：`.venv/`；
- 卸载 Ollama：`winget uninstall Ollama.Ollama`（如果想连模型一起清，
  再删掉 `OLLAMA_MODELS` 指向的目录）。

原始游戏目录**从来没有被修改过**，所以不需要"还原"步骤。
