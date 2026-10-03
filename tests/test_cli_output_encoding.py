r"""★ 回归：CLI 的**重定向输出必须是 UTF-8**（不是系统代码页 GBK）。

## 这个 bug 为什么值得单独一个测试文件

它一直存在到 v1.6.0，而且**在终端里看不出来** —— 终端按控制台代码页
显示，GBK 反而"正常"。只有**重定向到文件**时才暴露 ⇒ 恰好是所有
后台服务、CI、日志采集的场景。

它真正咬人的方式很隐蔽：**同一个日志文件里两种编码混排**。

    [2026-10-03 13:18:50] 守望服务启动：库=...        ← PowerShell 写，UTF-8
    鎵弿娓告垙搴?E:\lush\1\newlytransport            ← Python CLI 写，GBK

于是任何按 UTF-8 读日志的工具都会看到一半乱码，而**排查方向会被
带偏**（以为"日志坏了"，实际是写入方用了系统代码页）。

## 根因（值得记住，因为它和"看起来对"的代码相反）

`pyproject.toml` 里写的是：

    novaloc = "novaloc.cli:app"      # ← Typer **应用对象**本身

于是 `novaloc --version` 直接调用 Typer 应用，而 `main()` 只是它的
**callback**。`--version` 是 **eager** 选项，callback 里那个
`raise typer.Exit()` 让函数体（也就是 `reconfigure`）**永远不执行**。

⚠️ 所以"代码里有 reconfigure"和"reconfigure 真的跑了"是两件事 ——
这正是本项目反复吃亏的同一类问题（见 CHANGELOG「两段重复实现互相抵消」）。

## 判据（事先写死，且不靠"看着像"）

跑真实控制台脚本，**显式清掉 `PYTHONIOENCODING`**（否则测的是环境变量
而不是代码），然后对字节做**编码判定**：

| 字节形态 | 判定 |
| --- | --- |
| 能按 UTF-8 解码 | ✅ 通过 |
| 按 UTF-8 失败、按 GBK 成功 | ❌ 就是本 bug |
| 两者都失败 | ❌ 混排，更糟 |

同时断言**中文确实在输出里**（防止"输出为空也算通过"这种假绿）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "novaloc.exe"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

pytestmark = pytest.mark.skipif(
    not EXE.is_file(), reason="需要已安装的控制台脚本 .venv/Scripts/novaloc.exe"
)


def _clean_env() -> dict[str, str]:
    """清掉会"替代码遮丑"的环境变量。

    ★ `PYTHONIOENCODING=utf-8` 会让输出变对 —— 但那是**环境变量**的功劳，
    不是代码的。本项目开发环境恰好设了它，所以这个 bug 在本机开发时
    完全隐形。测试必须把它去掉，才能测到代码本身。
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8")
    }
    env["PYTHONPATH"] = "src"
    return env


def _classify(raw: bytes) -> str:
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        try:
            raw.decode("gbk")
            return "gbk"
        except UnicodeDecodeError:
            return "mixed"


def _run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(EXE), *args],
        capture_output=True,
        env=_clean_env(),
        cwd=str(ROOT),
        timeout=300,
    )


def test_version_output_is_utf8() -> None:
    """★ `--version`（eager 路径）必须是 UTF-8。

    这是**最容易被漏掉**的路径：它不经过 `main()` 的函数体，
    所以只有"模块导入时就切编码"才能救它。
    """
    r = _run(["--version"])
    assert r.returncode == 0, f"退出码 {r.returncode}: {r.stderr[:300]!r}"
    kind = _classify(r.stdout)
    assert kind == "utf-8", (
        f"`novaloc --version` 输出不是 UTF-8 而是 {kind}\n"
        f"  字节: {r.stdout[:40].hex(' ')}\n"
        f"  按 GBK: {r.stdout.decode('gbk', 'replace').strip()!r}\n"
        f"  ⇒ 检查 cli.py 里 `_ensure_utf8_streams()` 是否在**模块导入时**调用"
    )
    text = r.stdout.decode("utf-8")
    assert "NovaLoc" in text
    assert "\u65b0\u8bd1" in text, f"输出里应当有「新译」两个字：{text!r}"


def test_help_output_is_utf8() -> None:
    """★ `--help` 同样走 eager 路径，且**内容最多中文**。

    帮助文本里有大量中文说明，所以它是"编码坏掉"最明显的受害者。
    """
    r = _run(["--help"])
    assert r.returncode == 0, f"退出码 {r.returncode}: {r.stderr[:300]!r}"
    kind = _classify(r.stdout)
    assert kind == "utf-8", (
        f"`novaloc --help` 输出不是 UTF-8 而是 {kind}\n"
        f"  字节: {r.stdout[:40].hex(' ')}"
    )
    text = r.stdout.decode("utf-8")
    assert "Usage" in text or "用法" in text
    # 帮助里一定有中文（命令说明都是中文）
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    assert cjk > 20, f"帮助文本里中文太少（{cjk} 个）⇒ 可能压根没输出全"


def test_subcommand_help_is_utf8() -> None:
    """★ 叶子命令的帮助（走 Typer 子命令 machinery）也必须是 UTF-8。"""
    r = _run(["auto", "--help"])
    assert r.returncode == 0, f"退出码 {r.returncode}: {r.stderr[:300]!r}"
    assert _classify(r.stdout) == "utf-8", (
        f"`novaloc auto --help` 不是 UTF-8：{r.stdout[:40].hex(' ')}"
    )
    assert "--watch-only" in r.stdout.decode("utf-8")


def test_stderr_is_utf8_too() -> None:
    """★ stderr 也要是 UTF-8 —— 日志的**错误行**同样会被采集。

    用"不存在的库"逼出一条中文错误信息（走 `console.print` 或 logging，
    两者都可能落到 stderr）。
    """
    r = _run(["auto", "Z:\\definitely\\not\\a\\real\\path"])
    assert r.returncode != 0, "不存在的库应当失败"
    combined = r.stdout + r.stderr
    assert combined, "应当有错误输出"
    assert _classify(combined) == "utf-8", (
        f"错误输出不是 UTF-8：{combined[:60].hex(' ')}\n"
        f"  按 GBK: {combined.decode('gbk', 'replace')[:200]!r}\n"
        f"  按 UTF-8: {combined.decode('utf-8', 'replace')[:200]!r}"
    )


def test_entry_point_is_a_function_not_the_app_object() -> None:
    """★ 防回归（结构性）：入口必须是**函数**，不能是 Typer 应用对象。

    写成 `novaloc.cli:app` 时 `main()` 的函数体不会执行 ⇒ 放在里面的
    初始化逻辑（含编码修正）静默失效。这个断言直接钉住 `pyproject.toml`，
    因为该文件的这一行**看起来完全正常**。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'novaloc = "novaloc.cli:run_app"' in text, (
        "入口点必须指向函数 `run_app`；写成 `:app` 会让 `main()` 的"
        "函数体不执行（见本文件 docstring 的根因分析）"
    )
    assert 'novaloc = "novaloc.cli:app"' not in text, (
        "检测到旧的错误入口 `novaloc.cli:app`"
    )


def test_module_level_reconfigure_exists() -> None:
    """★ 防回归（结构性）：`_ensure_utf8_streams()` 必须在**模块级**被调用。

    只在 `main()` 里调用是不够的 —— eager 路径（`--version`/`--help`）
    不会进 `main()` 的函数体。所以要有一次**模块导入时**的调用。
    """
    src = (ROOT / "src" / "novaloc" / "cli.py").read_text(encoding="utf-8")
    assert "def _ensure_utf8_streams()" in src
    # 模块级调用：以行首开始（无缩进），区别于 main() 里的那次
    module_level = [
        ln for ln in src.splitlines()
        if ln.strip() == "_ensure_utf8_streams()" and not ln.startswith((" ", "\t"))
    ]
    assert module_level, (
        "找不到**模块级**的 `_ensure_utf8_streams()` 调用 ⇒ "
        "`--version` / `--help` 会重新变回 GBK"
    )


def test_reconfigure_failure_never_raises(tmp_path: Path) -> None:
    """★ 健壮性：被包装/非文本流不支持 `reconfigure` 时**不许抛**。

    编码修不好不该让整个命令失败。

    ⚠️ 写进**真正的 .py 文件**再跑，不能用 `-c`：
    `-c` 里没法用分号拼一个 `class` 定义（实测 `SyntaxError`）。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from novaloc.cli import _ensure_utf8_streams\n"
        "\n"
        "class Bad:\n"
        "    encoding = 'gbk'\n"
        "    def reconfigure(self, **kw):\n"
        "        raise OSError('nope')\n"
        "\n"
        "sys.stdout = Bad()\n"
        "sys.stderr = Bad()\n"
        "_ensure_utf8_streams()\n"
        "print('survived', file=sys.__stdout__)\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [str(PY), str(probe)], capture_output=True,
        env=_clean_env(), cwd=str(ROOT), timeout=300,
    )
    # ★ 判据是"没被异常打断"，不是退出码：探针把一个非流的 `Bad()` 装成
    #   `sys.stdout`，解释器退出时 flush 它会拿到 120（CPython 的
    #   "error in sys.stdout.flush()" 专用码）。那与 `_ensure_utf8_streams`
    #   无关，所以这里断言"没有 traceback" + "确实走到了下一行"。
    assert b"survived" in r.stdout, (
        f"`_ensure_utf8_streams()` 应当吞掉 reconfigure 的异常并继续："
        f"{r.stderr[:400]!r}"
    )
    assert b"Traceback" not in r.stderr, (
        f"不该有回溯 —— reconfigure 的异常必须被吞掉：{r.stderr[:400]!r}"
    )
    assert b"OSError" not in r.stderr, (
        f"OSError 不该漏出来：{r.stderr[:400]!r}"
    )
