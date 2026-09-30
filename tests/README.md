# 测试套件

## 怎么跑

```powershell
# 全量（约 3 分钟；字体合并那几组比较慢）
.\.venv\Scripts\python.exe -m pytest tests -q

# 单个套件
.\.venv\Scripts\python.exe -m pytest tests/test_font_real.py -q

# 每个套件也能独立当脚本跑，输出的是带实测数字的分节报告
.\.venv\Scripts\python.exe tests/test_font_real.py
```

## 为什么测试长得像脚本

每个 `test_*.py` 主体是一个 `main()`，内部用 `check()` 汇总断言并打印
**实测数据**（覆盖率百分比、耗时、具体缺哪些字），末尾既有
`test_suite()`（给 pytest）也有 `if __name__ == "__main__"`（给人）。

保留这种形态是刻意的：这个项目的核心承诺是"绝不出口口口"，
而验证它靠的是**数字**——"最好的单字体只有 98.33%"、
"合并后 ascender/upem 从 8.63 回到 0.93"。重写成纯 `assert`
会让这些证据从输出里消失，而它们才是判断"改对了没有"的依据。

## 各组覆盖什么

| 套件 | 断言数 | 覆盖 |
| --- | --- | --- |
| `test_placeholders.py` | — | 占位符屏蔽/还原：RPG Maker `\V[n]`、Ren'Py、富文本、printf、全角化、原文泄漏 |
| `test_json_parse.py` | 16 | 小模型输出的 JSON 恢复阶梯（围栏、废话、尾随逗号、中文引号、编号列表） |
| `test_translate_integration.py` | 19 | 假 Ollama 服务端下的翻译链路，含截断/占位符损坏/复读/抄记号 |
| `test_texture_service.py` | 17 | 贴图文字：检测→识别→翻译→重绘，非 ASCII 路径 I/O |
| `test_texture_pipeline.py` | 12 | 贴图整条流水线与产物落盘 |
| `test_textgroup_overlap.py` | 12 | **重叠文字块必须合并**（纯几何，不需要字体/GPU/模型，CI 里跑） |
| `test_texture_overlap.py` | 4 | 重叠块的**重读 + 整体重绘**，产物不得留下原始外文 |
| `test_font_service.py` | 25 | 字体审计、候选池、规划、注入 |
| `test_engine_rpgmaker.py` | 34 | RPG Maker MV/MZ 适配器：检测、抽取、回写、字体接线 |
| `test_engines_all.py` | 39 | 四个引擎适配器（RPG Maker / Ren'Py / Unity / 散装）的统一行为 |
| `test_font_wiring.py` | 26 | 字体接线：RPG Maker CSS `src` 改写、Ren'Py 生成 `novaloc_fonts.rpy` |
| `test_vlm_fallback.py` | 28 | 低置信度文字的视觉兜底（含"读到更差结果时不许替换"） |
| `test_pipeline_e2e.py` | 45 | 八阶段流水线端到端 + 质检 |
| `test_api_e2e.py` | 58 | 32 条 HTTP 路由、WebSocket 事件流、设置深合并与拒收未知键 |
| `test_font_real.py` | 21 | **用真实字体**验证字体合并：覆盖率、八个确定性不变量、UPEM 度量换算、渲染墨迹 |
| `test_charset_merge.py` | — | 字符集规划与合并 |
| `test_merge.py` | 5 | 底层 `merge_fonts` 多组合冒烟（依赖 Windows 自带字体） |

## 外部前提与跳过策略

有些测试需要本机资源，**缺失时 skip 而不是失败**——否则新克隆仓库的人
会看到一片红，却不知道只是没准备素材。

| 前提 | 影响的套件 | 怎么补 |
| --- | --- | --- |
| 中文字体 fixture | `test_font_service.py`、`test_charset_merge.py` | 把 OFL 中文字体（如 LXGW WenKai）放进 `tests/fixtures/fonts/` |
| **本机装有中文字体** | `test_api_e2e.py`、`test_pipeline_e2e.py`、`test_vlm_fallback.py`（标了 `needs_fonts`） | Windows/macOS 自带；Linux 装 `fonts-noto-cjk`。这几套要真的把中文渲染进贴图，没有字体就会 `RuntimeError: 找不到可用的中文字体` |
| Windows 系统字体 | `test_font_real.py`、`test_merge.py` | Windows 自带；其它平台跳过 |
| DirectML / ONNX Runtime | OCR 相关断言 | 装 `pip install -e ".[dml]"`；无 GPU 时退化到 CPU（慢约 84 倍） |
| PP-OCRv6 模型 | 贴图识别 | 首次运行自动下载到 `<data_root>/models/rapidocr` |
| Ollama | 只影响真机端到端；测试用假服务端 | 见 README 的 Ollama 一节 |

注意区分两种"要字体"：

* **要一个能用的字体把字画上去** → 用 `tests/_minimal_font.py` 自己造，
  不该依赖本机（`test_engine_rpgmaker.py` 就属于这种，它标 **不需要**
  `needs_fonts`）；
* **要真中文字体**（因为要画汉字、要跑 OCR 认中文）→ 标 `needs_fonts`。

`_minimal_font.py` 的字形轮廓是**从 Pillow 内置字体里抽出来重新组装**的
（Aileron，随 Pillow 分发），所以是真字母形状、OCR 能认，而仓库里
**不需要放任何字体文件**。

> ⚠️ 早先那版最小字体给每个字符都画同一个**方块**：PIL 能加载、能画上
> 墨迹、静态检查全绿，但 OCR 完全认不出来（`NEW GAME` 40px 时"墨迹"
> 占 88% 像素，就是一整块实心矩形），导致贴图汉化报告"0 处文字"。
> 这正是本项目最怕的失败模式：**看起来成功，其实什么都没做**。

## 一处"指标本身写错了"的教训

`test_texture_overlap.py` 要断言"产物上没有残留的原始外文"，但机器
**看不见图**，只能靠像素统计。第一版我写的是"原文字区域里亮像素占比
小于 5%" —— 听着合理，实际**正确产物也过不了**：译文自己的笔画就占
11%。这种阈值等于没测，而且方向是错的（它会惩罚正确的输出）。

改成"墨迹可解释度"：把译文用**产品同一个渲染器、同一套内边距**渲染出来，
取其 alpha 掩码对齐到原文字的包围盒，再看产物的亮像素有多少能落在
掩码内。落在掩码之外的越多，说明越可能有残留外文。

关键是**先量再定阈值**（`.scratch/_calib_leftover.py`）：

| 状态 | extra（无法由译文解释的墨迹占比） |
| --- | --- |
| 修复后 | 0.031 |
| 修复前（模拟） | 0.710 |

20 倍差距，阈值取 0.15 —— 两边都稳。**任何"目测像合理"的阈值，
在写下之前都该先量一遍它在正确/错误两种状态下分别是多少。**

**字体文件不入库**：`tests/fixtures/fonts/` 在 `.gitignore` 里。原因是体积
（两个 CJK TTF 就有 21 MB）和许可证（LXGW Neo XiHei 是 IPA-1.0，
不允许再分发）。详见 `docs/FONTS.md`。

## 测试之间的共享状态

各套件把自己写到 `tests/fixtures/<名字>/` 下（`out`、`tex_test`、
`pipe_test`、`fontsvc` 等），这些目录都在 `.gitignore` 里。

**合成游戏工程不再落在仓库里**。以前 `test_engine_rpgmaker.py` 会把
工程造到 `tests/fixtures/rpgmaker_game/`，其中 `data/` 被通用的
`data/` 规则忽略 —— 于是全新 clone 出来**只有那个套件跑过之后**才存在。
而 `test_api_e2e.py`、`test_pipeline_e2e.py`、`test_engines_all.py`
都直接读它，后果是：

* 单独跑其中任何一个都会失败（或更糟：像 `test_engines_all.py` 那样
  因为 `if not path.exists(): continue` 而**静默跳过整条检查**，
  看着是绿的）；
* 跑全量套件却永远通过 —— 因为顺序恰好对。

现在统一改成会话级临时目录：`conftest.py` 的 `_fake_game_dir` 建目录并把
路径放进 `NOVALOC_FAKE_GAME_DIR`，需要的套件通过共享构造函数
`tests/_fake_game.py` 自己造。**谁先跑都一样，单独跑也不会失败。**

自查方式：逐个文件单独跑一遍

```powershell
.\.venv\Scripts\python.exe .scratch\_run_each_alone.py
```

**已知局限**：少数套件会把产物留在固定目录并被后续断言读取
（例如合并出来的字体文件）。需要干净复现时先删掉
`tests/fixtures/out*`、`tests/fixtures/tex_test`、`tests/fixtures/pipe_test`
再跑。

