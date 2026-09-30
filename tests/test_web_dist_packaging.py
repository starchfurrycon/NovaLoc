"""确认前端构建产物**随包分发**，且当它缺失时会明确报错。

这一条是被真实的"静默残废"逼出来的：前端产物原本放在仓库根的
``web/dist``，而 ``pyproject.toml`` 里 hatchling 只打包 ``src/novaloc``，
于是 **wheel 里没有前端**。GitHub 自动生成的 wheel 不会因此报错，
``pip install nova-loc`` 装完所有接口正常、只有根路径 404，
用户看到的是"打不开页面"而毫无线索。

而且这个 bug **极其容易漏测**：在仓库里跑 ``create_app()`` 永远成功，
因为 ``_web_dist()`` 找到了源码树里的那份。所以下面
``test_bundled_path_layout_is_package_relative`` 断言的是
"包内那条路径确实存在"，而不是"能找到一个目录"。

放在包内（而不是让 hatchling 额外收一个包外目录）是刻意的：
这样 ``pip install`` 与 ``pip install -e .`` 用的是同一批产物，
不会有"开发时能跑、装完就废"的偏差。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from novaloc.api.app import _web_dist  # noqa: E402


def test_web_dist_is_found() -> None:
    """前端产物必须能被找到（源码树里跑）。"""
    d = _web_dist()
    assert d is not None, "找不到前端产物"
    assert (d / "index.html").is_file()


def test_bundled_path_layout_is_package_relative() -> None:
    """产物必须在**包内**（``src/novaloc/web_dist``）。

    这是保证 wheel 里也有的前提 —— hatchling 只收 ``src/novaloc``。
    如果哪天有人为了"顺手"把产物挪到仓库根，这个测试会立刻失败，
    而不是等到用户 ``pip install`` 之后才发现界面打不开。
    """
    pkg_dir = SRC / "novaloc"
    bundled = pkg_dir / "web_dist"
    assert (bundled / "index.html").is_file(), (
        f"前端产物不在包内：期望 {bundled / 'index.html'}。"
        "放进包内才能随 wheel 分发（见本文件顶部说明）。"
    )
    # 也必须真的被 _web_dist 优先选中
    d = _web_dist()
    assert d is not None and d.resolve() == bundled.resolve()


def test_gyro_pyproject_wheel_target_covers_web_dist() -> None:
    """``pyproject.toml`` 的 wheel 目标是 ``src/novaloc``。

    这个断言看着像在测配置文件，但它是**因果链的关键一环**：
    只要 wheel 收的是整个 ``src/novaloc``，包内的 ``web_dist``
    就一定会被打进去。哪天有人把 ``packages`` 收窄成具体子模块，
    前端就会再次静默消失。
    """
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.hatch.build.targets.wheel]" in text
    assert 'packages = ["src/novaloc"]' in text, (
        "wheel 打包目标变了。若不再是整个 src/novaloc，"
        "必须先确认 web_dist 仍在包里（否则 wheel 会缺前端）。"
    )


def test_assets_dir_exists_with_built_files() -> None:
    """``index.html`` 引用的 ``assets/`` 必须真的存在，否则页面白屏。"""
    d = _web_dist()
    assert d is not None
    assets = d / "assets"
    assert assets.is_dir(), "缺 assets/ 目录，前端会白屏"
    files = list(assets.iterdir())
    assert any(f.suffix == ".js" for f in files), "缺打包后的 JS"
    # CSS 可能被 Tailwind 产出，检查存在性但容忍极端情况
    assert any(f.suffix == ".css" for f in files), "缺打包后的 CSS"


def test_missing_web_dist_does_not_break_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """前端缺失时 **API 仍要正常**，只是界面不可用。

    这不是"期望如此"，而是必须保证的降级：后端是工具的全部能力所在，
    不能因为静态资源缺失就整个起不来。
    """
    import novaloc.api.app as app_mod

    monkeypatch.setattr(app_mod, "_web_dist", lambda: None)
    app = app_mod.create_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert any(p and p.startswith("/api") for p in paths), "API 路由应当仍然存在"
    # 前端专属的根路由不应被注册（否则会 FileResponse 一个不存在的文件）
    assert "/" not in paths, "前端缺失时不该注册根路由"


def test_module_is_importable_as_module_not_instance() -> None:
    """``import novaloc.api.app as m`` 必须拿到**模块**。

    这里踩过一个很难查的坑：模块末尾原本写着 ``app = create_app()``，
    那个全局变量会**遮蔽同名的子模块**，于是上面这行拿到的是 FastAPI
    实例，``m._web_dist`` 直接
    ``AttributeError: 'FastAPI' object has no attribute '_web_dist'``。
    而 ``from novaloc.api.app import create_app`` 却是好的
    （import 机制会回退到 ``sys.modules``）—— 只在 ``import ... as`` 时炸。
    改用 PEP 562 的模块级 ``__getattr__`` 后两种写法都对。
    """
    import novaloc.api.app as app_mod  # noqa: PLC0415

    assert isinstance(app_mod, types.ModuleType), (
        f"`import novaloc.api.app` 拿到的不是模块而是 {type(app_mod).__name__}；"
        "模块里不该存在叫 `app` 的模块级变量（见 app.py 的 __getattr__ 说明）"
    )
    assert callable(app_mod._web_dist)
    # 工厂必须能直接被调用（uvicorn 的 "novaloc.api.app:create_app" 依赖这点）
    assert callable(app_mod.create_app)
    # 实例仍然取得到，且是惰性单例
    assert app_mod.get_app() is app_mod.get_app()
    # 未知属性要照常报 AttributeError，不能什么都返回
    with pytest.raises(AttributeError):
        app_mod.definitely_not_a_real_attribute  # noqa: B018


def test_version_is_single_source() -> None:
    """版本号必须只有**一个**来源，三处不许各写一份。

    真实事故：``pyproject.toml``、``novaloc/__init__.py``、
    ``api/app.py`` 各写了一份版本号。发版时前两个升到 0.2.0，
    第三个忘了 —— 于是 ``/api/health`` 报 0.1.0，
    而打包出来的 wheel 叫 0.2.0。这种"能跑但信息是错的"最难发现。

    现在 ``pyproject.toml`` 用 ``dynamic = ["version"]`` +
    ``[tool.hatch.version] path = ...`` 从包读取，
    ``api/app.py`` 直接 import 包里的 ``__version__``。
    """
    import tomllib  # noqa: PLC0415

    import novaloc  # noqa: PLC0415

    raw = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    project = data["project"]

    assert "version" not in project, (
        "pyproject.toml 里又出现了写死的 version —— "
        "那就会和包里的 __version__ 漂移。请保持 dynamic = [\"version\"]。"
    )
    assert "version" in project.get("dynamic", []), "缺少 dynamic = [\"version\"]"
    hatch_version = data.get("tool", {}).get("hatch", {}).get("version", {})
    assert hatch_version.get("path") == "src/novaloc/__init__.py", (
        f"hatch 的版本来源应为 src/novaloc/__init__.py，实际 {hatch_version.get('path')!r}"
    )

    # 运行时三方必须一致
    from novaloc.api.app import VERSION  # noqa: PLC0415

    assert novaloc.__version__ == VERSION, f"api VERSION={VERSION} 与包版本 {novaloc.__version__} 不一致"
    assert novaloc.__version__, "版本号不能为空"
