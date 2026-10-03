r"""检查并修正 **Windows 脚本的编码硬要求**。

## 两条要求，都是实测踩出来的

### `.cmd`：必须 **CRLF + 纯 ASCII**

* **CRLF**：`cmd.exe` 用 LF-only 行尾时会**按字节偏移错位地读**，
  把每行开头几个字符吃掉。实测输出：

      REM NovaLoc busy-watcher launcher (ASCII ONLY).
        -> 'sy-watcher' is not recognized as an internal or external command
      setlocal
        -> 'use' is not recognized ...

  看起来像文件损坏，其实只是行尾。**极难联想到**。

* **纯 ASCII**：cmd.exe 按 OEM 代码页（本机 GBK）解析 `.cmd`。
  UTF-8 中文会被当 GBK 解，容易在字符串中间拼出 `"` 或 `&` ⇒ 语法崩。

### `.ps1`：必须 **UTF-8 with BOM**

PowerShell 5.1 读无 BOM 的文件时按 **ANSI(GBK)** 解码，
文件里的中文会乱码（已有的 `scripts/fix-ps1-bom.ps1` 负责加 BOM）。
本脚本只**检查**，不重复那个功能。

### 附带的坑：注释不是安全区

`.ps1` 里连**注释**中都不能出现"美元符 + 变量名 + 冒号"的形状
（如 `$round:`），它会被当成**驱动器引用**而 ParserError。
所以本脚本还会扫这一条 —— 用 PowerShell 自己的 Parser 做语法校验，
那是唯一可信的判据（正则判断容易漏）。

## 用法

    python scripts/check-script-encoding.py            # 只检查
    python scripts/check-script-encoding.py --fix      # 检查并修正
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

CMD_SUFFIXES = {".cmd", ".bat"}
PS_SUFFIXES = {".ps1"}


def check_cmd(path: pathlib.Path, *, fix: bool) -> list[str]:
    """返回问题列表；``fix`` 则就地修成 CRLF + ASCII 检查。"""
    problems: list[str] = []
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    # 行尾
    crlf = raw.count(b"\r\n")
    lf_total = raw.count(b"\n")
    lf_only = lf_total - crlf
    if lf_only:
        problems.append(f"有 {lf_only} 行是 LF-only（cmd.exe 会错位解析）")

    # 非 ASCII
    bad_lines = []
    for i, ln in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if any(ord(c) > 127 for c in ln):
            bad_lines.append(i)
    if bad_lines:
        problems.append(f"有 {len(bad_lines)} 行含非 ASCII（GBK 解析会崩）：行 {bad_lines[:6]}")

    if fix:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out = "\r\n".join(lines)
        if not out.endswith("\r\n"):
            out += "\r\n"
        if bad_lines:
            # 中文行没法自动改成 ASCII（语义会丢），只报不改
            pass
        else:
            path.write_bytes(out.encode("ascii"))
    return problems


def check_ps1(path: pathlib.Path, *, fix: bool) -> list[str]:
    """检查 BOM 与语法（语法用 PowerShell 自己的 Parser）。"""
    problems: list[str] = []
    raw = path.read_bytes()
    has_bom = raw[:3] == b"\xef\xbb\xbf"
    if not has_bom:
        problems.append("缺 UTF-8 BOM（PS 5.1 会按 GBK 解码中文）")
        if fix:
            path.write_bytes(b"\xef\xbb\xbf" + raw)

    # 语法：用 PowerShell Parser 才算数
    script = (
        "$e=$null;"
        f"$null=[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path.resolve()}',[ref]$null,[ref]$e);"
        "if($e){$e|ForEach-Object{$_.Message}}else{'OK'}"
    )
    try:
        p = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=60,
            check=False,
        )
        out = p.stdout.decode("utf-8", errors="replace").strip()
        # GBK 控制台可能把 OK 也弄乱，只要不含典型错误字样就认为通过
        if out and "OK" not in out:
            problems.append(f"语法错误：{out.splitlines()[0][:90]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        problems.append(f"语法校验跑不起来：{type(exc).__name__}")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="检查 Windows 脚本的编码硬要求")
    ap.add_argument("--fix", action="store_true", help="就地修正能自动修的问题")
    ap.add_argument("paths", nargs="*", help="默认扫 scripts/ 与仓库根的 .cmd/.ps1")
    ns = ap.parse_args(argv)

    root = pathlib.Path(__file__).resolve().parents[1]
    targets = [pathlib.Path(p) for p in ns.paths] or sorted(
        list((root / "scripts").glob("*")) + list(root.glob("*.cmd")) + list(root.glob("*.ps1"))
    )

    n_bad = 0
    for p in targets:
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in CMD_SUFFIXES:
            probs = check_cmd(p, fix=ns.fix)
        elif suf in PS_SUFFIXES:
            probs = check_ps1(p, fix=ns.fix)
        else:
            continue
        if probs:
            n_bad += 1
            print(f"  ❌ {p.name}")
            for x in probs:
                print(f"       {x}")
            if ns.fix:
                print("       （已尝试修正，请重跑确认）")
        else:
            print(f"  ✅ {p.name}")

    print()
    print(f"  检查完成：{len(targets)} 个目标，{n_bad} 个有问题")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
