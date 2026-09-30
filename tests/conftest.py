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
