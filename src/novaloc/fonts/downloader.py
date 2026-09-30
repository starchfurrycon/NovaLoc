"""模型/字体下载器。

本机环境有个必须绕开的坑（已实测）：

* ``hosts`` 文件把 ``github.com``、``huggingface.co``、``raw.githubusercontent.com``
  等 **258 个域名**全部指向 ``127.0.0.1``。
* 即使直连，Windows 上的 Python/OpenSSL **无法完成证书链校验**
  （``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``），
  连 certifi 的 CA bundle 也救不回来。
* 但是 **curl.exe 加 ``--ssl-no-revoke`` 可以正常下载**（实测拿到 7.13 MB 的 TTF，
  magic 校验通过），``www.modelscope.cn`` 也可以被 Python 直接访问。

所以下载策略做成三级降级：

1. **httpx**（首选，能拿到镜像站的正常 TLS；ModelScope 实测可用）
2. **curl.exe**（Windows 10+ 自带；加 ``--ssl-no-revoke`` 才能过 GitHub/HF）
3. **镜像前缀改写**（ghproxy / hf-mirror / ModelScope），把原始 URL 换成国内可达地址

下载一律**先写临时文件再原子改名**，并做 SHA-256 校验与大小校验，
避免半截文件被当成完整文件用。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int | None], None]

#: 可用的 GitHub 加速前缀（按可靠性排序）
GITHUB_PROXIES: tuple[str, ...] = (
    "",  # 直连
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
)

#: HuggingFace 镜像
HF_MIRROR = "https://hf-mirror.com"

#: ModelScope 支持的仓库（实测可直连）
MODELSCOPE_HOST = "www.modelscope.cn"

_CURL_HINT = (
    "下载失败。若目标站点是 github.com / huggingface.co，本机 hosts 可能屏蔽了它们，"
    "请在设置里配置镜像前缀，或改用 ModelScope 源。"
)


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    path: Path
    method: str
    url: str
    bytes_written: int
    sha256: str
    ok: bool = True
    message: str = ""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def find_curl() -> str | None:
    """找可用的 curl.exe。Windows 10 1803+ 自带。"""
    for cand in ("curl.exe", "curl"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def _proxy_urls(url: str, proxies: tuple[str, ...] = GITHUB_PROXIES) -> list[str]:
    """把 GitHub / HF 的 URL 展开成一组候选（直连 + 各级镜像）。"""
    host = (urlparse(url).hostname or "").lower()
    out: list[str] = []
    for p in proxies:
        out.append(url if not p else p + url)
    if host.endswith("huggingface.co"):
        out.append(url.replace("https://huggingface.co", HF_MIRROR, 1))
        out.append(url.replace("http://huggingface.co", HF_MIRROR, 1))
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def file_sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 各传输方式
# --------------------------------------------------------------------------


def _download_httpx(url: str, dest: Path, progress: ProgressFn | None, timeout: float) -> int:
    import httpx

    headers = {"User-Agent": "NovaLoc/0.1 (+https://github.com/starchfurrycon/novaloc)"}
    written = 0
    # trust_env=False：本机存在奇怪的代理环境变量时不要走它们
    with httpx.Client(follow_redirects=True, timeout=timeout, trust_env=False) as client:
        with client.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) or None
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written, total)
    return written


def _download_curl(
    url: str,
    dest: Path,
    progress: ProgressFn | None,
    timeout: float,
    *,
    curl: str | None = None,
) -> int:
    """用 curl.exe 下载。

    ``--ssl-no-revoke`` 是关键：本机 Schannel 的吊销检查会失败
    （``CRYPT_E_NO_REVOCATION_CHECK``），关掉它就能过 GitHub。
    """
    exe = curl or find_curl()
    if not exe:
        raise DownloadError("系统里没有 curl.exe")

    cmd = [
        exe, "-sS", "-L",
        "--ssl-no-revoke",
        "--retry", "3",
        "--retry-delay", "2",
        "--connect-timeout", "20",
        "--max-time", str(max(60, int(timeout * 20))),
        "-o", str(dest),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise DownloadError(f"curl 失败(exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    if not dest.exists():
        raise DownloadError("curl 未产出文件")
    size = dest.stat().st_size
    if progress:
        progress(size, size)
    return size


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def download(
    url: str,
    dest: Path,
    *,
    progress: ProgressFn | None = None,
    sha256: str | None = None,
    min_bytes: int = 1024,
    timeout: float = 60.0,
    use_mirrors: bool = True,
    prefer_curl: bool = False,
) -> DownloadResult:
    """下载 ``url`` 到 ``dest``，必要时自动换镜像。

    ``min_bytes`` 用来挡住"下载到一个 HTML 错误页"这种静默失败。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates = _proxy_urls(url) if use_mirrors else [url]
    errors: list[str] = []

    for i, cand in enumerate(candidates):
        tmp = dest.with_suffix(dest.suffix + f".part{i}")
        tmp.unlink(missing_ok=True)
        for method in (("curl", "httpx") if prefer_curl else ("httpx", "curl")):
            try:
                fn = _download_curl if method == "curl" else _download_httpx
                n = fn(cand, tmp, progress, timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"[{method}] {cand[:110]} → {type(exc).__name__}: {str(exc)[:120]}")
                tmp.unlink(missing_ok=True)
                continue

            if n < min_bytes:
                errors.append(f"[{method}] {cand[:110]} → 文件过小({n}B)，可能是错误页")
                tmp.unlink(missing_ok=True)
                continue

            digest = file_sha256(tmp)
            if sha256 and digest.lower() != sha256.lower():
                errors.append(f"[{method}] {cand[:110]} → SHA256 不匹配")
                tmp.unlink(missing_ok=True)
                continue

            tmp.replace(dest)
            log.info("下载完成 %s (%d 字节, via %s)", dest.name, n, method)
            return DownloadResult(
                path=dest, method=method, url=cand, bytes_written=n, sha256=digest
            )

    detail = "\n  ".join(errors[-6:])
    raise DownloadError(f"所有下载通道均失败：\n  {detail}\n{_CURL_HINT}")


def download_with_mirror_hint(url: str, dest: Path, **kw) -> DownloadResult:
    return download(url, dest, **kw)


# --------------------------------------------------------------------------
# 压缩包解压
# --------------------------------------------------------------------------


def extract_archive(
    archive: Path,
    out_dir: Path,
    *,
    members: list[str] | None = None,
    suffixes: tuple[str, ...] = (".ttf", ".otf", ".ttc", ".otc"),
) -> list[Path]:
    """解压字体压缩包，返回解出来的字体文件。（``members`` 非空时只取这些）"""
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = archive.suffix.lower()

    if kind == ".7z":
        return _extract_7z(archive, out_dir, members, suffixes)
    if kind in (".zip",):
        return _extract_zip(archive, out_dir, members, suffixes)
    if kind in (".gz", ".tgz") or archive.name.endswith((".tar.gz", ".tar.xz")):
        return _extract_tar(archive, out_dir, members, suffixes)

    # 后缀不认识就按 zip 试一把（GitHub release 有时没有扩展名）
    try:
        return _extract_zip(archive, out_dir, members, suffixes)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"无法识别的压缩格式：{archive.name}（{exc}）") from exc


def _pick(paths: list[Path], members: list[str] | None, suffixes: tuple[str, ...]) -> list[Path]:
    out = [p for p in paths if p.suffix.lower() in suffixes]
    if members:
        wanted = {m.lower() for m in members}
        out = [p for p in out if p.name.lower() in wanted]
    return sorted(out)


def _extract_zip(
    archive: Path, out_dir: Path, members: list[str] | None, suffixes: tuple[str, ...]
) -> list[Path]:
    import zipfile

    found: list[Path] = []
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if Path(name).suffix.lower() not in suffixes:
                continue
            if members and name.lower() not in {m.lower() for m in members}:
                continue
            target = out_dir / name
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            found.append(target)
    return sorted(found)


def _extract_tar(
    archive: Path, out_dir: Path, members: list[str] | None, suffixes: tuple[str, ...]
) -> list[Path]:
    import tarfile

    found: list[Path] = []
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            name = Path(m.name).name
            if Path(name).suffix.lower() not in suffixes:
                continue
            if members and name.lower() not in {x.lower() for x in members}:
                continue
            f = t.extractfile(m)
            if f is None:
                continue
            target = out_dir / name
            with target.open("wb") as dst:
                shutil.copyfileobj(f, dst)
            found.append(target)
    return sorted(found)


def _extract_7z(
    archive: Path, out_dir: Path, members: list[str] | None, suffixes: tuple[str, ...]
) -> list[Path]:
    """7z 优先用 py7zr，其次找系统 7z/7za。"""
    try:
        import py7zr  # type: ignore[import-not-found]

        found: list[Path] = []
        with py7zr.SevenZipFile(archive, mode="r") as z:
            names = z.getnames()
            wanted = [
                n
                for n in names
                if Path(n).suffix.lower() in suffixes
                and (not members or Path(n).name.lower() in {m.lower() for m in members})
            ]
            if wanted:
                z.extract(path=out_dir, targets=wanted)
                for n in wanted:
                    p = out_dir / n
                    if p.exists():
                        found.append(p)
        # 把嵌套目录里的文件提到顶层，方便调用方按名字找
        return sorted(_flatten(found, out_dir))
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("py7zr 解压失败，尝试外部 7z：%s", exc)

    exe = None
    for cand in ("7z", "7za", "7zr"):
        exe = shutil.which(cand)
        if exe:
            break
    if not exe:
        raise DownloadError(
            "需要解压 .7z 但既没有安装 py7zr，也找不到 7z 可执行文件。"
            "请运行：pip install py7zr"
        )

    proc = subprocess.run(
        [exe, "x", "-y", f"-o{out_dir}", str(archive)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise DownloadError(f"7z 解压失败：{(proc.stderr or proc.stdout or '')[:200]}")
    allfonts = [p for p in out_dir.rglob("*") if p.suffix.lower() in suffixes]
    return sorted(_pick(allfonts, members, suffixes))


def _flatten(paths: list[Path], out_dir: Path) -> list[Path]:
    result: list[Path] = []
    for p in paths:
        if p.parent == out_dir:
            result.append(p)
            continue
        target = out_dir / p.name
        try:
            if not target.exists():
                shutil.move(str(p), str(target))
            result.append(target)
        except Exception:  # noqa: BLE001
            result.append(p)
    return result


def disk_free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except Exception:  # noqa: BLE001
        return 0.0


def env_hf_mirror() -> dict[str, str]:
    """给 huggingface_hub / transformers 用的环境变量（本机 HF 被屏蔽）。"""
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", HF_MIRROR)
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return env


def probe_endpoints() -> dict[str, bool]:
    """快速探测各下载源是否可用（用于设置页显示）。"""
    import httpx

    targets = {
        "modelscope": MODELSCOPE_HOST,
        "hf-mirror": "hf-mirror.com",
        "github": "github.com",
        "ghproxy": "ghproxy.net",
        "pypi": "pypi.org",
    }
    out: dict[str, bool] = {}
    for key, host in targets.items():
        try:
            with httpx.Client(timeout=6.0, trust_env=False, follow_redirects=True) as c:
                r = c.head(f"https://{host}/")
                out[key] = r.status_code < 500
        except Exception:  # noqa: BLE001
            out[key] = False
    return out
