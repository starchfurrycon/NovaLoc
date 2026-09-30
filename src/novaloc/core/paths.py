"""路径解析：应用数据目录、工作区、模型目录。

设计约束：C 盘是本机最紧张的分区（约 41 GB 可用），而 D 盘有 459 GB。
因此**所有大体积产物（模型、工作区副本、日志）默认落在 D 盘**，
只有配置可以留在用户配置目录。
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir

APP_DIR_NAME = "NovaLoc"


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser().resolve() if v else None


def repo_root() -> Path:
    """源码仓库根目录（开发态）。"""
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    """配置文件目录（小，留在用户目录）。"""
    d = _env_path("NOVALOC_CONFIG_DIR") or Path(user_config_dir(APP_DIR_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pick_big_disk_base() -> Path:
    """挑一个空间充足的分区作为数据根，避免把几十 GB 模型塞进系统盘。"""
    candidates: list[Path] = []
    if os.name == "nt":
        for letter in "DEFGH":
            p = Path(f"{letter}:/")
            if p.exists():
                candidates.append(p)
    else:
        candidates.append(Path.home())

    best: tuple[int, Path] | None = None
    for drive in candidates:
        try:
            import shutil

            free = shutil.disk_usage(drive).free
        except OSError:
            continue
        if best is None or free > best[0]:
            best = (free, drive)
    if best is None:
        return Path.home() / APP_DIR_NAME
    return best[1] / APP_DIR_NAME


def data_root() -> Path:
    """大体积数据根目录。优先 ``NOVALOC_DATA_ROOT``，否则自动挑最大分区。"""
    d = _env_path("NOVALOC_DATA_ROOT")
    if d is None:
        # 开发态：仓库里已有 data/ 就沿用，便于调试
        local = repo_root() / "data"
        d = local if local.parent.exists() and (local.exists() or (repo_root() / "pyproject.toml").exists()) else _pick_big_disk_base()
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspaces_dir() -> Path:
    d = data_root() / "workspaces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = data_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_root() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fonts_dir() -> Path:
    """内置/下载的中文字体目录。"""
    d = data_root() / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_fonts_dir() -> Path:
    """随包分发的字体（仅限可再分发的开源字体）。"""
    return Path(__file__).resolve().parent / "assets" / "fonts"


def config_json() -> Path:
    """**权威**配置文件：``<data_root>/config.json``。

    配置和游戏数据一起放在大分区上（C 盘常年吃紧），
    网页设置页、CLI、以及核心 :class:`..core.config.Config` 都读写这一个文件。
    """
    return data_root() / "config.json"


def config_file() -> Path:
    """旧版 TOML 配置位置，仅用于向后兼容读取与一次性迁移。

    新代码请用 :func:`config_json`。
    """
    return config_dir() / "config.toml"


def summary() -> dict[str, str]:
    return {
        "repo_root": str(repo_root()),
        "config_dir": str(config_dir()),
        "data_root": str(data_root()),
        "workspaces": str(workspaces_dir()),
        "models": str(models_dir()),
        "fonts": str(fonts_dir()),
    }
