"""事件总线：把流水线进度推给 CLI 与 Web UI。

刻意做成同步的简单实现 —— 流水线是 CPU/GPU 密集型的，
引入 asyncio 广播反而增加心智负担；Web 层用线程安全的队列桥接即可。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..models import Severity


@dataclass
class Event:
    kind: str
    stage: str = ""
    message: str = ""
    progress: float | None = None
    severity: Severity = Severity.INFO
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "severity": self.severity.value,
            "data": self.data,
            "ts": self.ts,
        }


Subscriber = Callable[[Event], None]


class EventBus:
    """线程安全的事件分发器。订阅者抛异常不会影响流水线。"""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._history: list[Event] = []
        self._history_limit = 2000

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsub() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsub

    def emit(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - 订阅者的问题不该拖垮流水线
                pass

    # 便捷方法
    def log(self, message: str, *, stage: str = "", severity: Severity = Severity.INFO, **data: Any) -> None:
        self.emit(Event("log", stage=stage, message=message, severity=severity, data=data))

    def progress(self, stage: str, progress: float, message: str = "", **data: Any) -> None:
        self.emit(Event("progress", stage=stage, message=message, progress=max(0.0, min(1.0, progress)), data=data))

    def stage_start(self, stage: str, message: str = "") -> None:
        self.emit(Event("stage_start", stage=stage, message=message))

    def stage_end(self, stage: str, message: str = "", **stats: Any) -> None:
        self.emit(Event("stage_end", stage=stage, message=message, data=stats))

    def stage_error(self, stage: str, message: str, **data: Any) -> None:
        self.emit(Event("stage_error", stage=stage, message=message, severity=Severity.ERROR, data=data))

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._history[-limit:]]

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


class ProgressThrottle:
    """限制进度事件频率，避免 UI 被刷爆。"""

    def __init__(self, bus: EventBus, stage: str, min_interval_s: float = 0.1, min_delta: float = 0.01) -> None:
        self.bus = bus
        self.stage = stage
        self.min_interval_s = min_interval_s
        self.min_delta = min_delta
        self._last_ts = 0.0
        self._last_val = -1.0

    def __call__(self, value: float, message: str = "", **data: Any) -> None:
        now = time.time()
        if value < 1.0 and (now - self._last_ts) < self.min_interval_s and (value - self._last_val) < self.min_delta:
            return
        self._last_ts = now
        self._last_val = value
        self.bus.progress(self.stage, value, message, **data)
