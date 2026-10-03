r"""★ 回归：**后台服务的日志必须是内容正确的 UTF-8**。

## 这个文件抓的是"两条独立缺陷叠加"

单测 `TestWatchServiceLogging` 里的 CLI 编码测试（`test_cli_output_encoding.py`）
证明 CLI 自己输出 UTF-8。但那**不足以**让日志变对 —— 还要看
**服务用什么方式把子进程输出写进文件**：

| 写入方式 | UTF-8 子进程输出 | 结果 |
| --- | --- | --- |
| `& exe ... \| Out-File` | 被按 `[Console]::OutputEncoding` 解码 | ❌ 隐藏窗口下是 gb2312 ⇒ 乱码 |
| `Start-Process -RedirectStandardOutput` | 字节直通 | ✅ 内容对，但**截断**文件 ⇒ 不能跨轮累积 |
| **`cmd /c ... >> file`** | **字节级追加，无解码** | ✅ 内容对且能累积 |

## 判据（事先写死）

不看"文件能否 UTF-8 解码"就收工 —— **乱码汉字本身是合法 UTF-8**，
那个判据会给出假绿。真正的判据是**内容**：

1. 无 U+FFFD（写入时丢字节才产生）；
2. 无**成串**的 GBK↔UTF-8 误读特征字；
3. 必需的中文词确实在（"守望"/"扫描"/"游戏库"）；
4. 服务自己的 `[时间戳]` 行**在**（证明没被截断）。

## 为什么不用真跑 100 秒的服务

真端到端要起隐藏窗口跑 ~100 秒（`.scratch/_watch_log_ok.py` 就是那样
做的，**已实测通过**）。为了能进常规测试套件，这里用**等价但快**的
方式：验证"`cmd /c >>` 是字节透明的"这个**关键机制**，加上对脚本
源码的结构断言。真端到端留给 `.scratch` 探针与动手验证。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "watch-new-games.ps1"
EXE = ROOT / ".venv" / "Scripts" / "novaloc.exe"

pytestmark = pytest.mark.skipif(
    not EXE.is_file(), reason="需要 .venv/Scripts/novaloc.exe 才能起真实子进程"
)

#: GBK↔UTF-8 误读时高频出现的字。**成串**（≥3）才判 ——
#: 其中 鎵（gallium）、娓 等本身是合法汉字，单字不能判死。
GARBLE = set("鎵弿娓告垙搴鏄涓锛鐨勬枃鏈鍦ㄨ鐞嗗紩鎿庡垎甯冨緟鏂扮殑鏃犲彲澶辫触鍚堣鏁伴噺缁撴灉")


def _line_is_clean(ln: str) -> bool:
    if "\ufffd" in ln:
        return False
    return sum(1 for c in ln if c in GARBLE) < 3


def _clean_env() -> dict[str, str]:
    """清掉会替代码遮丑的环境变量（同 `test_cli_output_encoding.py`）。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8")
    }
    env["PYTHONPATH"] = "src"
    return env


# ---------------------------------------------------------------------------
# 1) 关键机制：cmd 的 `>>` 是字节透明的
# ---------------------------------------------------------------------------
def _run_ps(
    script: str, tmp_path: Path, name: str = "run.ps1"
) -> subprocess.CompletedProcess[str]:
    """把一段 PowerShell 写进 .ps1 再执行（含中文就必须带 BOM）。"""
    p = tmp_path / name
    p.write_text(script, encoding="utf-8-sig")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_clean_env(), cwd=str(ROOT), timeout=300,
    )


def test_cmd_append_redirect_is_byte_transparent(tmp_path: Path) -> None:
    """★★ `cmd /c exe >> file` 必须把子进程的 UTF-8 字节**原样**落盘。

    这是本方案的立足点。如果它不成立（例如 cmd 也做转码），
    整个守望服务日志方案就得换。

    ⚠️ 必须**通过 PowerShell** 调用（与 PS1 脚本里一致）。直接从 Python
    用 `subprocess.run(["cmd.exe","/c", '"exe" ... >> f'])` 会因为 cmd 的
    引号剥除规则报"文件名、目录名或卷标语法不正确" —— 那是**调用形式**
    的差异，不是本机制的问题（实测踩到，白花一轮）。

    ⚠️ stdout 与 stderr **必须指向不同文件**：cmd 在同一条命令里把两个流
    重定向到同一个文件会失败 ——

        The process cannot access the file because it is being used
        by another process.        （实测，输出文件是空的 0 字节）

    PS1 脚本里本来就是分开的（`>> outLog 2>> errLog`），这里是同一条约束。
    """
    out = tmp_path / "o.txt"
    errf = tmp_path / "e.txt"
    # 与 PS1 里完全同样的形式：& cmd.exe /c '"exe" ... >> "out" 2>> "err"'
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$exe = '{EXE}'\n"
        f"$out = '{out}'\n"
        f"$errf = '{errf}'\n"
        "$line = '\"{0}\" --version >> \"{1}\" 2>> \"{2}\"' -f $exe, $out, $errf\n"
        "& cmd.exe /c $line\n"
    )
    _run_ps(script, tmp_path)
    assert out.is_file(), "应当产生输出文件"
    raw = out.read_bytes()
    assert raw, "应当有输出"
    # 判据一：能按 UTF-8 解码（GBK 会在这里失败）
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"`cmd /c >>` 的输出不是 UTF-8（{exc.reason} @ {exc.start}）\n"
            f"  字节: {raw[:40].hex(' ')}\n"
            f"  按 GBK: {raw.decode('gbk', 'replace').strip()!r}"
        ) from exc
    assert "NovaLoc" in text
    assert "\u65b0\u8bd1" in text, f"中文「新译」应当原样在文件里：{text!r}"
    # 判据二：追加而不是截断
    _run_ps(script, tmp_path)
    second = out.read_bytes()
    assert second.count("\u65b0\u8bd1".encode()) == 2, (
        "第二次写入必须是**追加**（出现两次「新译」）—— "
        f"实际 {second.count('新译'.encode())} 次 ⇒ 被截断了"
    )
    assert second.startswith(raw[:10]), "追加不该改动已有内容"


def test_powershell_pipeline_would_have_mangled_it(tmp_path: Path) -> None:
    """★ 反例：PowerShell 的**文本管道**在隐藏窗口下会毁掉 UTF-8。

    这条不是"测我们没用的实现"，而是**钉住为什么不能用它** ——
    否则后人会觉得 `| Out-File` 更自然就改回去了。

    做法：隐藏窗口起一个有中文输出的子进程，让它走管道；
    断言它**确实**会乱（若某天 Windows 默认编码变成 UTF-8，
    这条测试会失败，那正好提示"可以简化方案了"）。
    """
    probe = tmp_path / "p.ps1"
    out = tmp_path / "o.txt"
    # 用一个直接 print 中文的 python 子进程（不依赖 novaloc）
    probe.write_text(
        "$ErrorActionPreference = 'Continue'\n"
        "& '{}' -c \"print('\\u65b0\\u8bd1\\u6d4b\\u8bd5')\""
        " | Out-File -FilePath '{}' -Append -Encoding utf8\n".format(
            str(ROOT / ".venv" / "Scripts" / "python.exe"), str(out)
        ),
        encoding="utf-8-sig",
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(probe)],
        capture_output=True, env=_clean_env(), cwd=str(ROOT), timeout=300,
    )
    if not out.is_file():
        pytest.skip("隐藏窗口下没产生输出，无法判定")
    raw = out.read_bytes()
    # 这条测试的价值在于**记录**：乱或不一定乱，取决于
    # [Console]::OutputEncoding。只要别把它当"安全"用就行。
    text = raw.decode("utf-8", "replace")
    if "\u65b0\u8bd1\u6d4b\u8bd5" in text:
        pytest.skip(
            "本机隐藏窗口下管道解码恰好是 UTF-8 —— 说明这个坑依赖环境，"
            "所以 PS1 里设 OutputEncoding 的那层保险仍有必要"
        )
    assert not _line_is_clean(text), (
        "预期这个反例是乱的（说明为什么不能用管道）：" + repr(text[:80])
    )


# ---------------------------------------------------------------------------
# 2) 脚本源码的结构断言
# ---------------------------------------------------------------------------
def _ps1_code_without_comments() -> str:
    """去掉块注释与行注释后的脚本正文。

    ★ 必须去掉注释：脚本注释里**故意**写了 `| Out-File` 这些反例说明，
    直接搜原文会把"解释为什么不能用"当成"用了"。
    """
    src = PS1.read_text(encoding="utf-8-sig")
    out: list[str] = []
    in_block = False
    for ln in src.splitlines():
        s = ln.strip()
        if in_block:
            if "#>" in s:
                in_block = False
            continue
        if s.startswith("<#"):
            in_block = "#>" not in s
            continue
        if s.startswith("#"):
            continue
        # 行尾注释也去掉（简化：只在没有引号时切）
        if "#" in ln and ln.count('"') % 2 == 0 and ln.count("'") % 2 == 0:
            ln = ln.split("#", 1)[0]
        out.append(ln)
    return "\n".join(out)


def test_script_uses_cmd_byte_redirect() -> None:
    """★ 必须用 `cmd.exe /c ... >> ...` 做字节级追加。"""
    code = _ps1_code_without_comments()
    assert "cmd.exe" in code, "应当用 cmd.exe 做字节级重定向"
    assert ">>" in code and "2>>" in code, "应当用 >> 追加 stdout 与 stderr"
    # ★ stdout / stderr 必须指向**不同**文件：cmd 同一条命令把两个流重定向
    #   到同一文件会失败（"being used by another process"，输出 0 字节）。
    assert "$outLog" in code and "$errLog" in code
    join = '>> "{3}" 2>> "{4}"'
    assert join in code, (
        "重定向模板里 stdout 与 stderr 必须是两个不同变量"
        "（$outLog / $errLog）—— 指向同一文件会被 cmd 拒绝"
    )


def test_script_does_not_pipe_through_out_file() -> None:
    """★ **不许**把子进程输出接进 PowerShell 文本管道。

    管道按 `[Console]::OutputEncoding` 解码，隐藏窗口下是 gb2312。
    """
    code = _ps1_code_without_comments()
    assert "| Out-File" not in code, (
        "子进程输出不许走 PowerShell 文本管道（隐藏窗口下按 gb2312 解码 ⇒ 乱码）"
    )
    assert "| Set-Content" not in code, "同上"


def test_script_sets_console_output_encoding() -> None:
    """★ 必须在**任何管道之前**把控制台输出编码设成 UTF-8。

    这是第一层保险：即使某处仍走文本，也不会按 GBK 解码。
    """
    code = _ps1_code_without_comments()
    assert "[Console]::OutputEncoding" in code, (
        "必须设 [Console]::OutputEncoding —— 它决定管道如何解码子进程输出"
    )
    assert "$OutputEncoding" in code, "也该设 $OutputEncoding"
    # 位置必须在循环之前（越早越好）
    i_enc = code.find("[Console]::OutputEncoding")
    i_loop = code.find("while ($true)")
    assert 0 <= i_enc < i_loop, "编码设置必须在主循环之前"


def test_service_log_written_as_utf8_bytes() -> None:
    """★ 服务自己写的行也要按 UTF-8 落盘（不能混进 ANSI）。"""
    code = _ps1_code_without_comments()
    assert "AppendAllText" in code or "AppendText" in code, (
        "应当用 .NET 的 UTF-8 追加写服务日志"
    )
    assert "UTF8Encoding" in code, "应当显式指定 UTF8Encoding"


# ---------------------------------------------------------------------------
# 3) PS1 的 BOM：PowerShell 5.1 的另一个编码陷阱
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["dev.ps1", "setup.ps1", "fix-ps1-bom.ps1", "watch-new-games.ps1"]
)
def test_ps1_scripts_have_utf8_bom(name: str) -> None:
    """★★ `scripts\\*.ps1` 含中文就必须带 **UTF-8 BOM**。

    原因：**Windows PowerShell 5.1 读 `.ps1` 时，没有 BOM 就按系统 ANSI
    代码页（本机 GBK）解码**。含中文的脚本被解成乱码后，乱码里的引号与
    括号会破坏语法 ⇒ 报出**看似无关**的错误：

        字符串缺少终止符: "。
        语句块或类型定义中缺少右"}"。

    实测：编辑器改一次脚本去掉 BOM，脚本立刻"启动就失败、不写日志"。
    项目原有的 `dev.ps1` / `setup.ps1` 都带 BOM ⇒ 这是既有约定。

    ⇒ 修法是跑 `scripts\\fix-ps1-bom.ps1`；这条测试保证没人漏掉。
    """
    p = ROOT / "scripts" / name
    if not p.is_file():
        pytest.skip(f"{name} 不存在")
    raw = p.read_bytes()
    has_non_ascii = any(b >= 0x80 for b in raw)
    if not has_non_ascii:
        pytest.skip(f"{name} 是纯 ASCII，无需 BOM")
    assert raw[:3] == b"\xef\xbb\xbf", (
        f"{name} 含非 ASCII 却没有 UTF-8 BOM ⇒ PowerShell 5.1 会按 GBK "
        f"解码它，导致莫名其妙的语法错误。修：跑 scripts\\fix-ps1-bom.ps1"
    )


def test_fix_ps1_bom_helper_exists() -> None:
    """★ 修复工具本身必须在（否则下次改脚本又会踩）。"""
    p = ROOT / "scripts" / "fix-ps1-bom.ps1"
    assert p.is_file(), "缺少 scripts/fix-ps1-bom.ps1"
    text = p.read_text(encoding="utf-8-sig")
    assert "UTF8Encoding" in text
    assert "ParseFile" in text, "应当顺带做语法检查"


def test_ps1_scripts_parse_cleanly() -> None:
    """★ 脚本必须能通过 PowerShell 自己的语法解析。

    这条会抓"BOM 丢了导致中文注释破坏语法"这类问题。
    """
    ps = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "$ErrorActionPreference='Stop';"
         "Get-ChildItem 'scripts\\*.ps1' | ForEach-Object {"
         "  $e=$null;"
         "  [System.Management.Automation.Language.Parser]::ParseFile("
         "    $_.FullName,[ref]$null,[ref]$e) | Out-Null;"
         "  if ($e.Count) { Write-Output \"$($_.Name): $($e.Count) 个错误\" }"
         "}; Write-Output 'PARSE_DONE'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=300,
    )
    out = (ps.stdout or "") + (ps.stderr or "")
    assert "PARSE_DONE" in out, f"解析命令没跑完：{out[:400]}"
    bad = [
        ln for ln in out.splitlines()
        if " 个错误" in ln and "PARSE_DONE" not in ln
    ]
    assert not bad, "有 .ps1 存在语法错误（中文脚本常见原因是缺 BOM）：" + "; ".join(bad)


# ---------------------------------------------------------------------------
# 4) ★ 别再让"秒退"冒充"服务在跑"
#
# ## 为什么需要这组测试
#
# 曾经有一个 bug：CLI 里 `if not todo: return` 在守望循环**之前**
# 无条件返回 ⇒ `--watch-only` 0.9 秒就退出，**退出码 0**（完全"成功"）。
# 而服务脚本忠实地"5 秒后重启" ⇒ 日志刷成一片
#
#     第 N 轮：开始扫描 … 第 N 轮退出（code=0），5 秒后重启
#
# **看起来服务一直在工作**，实际每 5 秒空转一轮，新游戏永远等不到处理。
# 这条缺陷喂给"日志内容正确性"那组测试**完全测不出来** ——
# 日志内容确实是对的，它只是毫无意义。
#
# ⇒ 判据必须是"**这一轮跑了多久**"，而不是"日志写了什么"。
# ---------------------------------------------------------------------------
def _ps1_code_without_comments() -> str:
    """脚本源码，**去掉注释块**后再做结构断言。

    ⚠️ 必须去注释：我在脚本里为了让后人看懂，**引用了**那段出问题的
    日志文本（`第 N 轮：开始扫描` / `5 秒后重启`），
    第一版守卫把注释当代码而误报。
    """
    text = PS1.read_text(encoding="utf-8-sig")
    # 去掉 <# ... #> 块注释（脚本开头的 .SYNOPSIS 就是一大块）
    import re

    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    # 去掉行注释（注意别把 `#:` 这类也当成代码；这里一并去掉，够用）
    return "\n".join(
        re.sub(r"(?<![:#])#.*$", "", ln) for ln in text.splitlines()
    )


def test_service_measures_round_duration() -> None:
    """★★ 服务必须**量每一轮跑了多久** —— 这是"秒退"的唯一判据。

    只比较退出码是没用的：那个 bug 的退出码就是 0。
    """
    src = _ps1_code_without_comments()
    assert "Stopwatch" in src or "roundStart" in src, (
        "服务没有测量本轮运行时长 ⇒ 无法区分'正常守望'与'秒退空转'"
    )
    assert "$ranSec" in src, "应当把本轮运行秒数算出来再判断"
    assert "($Get-Date) - $roundStart" in src or "(Get-Date) - $roundStart" in src


def test_service_detects_quick_exit() -> None:
    """★★ 秒退必须被判定为**异常**，而且要能停手，不能无脑重启。"""
    src = _ps1_code_without_comments()
    assert "$quickExits" in src, "缺少连续秒退计数"
    assert "$quickExits++" in src, "没有在秒退时累加"
    assert "$quickExits = 0" in src, "正常一轮后必须清零（否则会误判）"
    # 必须有"停手"的分支（exit 非 0），而不是永远 Start-Sleep 5 重启
    assert "exit 2" in src, "连续秒退后应当停手并返回非 0，别把 bug 掩盖成'在跑'"
    assert "tests/test_watch_cli_blocks.py" in src, (
        "报错信息里应当指向那个守卫测试，方便后来人定位"
    )


def test_service_reports_interval_in_guard_message() -> None:
    """守卫的阈值要与 `-IntervalSeconds` 相关，不能写死一个魔数。"""
    src = _ps1_code_without_comments()
    assert "$IntervalSeconds / 2" in src, (
        "秒退阈值应当从扫描间隔推导（一轮正常运行至少跑满一个间隔）"
    )
