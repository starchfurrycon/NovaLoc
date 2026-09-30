"""Ollama 环境自检与模型拉取。

CLI 的 ``novaloc ollama status`` / ``novaloc ollama pull`` 都建立在这里。
除了 ``httpx`` 之外**不引入任何第三方依赖** —— 定位 Ollama 可执行文件、
探测服务、拉模型都只用标准库 + httpx。

设计约束（和整个项目一致）：

* **任何"没装 / 没起 / 拉不动"的情况都不抛异常**，而是返回 ``False`` 或
  ``None``，由调用方决定怎么提示。CLI 里打印一句人话比抛一堆栈有用得多。
* 拉模型必须**真流式**。4B 级模型动辄 2~4 GB，一次性读完整响应会白占
  几 GB 内存，而且用户会盯着一个没有任何输出的终端干等十几分钟。
  所以走 ``httpx.stream`` + NDJSON 逐行回调。
* 显存在这台机器上是硬约束（RTX 4060 Laptop，8 GB）。推荐列表里每个模型
  都写清楚大致占用，避免用户一口气把三个模型都拉下来然后互相换入换出。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

#: 默认服务地址。和 :class:`novaloc.core.config.OllamaConfig` 的默认值保持一致。
DEFAULT_BASE_URL = "http://127.0.0.1:11434"

#: 本地探测用的短超时。Ollama 没起时 connect 会立刻失败，不该让 CLI 卡住。
PROBE_TIMEOUT_S = 3.0

#: 拉模型时的读超时。模型很大、上游也偶尔会短暂卡住，
#: 但只要有字节流进来 httpx 就会重置这个计时，所以给一个宽松的值。
PULL_TIMEOUT_S = 1800.0


# --------------------------------------------------------------------------
# 可执行文件定位
# --------------------------------------------------------------------------


def _windows_extra_paths() -> list[Path]:
    """Windows 上 Ollama 安装器实际会落到的位置。

    除了官方安装器与 winget 的位置，还要考虑**便携版**：用户常把它解到
    ``D:\\Ollama``、``D:\\Tools\\ollama`` 或数据根目录下。这类位置无法穷举，
    所以先列常见固定位置，再按下面 ``_scan_portable_roots()`` 扫有限深度 ——
    用户装字体的目的就是让工具找到它，找不到会报"未安装 Ollama"，
    而用户明明装了。
    """
    cands: list[Path] = []

    local = _getenv_any("LOCALAPPDATA", "LocalAppData")
    if local:
        cands.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")

    pf = _getenv_any("PROGRAMFILES", "ProgramFiles") or r"C:\Program Files"
    cands.append(Path(pf) / "Ollama" / "ollama.exe")
    # 32 位 PowerShell 下 ProgramFiles 会指向 x86，补一份以防万一
    cands.append(Path(r"C:\Program Files\Ollama\ollama.exe"))

    home = _getenv_any("USERPROFILE", "UserProfile")
    if home:
        # 便携版（Ollama.Ollama.Portable）有时被解到用户目录
        cands.append(Path(home) / "Ollama" / "ollama.exe")
        cands.append(Path(home) / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe")

    cands.extend(_scan_portable_roots())
    return cands


def _scan_portable_roots(max_depth: int = 3) -> list[Path]:
    """在候选根目录下按有限深度找 ``ollama*.exe``。

    刻意限制深度与目录数：这里会在 ``doctor`` 等命令里同步执行，
    扫整个盘符会让"检查环境"变成几秒钟的卡顿。根目录只取
    数据根目录、各盘符根、以及盘符下的少量常见目录名。
    """
    roots: list[Path] = []
    # 数据根目录（NovaLoc 常把便携工具放在自己旁边）
    try:
        from ..core.paths import data_root

        dr = data_root()
        roots.append(dr)
        if dr.parent != dr:
            roots.append(dr.parent)
    except Exception:  # noqa: BLE001 - 取不到就不扫，不该因此报错
        pass

    names = ("Ollama", "ollama", "Tools", "tools", "Programs", "Apps")
    for letter in "CDEFGH":
        base = Path(f"{letter}:\\")
        try:
            if not base.is_dir():
                continue
        except OSError:
            continue
        roots.append(base)
        roots.extend(base / n for n in names)

    found: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        try:
            if not r.is_dir():
                continue
            key = str(r).lower()
            if key in seen:
                continue
            seen.add(key)
        except OSError:
            continue
        for exe in _exe_names():
            # rglob 在 depth 受限时仍可能很慢，所以先直接查一层
            direct = r / exe
            try:
                if direct.is_file():
                    found.append(direct)
                    continue
            except OSError:
                pass
            try:
                for hit in r.glob(f"{'*/' * max_depth}{exe}"):
                    if hit.is_file():
                        found.append(hit)
                        break
            except OSError:
                continue
        if found:
            break
    return found


def _exe_names() -> tuple[str, ...]:
    return ("ollama.exe", "ollama-windows-amd64.exe")


def _getenv_any(*names: str) -> str:
    """按顺序取环境变量，兼容不同的键大小写形式。

    Windows 环境变量名不区分大小写，但 ``os.environ`` 暴露的键大小写
    取决于进程启动方式 —— 有的环境给 ``ProgramFiles``，有的给
    ``PROGRAMFILES``。直接写死一种就会在某些机器上找不到路径。
    """
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def find_ollama() -> Path | None:
    """找 ``ollama`` 可执行文件。

    顺序：``PATH`` → ``%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe`` →
    ``C:\\Program Files\\Ollama\\ollama.exe`` → 几个常见的便携版位置。
    找不到返回 ``None``（绝不抛异常）。
    """
    for name in ("ollama.exe", "ollama"):
        try:
            hit = shutil.which(name)
        except Exception:  # noqa: BLE001 - PATH 里有畸形条目时 which 可能炸
            hit = None
        if hit:
            p = Path(hit)
            if p.is_file():
                return p

    for cand in _windows_extra_paths():
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# 服务探测
# --------------------------------------------------------------------------


def _url(base_url: str, path: str) -> str:
    return f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}{path}"


def is_running(base_url: str = DEFAULT_BASE_URL) -> bool:
    """GET ``/api/tags``，能通就说明服务活着。"""
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, trust_env=False) as c:
            r = c.get(_url(base_url, "/api/tags"))
        return r.status_code < 400
    except Exception:  # noqa: BLE001 - 连不上就是没运行，不是错误
        return False


def server_version(base_url: str = DEFAULT_BASE_URL) -> str:
    """取服务版本号；拿不到返回空串。"""
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, trust_env=False) as c:
            r = c.get(_url(base_url, "/api/version"))
        if r.status_code < 400:
            return str(r.json().get("version", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def list_models(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    """``/api/tags`` → 模型名列表。服务没起时返回空列表。"""
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, trust_env=False) as c:
            r = c.get(_url(base_url, "/api/tags"))
        if r.status_code >= 400:
            return []
        data = r.json()
    except Exception:  # noqa: BLE001
        return []

    out: list[str] = []
    for m in data.get("models", []) or []:
        name = m.get("name") or m.get("model") or ""
        if name:
            out.append(str(name))
    return out


def model_sizes_gb(base_url: str = DEFAULT_BASE_URL) -> dict[str, float]:
    """模型名 → 磁盘占用（GB）。仅用于 ``ollama status`` 展示。"""
    out: dict[str, float] = {}
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, trust_env=False) as c:
            r = c.get(_url(base_url, "/api/tags"))
        if r.status_code >= 400:
            return out
        data = r.json()
    except Exception:  # noqa: BLE001
        return out

    for m in data.get("models", []) or []:
        name = m.get("name") or m.get("model") or ""
        size = m.get("size") or 0
        if name:
            try:
                out[str(name)] = float(size) / (1024 ** 3)
            except (TypeError, ValueError):
                out[str(name)] = 0.0
    return out


def models_root() -> Path | None:
    """当前 ``OLLAMA_MODELS`` 指向哪里；查不到返回 ``None``。

    只看**进程环境**是不够的：用户通常用 ``setx OLLAMA_MODELS ...`` 或
    系统属性面板设置，那写的是**用户/系统级**环境变量，而已经打开的进程
    （包括本工具）看不到它 —— Ollama 服务是之后启动的，所以它能看到。

    后果很具体：``doctor`` 会打印"未设置 OLLAMA_MODELS，默认在 C 盘"，
    而用户明明已经把 7 GB 模型放在 D 盘了。这种自检谎报比不报更糟，
    因为它会让人去 C 盘找问题。

    所以按 进程 → 用户级 → 系统级 依次查。
    """
    v = os.environ.get("OLLAMA_MODELS")
    if not v:
        v = _persistent_env("OLLAMA_MODELS")
    return Path(v).expanduser() if v else None


def _persistent_env(name: str) -> str | None:
    """读**持久化**的用户级/系统级环境变量（Windows）。

    非 Windows 上返回 ``None``（那边一般通过 shell 配置导出，
    进程环境里已经有了）。
    """
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 需要
    except ImportError:
        return None
    for root, sub in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                val, _ = winreg.QueryValueEx(key, name)
            if val:
                return str(val)
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# 安装提示
# --------------------------------------------------------------------------


def install_hint() -> str:
    """没装 Ollama 时给用户的**可直接粘贴**的两行命令。

    特意写清楚 ``OLLAMA_MODELS``：这台机器 C 盘只剩 ~41 GB，
    而三个推荐模型加起来接近 8 GB；不重定向的话 C 盘会被吃掉一大块，
    而且 Ollama 默认装在 C 盘，模型也跟着落在 C 盘。
    """
    return (
        "本机没有检测到 Ollama。推荐用 winget 安装（任选一条）：\n"
        "    winget install Ollama.Ollama\n"
        "    winget install Ollama.Ollama.Portable\n"
        "\n"
        "安装后**务必把模型目录重定向到空间充足的分区**（本机 C 盘紧张、D 盘充裕）：\n"
        "    setx OLLAMA_MODELS \"D:\\NovaLoc\\ollama-models\"\n"
        "然后重开一个终端，运行 `ollama serve`（或直接启动桌面版 Ollama）。\n"
        "\n"
        "验证：`ollama list`，或用 `novaloc ollama status`。"
    )


# --------------------------------------------------------------------------
# 拉取模型
# --------------------------------------------------------------------------


def _progress_changed(last: dict[str, Any], cur: dict[str, Any]) -> bool:
    """NDJSON 里大量行内容完全一样，过滤掉免得刷屏。"""
    return any(last.get(k) != cur.get(k) for k in ("status", "digest", "completed", "total"))


def pull_model(
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> bool:
    """拉一个模型，逐行回调 NDJSON 进度。

    返回 ``True`` 表示成功。Ollama 没起、网络中断、模型名不存在等都返回
    ``False``（并通过 ``on_progress({"error": ...})`` 告知原因），**不抛异常**。
    """
    payload = {"model": model, "stream": True}
    last: dict[str, Any] = {}

    def emit(obj: dict[str, Any]) -> None:
        if on_progress is not None:
            try:
                on_progress(obj)
            except Exception:  # noqa: BLE001 - 回调的问题不该中断下载
                pass

    try:
        timeout = httpx.Timeout(PULL_TIMEOUT_S, connect=PROBE_TIMEOUT_S)
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream("POST", _url(base_url, "/api/pull"), json=payload) as resp:
                if resp.status_code >= 400:
                    body = ""
                    try:
                        body = resp.read().decode("utf-8", "replace")[:300]
                    except Exception:  # noqa: BLE001
                        pass
                    emit({"error": f"HTTP {resp.status_code}：{body}"})
                    return False

                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # 有些版本会在流里夹非 JSON 日志行，跳过即可
                        continue
                    if not isinstance(data, dict):
                        continue

                    err = data.get("error")
                    if err:
                        emit({"error": str(err)})
                        return False

                    if _progress_changed(last, data):
                        last = data
                        emit(data)

                    if data.get("status") == "success" or data.get("done") is True:
                        emit({"status": "success", "done": True})
                        return True

        # 流正常结束但没见到 success 行：以"模型是否真的存在"为准
        return model in list_models(base_url)
    except httpx.ConnectError as exc:
        emit({"error": f"连接不上 Ollama（{base_url}）：{exc}"})
        return False
    except httpx.TimeoutException as exc:
        emit({"error": f"拉取 {model} 超时：{exc}"})
        return False
    except Exception as exc:  # noqa: BLE001 - Ollama 有各种奇怪的失败方式
        emit({"error": f"拉取 {model} 失败：{exc}"})
        return False


def pull_many(
    models: list[str],
    base_url: str = DEFAULT_BASE_URL,
    *,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, bool]:
    """依次拉多个模型，返回 ``{模型: 是否成功}``。"""
    out: dict[str, bool] = {}
    for m in models:
        if on_progress is not None:
            try:
                on_progress(m, {"status": None})  # 标记"开始"
            except Exception:  # noqa: BLE001
                pass

        def _cb(obj: dict[str, Any], _m: str = m) -> None:
            if on_progress is not None:
                try:
                    on_progress(_m, obj)
                except Exception:  # noqa: BLE001
                    pass

        out[m] = pull_model(m, base_url, on_progress=_cb)
    return out


# --------------------------------------------------------------------------
# 推荐模型
# --------------------------------------------------------------------------

#: ``(模型名, 中文说明)`` —— 说明里带显存/体积提示。
#: 这台机器是 **RTX 4060 Laptop / 8 GB 显存**，所以三个模型都刻意选 4B 级或更小，
#: 并且建议 KV cache 量化 + 单模型驻留，避免反复换入换出。
RECOMMENDED_MODELS: tuple[tuple[str, str], ...] = (
    (
        "translategemma:4b",
        "翻译主力。约 3.3 GB，4B 级，8 GB 显存上留足余量；"
        "配合同步的 q8_0 KV cache 量化，8192 上下文约再省 1 GB 显存。",
    ),
    (
        "qwen3-vl:4b",
        "视觉模型，仅作**贴图艺术字兜底 OCR**。约 3.5 GB；"
        "主 OCR 走 PP-OCRv6 + DirectML（快 40~140 倍），这个只在置信度偏低时才用。",
    ),
    (
        "bge-m3",
        "向量模型，做术语表检索与翻译记忆模糊匹配。约 1.2 GB，"
        "可以和翻译模型同时驻留（配置里 max_loaded_models 设 2）。",
    ),
)

#: 模型名 → 中文说明，便于其它模块按名字取提示。
MODEL_NOTES: dict[str, str] = dict(RECOMMENDED_MODELS)


def recommended_names() -> list[str]:
    return [name for name, _ in RECOMMENDED_MODELS]


def installed_recommended(base_url: str = DEFAULT_BASE_URL) -> dict[str, bool]:
    """每个推荐模型是否已经在本机装好。服务没起时全部 ``False``。"""
    names = list_models(base_url)
    bases = {n.split(":")[0] for n in names}

    out: dict[str, bool] = {}
    for model, _ in RECOMMENDED_MODELS:
        if model in names:
            out[model] = True
        else:
            out[model] = model.split(":")[0] in bases
    return out


def status_snapshot(base_url: str = DEFAULT_BASE_URL, *, probe: bool = True) -> dict[str, Any]:
    """一次性收集给 CLI/UI 用的状态快照。

    ``probe=False`` 时只做本地查找，不发起任何网络请求
    （给单元测试和"离线模式"用）。
    """
    exe = find_ollama()
    snap: dict[str, Any] = {
        "executable": str(exe) if exe else "",
        "base_url": base_url,
        "running": False,
        "version": "",
        "models": [],
        "model_sizes_gb": {},
        "models_root": str(models_root()) if models_root() else "",
        "recommended": {name: False for name, _ in RECOMMENDED_MODELS},
    }
    if not probe:
        return snap

    snap["running"] = is_running(base_url)
    if snap["running"]:
        snap["version"] = server_version(base_url)
        snap["models"] = list_models(base_url)
        snap["model_sizes_gb"] = model_sizes_gb(base_url)
        snap["recommended"] = installed_recommended(base_url)
    return snap


def human_size(num_bytes: float) -> str:
    """给终端看的体积字符串。"""
    try:
        v = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} TB"


def format_pull_line(obj: dict[str, Any]) -> str:
    """把一行 NDJSON 进度渲染成单行文本。"""
    if obj.get("error"):
        return f"❌ {obj['error']}"

    status = str(obj.get("status") or "")
    total = obj.get("total") or 0
    done = obj.get("completed") or 0
    bits = [status or "…"]
    if total:
        pct = done / total * 100 if total else 0.0
        bits.append(f"{pct:5.1f}%  {human_size(done)}/{human_size(total)}")
    return "  ".join(bits)


# --------------------------------------------------------------------------
# 给 Ollama 进程用的环境变量建议
# --------------------------------------------------------------------------


def recommended_env(data_root: str | Path | None = None) -> dict[str, str]:
    """把模型目录放到数据盘所需的环境变量。"""
    root = Path(data_root) if data_root else Path(os.environ.get("NOVALOC_DATA_ROOT") or r"D:\NovaLoc")
    models_path = root if root.name == "ollama-models" else root / "ollama-models"
    return {
        "OLLAMA_MODELS": str(models_path),
        "OLLAMA_KEEP_ALIVE": "10m",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KV_CACHE_TYPE": "q8_0",
    }


def wait_until_ready(base_url: str = DEFAULT_BASE_URL, *, timeout_s: float = 15.0) -> bool:
    """等 Ollama 起来（安装完第一次启动时有用）。超时返回 ``False``。"""
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if is_running(base_url):
            return True
        time.sleep(0.5)
    return False


__all__ = [
    "DEFAULT_BASE_URL",
    "MODEL_NOTES",
    "RECOMMENDED_MODELS",
    "find_ollama",
    "format_pull_line",
    "human_size",
    "install_hint",
    "installed_recommended",
    "is_running",
    "list_models",
    "model_sizes_gb",
    "models_root",
    "pull_many",
    "pull_model",
    "recommended_env",
    "recommended_names",
    "server_version",
    "status_snapshot",
    "wait_until_ready",
]
