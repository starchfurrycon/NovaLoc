"""FastAPI 后端。

架构上的两个关键决定：

1. **流水线跑在后台线程里，不占事件循环。**
   OCR + 模型推理是 CPU/GPU 密集型的，放进 async 里会把整个
   HTTP 服务卡死（连 /health 都不响应）。所以用线程池 + 内存任务表。

2. **进度靠 EventBus 桥接到 WebSocket。**
   每个任务有自己的 EventBus 和一个事件环形缓冲。WebSocket 连上来时
   **先把历史事件补发一遍**，这样用户在任务跑到一半时刷新页面，
   仍然能看到完整进度，而不是从"半截"开始。

**安全边界**：本项目是本地工具，默认只监听 127.0.0.1。
所有文件路径参数都会被解析成绝对路径并校验存在性；
不提供任何"写任意路径"的接口。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from ..core.events import Event, EventBus
from ..core.registry import Context

log = logging.getLogger(__name__)

#: 每个任务保留的历史事件条数（够 UI 补发即可，不必无限增长）
EVENT_BUFFER = 500


@dataclass
class Job:
    """一个后台任务。"""

    id: str
    project_id: str = ""
    stage: str = ""
    kind: str = ""
    status: str = "pending"     # pending / running / done / failed / cancelled
    progress: float = 0.0
    message: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    bus: EventBus = field(default_factory=EventBus)
    _events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # 把总线上的事件同时记进环形缓冲，供 WebSocket 补发
        self.bus.subscribe(self._record)

    def _record(self, ev: Event) -> None:
        with self._lock:
            self._events.append(ev.to_dict())
            if ev.kind == "progress" and ev.progress is not None:
                self.progress = ev.progress
            if ev.stage:
                self.stage = ev.stage
            if ev.message and ev.kind in ("stage_start", "stage_end", "log", "done"):
                self.message = ev.message
            if ev.kind == "stage_error":
                self.error = ev.message

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "stage": self.stage,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
        }


class JobManager:
    """内存任务表 + 线程池。"""

    def __init__(self, ctx: Context, *, max_workers: int = 1) -> None:
        self.ctx = ctx
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # 默认单 worker：OCR 与模型推理争抢同一块 GPU，
        # 并行跑多个任务只会互相拖慢，还容易把显存打爆。
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="novaloc-job")

    def submit(
        self,
        fn: Callable[[EventBus, Job], dict[str, Any]],
        *,
        project_id: str = "",
        kind: str = "",
    ) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], project_id=project_id, kind=kind, status="pending")
        with self._lock:
            self.jobs[job.id] = job

        def runner() -> None:
            job.status = "running"
            job.started_at = time.time()
            try:
                res = fn(job.bus, job) or {}
                job.result = res
                job.status = "done"
                job.progress = 1.0
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
                job.bus.emit(Event("stage_error", stage=job.stage, message=str(exc)))
                log.exception("任务 %s 失败", job.id)
            finally:
                job.finished_at = time.time()

        job.future = self._pool.submit(runner)  # type: ignore[attr-defined]
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list(self, *, project_id: str | None = None, limit: int = 50) -> list[Job]:
        with self._lock:
            items = list(self.jobs.values())
        if project_id:
            items = [j for j in items if j.project_id == project_id]
        items.sort(key=lambda j: j.started_at or 0, reverse=True)
        return items[:limit]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# 上下文工厂
# --------------------------------------------------------------------------


def load_config() -> Config:
    """读取配置。

    直接委托给 :meth:`Config.load` —— 配置的读写规则只应有**一处**实现。
    早先这里自己拼 JSON 路径、自己 model_validate_json，与
    ``Config.load()`` 的 TOML 路径各说各话，用户在网页改的设置命令行看不到。
    """
    try:
        return Config.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("配置读取失败，改用默认配置：%s", exc)
        return Config()


def save_config(cfg: Config) -> None:
    """写出配置（委托 :meth:`Config.save`，统一 JSON 位置与格式）。"""
    cfg.save()


__all__ = ["Job", "JobManager", "load_config", "save_config"]
