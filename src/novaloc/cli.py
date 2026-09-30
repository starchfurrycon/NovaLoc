"""NovaLoc 新译 —— 命令行界面。

这一层只做三件事：**读输入、调已有的 API、把结果画成人看得懂的样子**。
所有业务逻辑都在 ``core`` / ``engines`` / ``fonts`` / ``pipeline`` / ``images``
里，CLI 不重复实现任何一条规则。

几个刻意的取舍：

* **后端（FastAPI）是惰性导入的。** ``novaloc serve`` 之外任何命令都不该因为
  ``novaloc.api.app`` 缺依赖而挂掉 —— 抽取/翻译/字体补丁是纯本地流程，
  跟 Web 层没有耦合。
* **配置写在 ``<data_root>/config.json``**，和 :mod:`novaloc.api.jobs` 读写的是
  同一个文件，避免 CLI 与 Web UI 各自维护一份配置。
* 事件总线是同步的，流水线跑在主线程里，所以 ``run`` 直接订阅
  :class:`~novaloc.core.events.EventBus` 做实时输出 —— 不需要额外的线程或队列。

退出码约定：成功 0；质检未通过、流水线阶段失败、参数非法都是 1。
"""

from __future__ import annotations

import csv
import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import __app_name__, __app_name_zh__, __version__
from .core import paths
from .core.config import Config
from .core.events import Event, EventBus
from .core.registry import Context, Providers
from .core.workspace import Workspace
from .models import EntryStatus
from .pipeline import STAGE_LABELS, STAGES, Pipeline, PipelineError, StageResult, run_qa

# --------------------------------------------------------------------------
# 控制台与全局选项
# --------------------------------------------------------------------------

#: 显式带上 encoding，避免 Windows 上默认 GBK 把中文行输出炸掉。
console = Console(highlight=False, emoji=True)

#: 全局 ``--json``：目前只有部分命令支持，但保持一致的名字。
_JSON_MODE = False

app = typer.Typer(
    name="novaloc",
    help=(
        f"{__app_name__} {__app_name_zh__} —— 全离线游戏汉化流水线。\n\n"
        "常用流程：doctor（自检）→ scan（认引擎）→ project new（建项目）→ "
        "run（跑流水线）→ qa（质检）。"
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

project_app = typer.Typer(
    name="project",
    help="项目管理：新建、列出、查看、删除。",
    no_args_is_help=True,
)
config_app = typer.Typer(
    name="config",
    help="配置查看与初始化（配置文件在数据根目录下）。",
    no_args_is_help=True,
)
text_app = typer.Typer(
    name="text",
    help="译文导入导出：CSV / JSON / PO。",
    no_args_is_help=True,
)
fonts_app = typer.Typer(
    name="fonts",
    help="字体目录与覆盖审计。",
    no_args_is_help=True,
)
ollama_app = typer.Typer(
    name="ollama",
    help="本地推理服务（Ollama）状态检查与模型拉取。",
    no_args_is_help=True,
)

app.add_typer(project_app, name="project")
app.add_typer(config_app, name="config")
app.add_typer(text_app, name="text")
app.add_typer(fonts_app, name="fonts")
app.add_typer(ollama_app, name="ollama")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"{__app_name__} {__app_name_zh__} v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="显示版本号并退出。",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """NovaLoc 新译：把一款外语游戏变成中文可玩版本。"""
    # Windows 控制台默认不是 UTF-8，中文与 ✅/❌ 会变成乱码方块。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 被重定向到非文本流时忽略
            pass


# --------------------------------------------------------------------------
# 基础设施：配置、上下文、渲染小工具
# --------------------------------------------------------------------------


def config_path() -> Path:
    """配置文件位置：``<data_root>/config.json``（与 Web 后端共用一份）。"""
    return paths.data_root() / "config.json"


def load_config() -> Config:
    """读配置。文件不存在或损坏时回退到默认值，绝不因此让命令失败。"""
    p = config_path()
    cfg: Config | None = None
    if p.is_file():
        try:
            cfg = Config.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = None
    if cfg is None:
        # 兼容早期写在用户配置目录里的 TOML
        legacy = paths.config_file()
        if legacy.is_file():
            try:
                cfg = Config.load(legacy)
            except Exception:  # noqa: BLE001
                cfg = None
    if cfg is None:
        cfg = Config()
    cfg.data_root = str(paths.data_root())
    return cfg


def save_config(cfg: Config) -> Path:
    """写回配置（原子替换，避免中途崩掉留下半个文件）。"""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    data.pop("data_root", None)  # 派生值，不落盘
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def make_ctx(*, workspace: Workspace | None = None, events: EventBus | None = None) -> Context:
    """组装流水线/子服务需要的运行时上下文。"""
    cfg = load_config()
    return Context(config=cfg, events=events or EventBus(), workspace=workspace, logger=None)


def config_summary() -> dict[str, Any]:
    cfg = load_config()
    return {
        "path": str(config_path()),
        "exists": config_path().is_file(),
        "log_level": cfg.log_level,
        "translate": {
            "primary_provider": cfg.translate.primary_provider,
            "target_lang": cfg.translate.target_lang,
            "source_lang": cfg.translate.source_lang,
        },
        "ollama": {
            "enabled": cfg.ollama.enabled,
            "host": cfg.resolved_ollama_host(),
            "text_model": cfg.ollama.text_model,
            "vision_model": cfg.ollama.vision_model,
            "embed_model": cfg.ollama.embed_model,
        },
        "font": {"strategy": cfg.font.strategy, "ui_font": cfg.font.ui_font},
        "ocr": {"model_tier": cfg.ocr.model_tier, "use_directml": cfg.ocr.use_directml},
    }


def _fail(message: str, *, code: int = 1) -> None:
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(code=code)


def _fmt_time(ts: float) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def _fmt_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(seconds), 60)
    return f"{m} 分 {s} 秒"


def _echo_json(obj: Any) -> None:
    """机器可读输出：走 stdout，保证中文不被转义成 \\uXXXX。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
    sys.stdout.flush()


def _open_ws(project_id: str) -> Workspace:
    try:
        return Workspace.open(project_id)
    except FileNotFoundError as exc:
        _fail(f"找不到项目 {project_id!r}：{exc}")


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格（汉字算两格），用于纯文本对齐。"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in text)
    return text + " " * max(0, width - w)


def _short(text: str, limit: int) -> str:
    t = (text or "").replace("\n", " ").strip()
    return t if len(t) <= limit else t[: max(0, limit - 1)] + "…"


def _safe_severity(ev: Event) -> str:
    try:
        return ev.severity.value
    except Exception:  # noqa: BLE001
        return str(ev.severity)


# --------------------------------------------------------------------------
# doctor：环境自检
# --------------------------------------------------------------------------


def _gpu_info() -> dict[str, Any]:
    """DirectML 可用性 + GPU 名称。

    GPU 名字只用标准库拿（注册表 → wmic），不想为了显示一行字引入
    pynvml / GPUtil 这种额外依赖。
    """
    info: dict[str, Any] = {
        "onnxruntime": "",
        "providers": [],
        "directml": False,
        "gpu_names": [],
        "error": "",
    }
    try:
        import onnxruntime as ort  # noqa: PLC0415 - 自检时才需要，别拖慢所有命令

        providers = list(ort.get_available_providers())
        info["onnxruntime"] = getattr(ort, "__version__", "")
        info["providers"] = providers
        info["directml"] = "DmlExecutionProvider" in providers
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)

    info["gpu_names"] = _gpu_names()
    return info


def _gpu_names() -> list[str]:
    names: list[str] = []
    if os.name != "nt":
        return names
    try:
        import winreg  # noqa: PLC0415

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}",
        )
        with key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, sub) as sk:
                        val, _ = winreg.QueryValueEx(sk, "DriverDesc")
                        if val and val not in names:
                            names.append(str(val))
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return names


def _ocr_models() -> list[dict[str, Any]]:
    """``<models>/rapidocr`` 下已经就位的 ONNX 模型。"""
    d = paths.models_dir() / "rapidocr"
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.rglob("*.onnx")):
        try:
            out.append({"name": f.name, "path": str(f), "mb": round(f.stat().st_size / 1048576, 2)})
        except OSError:
            continue
    return out


def _license_for(family: str, summary: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按家族名匹配字体目录条目。

    系统里装的变体名常带后缀（``LXGW Neo XiHei Plus``、``LXGW WenKai GB Screen``），
    精确相等匹配不到，所以再做一次双向子串匹配。
    """
    fam = (family or "").strip().lower()
    if not fam:
        return None
    for r in summary:
        if str(r["family"]).lower() == fam:
            return r
    for r in summary:
        cf = str(r["family"]).lower()
        if cf and (cf in fam or fam in cf):
            return r
    return None


def _cjk_fonts(ctx: Context) -> list[dict[str, Any]]:
    """本机可用的中文字体（随包字体 + 已下载缓存 + 系统安装的候选）。

    这里把 ``fonts/`` 根目录以及其下的 ``cache/`` 都扫一遍：
    :meth:`FontService.supplement_candidates` 只看两个固定目录，
    而 ``ensure_font`` 下载的字体落在 ``fonts/cache``，
    两边取并集才是"实际能用"的字体集合。
    """
    from .fonts.catalog import license_summary  # noqa: PLC0415

    summary = license_summary()
    seen: dict[str, dict[str, Any]] = {}

    def add(p: Path, source: str) -> None:
        key = str(p).lower()
        if key in seen:
            return
        family = ""
        try:
            from .fonts.coverage import load_font_info  # noqa: PLC0415

            fi = load_font_info(p)
            family = (fi.family if fi else "") or ""
        except Exception:  # noqa: BLE001
            family = ""
        spec = _license_for(family, summary)
        seen[key] = {
            "path": str(p),
            "family": family or p.stem,
            "source": source,
            "mb": round(p.stat().st_size / 1048576, 2) if p.is_file() else 0.0,
            "license": (spec or {}).get("license", "未知"),
            "bundle_ok": bool((spec or {}).get("bundle_ok", False)),
            "catalog_id": (spec or {}).get("id", ""),
        }

    try:
        from .fonts.service import FontService  # noqa: PLC0415

        for p in FontService(ctx).supplement_candidates():
            add(p, "候选")
    except Exception:  # noqa: BLE001
        pass

    for root, label in ((paths.fonts_dir(), "数据目录"), (paths.bundled_fonts_dir(), "随包")):
        try:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                    add(f, label)
        except OSError:
            continue

    return sorted(seen.values(), key=lambda r: (r["source"], r["family"]))


def _collect_doctor() -> dict[str, Any]:
    ctx = make_ctx()
    cfg = ctx.config

    from .engines import available_engines  # noqa: PLC0415
    from .translate import ollama_setup  # noqa: PLC0415

    gpu = _gpu_info()
    base_url = cfg.resolved_ollama_host()

    runtime: dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": f"{platform.system()} {platform.release()}",
        "frozen": bool(getattr(sys, "frozen", False)),
    }

    ollama = ollama_setup.status_snapshot(base_url)
    engines = [{"id": i, "display_name": n} for i, n in available_engines()]

    adapters: list[dict[str, Any]] = []
    try:
        diag = Providers(ctx).diagnostics()
        for kind, rows in diag.items():
            for r in rows:
                # 引擎适配器不实现 available() 协议（它们靠 detect() 自证），
                # 放进"可用性"表只会得到一排假的"不可用"，这里跳过。
                if kind == "engine":
                    continue
                adapters.append({"kind": kind, **r})
    except Exception as exc:  # noqa: BLE001
        adapters = [{"kind": "?", "name": "?", "available": False, "detail": str(exc)}]

    fonts = _cjk_fonts(ctx)

    return {
        "version": __version__,
        "runtime": runtime,
        "data_root": str(paths.data_root()),
        "config": config_summary(),
        "gpu": gpu,
        "ollama": ollama,
        "ocr_models": _ocr_models(),
        "fonts": fonts,
        "engines": engines,
        "adapters": adapters,
    }


@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON 而不是表格。"),
) -> None:
    """环境自检：Python、数据目录、显卡与 DirectML、Ollama、OCR 模型、中文字体、引擎适配器。"""
    global _JSON_MODE
    _JSON_MODE = json_output
    info = _collect_doctor()

    if json_output:
        _echo_json(info)
        return

    # ---- 运行环境 ----
    t = Table(title="① 运行环境", box=box.SIMPLE_HEAVY, title_justify="left", show_lines=False)
    t.add_column("项目", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    t.add_row("NovaLoc 版本", f"v{info['version']}")
    t.add_row("Python", f"{info['runtime']['python']}（{info['runtime']['executable']}）")
    t.add_row("系统", info["runtime"]["platform"])
    t.add_row("数据根目录", info["data_root"])
    t.add_row(
        "配置文件",
        info["config"]["path"] + ("" if info["config"]["exists"] else " [yellow]（尚未创建，用默认值）[/yellow]"),
    )
    console.print(t)

    # ---- 计算设备 ----
    gpu = info["gpu"]
    d = Table(title="② 计算设备（ONNX Runtime）", box=box.SIMPLE_HEAVY, title_justify="left")
    d.add_column("项目", style="bold cyan", no_wrap=True)
    d.add_column("值", overflow="fold")
    if gpu["error"]:
        d.add_row("ONNX Runtime", f"[red]不可用：{gpu['error']}[/red]")
    else:
        d.add_row("ONNX Runtime", gpu["onnxruntime"] or "（未知版本）")
        d.add_row(
            "DirectML",
            "[green]✅ 可用[/green]" if gpu["directml"] else "[yellow]⚠ 不可用（OCR 将退回 CPU，慢 40~140 倍）[/yellow]",
        )
        d.add_row("可用执行提供器", ", ".join(gpu["providers"]) or "（无）")
    names = gpu["gpu_names"]
    d.add_row("显卡", "\n".join(names) if names else "（未探测到）")
    console.print(d)

    # ---- Ollama ----
    ol = info["ollama"]
    o = Table(title="③ 本地推理服务（Ollama）", box=box.SIMPLE_HEAVY, title_justify="left")
    o.add_column("项目", style="bold cyan", no_wrap=True)
    o.add_column("值", overflow="fold")
    o.add_row("服务地址", ol["base_url"])
    o.add_row(
        "是否在线",
        f"[green]✅ 在线[/green]（版本 {ol['version'] or '未知'}）" if ol["running"] else "[yellow]⚠ 未运行[/yellow]",
    )
    o.add_row("可执行文件", ol["executable"] or "[yellow]（未找到 ollama）[/yellow]")
    o.add_row("模型目录", ol["models_root"] or "[yellow]（未设置 OLLAMA_MODELS，默认在 C 盘）[/yellow]")
    if ol["models"]:
        rows = []
        for m in ol["models"]:
            gb = ol["model_sizes_gb"].get(m, 0.0)
            rows.append(f"{m}（{gb:.1f} GB）" if gb else m)
        o.add_row("已装模型", "、".join(rows))
    else:
        o.add_row("已装模型", "（无）")
    rec = ol["recommended"]
    o.add_row(
        "推荐模型",
        "、".join(f"{'✅' if ok else '⬜'} {m}" for m, ok in rec.items()) or "（无）",
    )
    console.print(o)
    if not ol["running"]:
        console.print(
            Panel(
                _ollama_hint_text(ol),
                title="Ollama 未就绪",
                border_style="yellow",
                expand=False,
            )
        )

    # ---- OCR 模型 ----
    m = Table(title="④ OCR 模型（RapidOCR / PP-OCRv6）", box=box.SIMPLE_HEAVY, title_justify="left")
    m.add_column("模型文件", style="bold cyan", overflow="fold")
    m.add_column("路径", overflow="fold")
    models = info["ocr_models"]
    if models:
        for r in models:
            m.add_row(f"{r['name']}\n[dim]{r['mb']:.1f} MB[/dim]", r["path"])
    else:
        m.add_row("[yellow]（未发现）[/yellow]", str(paths.models_dir() / "rapidocr"))
    console.print(m)
    # ---- 中文字体 ----
    f = Table(title="⑤ 可用中文字体", box=box.SIMPLE_HEAVY, title_justify="left")
    f.add_column("字体家族", style="bold cyan", overflow="fold")
    f.add_column("来源/许可", no_wrap=True)
    f.add_column("可分发", no_wrap=True, justify="center")
    f.add_column("大小", justify="right", no_wrap=True)
    fonts = info["fonts"]
    if fonts:
        for r in fonts:
            f.add_row(
                r["family"] or Path(r["path"]).name,
                f"{r['source']} · {r['license']}",
                "✅" if r["bundle_ok"] else "—",
                f"{r['mb']:.1f} MB",
            )
    else:
        f.add_row("[yellow]（一个都没有）[/yellow]", "-", "-", "-")
    console.print(f)

    # ---- 引擎适配器 ----
    e = Table(title="⑥ 引擎适配器与组件", box=box.SIMPLE_HEAVY, title_justify="left")
    e.add_column("类别/名称", style="bold cyan", no_wrap=True)
    e.add_column("状态", no_wrap=True)
    e.add_column("说明", overflow="fold")
    for r in info["engines"]:
        e.add_row(f"engine · {r['id']}", "[green]已注册[/green]", r["display_name"])
    for r in info["adapters"]:
        ok = bool(r.get("available"))
        e.add_row(
            f"{r.get('kind', '')} · {r.get('name', '')}",
            "[green]✅ 可用[/green]" if ok else "[yellow]不可用[/yellow]",
            str(r.get("detail", ""))[:160],
        )
    console.print(e)

    # ---- 结论 ----
    problems: list[str] = []
    if gpu["error"]:
        problems.append("ONNX Runtime 不可用 —— 贴图 OCR 会整体失效。")
    elif not gpu["directml"]:
        problems.append("DirectML 不可用 —— OCR 退回 CPU，速度会慢 40~140 倍。")
    if not ol["running"]:
        problems.append("Ollama 未运行 —— 无法翻译；文本抽取与字体审计仍可用。")
    if not info["ocr_models"]:
        problems.append("rapidocr 模型目录为空 —— 贴图文字识别不可用。")
    if not fonts:
        problems.append("没有可用中文字体 —— 字体补丁与贴图重绘无法保证不出「口口口」。")

    if problems:
        console.print(
            Panel(
                "\n".join(f"• {p}" for p in problems),
                title="[yellow]需要处理[/yellow]",
                border_style="yellow",
                expand=False,
            )
        )
    else:
        console.print(Panel("[green]一切就绪 ✅[/green]", border_style="green", expand=False))


def _ollama_hint_text(ol: dict[str, Any]) -> str:
    from .translate import ollama_setup  # noqa: PLC0415

    if not ol.get("executable"):
        return ollama_setup.install_hint()
    return (
        f"已找到 Ollama：{ol['executable']}\n"
        "但服务没有响应。先把它跑起来：\n"
        "    ollama serve\n"
        "（装了桌面版的话，直接启动 Ollama 应用也可以。）\n\n"
        "另外，务必把模型目录挪到空间充足的分区：\n"
        '    setx OLLAMA_MODELS "D:\\NovaLoc\\ollama-models"\n'
        "然后重开终端。"
    )


# --------------------------------------------------------------------------
# scan：识别引擎
# --------------------------------------------------------------------------


@app.command("scan")
def scan(
    game_dir: str = typer.Argument(..., help="游戏根目录（不是 data/ ，是包含 data/ 的那一层）。"),
    extract: bool = typer.Option(False, "--extract", help="顺带跑一次文本抽取并打印条数。"),
    images: bool = typer.Option(False, "--images", help="顺带扫描贴图资源。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """识别游戏引擎，并可选地抽取文本/扫描贴图。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    root = Path(game_dir).expanduser()
    if not root.is_dir():
        _fail(f"游戏目录不存在或不是目录：{root}")

    from .engines import detect_engine  # noqa: PLC0415

    ctx = make_ctx()
    info = detect_engine(root, ctx)

    payload: dict[str, Any] = {
        "game_dir": str(root.resolve()),
        "engine_id": info.engine_id,
        "display_name": info.display_name,
        "version": info.version,
        "confidence": round(float(info.confidence), 4),
        "ok": bool(info.ok),
        "evidence": list(info.evidence),
    }

    if extract or images:
        adapter = None
        if info.engine_id and info.engine_id != "unknown":
            from .engines import get_adapter  # noqa: PLC0415

            adapter = get_adapter(info.engine_id, ctx)

        if extract:
            if adapter is None:
                payload["extract"] = {"ok": False, "error": "没有可用的适配器，无法抽取"}
            else:
                units, rep = adapter.extract_text(root)
                live = [u for u in units if u.source.strip()]
                kinds: dict[str, int] = {}
                for u in live:
                    kinds[u.kind.value] = kinds.get(u.kind.value, 0) + 1
                payload["extract"] = {
                    "ok": bool(units),
                    "units": len(units),
                    "non_empty": len(live),
                    "files_scanned": rep.files_scanned,
                    "files_matched": rep.files_matched,
                    "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
                    "skipped": rep.skipped,
                    "errors": rep.errors[:20],
                    "duration_s": round(rep.duration_s, 3),
                    "samples": [
                        {"uid": u.uid, "kind": u.kind.value, "source": _short(u.source, 80)}
                        for u in live[:10]
                    ],
                }

        if images:
            if adapter is None:
                payload["images"] = {"ok": False, "error": "没有可用的适配器，无法扫描贴图"}
            else:
                assets, irep = adapter.extract_images(root)
                paths = {a.path for a in assets}
                payload["images"] = {
                    "ok": True,
                    "count": len(paths),
                    "duplicates": max(0, len(assets) - len(paths)),
                    "errors": irep.errors[:20],
                }

    if json_output:
        _echo_json(payload)
        return

    t = Table(title=f"引擎识别 · {root.resolve()}", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("字段", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    t.add_row("引擎 id", info.engine_id)
    t.add_row("显示名", info.display_name)
    t.add_row("版本", info.version or "（未知）")
    conf = float(info.confidence)
    color = "green" if conf >= 0.7 else ("yellow" if conf > 0 else "red")
    t.add_row("置信度", f"[{color}]{conf:.0%}[/{color}]")
    t.add_row("根目录", str(root.resolve()))
    console.print(t)

    if info.evidence:
        console.print("[bold]判定依据[/bold]")
        for ev in info.evidence:
            console.print(f"  · {ev}")
    else:
        console.print("[yellow]没有判定依据 —— 可能不是受支持的引擎。[/yellow]")

    if not info.ok:
        console.print(
            Panel(
                "无法识别引擎。请确认给的是游戏**根目录**；"
                "若资源已被打包/加密，可先用 AssetStudio/UABEA 导出，再用「散装文件」模式。",
                border_style="yellow",
                expand=False,
            )
        )

    if "extract" in payload:
        ex = payload["extract"]
        if not ex.get("ok"):
            console.print(f"[yellow]抽取未成功：{ex.get('error', '没有抽到文本')}[/yellow]")
        else:
            et = Table(title="文本抽取结果", box=box.SIMPLE, title_justify="left")
            et.add_column("项目", style="bold cyan", no_wrap=True)
            et.add_column("值", justify="right")
            et.add_row("文本单元", str(ex["units"]))
            et.add_row("非空文本", str(ex["non_empty"]))
            et.add_row("文件扫描", str(ex["files_scanned"]))
            et.add_row("文件命中", str(ex["files_matched"]))
            et.add_row("用时", _fmt_duration(ex["duration_s"]))
            console.print(et)
            if ex["kinds"]:
                kt = Table(title="按类型分布", box=box.SIMPLE, title_justify="left")
                kt.add_column("类型", style="bold cyan")
                kt.add_column("条数", justify="right")
                for k, v in ex["kinds"].items():
                    kt.add_row(k, str(v))
                console.print(kt)
            if ex["samples"]:
                console.print("[bold]样例[/bold]")
                for s in ex["samples"]:
                    console.print(f"  [{s['kind']}] {s['source']}")
            for err in ex.get("errors", []):
                console.print(f"  [yellow]! {err}[/yellow]")

    if "images" in payload:
        im = payload["images"]
        if im.get("ok"):
            console.print(f"贴图候选：{im['count']} 张（重复 {im['duplicates']} 条已合并）")
        else:
            console.print(f"[yellow]贴图扫描未成功：{im.get('error')}[/yellow]")


# --------------------------------------------------------------------------
# project：项目管理
# --------------------------------------------------------------------------


@project_app.command("new")
def project_new(
    name: str = typer.Option(..., "--name", "-n", help="项目名（仅用于展示）。"),
    game: str = typer.Option(..., "--game", "-g", help="游戏根目录。"),
    engine: str = typer.Option("", "--engine", "-e", help="引擎 id；留空自动识别。"),
    target_lang: str = typer.Option("zh-Hans", "--target-lang", help="目标语言。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """新建一个汉化项目（工作区）。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    src = Path(game).expanduser()
    if not src.is_dir():
        _fail(f"游戏目录不存在或不是目录：{src}")

    ctx = make_ctx()
    engine_id = engine.strip()
    detected: dict[str, Any] = {}

    if not engine_id:
        from .engines import detect_engine  # noqa: PLC0415

        info = detect_engine(src, ctx)
        engine_id = info.engine_id
        detected = {
            "display_name": info.display_name,
            "version": info.version,
            "confidence": round(float(info.confidence), 4),
            "evidence": list(info.evidence)[:6],
        }

    if not engine_id or engine_id == "unknown":
        console.print(
            "[yellow]⚠ 未能识别引擎。项目会建好，但抽取阶段需要你确认引擎，"
            "或者先用工具把资源导出成散装文件。[/yellow]"
        )

    ws = Workspace.create(name, src, target_lang=target_lang)
    ws.project.engine = engine_id if engine_id != "unknown" else ""
    ws.project.engine_version = str(detected.get("version", ""))
    ws.project.stage = "created"
    ws.save()

    payload = {
        "id": ws.project.id,
        "name": ws.project.name,
        "game_dir": ws.project.game_dir,
        "engine": ws.project.engine,
        "target_lang": ws.project.target_lang,
        "root": str(ws.root),
        "detected": detected,
    }
    if json_output:
        _echo_json(payload)
        return

    console.print(f"[green]✅ 项目已创建[/green]  id = [bold]{ws.project.id}[/bold]")
    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("项目", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    t.add_row("名称", ws.project.name)
    t.add_row("引擎", ws.project.engine or "（未识别）")
    if detected:
        t.add_row("识别为", f"{detected.get('display_name', '')} {detected.get('version', '')}".strip())
        t.add_row("置信度", f"{float(detected.get('confidence', 0.0)):.0%}")
    t.add_row("游戏目录", ws.project.game_dir)
    t.add_row("工作区", str(ws.root))
    console.print(t)
    console.print(f"下一步： [bold]novaloc run {ws.project.id}[/bold]")


@project_app.command("list")
def project_list(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """列出所有项目。"""
    global _JSON_MODE
    _JSON_MODE = json_output
    projects = Workspace.list()

    if json_output:
        _echo_json(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "engine": p.engine,
                    "engine_version": p.engine_version,
                    "game_dir": p.game_dir,
                    "stage": p.stage,
                    "created_at": p.created_at,
                    "updated_at": p.updated_at,
                }
                for p in projects
            ]
        )
        return

    if not projects:
        console.print("还没有任何项目。用 [bold]novaloc project new --name 名字 --game 游戏目录[/bold] 新建一个。")
        return

    t = Table(title=f"项目列表（{len(projects)} 个）", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("id", style="bold cyan", no_wrap=True)
    t.add_column("名称", overflow="fold")
    t.add_column("引擎", no_wrap=True)
    t.add_column("游戏目录", overflow="fold")
    t.add_column("更新时间", no_wrap=True)
    for p in projects:
        t.add_row(p.id, p.name, p.engine or "-", p.game_dir, _fmt_time(p.updated_at))
    console.print(t)


@project_app.command("show")
def project_show(
    project_id: str = typer.Argument(..., help="项目 id。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """查看一个项目的详情与产物统计。"""
    global _JSON_MODE
    _JSON_MODE = json_output
    ws = _open_ws(project_id)
    p = ws.project

    units = ws.load_units()
    entries = ws.load_entries()
    images = ws.load_images()
    charset = ws.load_charset()
    qa_report = ws.read_json("qa/report.json", None)

    done = sum(1 for e in entries if e.is_done and e.target.strip())
    payload = {
        "id": p.id,
        "name": p.name,
        "game_dir": p.game_dir,
        "engine": p.engine,
        "engine_version": p.engine_version,
        "target_lang": p.target_lang,
        "source_langs": p.source_langs,
        "stage": p.stage,
        "notes": p.notes,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "root": str(ws.root),
        "out_dir": str(ws.out_dir),
        "out_exists": ws.out_dir.exists(),
        "counts": {
            "units": len(units),
            "entries": len(entries),
            "translated": done,
            "pending": max(0, len(entries) - done),
            "images": len(images),
            "charset": charset.total if charset else 0,
        },
        "qa_ok": (qa_report or {}).get("ok") if isinstance(qa_report, dict) else None,
    }
    if json_output:
        _echo_json(payload)
        return

    t = Table(title=f"项目 {p.id}", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("字段", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    t.add_row("名称", p.name)
    t.add_row("引擎", f"{p.engine or '（未识别）'} {p.engine_version}".strip())
    t.add_row("游戏目录", p.game_dir)
    t.add_row("目标语言", p.target_lang)
    t.add_row("当前阶段", p.stage or "-")
    t.add_row("创建时间", _fmt_time(p.created_at))
    t.add_row("更新时间", _fmt_time(p.updated_at))
    t.add_row("工作区", str(ws.root))
    t.add_row("输出目录", f"{ws.out_dir}" + (" [green]（已生成）[/green]" if ws.out_dir.exists() else " [yellow]（尚未生成）[/yellow]"))
    console.print(t)

    c = Table(title="产物统计", box=box.SIMPLE, title_justify="left")
    c.add_column("项目", style="bold cyan", no_wrap=True)
    c.add_column("数量", justify="right")
    c.add_row("文本单元", str(len(units)))
    c.add_row("译文条目", str(len(entries)))
    c.add_row("已译", str(done))
    c.add_row("未译", str(max(0, len(entries) - done)))
    c.add_row("贴图资产", str(len(images)))
    c.add_row("项目字符集", str(charset.total if charset else 0))
    console.print(c)

    if isinstance(qa_report, dict):
        ok = bool(qa_report.get("ok"))
        console.print(f"最近一次质检：{'[green]通过[/green]' if ok else '[yellow]未通过[/yellow]'}")


@project_app.command("delete")
def project_delete(
    project_id: str = typer.Argument(..., help="项目 id。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认，直接删除。"),
) -> None:
    """删除一个项目（**连同工作区里的所有产物**，原游戏目录不受影响）。"""
    ws = _open_ws(project_id)
    console.print(f"将要删除项目 [bold]{ws.project.id}[/bold]（{ws.project.name}）")
    console.print(f"工作区目录：[red]{ws.root}[/red]")
    console.print("[yellow]原始游戏目录不会被改动。[/yellow]")
    if not yes and not typer.confirm("确认删除？"):
        console.print("已取消。")
        raise typer.Exit(code=1)
    ws.delete()
    console.print("[green]✅ 已删除[/green]")


# --------------------------------------------------------------------------
# config：配置查看 / 初始化
# --------------------------------------------------------------------------


@config_app.command("show")
def config_show() -> None:
    """显示当前配置（CLI 与 Web UI 共用同一份 config.json）。

    配置项太多，表格反而不如 JSON 好读，所以固定输出 JSON。
    """
    cfg = load_config()
    data = cfg.model_dump(mode="json")
    data["data_root"] = str(paths.data_root())
    _echo_json({"path": str(config_path()), "exists": config_path().is_file(), "config": data})


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="已存在时也覆盖（会丢掉现有设置）。"),
) -> None:
    """把默认配置写到 ``<data_root>/config.json``。"""
    p = config_path()
    if p.is_file() and not force:
        console.print(f"配置文件已存在：{p}（用 --force 覆盖）")
        raise typer.Exit(code=0)
    cfg = Config()
    cfg.data_root = str(paths.data_root())
    save_config(cfg)
    console.print(f"[green]✅ 已写入默认配置：{p}[/green]")
    from .translate import ollama_setup  # noqa: PLC0415

    console.print("[dim]Ollama 相关建议环境变量：[/dim]")
    for k, v in ollama_setup.recommended_env(paths.data_root()).items():
        console.print(f'    setx {k} "{v}"')


@config_app.command("path")
def config_path_cmd() -> None:
    """打印配置文件路径（方便脚本里取）。"""
    sys.stdout.write(str(config_path()) + "\n")


# --------------------------------------------------------------------------
# run：跑流水线
# --------------------------------------------------------------------------


def _stage_order() -> list[tuple[str, str]]:
    return list(STAGES)


def _validate_stage(stage: str) -> str:
    valid = [s for s, _ in STAGES]
    key = stage.strip().lower()
    if key not in valid:
        _fail(f"未知阶段 {stage!r}。可用阶段：" + "、".join(f"{s}（{lb}）" for s, lb in STAGES))
    return key


def print_results_table(results: list[StageResult], title: str = "流水线结果") -> None:
    t = Table(title=title, box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("阶段", style="bold cyan", no_wrap=True)
    t.add_column("结果", no_wrap=True)
    t.add_column("用时", justify="right", no_wrap=True)
    t.add_column("说明", overflow="fold")
    for r in results:
        label = STAGE_LABELS.get(r.stage, r.stage)
        mark = "[green]✅[/green]" if r.ok else "[red]❌[/red]"
        msg = r.message or r.error or ""
        t.add_row(label, mark, _fmt_duration(r.duration_s), _short(msg, 90))
    console.print(t)


@app.command("run")
def run(
    project_id: str = typer.Argument(..., help="项目 id（novaloc project list 可以查）。"),
    stage: str = typer.Option("", "--stage", help="只跑一个阶段；见 STAGES，如 extract / translate / fonts / apply。"),
    only_pending: bool = typer.Option(
        True,
        "--only-pending/--all",
        help="翻译时是否跳过已有译文的条目（默认只翻待翻的）。",
    ),
    force: bool = typer.Option(False, "--force", help="强制重做字体注入与贴图汉化（忽略已有产物）。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只做安全检查并打印将要执行的阶段，不写任何文件。"),
) -> None:
    """跑完整汉化流水线，实时显示进度与日志。"""
    ws = _open_ws(project_id)
    cfg = load_config()

    bus = EventBus()
    ctx = Context(config=cfg, events=bus, workspace=ws, logger=None)
    pipe = Pipeline(ws, ctx)

    order = _stage_order()
    todo: list[tuple[str, str]] = order
    if stage:
        key = _validate_stage(stage)
        todo = [s for s in order if s[0] == key]

    # ---------------------------------------------------------------
    # dry-run：只检查前置条件，不做任何写入
    # ---------------------------------------------------------------
    if dry_run:
        console.print(
            Panel(
                f"项目 [bold]{ws.project.id}[/bold]（{ws.project.name}）\n"
                f"游戏目录：{ws.source_dir}\n"
                f"输出目录：{ws.out_dir}\n"
                f"翻译模式：{'只翻待翻条目' if only_pending else '全部重翻'}"
                f"{'　强制重做字体/贴图' if force else ''}",
                title="[yellow]试运行（不会写任何文件）[/yellow]",
                border_style="yellow",
                expand=False,
            )
        )

        units = ws.load_units()
        entries = ws.load_entries()
        images = ws.load_images()
        charset = ws.load_charset()

        t = Table(title="将要执行的阶段", box=box.SIMPLE_HEAVY, title_justify="left")
        t.add_column("#", justify="right", no_wrap=True)
        t.add_column("阶段", style="bold cyan", no_wrap=True)
        t.add_column("id", no_wrap=True)
        t.add_column("就绪情况", overflow="fold")
        for i, (sid, label) in enumerate(todo, 1):
            t.add_row(str(i), label, sid, _stage_readiness(sid, ws, units, entries, images, charset))
        console.print(t)

        blocked = [s for s, _ in todo if _stage_blocker(s, units, entries, images) ]
        if blocked:
            console.print(
                f"[yellow]⚠ 阶段 {', '.join(blocked)} 的前置数据还不齐，正式跑会在更早的阶段失败。[/yellow]"
            )
        return

    # ---------------------------------------------------------------
    # 实时进度：Progress 的 task 描述 + 一个滚动的日志尾巴
    # ---------------------------------------------------------------
    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=28),
        TextColumn("{task.percentage:>5.1f}%"),
        console=console,
        transient=False,
    )
    task_id = progress.add_task("准备中…", total=max(1, len(todo)))
    log_lines: list[str] = []

    # 质检阶段会为每一条没有译文的串各发一条 warn 日志（几十上百条），
    # 直接把终端刷爆。同类消息只打前几条，其余汇总。
    MAX_SAME_LOG = 3
    seen_logs: dict[str, int] = {}
    suppressed: dict[str, int] = {}

    def on_event(ev: Event) -> None:
        sev = _safe_severity(ev)
        if ev.kind == "stage_start":
            progress.update(
                task_id,
                description=f"{STAGE_LABELS.get(ev.stage, ev.stage)}（{ev.stage}）",
            )
        elif ev.kind == "progress" and ev.progress is not None:
            progress.update(task_id, description=f"{STAGE_LABELS.get(ev.stage, ev.stage)} · {ev.progress * 100:.0f}%")
        elif ev.kind == "log":
            style = {"warn": "yellow", "error": "red"}.get(sev, "")
            prefix = {"warn": "⚠ ", "error": "✗ "}.get(sev, "  ")
            n = seen_logs.get(ev.message, 0)
            seen_logs[ev.message] = n + 1
            if n >= MAX_SAME_LOG:
                suppressed[ev.message] = suppressed.get(ev.message, 0) + 1
                return
            log_lines.append(f"{prefix}{ev.message}")
            console.print(f"[{style}]{prefix}{ev.message}[/{style}]" if style else f"{prefix}{ev.message}")
        elif ev.kind == "stage_error":
            log_lines.append(f"✗ [{STAGE_LABELS.get(ev.stage, ev.stage)}] {ev.message}")
            console.print(f"[red]✗ [{STAGE_LABELS.get(ev.stage, ev.stage)}] {ev.message}[/red]")
        elif ev.kind == "stage_end":
            skipped = sum(suppressed.values())
            if skipped:
                console.print(f"[dim]…另有 {skipped} 条重复提示已折叠。[/dim]")
                suppressed.clear()
            log_lines.append(f"✅ {STAGE_LABELS.get(ev.stage, ev.stage)} 完成：{ev.message}")
            console.print(
                f"[green]✅ {STAGE_LABELS.get(ev.stage, ev.stage)} 完成[/green]"
                + (f"：{ev.message}" if ev.message else "")
            )
        elif ev.kind == "done":
            log_lines.append(f"完成：{ev.message}")

    unsub = bus.subscribe(on_event)
    failed: PipelineError | None = None
    try:
        with progress:
            for sid, label in todo:
                progress.update(task_id, description=f"{label}（{sid}）")
                if sid == "detect":
                    pipe.stage_detect()
                elif sid == "extract":
                    pipe.stage_extract()
                elif sid == "images_scan":
                    pipe.stage_images_scan()
                elif sid == "translate":
                    pipe.stage_translate(only_pending=only_pending)
                elif sid == "fonts":
                    pipe.stage_fonts(force=force)
                elif sid == "images_localize":
                    pipe.stage_images_localize(force=force)
                elif sid == "qa":
                    pipe.stage_qa()
                elif sid == "apply":
                    pipe.stage_apply()
                progress.advance(task_id)
    except PipelineError as exc:
        failed = exc
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。已完成的阶段产物都留在工作区里，可以重跑。[/yellow]")
        raise typer.Exit(code=130) from None
    finally:
        unsub()

    results = pipe.results
    if results:
        print_results_table(results, title=f"流水线结果 · {ws.project.id}")

    if failed is not None:
        label = STAGE_LABELS.get(failed.stage, failed.stage)
        console.print(f"[bold red]✗ 阶段「{label}」失败：{failed.message}[/bold red]")
        if results:
            last_ok = [r for r in results if r.ok]
            console.print(
                f"已完成 {len(last_ok)}/{len(todo)} 个阶段。"
                f"修掉上面的问题后可以只重跑该阶段："
                f"[bold]novaloc run {ws.project.id} --stage {failed.stage}[/bold]"
            )
        raise typer.Exit(code=1)

    if results and all(r.ok for r in results):
        if any(r.stage == "apply" for r in results):
            console.print(f"[green]🎉 全部完成。可玩目录：{ws.out_dir}[/green]")
        else:
            console.print("[green]🎉 所选阶段全部完成。[/green]")


def _stage_readiness(
    sid: str,
    ws: Workspace,
    units: list[Any],
    entries: list[Any],
    images: list[Any],
    charset: Any,
) -> str:
    """给 dry-run 用的一句话就绪说明。"""
    if sid == "detect":
        return f"当前记录：{ws.project.engine or '（未识别）'}"
    if sid == "extract":
        return f"已有 {len(units)} 条文本单元" if units else "尚无文本单元（本阶段会生成）"
    if sid == "images_scan":
        return f"已有 {len(images)} 张贴图资产" if images else "尚无贴图记录（本阶段会生成）"
    if sid == "translate":
        pending = sum(1 for e in entries if not (e.is_done and e.target.strip()))
        return f"译文 {len(entries)} 条，其中待翻 {pending} 条" if entries else "尚无译文"
    if sid == "fonts":
        return f"字符集 {charset.total} 字" if charset else "尚无字符集（依赖译文）"
    if sid == "images_localize":
        return f"{len(images)} 张候选贴图"
    if sid == "qa":
        return "随时可跑"
    if sid == "apply":
        done = sum(1 for e in entries if e.is_done and e.target.strip())
        return f"可回写 {done} 条译文"
    return "-"


def _stage_blocker(sid: str, units: list[Any], entries: list[Any], images: list[Any]) -> bool:
    if sid == "translate":
        return not units
    if sid in ("fonts", "apply"):
        return not any(getattr(e, "target", "").strip() for e in entries)
    return False


# --------------------------------------------------------------------------
# text：译文导入导出
# --------------------------------------------------------------------------

CSV_HEADER = ["uid", "source", "target", "status", "kind"]


@text_app.command("export")
def text_export(
    project_id: str = typer.Argument(..., help="项目 id。"),
    format: str = typer.Option("csv", "--format", "-f", help="导出格式：csv | json | po。"),
    out: str = typer.Option(..., "--out", "-o", help="输出文件路径。"),
) -> None:
    """把译文导出成 CSV / JSON / PO，交给外部工具或人工校对。"""
    global _JSON_MODE
    fmt = format.strip().lower()
    if fmt not in ("csv", "json", "po"):
        _fail(f"不支持的格式 {format!r}；可用：csv、json、po")

    ws = _open_ws(project_id)
    units = {u.uid: u for u in ws.load_units()}
    entries = ws.load_entries()

    # 还没有译文条目时，用抽出来的文本单元**合成空条目**（target 为空）。
    # 这样"导出 → 外部翻译 → 导入"这条工作流在翻译阶段之前就能走通，
    # 不必先跑一次 Ollama。
    synthesized = not entries
    if synthesized:
        from .models import TranslationEntry  # noqa: PLC0415

        entries = [
            TranslationEntry(
                uid=u.uid,
                source=u.source,
                kind=u.kind,
                status=EntryStatus.PENDING,
            )
            for u in units.values()
            if u.source.strip()
        ]
        if not entries:
            _fail("这个项目还没有抽到任何文本。先跑 `novaloc run <id> --stage extract`。")

    dest = Path(out).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 排序：先按 kind，再按 uid —— 让同类文本挨在一起，方便人工校对
    rows = sorted(entries, key=lambda e: (e.kind.value, e.uid))

    if fmt == "csv":
        # utf-8-sig：Excel 双击打开中文不乱码
        with dest.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            for e in rows:
                w.writerow([e.uid, e.source, e.target, e.status.value, e.kind.value])
    elif fmt == "json":
        payload = [
            {
                "uid": e.uid,
                "source": e.source,
                "target": e.target,
                "status": e.status.value,
                "kind": e.kind.value,
                "provider": e.provider,
                "model": e.model,
                "warnings": list(e.warnings),
                "context": (units[e.uid].context if e.uid in units else ""),
                "location": (units[e.uid].location.file if e.uid in units else ""),
            }
            for e in rows
        ]
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:  # po
        lines = [
            'msgid ""',
            'msgstr ""',
            '"Content-Type: text/plain; charset=UTF-8\\n"',
            '"Content-Transfer-Encoding: 8bit\\n"',
            f'"X-NovaLoc-Project: {ws.project.id}\\n"',
            "",
        ]
        for e in rows:
            ref = units[e.uid].location.file if e.uid in units else ""
            lines.append(f"#: uid={e.uid}" + (f" {ref}" if ref else ""))
            lines.append(f"#. kind={e.kind.value} status={e.status.value}")
            if e.warnings:
                lines.append(f"# warning: {', '.join(e.warnings)}")
            lines.append("msgid " + _po_quote(e.source))
            lines.append("msgstr " + _po_quote(e.target))
            lines.append("")
        dest.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    console.print(f"[green]✅ 已导出 {len(rows)} 条译文（{fmt}）→ {dest}[/green]")
    if synthesized:
        console.print("[dim]这些条目还没有译文（target 为空），导出的是待翻译清单。[/dim]")


def _po_quote(s: str) -> str:
    """PO 字符串转义。"""
    body = (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{body}"'


def _po_unquote(s: str) -> str:
    body = s.strip()
    if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
        body = body[1:-1]
    return (
        body.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


@text_app.command("import")
def text_import(
    project_id: str = typer.Argument(..., help="项目 id。"),
    file: str = typer.Option(..., "--file", "-f", help="要导入的文件：CSV / JSON / PO。"),
    create: bool = typer.Option(
        True,
        "--create/--no-create",
        help="导入文件里出现了本项目已抽取、但还没有译文条目的 uid 时，是否据此新建条目。",
    ),
) -> None:
    """从 CSV / JSON / PO 导入译文，按 uid（或原文）匹配条目并写回。"""
    ws = _open_ws(project_id)
    src = Path(file).expanduser()
    if not src.is_file():
        _fail(f"文件不存在：{src}")

    units = {u.uid: u for u in ws.load_units()}
    entries = ws.load_entries()
    if not entries and not units:
        _fail("这个项目还没有抽到任何文本。先跑 `novaloc run <id> --stage extract`。")

    from .models import TranslationEntry  # noqa: PLC0415

    by_uid = {e.uid: e for e in entries}
    by_source: dict[str, list[Any]] = {}
    for e in entries:
        by_source.setdefault(e.source.strip(), []).append(e)

    suffix = src.suffix.lower()
    try:
        if suffix == ".csv":
            pairs = _read_csv_rows(src)
        elif suffix == ".json":
            pairs = _read_json_rows(src)
        elif suffix in (".po", ".pot"):
            pairs = _read_po_rows(src)
        else:
            _fail(f"不认识的扩展名 {suffix!r}；支持 .csv / .json / .po")
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail(f"解析 {src.name} 失败：{exc}")

    updated = 0
    created = 0
    unknown: list[str] = []
    skipped_empty = 0

    for row in pairs:
        uid = (row.get("uid") or "").strip()
        source = (row.get("source") or "").strip()
        target = row.get("target")
        status = (row.get("status") or "").strip().lower()

        hit = by_uid.get(uid) if uid else None
        if hit is None and source:
            cands = by_source.get(source) or []
            if len(cands) == 1:
                hit = cands[0]

        # 文件里是项目已抽取的 uid、但还没有对应条目 → 按抽取结果补一条
        if hit is None and create and uid and uid in units:
            unit = units[uid]
            hit = TranslationEntry(uid=unit.uid, source=unit.source, kind=unit.kind)
            entries.append(hit)
            by_uid[uid] = hit
            by_source.setdefault(unit.source.strip(), []).append(hit)
            created += 1

        if hit is None:
            unknown.append(uid or source or "(空行)")
            continue

        if target is None:
            skipped_empty += 1
            continue
        target = str(target)
        changed = hit.target != target
        hit.target = target

        new_status = _parse_status(status)
        if new_status is not None:
            hit.status = new_status
        elif changed:
            # 只有在**译文确实变了**的时候才按内容推断状态。
            # 否则导入一份 target 列大部分为空的清单，会把已经翻好的条目
            # 全部打回 pending —— 这种"静默回退"最难排查。
            hit.status = EntryStatus.TRANSLATED if target.strip() else EntryStatus.PENDING
        if changed:
            hit.updated_at = time.time()
        updated += 1

    ws.save_entries(entries)

    console.print(
        f"[green]✅ 导入完成[/green]  更新 [bold]{updated}[/bold] 条"
        + (f"，新建 [bold]{created}[/bold] 条" if created else "")
        + f"，未知 uid/原文 [bold]{len(unknown)}[/bold] 条"
        + (f"，跳过空行 {skipped_empty} 条" if skipped_empty else "")
    )
    if unknown:
        preview = "、".join(str(u)[:40] for u in unknown[:8])
        console.print(f"[yellow]未知条目前几个：{preview}"
                      f"{' …' if len(unknown) > 8 else ''}[/yellow]")
        console.print(
            "[yellow]提示：未知条目通常是源文件换过版本导致 uid 变化，"
            "或导入文件来自另一个项目。[/yellow]"
        )
    if not updated:
        raise typer.Exit(code=1)


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法确定文件编码（试过 utf-8 / gbk）")

    reader = csv.DictReader(text.splitlines())
    fields = [f.strip().lower() for f in (reader.fieldnames or [])]
    if "uid" not in fields and "source" not in fields:
        raise ValueError(f"CSV 需要有 uid 或 source 列，实际列：{fields}")
    out: list[dict[str, Any]] = []
    for raw in reader:
        row = {(k or "").strip().lower(): v for k, v in raw.items()}
        out.append(row)
    return out


def _read_json_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # 允许 {"uid": "译文"} 这种最简映射
        if all(isinstance(v, str) for v in data.values()):
            return [{"uid": k, "target": v} for k, v in data.items()]
        data = data.get("entries") or data.get("translations") or []
    if not isinstance(data, list):
        raise ValueError("JSON 顶层需要是数组，或 {uid: 译文} 的对象")
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        out.append({str(k).lower(): v for k, v in item.items()})
    return out


def _read_po_rows(path: Path) -> list[dict[str, Any]]:
    """极简 PO 解析：只认 msgid / msgstr / #: 引用，够导入自己导出的文件。"""
    rows: list[dict[str, Any]] = []
    uid = ""
    source = ""
    target = ""
    state = ""

    def flush() -> None:
        nonlocal uid, source, target, state
        if uid or source:
            rows.append({"uid": uid, "source": source, "target": target})
        uid, source, target, state = "", "", "", ""

    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#:"):
            ref = s[2:].strip()
            # 我们导出时会把 uid 作为额外引用写进去
            if ref.startswith("uid="):
                uid = ref[4:].strip()
            elif not uid:
                uid = ref
            continue
        if s.startswith("#"):
            continue
        if not s:
            flush()
            continue
        if s.startswith("msgid"):
            source = _po_unquote(s[len("msgid"):])
            state = "id"
        elif s.startswith("msgstr"):
            target = _po_unquote(s[len("msgstr"):])
            state = "str"
        elif s.startswith('"') and state == "id":
            source += _po_unquote(s)
        elif s.startswith('"') and state == "str":
            target += _po_unquote(s)
    flush()
    return [r for r in rows if r["uid"] or r["source"]]


def _parse_status(value: str) -> EntryStatus | None:
    """把状态字符串解析成枚举；不认识就返回 None（由调用方按内容推断）。"""
    if not value:
        return None
    try:
        return EntryStatus(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# fonts：目录与审计
# --------------------------------------------------------------------------


@fonts_app.command("list")
def fonts_list(
    redistributable: bool = typer.Option(
        False, "--redistributable", "-r", help="只列出允许下载/随包分发的字体。"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """列出字体目录（许可、是否可分发、体积）。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    from .fonts import catalog  # noqa: PLC0415

    specs = catalog.redistributable() if redistributable else list(catalog.CATALOG)

    rows = [
        {
            "id": s.id,
            "family": s.family,
            "display_zh": s.display_zh,
            "license": s.license,
            "bundle_ok": s.bundle_ok,
            "approx_mb": s.approx_mb,
            "coverage": s.coverage,
            "use_cases": list(s.use_cases),
            "direct_url": bool(s.direct_url),
            "homepage": s.homepage,
        }
        for s in specs
    ]
    if json_output:
        _echo_json(rows)
        return

    title = "字体目录（仅可再分发）" if redistributable else f"字体目录（全部 {len(rows)} 个）"
    t = Table(title=title, box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("id", style="bold cyan", no_wrap=True)
    t.add_column("字体家族", overflow="fold")
    t.add_column("许可", no_wrap=True)
    t.add_column("可分发", no_wrap=True, justify="center")
    t.add_column("体积", justify="right", no_wrap=True)
    t.add_column("直链", no_wrap=True, justify="center")
    for s in specs:
        t.add_row(
            s.id,
            f"{s.display_zh}\n[dim]{s.family}[/dim]",
            s.license,
            "[green]✅[/green]" if (s.bundle_ok and s.license in catalog.BUNDLE_OK_LICENSES) else "[yellow]—[/yellow]",
            f"{s.approx_mb:.1f} MB",
            "✅" if s.direct_url else "—",
        )
    console.print(t)
    console.print(
        "[dim]「可分发」= 许可为 OFL/Apache/MIT/公有领域**且**登记为 bundle_ok；"
        "其余字体只允许在本机下载与渲染，不随安装包分发。[/dim]"
    )


@fonts_app.command("audit")
def fonts_audit(
    project_id: str = typer.Argument(..., help="项目 id。"),
    limit: int = typer.Option(60, "--limit", help="每个字体最多打印多少个缺字。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """用项目实际字符集审计游戏自带字体，打印覆盖率与缺字。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    from .engines import get_adapter  # noqa: PLC0415
    from .fonts.service import FontService  # noqa: PLC0415

    ws = _open_ws(project_id)
    ctx = make_ctx(workspace=ws)
    fs = FontService(ctx)

    units = ws.load_units()
    entries = ws.load_entries()
    translated = [e.target for e in entries if e.target.strip()]
    # 译文为空的条目：UI 仍会显示原文，那些字符同样需要覆盖
    translated += [e.source for e in entries if not e.target.strip() and e.source.strip()]
    source_texts = [u.source for u in units]
    charset = fs.build_charset(translated, source_texts)

    engine_id = ws.project.engine
    if not engine_id:
        from .engines import detect_engine  # noqa: PLC0415

        info = detect_engine(ws.source_dir, ctx)
        engine_id = info.engine_id
        ws.project.engine = engine_id
        ws.project.engine_version = info.version
        ws.save()

    adapter = get_adapter(engine_id, ctx)
    if adapter is None:
        _fail(f"没有 id 为 {engine_id!r} 的引擎适配器，无法发现游戏字体。")

    game_fonts = adapter.discover_fonts(ws.source_dir)
    audits: list[dict[str, Any]] = []
    for gf in game_fonts:
        fp = ws.source_dir / gf.path
        if not fp.is_file():
            audits.append({"font_id": gf.font_id, "path": gf.path, "ok": False, "error": "文件不存在"})
            continue
        try:
            a = fs.audit(fp, charset)
        except Exception as exc:  # noqa: BLE001
            audits.append({"font_id": gf.font_id, "path": gf.path, "ok": False, "error": str(exc)})
            continue
        audits.append({
            "font_id": gf.font_id,
            "path": gf.path,
            "family": a.family,
            "ok": a.ok,
            "total": a.total,
            "covered": a.covered,
            "coverage": round(a.coverage, 6),
            "missing_count": len(a.missing),
            "missing": a.missing,
        })

    payload = {
        "project": ws.project.id,
        "engine": engine_id,
        "charset_total": len(set(charset)),
        "game_fonts": len(game_fonts),
        "audits": audits,
    }
    if json_output:
        _echo_json(payload)
        return

    console.print(
        f"项目字符集：[bold]{len(set(charset))}[/bold] 个字（译文 {len(translated)} 段 + 原文 {len(source_texts)} 段）"
    )
    if not game_fonts:
        console.print(
            Panel(
                "这个引擎没有发现可替换的字体文件。界面文字将走系统/内置字体，"
                "贴图重绘仍使用本地中文字体渲染。",
                border_style="yellow",
                expand=False,
            )
        )
        return

    t = Table(title="游戏字体覆盖审计", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("字体", style="bold cyan", overflow="fold")
    t.add_column("文件", overflow="fold")
    t.add_column("覆盖率", justify="right", no_wrap=True)
    t.add_column("覆盖/总数", justify="right", no_wrap=True)
    t.add_column("缺字", justify="right", no_wrap=True)
    for a in audits:
        if not a.get("ok") and "error" in a:
            t.add_row(a["font_id"], a["path"], "[red]审计失败[/red]", "-", "-")
            continue
        cov = float(a.get("coverage", 0.0))
        color = "green" if cov >= 0.999 else ("yellow" if cov >= 0.95 else "red")
        t.add_row(
            a.get("family") or a["font_id"],
            a["path"],
            f"[{color}]{cov:.3%}[/{color}]",
            f"{a.get('covered', 0)}/{a.get('total', 0)}",
            str(a.get("missing_count", 0)),
        )
    console.print(t)

    for a in audits:
        if a.get("error"):
            console.print(f"[red]✗ {a['path']}：{a['error']}[/red]")
            continue
        missing = a.get("missing") or ""
        if missing:
            shown = missing[:limit]
            console.print(
                f"[yellow]{a['path']} 缺 {len(missing)} 字：[/yellow]{shown}"
                + (f" [dim]…另有 {len(missing) - len(shown)} 个[/dim]" if len(missing) > len(shown) else "")
            )

    bad = [a for a in audits if a.get("missing_count")]
    if bad:
        console.print(
            f"[yellow]⚠ {len(bad)} 个字体缺字。跑 `novaloc run {ws.project.id} --stage fonts` "
            "会把中文字形注入原字体（merge 策略）。[/yellow]"
        )
    else:
        console.print("[green]✅ 所有游戏字体都覆盖了项目字符集。[/green]")


# --------------------------------------------------------------------------
# qa：质检
# --------------------------------------------------------------------------


@app.command("qa")
def qa(
    project_id: str = typer.Argument(..., help="项目 id。"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
    limit: int = typer.Option(50, "--limit", help="最多打印多少条问题。"),
) -> None:
    """对项目做质检（占位符、漏译、长度、术语一致性、字体缺字、贴图）。通过返回 0，否则 1。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    ws = _open_ws(project_id)
    ctx = make_ctx(workspace=ws)
    report = run_qa(ws, ctx)

    if json_output:
        _echo_json(report)
        raise typer.Exit(code=0 if report.get("ok") else 1)

    stats = report.get("stats", {})
    ok = bool(report.get("ok"))
    t = Table(title=f"质检报告 · {ws.project.id}", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("指标", style="bold cyan", no_wrap=True)
    t.add_column("值", justify="right")
    t.add_row("文本条目", str(stats.get("entries", 0)))
    t.add_row("已有译文", str(stats.get("translated", 0)))
    t.add_row("缺译文", str(stats.get("missing", 0)))
    t.add_row("错误", f"[red]{stats.get('errors', 0)}[/red]" if stats.get("errors") else "0")
    t.add_row("警告", f"[yellow]{stats.get('warnings', 0)}[/yellow]" if stats.get("warnings") else "0")
    t.add_row("同源多译", str(stats.get("inconsistent", 0)))
    t.add_row("项目字符集", str(stats.get("charset", 0)))
    t.add_row(
        "字体缺字",
        f"[red]{stats.get('font_missing', 0)}[/red]" if stats.get("font_missing") else "0",
    )
    t.add_row("贴图记录", str(stats.get("images", 0)))
    t.add_row("贴图待复核", str(stats.get("images_review", 0)))
    console.print(t)

    issues = report.get("issues", [])
    if issues:
        it = Table(title=f"问题清单（{len(issues)} 条）", box=box.SIMPLE, title_justify="left")
        it.add_column("级别", no_wrap=True)
        it.add_column("阶段", no_wrap=True)
        it.add_column("说明", overflow="fold")
        for issue in issues[:limit]:
            sev = issue.get("severity", "info")
            style = {"error": "red", "warn": "yellow"}.get(sev, "dim")
            it.add_row(f"[{style}]{sev}[/{style}]", issue.get("stage", ""), _short(issue.get("message", ""), 100))
        console.print(it)
        if len(issues) > limit:
            console.print(f"[dim]…另有 {len(issues) - limit} 条未显示（用 --limit 调整）。[/dim]")

    if ok:
        console.print("[green]✅ 质检通过[/green]")
    else:
        console.print("[red]✗ 质检未通过[/red] —— 有 error 级问题，回写前必须修掉。")
    raise typer.Exit(code=0 if ok else 1)


# --------------------------------------------------------------------------
# serve：起 Web 后端
# --------------------------------------------------------------------------


@app.command("serve")
def serve(
    host: str = typer.Option("", "--host", help="监听地址；留空用配置里的值（默认 127.0.0.1）。"),
    port: int = typer.Option(0, "--port", help="监听端口；0 表示用配置里的值。"),
    reload: bool = typer.Option(False, "--reload", help="开发模式：代码变动自动重启。"),
) -> None:
    """启动 Web 界面（FastAPI + uvicorn）。"""
    cfg = load_config()
    bind_host = host or cfg.ui.host
    bind_port = int(port or cfg.ui.port)

    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        _fail(f"缺少 uvicorn，无法启动 Web 服务：{exc}\n请先 pip install 'uvicorn[standard]'。")

    # 惰性导入：其它命令不该因为后端缺依赖而挂掉
    try:
        from .api.app import create_app  # noqa: PLC0415
    except ImportError as exc:
        console.print(
            Panel(
                "后端尚未就绪：导入 novaloc.api.app 失败。\n"
                f"原因：{exc}\n\n"
                "Web 层仍在开发中。CLI 的抽取、翻译、字体、质检、回写功能不受影响，"
                "可以直接用 `novaloc run <id>` 跑完整流程。",
                title="[yellow]后端尚未就绪[/yellow]",
                border_style="yellow",
                expand=False,
            )
        )
        raise typer.Exit(code=1) from None

    try:
        application = create_app()
    except Exception as exc:  # noqa: BLE001
        _fail(f"后端初始化失败：{exc}")

    url = f"http://{bind_host}:{bind_port}"
    console.print(f"[green]NovaLoc 新译 Web 界面：[/green]{url}  [dim](Ctrl+C 停止)[/dim]")
    if bind_host in ("127.0.0.1", "localhost") and cfg.ui.open_browser:
        try:
            import webbrowser  # noqa: PLC0415

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        uvicorn.run(application, host=bind_host, port=bind_port, reload=reload, log_level="info")
    except KeyboardInterrupt:
        console.print("\n[yellow]已停止。[/yellow]")


# --------------------------------------------------------------------------
# ollama：本地推理服务
# --------------------------------------------------------------------------


@ollama_app.command("status")
def ollama_status(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读的 JSON。"),
) -> None:
    """检查 Ollama 是否安装/运行，以及推荐模型是否就位。"""
    global _JSON_MODE
    _JSON_MODE = json_output

    from .translate import ollama_setup  # noqa: PLC0415

    cfg = load_config()
    base_url = cfg.resolved_ollama_host()
    snap = ollama_setup.status_snapshot(base_url)

    if json_output:
        _echo_json(snap)
        return

    t = Table(title="Ollama 状态", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("项目", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    t.add_row("可执行文件", snap["executable"] or "[yellow]（未找到）[/yellow]")
    t.add_row("服务地址", snap["base_url"])
    t.add_row(
        "服务状态",
        f"[green]✅ 在线[/green]（v{snap['version']}）" if snap["running"] else "[red]✗ 未运行[/red]",
    )
    t.add_row(
        "模型目录",
        snap["models_root"] or "[yellow]未设置 OLLAMA_MODELS（会落在 C 盘）[/yellow]",
    )
    console.print(t)

    rec = Table(title="推荐模型", box=box.SIMPLE, title_justify="left")
    rec.add_column("模型", style="bold cyan", no_wrap=True)
    rec.add_column("状态", no_wrap=True)
    rec.add_column("说明", overflow="fold")
    for model, note in ollama_setup.RECOMMENDED_MODELS:
        have = bool(snap["recommended"].get(model))
        rec.add_row(model, "[green]✅ 已装[/green]" if have else "[yellow]⬜ 未装[/yellow]", note)
    console.print(rec)

    installed = snap["models"]
    if installed:
        it = Table(title="本机已安装模型", box=box.SIMPLE, title_justify="left")
        it.add_column("模型", style="bold cyan")
        it.add_column("体积", justify="right")
        for m in installed:
            gb = snap["model_sizes_gb"].get(m, 0.0)
            it.add_row(m, f"{gb:.2f} GB" if gb else "-")
        console.print(it)
    elif snap["running"]:
        console.print("[yellow]本机还没有任何模型。[/yellow]")

    if not snap["running"]:
        console.print(Panel(ollama_setup.install_hint(), title="[yellow]如何装/起 Ollama[/yellow]", border_style="yellow", expand=False))
        if snap["executable"]:
            console.print(f"已找到可执行文件，试试直接运行：[bold]{snap['executable']} serve[/bold]")
    else:
        missing = [m for m, ok in snap["recommended"].items() if not ok]
        if missing:
            console.print(
                f"拉取推荐模型：[bold]novaloc ollama pull {' '.join(missing)}[/bold]"
                "（不带参数则拉全部推荐的）"
            )


@ollama_app.command("pull")
def ollama_pull(
    models: list[str] = typer.Argument(None, help="要拉取的模型名；留空则拉取全部推荐模型。"),
    base_url: str = typer.Option("", "--base-url", help="Ollama 地址；留空用配置里的值。"),
) -> None:
    """拉取模型（默认拉取全部推荐模型）。"""
    from .translate import ollama_setup  # noqa: PLC0415

    cfg = load_config()
    url = base_url or cfg.resolved_ollama_host()

    exe = ollama_setup.find_ollama()
    if not ollama_setup.is_running(url):
        console.print(Panel(ollama_setup.install_hint(), title="[yellow]Ollama 未运行[/yellow]", border_style="yellow", expand=False))
        if exe:
            console.print(f"已找到：[bold]{exe}[/bold]，先执行 `ollama serve` 再回来。")
        raise typer.Exit(code=1)

    todo = list(models) if models else ollama_setup.recommended_names()
    if not todo:
        console.print("[yellow]没有指定模型。[/yellow]")
        raise typer.Exit(code=1)

    already = set(ollama_setup.list_models(url))
    results: dict[str, bool] = {}

    for model in todo:
        if model in already:
            console.print(f"[green]✅ {model} 已经装好，跳过[/green]")
            results[model] = True
            continue

        console.print(f"[bold]开始拉取 {model}[/bold]")
        total_mb = 0.0
        last_line = ""
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("{task.percentage:>5.1f}%"),
            console=console,
            transient=False,
        ) as prog:
            tid = prog.add_task(model, total=100.0)

            def on_progress(obj: dict[str, Any], _tid: Any = tid) -> None:
                nonlocal total_mb, last_line
                if obj.get("error"):
                    prog.update(_tid, description=f"[red]{model} 失败[/red]")
                    return
                if obj.get("status") is None:
                    return
                total = float(obj.get("total") or 0)
                done = float(obj.get("completed") or 0)
                if total:
                    total_mb = total / 1048576
                    prog.update(_tid, completed=min(100.0, done / total * 100))
                line = ollama_setup.format_pull_line(obj)
                if line != last_line:
                    last_line = line
                    prog.update(_tid, description=f"{model} · {line[:70]}")

            ok = ollama_setup.pull_model(model, url, on_progress=on_progress)
            prog.update(tid, completed=100.0 if ok else 0.0)

        results[model] = ok
        if ok:
            console.print(f"[green]✅ {model} 拉取完成[/green]" + (f"（约 {total_mb:.0f} MB）" if total_mb else ""))
        else:
            console.print(f"[red]✗ {model} 拉取失败[/red]")

    ok_models = [m for m, ok in results.items() if ok]
    bad = [m for m, ok in results.items() if not ok]
    console.print(f"\n完成 {len(ok_models)}/{len(results)} 个模型。")
    if bad:
        console.print(f"[red]失败：{', '.join(bad)}[/red]")
        console.print("常见原因：模型名拼错、磁盘空间不足、网络中断。可以先手动验证：`ollama pull <模型>`。")
        raise typer.Exit(code=1)


@ollama_app.command("env")
def ollama_env() -> None:
    """打印把模型重定向到数据盘所需的环境变量。"""
    from .translate import ollama_setup  # noqa: PLC0415

    env = ollama_setup.recommended_env(paths.data_root())
    t = Table(title="建议设置的环境变量", box=box.SIMPLE_HEAVY, title_justify="left")
    t.add_column("变量", style="bold cyan", no_wrap=True)
    t.add_column("值", overflow="fold")
    for k, v in env.items():
        t.add_row(k, v)
    console.print(t)
    console.print("一次性设置（需重开终端）：")
    for k, v in env.items():
        console.print(f'    setx {k} "{v}"')


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def run_app() -> None:
    """供 ``novaloc`` 控制台脚本与 ``python -m novaloc.cli`` 共用。"""
    app()


if __name__ == "__main__":
    run_app()
