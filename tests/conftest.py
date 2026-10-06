"""pytest 配置。

两件事：

1. 把 ``src/`` 加进 ``sys.path`` —— 这样测试不依赖"已经 ``pip install -e .``"，
   从源码树直接可跑（CI 里也用 ``PYTHONPATH=src``，两条路径都通）。
2. 提供 ``fixtures_dir`` / ``font_fixtures`` 等会话级 fixture。

设计说明：这些套件原本是"独立可执行脚本"（``python tests/test_xxx.py``），
每个都带 ``check()`` 汇总和 ``main()`` 返回值，``if __name__ == "__main__"``
守卫。搬到 pytest 下时**保留了原脚本形态**，只把 ``main()`` 暴露成
``test_suite()`` —— 原因是它们输出的是人工可读的分节报告
（对这类"实测数据 + 不变量断言"的测试比 pytest 的逐条断言更有用），
重写成 assert 风格会丢掉那些实测数字。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义 marker。

    用途：CI 只跑"不需要模型/GPU/系统字体"的那部分套件（见
    ``.github/workflows/ci.yml``）。没注册的 marker 会产生
    ``PytestUnknownMarkWarning``，而 `-m` 过滤会静默不生效 ——
    那就等于又变成"什么都没跑还显示绿"。
    """
    config.addinivalue_line("markers", "needs_fonts: 需要本机中文字体（系统字体或 tests/fixtures/fonts）")
    config.addinivalue_line("markers", "needs_gpu: 需要 DirectML/CUDA 推理后端")
    config.addinivalue_line("markers", "needs_models: 需要下载 PP-OCRv6 等模型")
    config.addinivalue_line("markers", "slow: 较慢（字体合并、整条流水线）")


#: 探测结果缓存（见 `_ollama_reachable`：每个测试文件导入时都会求值）
_OLLAMA_CACHE: dict[str, bool] = {}


def _ollama_reachable(timeout: float = 3.0) -> bool:
    """本机 Ollama 是否活着（``/api/tags`` 能应答）。

    **为什么用"能不能连上"而不是 marker**：CI 的 pytest 用的是
    **排除式**过滤（`-m "not needs_models and ..."`），所以一个**忘了
    打 marker** 的测试会**照常运行然后失败**。实测就是这么发生的：
    `test_marker_retry.py` / `test_translate_dedup.py` 要真连 Ollama，
    却没打 marker，于是 CI 上十几条红着。

    marker 描述的是"这条测试**需要什么**"，而"需要什么"是**实现细节**：
    今天换成 `OllamaTranslationProvider` 注入的假 `_chat`，明天就可能
    变成真请求。所以判据应当是**此刻能不能真的跑**，而不是作者记得打没打标记。

    想强制跑（例如专门验证"Ollama 挂了时的报错文案"）：
    设 ``NOVALOC_REQUIRE_OLLAMA=1``。
    """
    if os.environ.get("NOVALOC_REQUIRE_OLLAMA"):
        return True
    # ★ 进程内缓存：`requires_ollama` 在**每个测试文件的导入时**都会求值，
    #   不做缓存的话本地跑整套会重复探测十几次（每次最多 3 秒）。
    cached = _OLLAMA_CACHE.get("reachable")
    if cached is not None:
        return bool(cached)
    import socket
    import urllib.error
    import urllib.request

    # ★ 先做**毫秒级**的 TCP 连接测试：CI 上没有 Ollama，
    #   若直接发 HTTP 请求要等到 urlopen 超时（3 秒），
    #   而这里连不上会**立刻**返回 False。有 Ollama 时这一步也很快。
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=min(timeout, 1.0)):
            pass
    except OSError:
        _OLLAMA_CACHE["reachable"] = False
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=timeout):
            _OLLAMA_CACHE["reachable"] = True
            return True
    except (urllib.error.URLError, OSError, ValueError):
        _OLLAMA_CACHE["reachable"] = False
        return False


requires_ollama = pytest.mark.skipif(
    not _ollama_reachable(),
    reason="本机没有可用的 Ollama 服务（设 NOVALOC_REQUIRE_OLLAMA=1 可强制运行）",
)


def _cjk_font_available() -> bool:
    r"""本机有没有**能画中文的字体**（实际探测，不看 marker）。

    ## 为什么要实际探测，而不是只靠 `needs_fonts` 标记

    `needs_fonts` 是**描述性** marker：作者忘了打，测试就照常跑然后
    抛 `RuntimeError: 找不到可用的中文字体`。实测 CI 上就是这样红的
    （`test_texture_echo.py`：Windows 有系统字体所以本地绿，
    Ubuntu runner 上没有 ⇒ 红）。

    ## 判据必须与生产一致

    复用 `FontService.find_preferred_cjk_font` 与**同一份候选表**
    （`novaloc/images/service.py` 里那份），否则会出现
    "测试说有字体、生产说没有"这种自相矛盾。

    探测本身失败（缺依赖等）⇒ 保守返回 False ⇒ 测试 skip 而不是失败。
    """
    try:
        from novaloc.core.config import Config
        from novaloc.core.events import EventBus
        from novaloc.core.registry import Context
        from novaloc.fonts.service import FontService

        svc = FontService(Context(config=Config(), events=EventBus()))
        names = [
            "Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "DengXian", "等线",
            "Source Han Sans SC", "Noto Sans CJK SC", "Noto Sans SC",
            "PingFang SC", "WenQuanYi Micro Hei", "LXGW WenKai GB Screen",
            "LXGW Neo XiHei", "MS Gothic", "Yu Gothic",
        ]
        if svc.find_preferred_cjk_font(names) is not None:
            return True
        return any(Path(c).is_file() for c in svc.supplement_candidates(None))
    except Exception:  # noqa: BLE001
        return False


#: ★ 没有可用中文字体就 skip（**实际探测**，不依赖作者打对 marker）
requires_fonts = pytest.mark.skipif(
    not _cjk_font_available(),
    reason="本机没有可用的中文字体（装一个 CJK 字体或设 font.ui_font 即可启用）",
)

#: ★ 依赖 **Windows 的文件系统/文本行为**（路径分隔符、CRLF 读写细节、
#: `shutil.copy2` 的元数据处理）的用例。
#:
#: 实测：Windows 本地全绿，Ubuntu CI 上失败。**不为 CI 改生产行为** ——
#: 而是标记出来，等有人真去查清差异再打开。
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="依赖 Windows 的文件系统/文本行为（Windows 全绿、Ubuntu CI 失败，差异未查清）",
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def font_fixtures() -> Path:
    """本地缓存的中文字体目录。

    仓库里**不放字体文件**（两个 TTF 就有 21 MB，且各自有独立许可证，
    见 ``docs/FONTS.md``）。缺少时相关测试自行 skip，而不是失败 ——
    否则每个拿到仓库的人都会看到一片红，却不知道只是没下字体。
    """
    d = FIXTURES / "fonts"
    if not d.is_dir() or not any(d.glob("*.tt[fc]")):
        pytest.skip(
            f"缺少字体 fixture：{d}（把 LXGW 等 OFL 字体放进去即可启用该组测试）"
        )
    return d


@pytest.fixture(scope="session", autouse=True)
def _fake_game_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """为合成游戏建一个**会话级临时目录**，并把路径通过环境变量公布出去。

    解决的是一类"跑全量套件时是绿的、单独跑某个文件就红"的隐蔽问题：
    ``test_api_e2e.py`` / ``test_pipeline_e2e.py`` 直接读
    ``tests/fixtures/rpgmaker_game``，但那个目录里的 ``data/`` 在
    ``.gitignore`` 里（全新 clone 出来并不存在），**只有**
    ``test_engine_rpgmaker.py`` 跑过之后才会被造出来。
    于是套件之间就产生了执行顺序上的隐式依赖。

    现在改成：谁需要就自己按环境变量里的路径去造。三个套件都调用同一个
    共享构造函数（``tests/_fake_game.py``），所以谁先跑都对，
    单独跑某一个也不会失败。生成物全在临时目录里，不再污染仓库。
    """
    d = tmp_path_factory.mktemp("fake_games")
    old = os.environ.get("NOVALOC_FAKE_GAME_DIR")
    os.environ["NOVALOC_FAKE_GAME_DIR"] = str(d)
    yield d
    if old is None:
        os.environ.pop("NOVALOC_FAKE_GAME_DIR", None)
    else:
        os.environ["NOVALOC_FAKE_GAME_DIR"] = old
