"""FastAPI 应用：把流水线暴露成 HTTP + WebSocket 接口。

前端契约见 ``web/src/api.ts``（由前端并行开发，两边必须一致）。
静态文件服务 ``novaloc/web_dist``（构建产物**随包分发**），
所以构建后的前端由本服务直接托管，用户不需要装 Node。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.events import Event, EventBus
from ..core.registry import Context
from ..core.workspace import Workspace
from ..engines import available_engines, detect_engine
from ..models import EntryStatus, TranslationEntry
from ..pipeline import STAGE_LABELS, STAGES, Pipeline
from .jobs import Job, JobManager, load_config, save_config

log = logging.getLogger(__name__)

# **唯一版本来源**。以前这里硬写 "0.1.0"，而 pyproject.toml 与
# ``novaloc/__init__.py`` 各自也写了一份 —— 三处手工同步必然漂移：
# 发版时包版本升到 0.2.0，``/api/health`` 却仍然报 0.1.0，
# 前端"关于"里显示的还是旧号。测试
# ``test_version_is_single_source`` 锁住这一点。
from .. import __version__ as VERSION  # noqa: E402

# --------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    game_dir: str
    engine: str = ""
    target_lang: str = "zh-Hans"


class TextPatch(BaseModel):
    uid: str
    target: str | None = None
    status: str | None = None


class TextPatchBody(BaseModel):
    items: list[TextPatch] = Field(default_factory=list)


class ImagePatch(BaseModel):
    uid: str
    block_id: str
    target: str


class ImagePatchBody(BaseModel):
    items: list[ImagePatch] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="NovaLoc 新译", version=VERSION, docs_url="/api/docs")

    # 本地工具：允许任意来源，方便前端 dev server（vite 5173）直连
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cfg = load_config()
    bus = EventBus()
    ctx = Context(config=cfg, events=bus)
    jobs = JobManager(ctx)

    app.state.ctx = ctx
    app.state.jobs = jobs
    app.state.bus = bus

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def get_ws(pid: str) -> Workspace:
        try:
            return Workspace.open(pid)
        except FileNotFoundError as exc:
            raise HTTPException(404, f"项目不存在：{pid}") from exc

    def make_pipeline(ws: Workspace, job_bus: EventBus) -> Pipeline:
        """给任务一个独立的 Context/EventBus，这样进度只推给该任务的 WS。"""
        task_ctx = Context(config=ctx.config, events=job_bus)
        return Pipeline(ws, task_ctx)

    def run_stage_job(pid: str, stage: str | None, **kw: Any) -> Job:
        """通用：把某个阶段（或整条流水线）放进后台任务。"""

        def fn(job_bus: EventBus, job: Job) -> dict[str, Any]:
            ws = Workspace.open(pid)
            pipe = make_pipeline(ws, job_bus)
            job_bus.log(f"准备执行：{STAGE_LABELS.get(stage or '', '完整流水线')}", stage="pipeline")
            if stage is None:
                results = pipe.run_all(only_pending=kw.get("only_pending", True))
            else:
                fn_map = {
                    "detect": lambda: pipe.stage_detect(),
                    "extract": lambda: pipe.stage_extract(),
                    "images_scan": lambda: pipe.stage_images_scan(),
                    "translate": lambda: pipe.stage_translate(
                        only_pending=kw.get("only_pending", True)
                    ),
                    "fonts": lambda: pipe.stage_fonts(force=kw.get("force", False)),
                    "images_localize": lambda: pipe.stage_images_localize(
                        force=kw.get("force", False)
                    ),
                    "qa": lambda: pipe.stage_qa(),
                    "apply": lambda: pipe.stage_apply(),
                }
                if stage not in fn_map:
                    raise ValueError(f"未知阶段：{stage}")
                results = [fn_map[stage]()]
            return {"stages": [r.to_dict() for r in results]}

        return jobs.submit(fn, project_id=pid, kind=stage or "run_all")

    # ------------------------------------------------------------------
    # 健康检查 / 设置
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        directml = False
        gpu = ""
        try:
            import onnxruntime as ort

            providers = ort.get_available_providers()
            directml = "DmlExecutionProvider" in providers
        except Exception:  # noqa: BLE001
            providers = []

        # Ollama 状态
        ollama: dict[str, Any] = {"available": False, "base_url": "", "models": []}
        try:
            from ..translate.ollama_setup import is_running, list_models

            base = getattr(ctx.config.translate, "base_url", "http://127.0.0.1:11434")
            ollama["base_url"] = base
            if is_running(base):
                ollama["available"] = True
                ollama["models"] = list_models(base)
        except Exception as exc:  # noqa: BLE001
            ollama["error"] = str(exc)

        try:
            import torch  # noqa: F401

            gpu = "torch 可用"
        except Exception:  # noqa: BLE001
            gpu = ""

        return {
            "ok": True,
            "version": VERSION,
            "ollama": ollama,
            "gpu": {"directml": directml, "name": gpu, "providers": providers},
            "engines": [{"id": i, "display_name": n} for i, n in available_engines()],
            "stages": [{"id": s, "label": STAGE_LABELS[s]} for s, _ in STAGES],
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return ctx.config.model_dump()

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """更新设置。

        两个刻意的设计：

        1. **深层合并**：前端只发 ``{"translate": {"target_lang": "zh-Hant"}}``
           时不能把 translate 段其它字段冲掉。浅合并会静默重置用户的
           provider / memory 等设置，这类 bug 很难被发现。
        2. **拒绝未知键**：pydantic 默认 ``extra="ignore"``，所以
           ``{"translate": {"temperture": 0.3}}``（拼错）会**静默成功**，
           用户以为改好了其实没生效。这里显式报 400。
        """
        current = ctx.config.model_dump()
        unknown_top = [k for k in payload if k not in current]
        if unknown_top:
            raise HTTPException(
                400, f"未知的设置项：{', '.join(unknown_top)}"
            )

        merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in current.items()}
        bad_keys: list[str] = []
        for key, val in payload.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                sub_known = set(merged[key])
                bad = [k for k in val if k not in sub_known]
                if bad:
                    bad_keys.extend(f"{key}.{b}" for b in bad)
                    continue
                merged[key].update(val)
            else:
                merged[key] = val

        if bad_keys:
            raise HTTPException(400, f"未知的设置项：{', '.join(bad_keys)}")

        try:
            new_cfg = type(ctx.config).model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"设置不合法：{exc}") from exc

        ctx.config = new_cfg
        app.state.ctx.config = new_cfg
        save_config(new_cfg)
        return {"ok": True, "settings": new_cfg.model_dump()}

    # ------------------------------------------------------------------
    # 项目
    # ------------------------------------------------------------------

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        out = []
        for p in Workspace.list():
            out.append({
                "id": p.id,
                "name": p.name,
                "game_dir": p.game_dir,
                "engine": p.engine,
                "engine_version": p.engine_version,
                "target_lang": p.target_lang,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
                "stage": p.stage,
            })
        return out

    @app.post("/api/projects")
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        src = Path(body.game_dir).expanduser()
        if not src.exists():
            raise HTTPException(400, f"游戏目录不存在：{src}")
        if not src.is_dir():
            raise HTTPException(400, f"不是目录：{src}")
        try:
            ws = Workspace.create(
                body.name, src, target_lang=body.target_lang or "zh-Hans"
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"创建项目失败：{exc}") from exc

        # 引擎：没指定就自动识别
        engine = body.engine
        if not engine:
            try:
                info = detect_engine(ws.source_dir, ctx)
                engine = info.engine_id
                ws.project.engine = info.engine_id
                ws.project.engine_version = info.version
                ws.save()
            except Exception as exc:  # noqa: BLE001
                log.warning("自动识别引擎失败：%s", exc)

        return {
            "id": ws.project.id, "name": ws.project.name,
            "game_dir": ws.project.game_dir, "engine": engine,
            "engine_version": ws.project.engine_version,
            "target_lang": ws.project.target_lang,
            "created_at": ws.project.created_at, "updated_at": ws.project.updated_at,
        }

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        p = ws.project
        # 前端头部要显示"当前跑到哪一步"。取最近一个该项目的任务，
        # 没有就跑过就返回 None，前端会显示"暂无流水线记录"。
        recent = jobs.list(project_id=pid, limit=1)
        progress = None
        if recent:
            j = recent[0]
            total = len(STAGES)
            done = 0
            if j.status == "done":
                done = total
            elif j.stage in STAGE_LABELS:
                done = [s for s, _ in STAGES].index(j.stage)
            progress = {
                "stage": j.stage,
                "stage_label": STAGE_LABELS.get(j.stage, j.stage),
                "status": j.status,
                "progress": j.progress,
                "done_stages": done,
                "total_stages": total,
                "message": j.message or j.error,
                "job_id": j.id,
            }
        return {
            "id": p.id, "name": p.name, "game_dir": p.game_dir,
            "engine": p.engine, "engine_version": p.engine_version,
            "target_lang": p.target_lang, "created_at": p.created_at,
            "updated_at": p.updated_at, "stage": p.stage,
            "progress": progress,
            "paths": {
                "out_dir": str(ws.out_dir),
                "workspace": str(ws.root),
            },
        }

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        ws.delete()
        return {"ok": True}

    # ------------------------------------------------------------------
    # 操作（都是后台任务）
    # ------------------------------------------------------------------

    @app.post("/api/projects/{pid}/detect")
    def do_detect(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        info = detect_engine(ws.source_dir, ctx)
        ws.project.engine = info.engine_id
        ws.project.engine_version = info.version
        ws.save()
        return {
            "engine_id": info.engine_id,
            "display_name": info.display_name,
            "confidence": info.confidence,
            "version": info.version,
            "evidence": info.evidence,
        }

    @app.post("/api/projects/{pid}/extract")
    def do_extract(pid: str) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "extract").id}

    @app.post("/api/projects/{pid}/translate")
    def do_translate(pid: str, only_pending: bool = True) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "translate", only_pending=only_pending).id}

    @app.post("/api/projects/{pid}/fonts")
    def do_fonts(pid: str, force: bool = False) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "fonts", force=force).id}

    @app.post("/api/projects/{pid}/images")
    def do_images(pid: str, force: bool = False) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "images_localize", force=force).id}

    @app.post("/api/projects/{pid}/qa")
    def do_qa(pid: str) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "qa").id}

    @app.post("/api/projects/{pid}/apply")
    def do_apply(pid: str) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, "apply").id}

    @app.post("/api/projects/{pid}/run")
    def do_run(pid: str, only_pending: bool = True) -> dict[str, Any]:
        get_ws(pid)
        return {"job_id": run_stage_job(pid, None, only_pending=only_pending).id}

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------

    @app.get("/api/projects/{pid}/text")
    def get_text(
        pid: str,
        status: str = Query(""),
        kind: str = Query(""),
        q: str = Query(""),
        limit: int = Query(0, ge=0),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        ws = get_ws(pid)
        units = {u.uid: u for u in ws.load_units()}
        entries = {e.uid: e for e in ws.load_entries()}

        # 先把"抽出的单元"和"已有译文"合并成一份完整视图，
        # 再做过滤。早先是先过滤 entries、再把没有译文的单元补到末尾，
        # 结果补进来的条目**绕过了所有过滤条件**（按 kind 筛选却混进
        # 别的 kind、搜索关键词也被忽略）。必须统一在一份数据上过滤。
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for uid, u in units.items():
            if not u.source.strip():
                continue
            e = entries.get(uid)
            seen.add(uid)
            rows.append({
                "uid": uid,
                "source": e.source if e else u.source,
                "target": e.target if e else "",
                "status": (e.status.value if e else EntryStatus.PENDING.value),
                "kind": (e.kind.value if e else u.kind.value),
                "speaker": (u.speaker or ""),
                "warnings": list(e.warnings) if e else [],
                "placeholder_ok": e.placeholder_ok if e else True,
                "file": u.location.file,
                "pointer": u.location.pointer,
            })
        # entries 里可能有单元表里没有的（比如引擎换了之后残留），也带上
        for uid, e in entries.items():
            if uid in seen:
                continue
            u = units.get(uid)
            rows.append({
                "uid": uid,
                "source": e.source,
                "target": e.target,
                "status": e.status.value,
                "kind": e.kind.value,
                "speaker": (u.speaker if u else "") or "",
                "warnings": list(e.warnings),
                "placeholder_ok": e.placeholder_ok,
                "file": u.location.file if u else "",
                "pointer": u.location.pointer if u else "",
            })

        if status:
            rows = [r for r in rows if r["status"] == status]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in (r["source"] + r["target"]).lower()]

        total = len(rows)
        if limit:
            rows = rows[offset : offset + limit]
        return {"total": total, "entries": rows}

    @app.patch("/api/projects/{pid}/text")
    def patch_text(pid: str, body: TextPatchBody) -> dict[str, Any]:
        ws = get_ws(pid)
        entries = {e.uid: e for e in ws.load_entries()}
        units = {u.uid: u for u in ws.load_units()}
        updated = 0
        for item in body.items:
            e = entries.get(item.uid)
            if e is None:
                u = units.get(item.uid)
                if u is None:
                    continue
                e = TranslationEntry(
                    uid=u.uid, source=u.source, kind=u.kind,
                    provider="manual", model="manual",
                )
                entries[e.uid] = e
            if item.target is not None:
                e.target = item.target
                if e.status in (EntryStatus.PENDING, EntryStatus.FAILED):
                    e.status = EntryStatus.TRANSLATED
            if item.status:
                try:
                    e.status = EntryStatus(item.status)
                except ValueError:
                    raise HTTPException(400, f"未知状态：{item.status}") from None
            # 人工改过的条目要标记，避免下次流水线覆盖
            e.meta = {**(e.meta or {}), "manual": True}
            updated += 1
        ws.save_entries(list(entries.values()))
        return {"updated": updated}

    # ------------------------------------------------------------------
    # 贴图
    # ------------------------------------------------------------------

    @app.get("/api/projects/{pid}/images")
    def get_images(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        images = ws.load_images()
        return {
            "total": len(images),
            "images": [
                {
                    "uid": a.uid,
                    "path": a.path,
                    "width": a.width,
                    "height": a.height,
                    "analyzed": a.analyzed,
                    "ocr_engine": a.ocr_engine,
                    "warnings": list(a.warnings),
                    "blocks": [
                        {
                            "id": b.id,
                            "box": list(b.box),
                            "source": b.source,
                            "target": b.target,
                            "status": b.status.value,
                            "confidence": b.confidence,
                            "warnings": list(b.warnings),
                        }
                        for b in a.blocks
                    ],
                }
                for a in images
            ],
        }

    @app.patch("/api/projects/{pid}/images")
    def patch_images(pid: str, body: ImagePatchBody) -> dict[str, Any]:
        ws = get_ws(pid)
        images = ws.load_images()
        by_uid = {a.uid: a for a in images}
        updated = 0
        for item in body.items:
            a = by_uid.get(item.uid)
            if a is None:
                continue
            for b in a.blocks:
                if b.id == item.block_id:
                    b.target = item.target
                    b.status = EntryStatus.TRANSLATED
                    updated += 1
                    break
        if updated:
            ws.save_images(images)
        return {"updated": updated}

    @app.get("/api/projects/{pid}/images/{uid}/annotated")
    def get_annotated(pid: str, uid: str) -> FileResponse:
        """把该贴图的文字框画出来，供人工复核。"""
        ws = get_ws(pid)
        asset = next((a for a in ws.load_images() if a.uid == uid), None)
        if asset is None:
            raise HTTPException(404, f"没有这个贴图：{uid}")
        src = ws.source_dir / asset.path
        if not src.is_file():
            raise HTTPException(404, f"源贴图不存在：{asset.path}")

        out_dir = ws.p("images", "analyzed")
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{uid}.png"

        from ..images.annotate import annotate_blocks

        try:
            annotate_blocks(src, asset.blocks, dst)
        except Exception as exc:  # noqa: BLE001
            log.warning("标注图生成失败，回退到原图：%s", exc)
            return FileResponse(src)
        return FileResponse(dst)

    # ------------------------------------------------------------------
    # 字体 / 质检
    # ------------------------------------------------------------------

    @app.get("/api/projects/{pid}/fonts")
    def get_fonts(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        coverage = ws.load_font_coverage()
        patches = ws.read_json("fonts/patches.json", []) or []
        cs = ws.load_charset()
        return {
            "coverage": [
                {
                    "font_id": c.font_id, "path": c.path, "family": c.family,
                    "coverage_ratio": c.coverage_ratio,
                    "missing_count": c.missing_count,
                    "missing": c.missing[:200],
                    "num_glyphs": c.num_glyphs,
                    "is_game_font": c.is_game_font,
                }
                for c in coverage
            ],
            "patches": patches,
            "charset": (
                {"total": cs.total, "all_chars": "".join(cs.all_chars)} if cs else None
            ),
        }

    @app.get("/api/projects/{pid}/qa")
    def get_qa(pid: str) -> dict[str, Any]:
        ws = get_ws(pid)
        cached = ws.read_json("qa/report.json", None)
        if cached:
            return cached
        return {"ok": True, "issues": [], "stats": {}, "note": "还没有跑过质检"}

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"没有这个任务：{job_id}")
        return {**job.to_dict(), "events": job.events()}

    @app.get("/api/jobs")
    def list_jobs(project_id: str = "") -> list[dict[str, Any]]:
        return [j.to_dict() for j in jobs.list(project_id=project_id or None)]

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"没有这个任务：{job_id}")
        fut = getattr(job, "future", None)
        if fut is not None and not fut.done():
            # 线程跑起来就停不下来了（OCR/推理是原子的），
            # 只能标记为取消，让 UI 知道别等了
            fut.cancel()
            job.status = "cancelled"
            return {"ok": True, "note": "已请求取消（正在执行的推理无法中断）"}
        return {"ok": True, "note": "任务已结束"}

    # ------------------------------------------------------------------
    # WebSocket：任务进度
    # ------------------------------------------------------------------

    @app.websocket("/ws/jobs/{job_id}")
    async def ws_job(websocket: WebSocket, job_id: str) -> None:
        await websocket.accept()
        job = jobs.get(job_id)
        if job is None:
            await websocket.send_json({"kind": "error", "message": f"没有这个任务：{job_id}"})
            await websocket.close()
            return

        # 先把历史事件补发一遍：用户中途刷新页面也能看到完整进度
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(ev: Event) -> None:
            # EventBus 是同步的、跑在 worker 线程里，必须跨线程投递
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev.to_dict())
            except RuntimeError:
                pass  # 事件循环已关闭

        for ev in job.events():
            await websocket.send_json(ev)

        unsub = job.bus.subscribe(on_event)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    # 心跳 + 终态检测
                    if job.status in ("done", "failed", "cancelled"):
                        # 把剩余事件排空后再收尾
                        while not q.empty():
                            await websocket.send_json(q.get_nowait())
                        await websocket.send_json({**job.to_dict(), "kind": "closed"})
                        break
                    await websocket.send_json({"kind": "heartbeat", "status": job.status})
                    continue
                await websocket.send_json(item)
                if item.get("kind") == "done":
                    await websocket.send_json({**job.to_dict(), "kind": "closed"})
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("WebSocket 异常：%s", exc)
        finally:
            unsub()
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 静态前端
    # ------------------------------------------------------------------

    web_dist = _web_dist()
    if web_dist is not None:
        log.debug("前端静态资源目录：%s", web_dist)
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> Any:
            """SPA 回退：任何非 /api 路径都交给前端路由。"""
            if full_path.startswith(("api/", "ws/")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = web_dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    else:
        # **要吵一声**。以前这里是静默跳过：接口全都正常，只有根路径 404，
        # 用户看到"打不开页面"却没有任何线索。前端是随包分发的，
        # 真找不到基本就是安装不完整。
        log.warning(
            "找不到前端静态资源，Web 界面不可用（API 仍然正常）。"
            "已查找：包内 novaloc/web_dist、源码树 web/dist、$NOVALOC_WEB_DIST。"
            "若你是从源码运行，请先构建前端；若刚 pip 安装完，"
            "可能是安装包不完整，建议重装 novaloc。"
        )

    @app.on_event("shutdown")
    def _shutdown() -> None:
        jobs.shutdown()

    return app


def _web_dist() -> Path | None:
    """找构建好的前端，返回 ``index.html`` 所在目录；找不到返回 ``None``。

    **按这个顺序找**：

    1. ``novaloc/web_dist`` —— 构建产物**随包分发**（在 ``src/novaloc/web_dist``）。
       这是安装后的正常情况（``pip install`` / ``pip install -e .``）。
       放在**包内**是刻意的：早先放在仓库根的 ``web/dist``，
       hatchling 打包时只收 ``src/novaloc``，导致 wheel 里根本没有前端，
       ``pip install nova-loc`` 装出来的 GUI 是打不开的 —— 而
       GitHub 自动生成的 wheel 又不会报错，属于"静默残废"。
    2. 源码树里的 ``web/dist`` —— 兼容旧布局与手工构建到那里的人。
    3. ``NOVALOC_WEB_DIST`` 环境变量 —— 开发时指向别处。

    三条都没有也不算错误：开发时可能还没构建前端。
    此时只提供 API，``create_app`` 会打印一行提示。
    """
    here = Path(__file__).resolve()

    env = os.environ.get("NOVALOC_WEB_DIST")
    if env:
        d = Path(env)
        if (d / "index.html").is_file():
            return d

    # 1) 包内（安装后的正常情况）
    bundled = here.parents[1] / "web_dist"
    if (bundled / "index.html").is_file():
        return bundled

    # 2) 源码树 / 手工构建
    for base in (here.parents[3], here.parents[2]):
        d = base / "web" / "dist"
        if (d / "index.html").is_file():
            return d
    return None


_app: FastAPI | None = None


def get_app() -> FastAPI:
    """进程级单例，给 ``uvicorn ... novaloc.api.app:get_app`` 之类的入口用。"""
    global _app
    if _app is None:
        _app = create_app()
    return _app


def __getattr__(name: str) -> Any:
    """让 ``app`` 与 ``create_app`` 都能从模块上取到，**且不在模块里存同名变量**。

    为什么绕这一圈：模块末尾如果直接写 ``app = create_app()``，
    那个变量会**遮蔽同名的子模块** —— 于是

        import novaloc.api.app as m
        m._web_dist            # AttributeError: 'FastAPI' object has no attribute

    拿到的是 FastAPI 实例而不是模块。``from ... import`` 不受影响
    （import 机制会回退到 ``sys.modules``），所以这个坑只在
    ``import ... as`` 时炸，非常难查 —— 写测试时就正好踩到了。

    改用模块级 ``__getattr__``（PEP 562）后三者都正确：

    * ``import novaloc.api.app as m`` → 模块；
    * ``from novaloc.api.app import app`` → FastAPI 实例；
    * ``from novaloc.api.app import create_app`` → 工厂函数。

    顺带得到一个好处：``import novaloc.api.app`` 不再产生副作用地
    构造一个 App。直接 ``uvicorn novaloc.api.app:app`` 仍然可用。
    """
    if name == "app":
        return get_app()
    if name == "create_app":
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# `app` 由上面的模块级 __getattr__ 惰性提供，模块里**没有**这个全局变量。
# ruff 的 F822 只做静态检查、不认 PEP 562，所以这条必须豁免。
__all__ = ["app", "create_app", "get_app"]  # noqa: F822
