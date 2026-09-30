# 架构说明

本文档描述 NovaLoc 新译的实际代码结构。所有内容以 `src/novaloc/` 下的源码为准，
模块名与函数名都可在仓库里直接对照。

---

## 1. 分层结构

```
┌──────────────────────────────────────────────────────────────────────┐
│  入口层                                                              │
│    cli.py                Typer CLI：doctor/scan/project/text/        │
│                          fonts/ollama/qa/run/serve                   │
│    api/app.py            FastAPI：REST + WebSocket + 静态前端         │
│    api/jobs.py           内存任务表 + 线程池 + 事件环形缓冲            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 组装 Context / Providers
┌───────────────────────────────▼──────────────────────────────────────┐
│  编排层                                                              │
│    pipeline/stages.py    8 个阶段、断点续跑、EventBus 广播            │
│    pipeline/qa.py        回写前的确定性缺陷检查                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  领域层（互不依赖，只依赖 core）                                       │
│    engines/     rpgmaker / renpy / unity / loose —— 读写某个引擎       │
│    translate/   ollama 提供者、占位符屏蔽、术语表、翻译记忆、QC 守卫     │
│    fonts/       字符集规划、覆盖审计、多源合并、字体 QA、下载           │
│    images/      OCR、分组、取色、inpaint、真实字体重绘、贴回            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  核心层  core/                                                       │
│    paths.py      数据根目录 / 工作区 / 模型 / 字体 / 日志的路径解析     │
│    config.py     Config（pydantic）+ TOML 持久化 + 环境变量覆盖        │
│    workspace.py  项目目录布局与 JSONL 读写（原子写）                   │
│    events.py     EventBus / Event / ProgressThrottle                 │
│    registry.py   register(kind, name) / Context / Providers / 协议     │
│  models.py       全部 pydantic 数据模型（TextUnit / ImageAsset / …）   │
└──────────────────────────────────────────────────────────────────────┘
```

**依赖方向是单向的**：`core` 不 import 任何领域层，领域层不 import `pipeline`，
`pipeline` 不 import `cli` / `api`。这条规则的实际好处是可以在没有 FastAPI、
没有 GPU、没有网络的情况下单独 import 并测试 `fonts/` 或 `translate/`。

唯一的例外是 `engines/__init__.py` 会 import 四个具体适配器（导入即注册），
这是有意的：识别阶段需要一个确定的适配器集合。

---

## 2. 八个流水线阶段

阶段常量定义在 `src/novaloc/pipeline/stages.py` 的 `STAGES`，顺序即执行顺序：

| # | id | 中文名 | 做什么 | 产物 |
|---|---|---|---|---|
| 1 | `detect` | 识别引擎 | 逐个适配器探测，取置信度最高者 | `project.json` 的 `engine` / `engine_version` |
| 2 | `extract` | 抽取文本 | 按引擎格式抽取文本单元 | `extracted/units.jsonl`、`extracted/report.json` |
| 3 | `images_scan` | 扫描贴图 | 找出候选贴图资产并按路径去重 | `extracted/images.jsonl` |
| 4 | `translate` | 翻译 | 按 `kind` 分批送翻译提供者，逐批落盘 | `translations/entries.jsonl` |
| 5 | `fonts` | 字体适配 | 算字符集 → 审计原字体 → 多源合并 → QA | `fonts/charset.json`、`fonts/analysis.jsonl`、`fonts/patches.json` |
| 6 | `images_localize` | 贴图汉化 | OCR → 分组 → 翻译 → 去字 → 真实字体重绘 → 单应贴回 | `images/rebuilt/**`、`images/localize.json` |
| 7 | `qa` | 质检 | 占位符 / 漏译 / 长度 / 术语一致性 / 字体缺字 / 贴图问题 | `qa/report.json` |
| 8 | `apply` | 回写产物 | 复制游戏目录到 `out/` 并写入译文、贴图、字体补丁 | `out/**` |

### 2.1 顺序为什么是这样

**`extract` 必须在 `translate` 之前。** 翻译需要稳定的 `uid` 作为索引；`uid` 由抽取阶段
生成并写进 `units.jsonl`。若先翻译，就没有可对齐的键，译文与原文只能靠位置匹配 ——
一旦中间有个条目被跳过，后面全部错位。

**`images_scan` 放在 `translate` 之前，但不参与翻译。** 它只负责**发现**贴图并按路径去重
（`stage_images_scan` 里有显式的 `seen` 集合去重，否则同一资产会被抽两遍、回写时译文叠加）。
真正的贴图翻译发生在 `images_localize`。

**`fonts` 必须在 `translate` 之后 —— 这是整个顺序里最硬的一条约束。**
字体补丁的本质是"把**这个项目实际会用到的字符**的字形合并进原字体"。
在翻译完成之前，你根本不知道要用到哪些字。`stage_fonts` 的第一步就是
`_collect_texts()`：把**译文**、**原文**、以及**贴图块的译文**都收进来，
再交给 `FontService.build_charset()` 生成字符集。

为什么连**原文**也要算进去：游戏里总有没被翻译的串（人名、型号、版本号、代码），
它们仍然要用这个字体渲染。漏掉它们 = 界面上出现口口口。
原文里没翻译的条目在 `_collect_texts()` 里被显式补进 `translated` 列表
（注释写得很清楚："UI 里可能直接显示未翻译的原文，那些字符同样需要字体覆盖"）。

**`images_localize` 必须在 `fonts` 之后。** 贴图重绘用的是**真实中文字体文件**光栅化
（`images/render.py`），它需要通过 `TextureTranslator._pick_font()` 找到一份可用的中文字体。
如果字体资产还没就绪，重绘就没有可用的笔。

**`qa` 在 `apply` 之前。** 质检的作用是"在污染 `out/` 之前发现问题"。
`stage_apply` 只回写 `placeholder_ok` 为真的条目；`stage_qa` 把占位符问题标成
`severity=ERROR` 并让 `ok=False`，这样 CLI 的 `novaloc qa` 会以退出码 1 结束，
可以用来卡流水线。

**`apply` 必须最后，且只写工作区副本。** `EngineAdapter.prepare_out()` 会把整个游戏目录
复制到 `ws.out_dir`，然后所有写入都发生在那份副本上。原始游戏目录在整个流程里**只读**。

### 2.2 幂等性与断点续跑

`stages.py` 的模块 docstring 写了三条设计原则，对应到代码：

1. **阶段之间只通过工作区文件交换数据，不靠内存对象。**
   每个 `stage_*` 方法开头都重新 `self.ws.load_*()`。进程被杀掉不会丢进度。
2. **每个阶段可单独重跑。** `novaloc run <id> --stage fonts` 只跑字体；
   `stage_translate(only_pending=True)` 会跳过已有译文的条目；
   `stage_translate` 合并时用 `{**existing, **{e.uid: e for e in entries}}`，
   所以**用户手工改过的译文不会被流水线覆盖**。
3. **任一步失败即中止，不继续往下跑。** `Pipeline._run()` 捕获异常后
   `raise PipelineError(stage, str(exc))`，`run_all()` 因此不会执行后续阶段。
   理由写在 `run_all` 的 docstring 里："下游阶段建立在错误数据上，继续跑只会产出
   更难排查的坏结果，还会白白烧几个小时的 GPU 时间。"

一个例外是**翻译批次失败**：`_translate_units()` 把单批失败收敛成
`EntryStatus.FAILED` 的条目并继续后面的批次，而不是让整条流水线挂掉 ——
"否则一条奇怪的文本就能让几小时的翻译白跑"。

---

## 3. 工作区目录布局

布局的唯一权威定义在 `src/novaloc/core/workspace.py` 的模块 docstring 与
`Workspace.create()` 的建目录列表里。

```
<data_root>/workspaces/<project_id>/
  project.json            项目元信息（Project 模型）
  project.src             原始游戏目录的绝对路径
  extracted/
    units.jsonl           抽取出的文本单元（每行一个 TextUnit）
    images.jsonl          发现的贴图资产（含每块的 OCR 结果与译文）
    report.json           抽取报告
  translations/
    entries.jsonl         译文（流水线可反复覆盖；用户编辑也写这里）
    glossary.jsonl        术语表
    memory.jsonl          翻译记忆库
  fonts/
    charset.json          项目字符集（all_chars / text_chars / ui_chars / total）
    analysis.jsonl        游戏自带字体的覆盖审计（缺哪些字）
    patches.json          字体补丁结果（含 coverage_before / after）
    patches/未在 docstring 中列出，实际由 stage_fonts 创建
    fallback/             注入用的中文字体
  images/
    analyzed/             带文字框标注的可视化图（人工复核用）
    rebuilt/              重绘后的贴图
    localize.json         逐图逐块的汉化记录
  qa/
    report.json           质检报告
  out/                    最终回写出的可玩游戏目录
  logs/
```

几个实现细节值得注意：

- **路径解析**在 `core/paths.py`。`data_root()` 优先取环境变量 `NOVALOC_DATA_ROOT`；
  否则开发态沿用仓库里的 `data/`，非开发态在 Windows 上遍历 `D:`~`H:` **挑可用空间最大的
  分区**（`_pick_big_disk_base()`）。这个设计的原因是明确的：C 盘是本机最紧张的分区，
  而模型 + 工作区副本动辄几十 GB。
- **配置文件的唯一权威位置是 `<data_root>/config.json`（JSON）**，
  由 `core/paths.py:config_json()` 给出，`Config.load()` / `Config.save()`
  直接读写它（`api/jobs.py:load_config()` 与 CLI 都委托给这两个方法）。
  早期还有一条 `<config_dir>/config.toml` 的路径，现已**降级为只读回退**：
  若 JSON 不存在而 TOML 存在，会读它并**自动迁移**成 JSON，老用户不丢配置。
  统一之前两边会各写各的（同一个 JSON 路径，一边用 `tomli_w`、一边用
  `model_dump_json`），用户在网页设置里改的项命令行看不到。
- **写入是原子的**：`_atomic_write()` 与 `_write_jsonl()` 都先写 `.tmp` 再 `replace()`。
- **单行损坏不会毁掉整个列表**：`_read_jsonl()` 对解析失败的行 `continue`。
- `Workspace.delete()` 有一个安全判断：只有当 `root.parent == paths.workspaces_dir()`
  时才执行 `rmtree`，避免误删。

---

## 4. EventBus 与 WebSocket 进度桥

### 4.1 EventBus 是同步的

`core/events.py` 的 docstring 说明了取舍：流水线是 CPU/GPU 密集型的，
引入 asyncio 广播只会增加心智负担，所以 `EventBus` 就是一个带锁的订阅者列表
加一个 2000 条的环形历史。两个刻意的设计：

- **订阅者抛异常不影响流水线**：`emit()` 里 `except Exception: pass`。
- **`ProgressThrottle`** 限制进度事件频率（默认 0.1 s / 1%），避免 UI 被刷爆。
  `stage_translate` 用 0.4 s / 0.5%，`stage_images_localize` 用 0.4 s。

### 4.2 任务级 EventBus

`api/app.py` 里有两层 bus：

- 应用级 `bus`（`app.state.bus`）用于全局信息；
- **每个任务一个独立 `EventBus`**（`make_pipeline()` 里 `Context(config=..., events=job_bus)`），
  这样进度只会推给关心这个任务的 WebSocket，不会串台。

`Job.__post_init__` 会把自己的 `_record` 订阅到自己的 bus 上，把事件同时写进一个
`deque(maxlen=500)` 的环形缓冲，并顺带维护 `progress` / `stage` / `message` / `error`。

### 4.3 跨线程投递

WebSocket 处理器（`ws_job`）在事件循环线程里，而事件是在 worker 线程里被 emit 的。
桥接方式是 `loop.call_soon_threadsafe(q.put_nowait, ev.to_dict())` —— 这是必需的，
不能直接 `q.put_nowait`。

连接建立时**先把历史事件补发一遍**（`for ev in job.events(): await websocket.send_json(ev)`），
然后才订阅。这是为了"用户任务跑到一半刷新页面仍能看到完整进度"。

收尾逻辑由 1 秒超时驱动：超时且任务已处于 `done/failed/cancelled` 时，排空队列后
发一条 `kind="closed"` 并关闭；否则发 `kind="heartbeat"` 保活。

### 4.4 为什么任务跑在线程池而不是事件循环

`api/jobs.py` 的模块 docstring 直接给了理由，`JobManager.__init__` 里还有第二层理由：

1. **不能阻塞事件循环。** OCR 与模型推理是 CPU/GPU 密集型的；放进 async 会把整个
   HTTP 服务卡死，连 `/api/health` 都不响应。
2. **默认 `max_workers=1`。** OCR 与模型推理争抢同一块 GPU，并行跑多个任务只会互相拖慢，
   还容易把显存打爆。
3. **取消语义简单。** `shutdown()` 用 `cancel_futures=True`。

Frontend 侧：前端静态资源由 FastAPI 直接挂载（`_web_dist()` → `novaloc/web_dist`），
非 `/api` 非 `/ws` 的路径走 SPA 回退。前端构建产物**随仓库提交**，所以运行时不需要 Node。

---

## 5. 注册中心、Context 与 Providers 模式

### 5.1 五类可替换组件

`core/registry.py` 定义了五个注册类别与对应的 Protocol：

| kind | Protocol | 关键方法 | 已知实现 |
|---|---|---|---|
| `translate` | `TranslationProvider` | `available()` / `translate_batch(items, target_lang)` | `ollama` |
| `ocr` | `OcrEngine` | `available()` / `detect_and_recognize(image, lang_hint)` | `ppocrv6` |
| `inpaint` | `InpaintEngine` | `available()` / `inpaint(image, mask)` | 目录为空；实际走 `images/inpaint.py` 的三档降级 |
| `vision` | `VisionEngine` | `available()` / `read_text()` / `describe_style()` | `ollama`（`translate/vision_ollama.py`），只做低置信度的兜底重读 |
| `engine` | —（不用 Protocol） | 见第 7 节 `EngineAdapter` | `rpgmaker` / `renpy` / `unity` / `loose` |

注册方式是装饰器 `@register(kind, name)`，它把 `name` 挂到 `cls.provider_name` 上并写进
模块级 `_REGISTRY`。

`available()` 的契约是**返回 `(bool, str)` 且不抛异常** —— 这样健康检查可以安全地遍历
所有适配器。

### 5.2 Context

`Context` 是一个 dataclass，是传给所有适配器的运行时上下文：

```python
@dataclass
class Context:
    config: Any          # Config
    events: Any          # EventBus
    workspace: Any = None
    logger: Any = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def cache_get(self, key, factory):  # 惰性构造 + 缓存
```

`cache_get` 的用途是**让重对象在一个 Context 内只构造一次**，
例如 `TextureTranslator.ocr` 属性就是 `ctx.cache_get("ocr", lambda: PPOcrV6Engine(ctx))`。

### 5.3 Providers

`Providers(ctx)` 是"按名字解析并缓存实例"的工厂：

- `get(kind, name)`：查 `_REGISTRY`，**带锁**，实例缓存在 `_instances[(kind, name)]`；
  构造抛 `TypeError` 时包装成 `ProviderError`（适配器构造签名不对是最常见的新手错误）。
- `resolve_translate()`：按 `primary_provider` → `fallback_providers` 的顺序依次尝试，
  取第一个 `available()` 为真的；全都不行时把**每一家的失败原因**拼进异常信息里。
- `diagnostics()`：给 UI 用的健康检查，遍历所有注册项调 `available()`。

`registry_snapshot()` 返回 `{kind: [names]}`，`list_registered(kind)` 返回某一类里的名字。

---

## 6. 从一次请求到一次回写（时序）

```
POST /api/projects/{pid}/run
        │
        ▼
  create_app() 里注册的 run_stage_job
        │  构造 fn(job_bus, job)
        ▼
  JobManager.submit(fn) ──► ThreadPoolExecutor(max_workers=1)
        │                         │
        │                         ▼
        │                   Workspace.open(pid)
        │                   Pipeline(ws, Context(config, events=job_bus))
        │                         │
        │                         ▼
        │                   ┌─ detect ──────────────► Project.engine
        │                   ├─ extract ─────────────► units.jsonl
        │                   ├─ images_scan ─────────► images.jsonl
        │                   ├─ translate ───────────► entries.jsonl
        │                   ├─ fonts ───────────────► charset.json / patches.json
        │                   ├─ images_localize ─────► images/rebuilt/**
        │                   ├─ qa ──────────────────► qa/report.json
        │                   └─ apply ───────────────► out/**
        │                         │
        │                         └─ 每个阶段 emit stage_start / progress / stage_end
        ▼                                              │
  Job._record 写入环形缓冲 ◄───────────────────────────┘
        │
        ▼
  GET /ws/jobs/{job_id}
        ├─ 先补发 job.events() 历史
        ├─ 订阅 job.bus，跨线程 call_soon_threadsafe 投递
        └─ 心跳 / done → closed
```

CLI 侧路径完全一样，只是 `novaloc run` 自己构造 `Context` 并直接调用 `Pipeline.run_all()`，
进度通过 Rich 表格打印（`print_results_table()`）。

---

## 7. EngineAdapter 接口与如何新增引擎

### 7.1 契约

`src/novaloc/engines/base.py`：

```python
class EngineAdapter:
    id: str = "base"
    display_name: str = "基类"
    priority: int = 100          # 越小越先被检查

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.cfg = ctx.config

    # 识别（只读）
    def detect(self, game_dir: Path) -> EngineInfo: ...

    # 抽取（只读）
    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]: ...
    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]: ...
    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]: ...
    def wire_fonts(self, out_dir: Path, installed: dict[str, str]) -> list[str]: ...

    # 回写（唯一允许写的地方，且只写 out_dir）
    def apply(self, game_dir, out_dir, units, translations, *,
              font_patches=None, rebuilt_images=None) -> ApplyResult: ...
```

**只读约束**是硬性的：`extract_*` / `detect` / `discover_fonts` 只允许读；
只有 `apply()` 会写，而且写的是 `ws.out_dir`。

`apply()` 的第一步必须是 `self.prepare_out(game_dir, out_dir)`，它把**整个**游戏目录复制到
输出目录。默认整目录复制而不是只复制要改的文件，是因为游戏运行时依赖大量未被修改的资源，
少复制一个就可能启动失败。

`wire_fonts()` 单独存在是因为一个非常具体的坑：**把补好的字体文件复制进 `fonts/`
并不会让引擎去用它**。每个引擎都有自己的"字体指向"配置，少了这一步，用户会看到
"字体文件确实被替换了，但游戏里还是口口口"，而且完全查不出原因。

### 7.2 识别优先级与置信度

`engines/detect_engine()` 遍历 `_discover()`（按 `priority` 升序）**全部**适配器，
取 `confidence` 最高者。优先级只影响检查顺序，不影响胜出规则。

| 适配器 | `id` | `priority` | 置信度计算 | 门槛 |
|---|---|---|---|---|
| RPG Maker | `rpgmaker` | 10 | `data/` 是目录且 4 个标记文件中至少 1 个存在 → `min(1.0, 0.4 + 0.15×present)`；`System.json` 解析失败 ×0.6；有 `js/` +0.15 | 至少一个标记文件 |
| Ren'Py | `renpy` | 20 | 基础 0.3；有 `renpy/` 目录 +0.35；有 `.rpy` +0.35；有 `options.rpy` +0.1；**只有 `.rpyc` 没有 `.rpy` 时 ×0.5** | 至少一个 `.rpy`/`.rpyc` 或 `renpy/` 目录 |
| Unity | `unity` | 30 | 有 `*_Data` 目录 → 0.5；有 `Managed/` +0.25；有 `globalgamemanagers` +0.1；有 `UnityPlayer.dll` +0.15 | 至少一个 `*_Data` 目录 |
| 散装文件 | `loose` | 90 | 有图片或文本文件 → 0.25 | 有可处理的文件 |

`EngineInfo.ok` 的定义是 `confidence > 0 and engine_id != "unknown"`。
`stage_detect` 在 `not info.ok` 时返回失败并给出明确建议（"请确认选择的是游戏根目录，
或改用散装文件模式"）。

注意 Ren'Py 的 `×0.5` 惩罚：这是把"只有编译产物"这种情况**降权**，
让它在与其它适配器的竞争中更容易输掉，同时在 `evidence` 里留下
"⚠️ 只有 .rpyc 没有 .rpy，无法安全修改文本"。

### 7.3 一个最小可用的新引擎适配器

假设要新增 Godot（`.pck` / `project.godot`）支持。步骤是：
**新建文件 → 写类 → 在两个地方登记**。

<details>
<summary>Skeleton：<code>src/novaloc/engines/godot.py</code></summary>

```python
"""Godot 适配器骨架。

Godot 的文本在 .tscn/.tres（纯文本）与 .pck（打包资源）里。
这里只示范"纯文本资源"这一条路径 —— 打包资源需要先解包，
属于独立工作量，不要在这里盲写。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.registry import Context, register
from ..models import (
    ExtractReport,
    FontCoverage,
    ImageAsset,
    TextKind,
    TextLocation,
    TextUnit,
)
from .base import ApplyResult, EngineAdapter, EngineInfo

#: 判定 Godot 工程的特征文件
_MARKERS = ("project.godot", "export_presets.cfg")


@register("engine", "godot")
class GodotAdapter(EngineAdapter):
    id = "godot"
    display_name = "Godot"
    #: 比 loose(90) 靠前，比 unity(30) 靠后：
    #: Godot 的 .tscn 是纯文本，误判代价小，但也不该抢在 Unity 前面
    priority = 40

    # ------------------------------------------------------------------
    # 识别：只读，必须给出 evidence 供用户核对误判
    # ------------------------------------------------------------------

    def detect(self, game_dir: Path) -> EngineInfo:
        info = EngineInfo(engine_id=self.id, display_name=self.display_name, root=game_dir)

        present = [m for m in _MARKERS if (game_dir / m).is_file()]
        if not present:
            # 一个特征都没有就直接返回 confidence=0，让别的适配器赢
            return info

        info.confidence = min(1.0, 0.5 + 0.2 * len(present))
        info.evidence.append(f"发现 {len(present)}/{len(_MARKERS)} 个 Godot 特征文件：{present}")

        # 顺便读一下版本，失败不影响识别
        cfg_file = game_dir / "project.godot"
        if cfg_file.is_file():
            m = re.search(r'config_version\s*=\s*(\d+)', cfg_file.read_text(
                encoding="utf-8", errors="replace")[:4000])
            if m:
                info.version = f"config_version={m.group(1)}"
                info.evidence.append(f"project.godot 版本字段：{m.group(1)}")
        return info

    # ------------------------------------------------------------------
    # 抽取：只读，绝不 mkdir / 绝不 write_text
    # ------------------------------------------------------------------

    def extract_text(self, game_dir: Path) -> tuple[list[TextUnit], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        units: list[TextUnit] = []

        files = sorted(game_dir.rglob("*.tscn"))
        report.files_scanned = len(files)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report.errors.append(f"{f.name} 读取失败：{exc}")
                continue
            # 实际实现需要真正的 .tscn 解析器；骨架里只演示 unit 的构造形状
            for i, m in enumerate(re.finditer(r'text\s*=\s*"([^"]+)"', text)):
                src = m.group(1)
                if not src.strip():
                    continue
                units.append(
                    TextUnit(
                        uid=f"{self._rel(game_dir, f)}#{i}",
                        source=src,
                        kind=TextKind.UI_LABEL,
                        location=TextLocation(
                            file=self._rel(game_dir, f),
                            line=0,
                            pointer=f"/{i}",       # pointer 是引擎内部的定位串
                        ),
                        engine=self.id,
                        adapter=self.id,
                    )
                )
        report.files_matched = len({u.location.file for u in units})
        return units, report

    def extract_images(self, game_dir: Path) -> tuple[list[ImageAsset], ExtractReport]:
        report = ExtractReport(adapter=self.id)
        assets: list[ImageAsset] = []
        for p in game_dir.rglob("*.png"):
            if p.is_file():
                assets.append(ImageAsset(uid=self._rel(game_dir, p), path=self._rel(game_dir, p)))
        report.files_scanned = len(assets)
        return assets, report

    def discover_fonts(self, game_dir: Path) -> list[FontCoverage]:
        out: list[FontCoverage] = []
        for p in game_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                rel = self._rel(game_dir, p)
                out.append(FontCoverage(font_id=rel, path=rel, family=p.stem, is_game_font=True))
        return out

    def wire_fonts(self, out_dir: Path, installed: dict[str, str]) -> list[str]:
        """让引擎真的用上新字体。

        对 Godot 而言，dynamic font 指向的是资源路径，
        最省事且可卸载的做法是生成一个 .tres/.gd 覆盖脚本，
        而不是去改用户的 .tscn。这里返回给用户看的操作说明。
        """
        if not installed:
            return []
        return [
            "Godot 的字体由主题（Theme）里的 FontFile 资源决定；"
            "已把补好的字体放到输出目录，请在 Godot 编辑器的 Theme 里重新指向它。"
        ]

    # ------------------------------------------------------------------
    # 回写：唯一允许写的地方，只写 out_dir
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
        res = ApplyResult(out_dir=out_dir)
        try:
            self.prepare_out(game_dir, out_dir)      # 整目录复制，必须第一步
        except Exception as exc:                     # noqa: BLE001
            res.error = f"复制游戏目录失败：{exc}"
            return res

        by_file: dict[str, list[TextUnit]] = {}
        for u in units:
            if u.uid in translations:
                by_file.setdefault(u.location.file, []).append(u)

        for rel, group in by_file.items():
            target = out_dir / rel
            if not target.is_file():
                res.files_skipped += 1
                res.warnings.append(f"目标文件不存在，已跳过：{rel}")
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except OSError as exc:
                res.files_skipped += 1
                res.warnings.append(f"读取失败：{rel}（{exc}）")
                continue
            for u in group:
                text = text.replace(u.source, translations[u.uid], 1)
            target.write_text(text, encoding="utf-8", newline="\n")
            res.files_written += 1

        # 贴图与字体：只覆盖存在的文件，缺失就记 warning，不要静默忽略
        for mapping, label in ((rebuilt_images or {}, "贴图"), (font_patches or {}, "字体")):
            for rel, src in mapping.items():
                dst = out_dir / rel
                if Path(src).is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(Path(src).read_bytes())
                else:
                    res.warnings.append(f"{label}源文件缺失：{src}")

        res.ok = res.error == ""
        return res
```

</details>

**登记的两个地方（缺一不可）**，都在 `src/novaloc/engines/__init__.py`：

1. 顶部导入，让装饰器执行：

   ```python
   from . import godot as _godot  # noqa: E402,F401
   ```

2. 加进 `_CLASSES` 元组与 `_resolve()` 的映射字典：

   ```python
   _CLASSES = (
       _rpgmaker.RpgMakerAdapter,
       _renpy.RenPyAdapter,
       _unity.UnityAdapter,
       _godot.GodotAdapter,        # ← 新增
       _loose.LooseFilesAdapter,
   )
   ```

**为什么不用注册中心反射**：`_discover()` 的注释解释了原因 ——
"注册中心在包导入期可能还没被填满，显式更可靠也更好读"。
所以 `@register("engine", ...)` 装饰器对引擎适配器来说主要是**自文档化**，
真正决定调度的是 `_CLASSES`。

### 7.4 新增翻译 / OCR / inpaint / vision 适配器

这四类**只需要装饰器**，不需要改 `_CLASSES`：

```python
from ..core.registry import Context, register

@register("translate", "my-provider")
class MyProvider:
    name = "my-provider"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def available(self) -> tuple[bool, str]:
        return True, "就绪"

    def translate_batch(self, items, target_lang):
        ...
```

然后在配置里把 `translate.primary_provider` 设为 `"my-provider"`。
`Providers.resolve_translate()` 会按主选 → 回退的顺序解析。

---

## 8. 数据模型与文件格式约定

- 所有跨阶段的数据都是 `models.py` 里的 pydantic 模型，便于校验与演进。
- 列表类数据一律 **JSONL**（一行一个对象），因为它是可追加、可流式读、
  单行坏了不会毁掉整个文件的格式。
- 配置与报告类数据用 **JSON**（`charset.json` / `patches.json` / `report.json`）。
- 写入一律原子（`.tmp` + `replace`）。
- 文本文件一律 `encoding="utf-8"`、`newline="\n"`。

---

## 9. 已知的结构性妥协

诚实列出，避免后来者以为是遗漏：

| 位置 | 妥协 | 影响 |
|---|---|---|
| `images/service.py::_pick_font` | 字体选择靠硬编码的候选路径列表（`msyhbd.ttc` / `simhei.ttf` / NotoSansCJK / PingFang），`FontSpec.local_path` 属性其实不存在，所以目录查找分支实际不生效 | 贴图重绘在非 Windows 或字体未装在默认路径时会抛"找不到可用的中文字体"。这是一个**明确的待修项** |
| `ocr_ppocrv6.py` | `available()` 只检查 `import rapidocr`，不检查模型是否已下载 | `doctor` 能报出模型目录为空，但 OCR 适配器本身会显示"可用" |
| `pipeline/stages.py::stage_apply` | 只把 `action == "merge"` 的补丁传给适配器 | `replace` / `fallback_only` 策略产出的字体不会通过这条路径回写 |
| `engines/base.py::wire_fonts` 默认实现 | 返回空列表（不改任何东西） | **只有 `rpgmaker` 与 `renpy` 覆写了它**；`unity` 与 `loose` 没有。Unity 的字体指向需要处理 TMP，见 `docs/FONTS.md` |
| ~~`api/jobs.py` 与 `core/config.py` 配置格式一分为二~~ | **已修**：统一到 `<data_root>/config.json`，`Config.load/save` 是唯一实现，API 与 CLI 都委托给它；旧 TOML 只在 JSON 缺失时作只读回退并自动迁移 | — |
| `pipeline/stage_images_localize` | 逐图串行处理 | 大项目贴图多时耗时长；并发会争抢 GPU，所以是有意保守 |
| `images/service.py::_vlm_rescue` | 逐块串行调用视觉模型，且**没有缓存** | 一张图里低置信度的块多时会连续发多次 `/api/chat`；同一文字块在别的图上重复出现也要重问。`vlm_reads` / `vlm_fixes` 两个计数器可用于观察命中率 |
| ~~`tests/` 目录不存在~~ | **已修**：16 个套件在 `tests/`，`pytest tests -q` 全量 18/18；CI 按 marker 跑无需模型/GPU 的 12 个（见 `tests/README.md`） | 少数套件在固定目录留产物并被后续断言读取，套件之间有轻微顺序耦合（README 里已注明） |
| `images/service.py::_pick_font` 的字体解析 | 已改为走 `FontService`（系统字体索引 + 缓存 + 可再分发下载） | `_system_font_index()` 用"目录 mtime 摘要"做缓存键，进程内只扫一次 |
