# NovaLoc 新译

> 把任意游戏翻译成中文 —— 包括**贴图里的文字** —— 并且保证游戏内**不出现口口口**。

NovaLoc 是一个**完全离线**的游戏汉化工具。它把三类文本都翻译成中文：

| 类型 | 举例 | 处理方式 |
|---|---|---|
| **结构化文本** | 对白、道具名、任务、界面标签 | 解析引擎的数据文件，原地改写 |
| **脚本文本** | Ren'Py 的 `.rpy`、Unity 的文本资源 | 按语法定位字符串字面量 |
| **贴图文本** | 标题画面、按钮、Logo、UI 底图上的字 | OCR 识别 → 去字 → **用真实中文字体重绘** |

---

## 为什么值得用

### 1. 贴图文字不靠生成模型"画"出来

很多同类工具用图像生成模型把英文"重画"成中文。**这条路不可靠**：
生成模型不保证笔画正确，也不需要真的存在这个字，
结果就是"看起来像中文但其实是错字"，而且尺寸、字重、描边每次都不一样。

NovaLoc 走的是另一条路：

```
OCR 识别出文字和它的四边形位置
   ↓
把原文字擦掉（inpaint：纯色区直接填充，复杂背景用 Telea/NS 算法修补）
   ↓
从原图采样前景色/背景色/描边，用**真实的中文字体文件**渲染译文
   ↓
按原文字的四边形做单应变换贴回（支持旋转、倾斜的文字）
```

因为是真实字体渲染，**笔画一定是对的**，而且颜色、描边、
倾斜角度都跟着原图走。代价是它不能"无中生有"地模仿特殊美术字，
但这是正确的取舍：**宁可风格略有差异，也不能画出错字。**

### 2. "不出乱码"是被硬保证的，不是"尽量"

游戏里出现 `口口口` 的根本原因是**字体缺字形**。
NovaLoc 把这个环节做成了一道**必须先通过的检查**：

1. 翻译完成后，先算出**这个项目实际会用到哪些字符**（汉字 + 标点 + 符号 + 原文里保留的字符）；
2. 逐个审计游戏自带字体，算出**具体缺哪几个字**；
3. 缺字就**注入字形**（把候选字体的对应字形合并进原字体，保留原字体的风格）；
4. 合并后再**验证一遍**：每个字符都要能在 cmap 里找到、能渲染出墨迹、
   不能是 `.notdef`、不能有重复 glyph id；
5. **只要有任何字符补不上，就硬失败并中止**，绝不输出一个会显示口口口的游戏。

最后一步尤其重要。多数工具会"尽力而为"然后交付，
用户装完游戏跑起来才发现某个界面全是方块。NovaLoc 的选择是：
**在能保证之前不交付。**

> 关于"为什么没有一个字体能搞定"
>
> 我们实测过常见字体对游戏文本的覆盖：SimHei 缺 `¶†‡•‹›↔↕♪♫✓✔✕✖❤`；
> 微软雅黑/等线/SimSun 同样缺 `↔↕♪♫✓✔✕✖❤`；`seguisym.ttf` 能覆盖 92.6% 的
> 符号但**一个汉字都没有**；霞鹜新晰黑汉字全覆盖却缺 `✔✕✖❤`。
> 所以**多字体贪心合并是必需的**，不是优化项。

### 3. 完全离线

翻译、OCR、图像处理全部在本机跑。没有 API Key，没有用量限制，
不会把你的游戏文本发到任何服务器。用 DirectML 走 GPU 加速。

实测性能（RTX 4060 Laptop，PP-OCRv6 medium）：

| 后端 | 720×300 一张图 |
|---|---|
| CPU | 23954 ms |
| **DirectML** | **284 ms** |

**84 倍差距**，所以 DirectML 是默认且必需的路径。

### 4. 原游戏目录永远只读

所有操作在独立的工作区里进行，产物写到 `out/`。
你不会因为工具跑了一半而毁掉原游戏。

---

## 支持的引擎

| 引擎 | 状态 | 覆盖内容 |
|---|---|---|
| **RPG Maker MV / MZ** | ✅ 完整 | `data/*.json` 全库（角色/道具/技能/敌人/状态/图块/系统术语）+ 地图事件指令（对白、选项、说话人名） |
| **Ren'Py** | ✅ 完整 | `.rpy` 对白、菜单选项、角色名、`config.name` 等 |
| **Unity** | ⚠️ 部分 | `StreamingAssets` 与 `*_Data` 下的明文 json/csv/txt/xml/po/ini。**序列化资源与 AssetBundle 不支持**（见下） |
| **散装文件** | ✅ 完整 | 一个文件夹里的图片或文本 —— 任何引擎的兜底方案 |

### 关于 Unity 的诚实说明

Unity 没有统一的文本存放位置，文本可能在 `.assets` 二进制、
AssetBundle，甚至编译进 `Assembly-CSharp.dll`。
**我们不会去盲写这些二进制格式** —— 盲写几乎必然破坏资源，
而且失败时表现为"游戏打不开"，用户根本查不出原因。

正确做法是：用 [UABEA](https://github.com/nesrak1/UABEA) 或
[AssetStudio](https://github.com/Perfare/AssetStudio) 把 TextAsset
**导出**成普通文件，然后用 NovaLoc 的**散装文件模式**处理。
NovaLoc 在扫描 Unity 工程时会明确告诉你检测到了多少个未处理的
序列化资源，并给出这个建议 —— 而不是假装全都翻好了。

#### Unity 的字体这道坎（必须知道）

Unity 的界面文字几乎都走 **TextMeshPro**，而 TMP 渲染用的是
**预先烘焙好的图集**（`m_AtlasTextures` 指向一张贴图，字形是那张贴图上的
位图块）。这意味着：

> **只替换 TTF 字体文件对已经烘焙好的 TMP 资源没有任何影响。**
> 游戏根本不会去读那个 TTF。

要真正让中文出现，必须**重新烘焙图集**，而那需要解析
`TMP_FontAsset` 的序列化格式、按原有字号重新光栅化字形、
重算 `m_GlyphTable` / `m_CharacterTable` / `m_FaceInfo`，
并且必须用 Unity 自身的排版度量才能和游戏完全一致。
**这是一个独立的大工程，本版本没有实现。**

所以 NovaLoc 在 Unity 上做的是"把能做的做到位 + 把做不到的说清楚"：

1. 把补好的字体复制到 `*_Data/StreamingAssets/_novaloc_fonts/`
   （`StreamingAssets` 会被原样打进构建，是运行时最可能被读到的位置）；
2. 检测工程里有没有 TMP 字体资源，有就明确告诉你**需要重新烘焙**，
   并给出用 Unity 编辑器 Font Asset Creator 的具体步骤；
3. 绝不假装已经修好 —— **虚假的成功比明确的失败更浪费时间。**

有一个真实存在的例外：如果游戏用的是**动态（Dynamic）** TMP 字体资源
（`m_AtlasPopulationMode` 为 `Dynamic`），它运行时会按需把字形加进图集，
此时替换 `StreamingAssets` 里的字体**是有效的**。NovaLoc 会提示你先试这一条。

RPG Maker 与 Ren'Py 没有这个问题 —— 它们直接读字体文件，
所以这两个引擎上的"不出现口口口"是**完全被保证的**。

---

## 安装

### 环境要求

- Windows 10/11（其他平台可用，但 DirectML 加速仅 Windows）
- Python 3.11+
- 建议 NVIDIA 显卡（走 DirectML）；CPU 也能跑，只是慢很多
- 磁盘空间：模型约 200 MB，工作区按游戏大小而定（建议放非系统盘）

### 步骤

**推荐：用一键脚本**（自动建虚拟环境、装依赖、跑自检）

```powershell
git clone https://github.com/starchfurrycon/NovaLoc.git novaloc
cd novaloc
.\scripts\setup.ps1
```

**手动安装：**

```powershell
git clone https://github.com/starchfurrycon/NovaLoc.git novaloc
cd novaloc

python -m venv .venv
.\.venv\Scripts\Activate.ps1

# dml = DirectML GPU 加速（快 84 倍）；ocr = PP-OCRv6 文字识别
# **两个都不能省**：只装 dml 的话能翻译文本，但贴图文字识别不了。
pip install -e ".[dml,ocr]"

novaloc doctor      # 检查环境，会告诉你缺什么
```

> 关于 `onnxruntime`：本项目**不在基础依赖里**声明它，因为它和
> `onnxruntime-directml` 提供同一个包名，一起装会让 DirectML
> 静默消失、速度退回 CPU（慢 84 倍）。所以必须用 `dml` / `gpu` / `cpu`
> 三个 extra **三选一**显式指定。
>
> 关于 `opencv`：`rapidocr` 会带入 `opencv-python`（带 GUI 的那个）。
> 代码里没有用到任何 GUI 函数，所以功能上没问题；如果你在无桌面环境
> 部署、想把 GUI 依赖也去掉，可以卸掉 `opencv-python` 后单独装
> `opencv-python-headless`（注意不要再让 rapidocr 把 GUI 版装回来）。

### 还需要准备的两样

上面只装了 **NovaLoc 自己**。要完整跑通还需要：

1. **Ollama + 翻译模型**（本地推理运行时）
   ```powershell
   novaloc ollama status     # 看是否已装/已启动
   novaloc ollama pull       # 拉取推荐模型（约 4 GB）
   ```
2. **OCR 模型**（PP-OCRv6，约 140 MB）—— 首次运行识别贴图时自动下载，
   也可以提前下好；`novaloc doctor` 会告诉你缺哪个、该放哪里。
   > 模型缺失时自检会**明确报不可用**，不会假装就绪然后联网下载。

```powershell
novaloc doctor      # 这一步应显示"一切就绪"
```

---

## 使用

### 图形界面（推荐）

```powershell
novaloc serve
```

浏览器会自动打开，默认地址是 **http://127.0.0.1:8791**
（即配置里的 `ui.host` / `ui.port`）。想换端口：

```powershell
novaloc serve --port 9000
```

> 注意：`scripts/dev.ps1`（前端开发模式）用的是 **8000** 端口，
> 因为 `web/vite.config.ts` 把 `/api` 与 `/ws` 代理到 8000。
> 日常使用不需要 Node，用上面的 8791 即可 —— 前端构建产物已随包提供。

### 命令行

```powershell
# 1. 看引擎识别结果
novaloc scan "D:\Games\SomeGame"

# 2. 建项目
novaloc project new --name "某游戏" --game "D:\Games\SomeGame"

# 3. 跑完整流程（识别→抽取→翻译→字体→贴图→质检→回写）
novaloc run <项目id>

# 4. 看质检报告
novaloc qa <项目id>
```

产物在 `novaloc project show <id>` 显示的 `out_dir` 里。
**直接玩 `out/` 目录**，不要覆盖原游戏。

### 分阶段跑（推荐大项目这样用）

```powershell
novaloc run <id> --stage extract
novaloc run <id> --stage translate     # 最耗时的一步
novaloc text export <id> --format csv --out 译文.csv   # 导出人工校对
novaloc text import <id> --file 译文.csv               # 改完导回
novaloc run <id> --stage fonts
novaloc run <id> --stage images_localize
novaloc run <id> --stage qa
novaloc run <id> --stage apply
```

每个阶段都是幂等的，可以反复重跑。改了译文只重跑 `fonts` 之后的步骤即可。

---

## 字体与授权

**默认不随工具分发任何字体文件。** 工具在本地渲染贴图、生成字体补丁，
交付的是**产物**而不是字体本身。

自动下载仅限可再分发的字体（`OFL-1.1` / `Apache-2.0` / `MIT` / 公有领域）。
系统字体（微软雅黑、黑体、宋体等）**只在本机已安装时使用，绝不自动下载**。

`novaloc fonts list` 会显示每个字体的授权与 `bundle_ok` 标记。

---

## 已知限制

- **贴图文字必须是"能识别的印刷体"**。手写体、极度装饰化的美术字
  OCR 会失败；这种情况下工具会保留原图并在报告里标出来，不会乱画。
- **复杂背景的去字依赖 LaMa**（可选，需要 torch）。没装 torch 时
  退化为纯色填充 / Telea 修补，在渐变或照片背景上会留痕。
- **Unity 序列化资源不支持**（见上文）。
- **只有 `.rpyc` 没有 `.rpy` 的 Ren'Py 发行版无法处理** —— 编译产物
  改了游戏也不认。工具会明确报告而不是假装成功。
- OCR 会把紧凑的英文标题切碎（例如 `NEW GAME` 可能被切成 `NEW` 和 `V GAME`）。
  译文长度与分块是按识别结果处理的，个别情况下需要人工在贴图审校页调整。

---

## 开发

```powershell
pip install -e ".[dev]"

# 全量测试（22 个套件 / 90 个 pytest 项，约 2.5 分钟）
pytest tests -q

# 单个套件也能直接当脚本跑，输出带实测数字的分节报告
python tests\test_font_real.py      # 用真实字体验证合并（覆盖率、UPEM 度量、渲染墨迹）
python tests\test_api_e2e.py        # 后端端到端（32 条路由 + WebSocket）

# 逐个文件单独跑一遍：防止套件之间出现「只能全量跑」的隐式耦合
python .scratch\_run_each_alone.py
```

有些套件需要本机资源（中文字体、DirectML、OCR 模型），**缺失时会 skip
而不是失败**。CI 只跑无需这些前提的子集，所以"CI 绿了"不等于"功能正确" ——
字体合并与贴图重绘只有在本机才能验证。细节见
[tests/README.md](tests/README.md)。

> 关于最后那条自查：本项目真的踩过这个坑 —— 三个端到端套件都读同一个
> 合成工程目录，而它里面 `data/` 是 git-ignored 的，**只有**另一个套件
> 跑过才会有。于是"单独跑必红、跑全量永远绿"，其中一处甚至因为
> `if not path.exists(): continue` 而**静默跳过**了整条断言。
> 现在合成工程造在会话级临时目录里，谁先跑都一样。

架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，
字体机制见 [docs/FONTS.md](docs/FONTS.md)，
排错见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

## 授权

本项目 MIT。第三方依赖与算法来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
