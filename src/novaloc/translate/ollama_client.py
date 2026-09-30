"""Ollama HTTP 客户端。

只用标准库 + httpx，不依赖官方 SDK —— 这样 Python 版本升级不会被打包问题卡住，
也方便我们精确控制超时、重试与 JSON 结构化输出。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaNotRunning(OllamaError):
    pass


class ModelMissing(OllamaError):
    def __init__(self, model: str, available: list[str]) -> None:
        self.model = model
        self.available = available
        hint = f"可用模型：{', '.join(available)}" if available else "本机还没有任何模型"
        super().__init__(f"模型 '{model}' 不存在。{hint}。可执行 `ollama pull {model}` 拉取。")


@dataclass
class ChatResult:
    text: str
    model: str
    done_reason: str = ""
    eval_count: int = 0
    total_duration_ns: int = 0


class OllamaClient:
    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        *,
        timeout: float = 300.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host.rstrip("/")
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, trust_env=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self.host}{path}"
        try:
            resp = self.client.request(method, url, **kw)
        except httpx.ConnectError as exc:
            raise OllamaNotRunning(
                f"连接不上 Ollama（{self.host}）。请确认服务已启动：`ollama serve`。原始错误：{exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(f"请求 Ollama 超时（{path}）：{exc}") from exc

        if resp.status_code == 404:
            body = _safe_json(resp)
            msg = body.get("error", resp.text)
            if "not found" in msg.lower() or "no such model" in msg.lower():
                raise ModelMissing(msg, self.list_models())
            raise OllamaError(f"Ollama 404：{msg}")
        if resp.status_code >= 400:
            raise OllamaError(f"Ollama 返回 {resp.status_code}：{resp.text[:400]}")
        return resp

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        try:
            self._request("GET", "/api/tags")
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        try:
            r = self._request("GET", "/api/version")
            return r.json().get("version", "")
        except OllamaError:
            return ""

    def list_models(self) -> list[str]:
        try:
            r = self._request("GET", "/api/tags")
        except OllamaError:
            return []
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]

    def has_model(self, model: str) -> bool:
        names = self.list_models()
        if model in names:
            return True
        base = model.split(":")[0]
        return any(n.split(":")[0] == base for n in names)

    def running_models(self) -> list[dict[str, Any]]:
        try:
            r = self._request("GET", "/api/ps")
            return r.json().get("models", [])
        except OllamaError:
            return []

    def show(self, model: str) -> dict[str, Any]:
        r = self._request("POST", "/api/show", json={"model": model})
        return r.json()

    # ------------------------------------------------------------------
    # 拉取模型
    # ------------------------------------------------------------------

    def pull(self, model: str, *, on_progress: Any = None) -> None:
        """拉取模型，``on_progress(status_dict)`` 可用于展示进度。"""
        url = f"{self.host}/api/pull"
        payload = {"model": model, "stream": True}
        try:
            with self.client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    raise OllamaError(f"拉取 {model} 失败：HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise OllamaError(f"拉取 {model} 失败：{data['error']}")
                    if on_progress:
                        on_progress(data)
        except httpx.ConnectError as exc:
            raise OllamaNotRunning(f"连接不上 Ollama（{self.host}）：{exc}") from exc

    def delete(self, model: str) -> None:
        self._request("DELETE", "/api/delete", json={"model": model})

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        fmt: Any = None,
        keep_alive: str | int | None = None,
        images: list[str] | None = None,
        think: bool | None = None,
    ) -> ChatResult:
        """非流式对话。``fmt`` 传 ``"json"`` 或 JSON Schema 可强制结构化输出。"""
        msgs = [dict(m) for m in messages]
        if images:
            if not msgs:
                raise ValueError("images 需要至少一条消息")
            msgs[-1]["images"] = images

        payload: dict[str, Any] = {"model": model, "messages": msgs, "stream": False}
        if options:
            payload["options"] = options
        if fmt is not None:
            payload["format"] = fmt
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        if think is not None:
            payload["think"] = think

        started = time.time()
        resp = self._request("POST", "/api/chat", json=payload)
        data = resp.json()
        if data.get("error"):
            raise OllamaError(str(data["error"]))

        content = (data.get("message") or {}).get("content", "")
        return ChatResult(
            text=content,
            model=data.get("model", model),
            done_reason=data.get("done_reason", ""),
            eval_count=data.get("eval_count", 0),
            total_duration_ns=int((time.time() - started) * 1e9),
        )

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        fmt: Any = None,
        keep_alive: str | int | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        if options:
            payload["options"] = options
        if fmt is not None:
            payload["format"] = fmt
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        resp = self._request("POST", "/api/generate", json=payload)
        data = resp.json()
        if data.get("error"):
            raise OllamaError(str(data["error"]))
        return ChatResult(
            text=data.get("response", ""),
            model=data.get("model", model),
            done_reason=data.get("done_reason", ""),
            eval_count=data.get("eval_count", 0),
        )

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        keep_alive: str | int | None = None,
    ):
        """流式对话，逐块 yield 文本增量。"""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options:
            payload["options"] = options
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        url = f"{self.host}/api/chat"
        try:
            with self.client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    raise OllamaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise OllamaError(str(data["error"]))
                    chunk = (data.get("message") or {}).get("content", "")
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        break
        except httpx.ConnectError as exc:
            raise OllamaNotRunning(f"连接不上 Ollama（{self.host}）：{exc}") from exc

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        r = self._request("POST", "/api/embed", json={"model": model, "input": texts})
        data = r.json()
        if data.get("error"):
            raise OllamaError(str(data["error"]))
        embs = data.get("embeddings")
        if embs is None:
            emb = data.get("embedding")
            embs = [emb] if emb else []
        return embs


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_array(text: str) -> list[Any] | None:
    """从模型输出里尽力抠出一个 JSON 数组。

    模型常见的坏习惯：包 ```json 代码块、前后加解释、数组后面拖废话。
    """
    if not text:
        return None
    candidates: list[str] = []

    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    candidates.append(text.strip())

    # 括号配平扫描，找最外层的 [ ... ]
    start = text.find("[")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            # 再退一步：把中文引号/尾逗号修一下
            fixed = _loose_fix(cand)
            try:
                data = json.loads(fixed)
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 模型有时返回 {"translations": [...]}
            for v in data.values():
                if isinstance(v, list):
                    return v
    return None


def _loose_fix(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r",\s*([\]}])", r"\1", s)  # 尾逗号
    return s
