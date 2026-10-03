<#
.SYNOPSIS
    NovaLoc 新译 —— 新游戏自动汉化的**常驻守望**服务。

.DESCRIPTION
    每 10 分钟扫一次游戏库，**只处理此后新加进来的游戏**。
    现有的一大批游戏由单独的全库 `auto` 负责，这里不碰。

    为什么用 `--watch-only` 而不是 `--watch`：
      `--watch` 会先把库里现有的所有游戏跑完 —— 那要很久，
      而本脚本的职责是"守望新游戏"，不是"补跑旧游戏"。

    崩了就重启：
      守望模式自己会处理异常，但进程被杀或机器重启时需要有人拉起来。
      重启不会重跑整库 —— 启动时会打印"读回 N 个已见游戏"，
      状态存在 `<数据根>\watch-state.json`。

.NOTES
    ★ 为什么是 .ps1 而不是 .cmd（踩过的坑）：
      一开始写的是 `watch-new-games.cmd`，里面带中文注释。结果
      **cmd.exe 按控制台 OEM 代码页（本机 GBK）解析 .cmd 文件**，
      不是 UTF-8 ⇒ 中文注释被解码成乱码 ⇒ cmd 把乱码**当命令执行** ⇒
      满屏 "'告垙锛夈€?REM' 不是内部或外部命令"。

      改成纯 ASCII 的 .cmd 能解决解析问题，但 `%DATE%` 在中文系统上
      会展开出"周六"，于是日志里出现 `[2026/10/03 ??? 13:14:52]`
      （cmd 的 GBK 字节混进了 UTF-8 日志）。

      .ps1 没有这个问题：PowerShell 用 Unicode 读脚本，
      `Get-Date -Format` 的产物也是 Unicode。**所以用 .ps1。**

.USAGE
    # 手动前台跑（调试）
    pwsh -File scripts\watch-new-games.ps1

    # 常驻后台（本会话用的方式）
    Start-Job / Start-Process powershell -WindowStyle Hidden -File ...

    # 随机自启 + 断线重连（需要管理员权限注册）
    Register-ScheduledTask -TaskName NovaLoc-Watch-NewGames ...
#>
[CmdletBinding()]
param(
    # 游戏库路径。默认取用户机器上的实际库。
    [string]$Library = 'E:\lush\1\newlytransport',

    # 扫描间隔（秒）。
    [int]$IntervalSeconds = 600,

    # 数据根（工作区、备份、守望状态都在这下面）。
    [string]$DataRoot = $(if ($env:NOVALOC_DATA_ROOT) { $env:NOVALOC_DATA_ROOT } else { 'D:\NovaLoc' }),

    # 只跑一轮就退出（用于自检；正常常驻时为 $false）。
    [switch]$Once
)

$ErrorActionPreference = 'Continue'

# ---- ★ 编码：必须在**任何管道**之前设好 -------------------------------------
#
# ## 这个坑（实测，花了很久才定位）
#
# 脚本是以 **隐藏窗口** 方式常驻的（`Start-Process -WindowStyle Hidden`）。
# 在这种方式下 PowerShell 的 `[Console]::OutputEncoding` 是**系统代码页
# （gb2312/gbk）**，而**前台**运行时它是 `utf-8`。实测对照：
#
#     前台运行      [Console]::OutputEncoding = utf-8      捕获到「新译」✅
#     隐藏窗口运行  [Console]::OutputEncoding = gb2312     捕获到乱码  ❌
#
# 为什么这会毁掉日志：`& $exe ... | Out-File` 走 PowerShell 的**文本管道**，
# 管道**按 `[Console]::OutputEncoding` 解码**子进程的输出字节。CLI 写的是
# UTF-8（这是对的），但被按 GBK 解码 ⇒ 变成乱码汉字 ⇒ 再按 UTF-8 写进
# 日志 ⇒ 日志是**合法 UTF-8 但内容是乱码**。
#
# ⚠️ 这正是"文件能按 UTF-8 解码"骗过检查的原因：坏内容也是合法 UTF-8。
#    我一开始只验"整文件能否 UTF-8 解码"，结论是 ✅ —— 而内容是坏的。
#
# ## 修法
#
# * `[Console]::OutputEncoding` —— 决定管道**如何解码子进程输出**（关键）；
# * `$OutputEncoding` —— 决定 PowerShell 往子进程 stdin 写时用什么编码；
# * `Out-File -Encoding utf8` 保持显式（不依赖会话默认值）。
#
# 三者都设，因为它们的默认值会随"前台 / 隐藏 / 计划任务"而变。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $utf8NoBom } catch { }
try { [Console]::InputEncoding = $utf8NoBom } catch { }
$OutputEncoding = $utf8NoBom

# ---- 环境：与项目其它入口保持一致 -------------------------------------------
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONWARNINGS   = 'ignore'
$env:PYTHONPATH       = 'src'
$env:NOVALOC_DATA_ROOT = $DataRoot
if (-not $env:OLLAMA_MODELS) { $env:OLLAMA_MODELS = 'D:\NovaLoc\ollama\models' }

$exe = Join-Path $repoRoot '.venv\Scripts\novaloc.exe'
$outLog = Join-Path $DataRoot 'watch-service.out.log'
$errLog = Join-Path $DataRoot 'watch-service.err.log'

function Write-ServiceLog {
    param([string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    # ★ 显式用 UTF-8 追加：PowerShell 5.1 的 `>>` / Add-Content 默认是
    #   系统 ANSI，中文会写成 GBK 而和 CLI 的 UTF-8 输出混在一个文件里。
    $line = "[$stamp] $Message`r`n"
    [System.IO.File]::AppendAllText(
        $outLog, $line, (New-Object System.Text.UTF8Encoding($false))
    )
}

if (-not (Test-Path $exe)) {
    Write-ServiceLog "FATAL: 找不到 novaloc.exe（$exe）"
    if ($Once) { exit 1 }
    exit 1
}
if (-not (Test-Path $Library)) {
    Write-ServiceLog "FATAL: 游戏库不存在（$Library）"
    exit 1
}

New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null

Write-ServiceLog "守望服务启动：库=$Library 间隔=${IntervalSeconds}s 数据根=$DataRoot"

$round = 0
while ($true) {
    $round++
    Write-ServiceLog "第 $round 轮：开始扫描（--watch-only，不重跑现有游戏）"

    # ★ 用 `cmd /c ... >> file 2>> file` 做**字节级追加**重定向。
    #
    # ## 为什么不用 PowerShell 管道（`& $exe ... | Out-File`）
    #
    # 管道走 PowerShell 的**文本解码**，用的是 `[Console]::OutputEncoding`。
    # 而那个值**随启动方式变化**（实测，`.scratch/_ps_outfile_enc.py`）：
    #
    #     前台运行      utf-8     ⇒ 解码正确 ✅
    #     隐藏窗口      gb2312    ⇒ 把 UTF-8 读成乱码 ❌   ← 生产就是这种
    #
    # 直接证据：隐藏窗口下子进程内 `[Console]::OutputEncoding` = `gb2312`，
    # 捕获到的"新译"字节是 `e9 8f 82 ...`（GBK 误读产物）。
    # 上面虽然已经设了 `[Console]::OutputEncoding`，但**依赖一个会随启动
    # 方式变化的环境值**不够稳 —— 计划任务 / 服务 / 其它 Windows 版本
    # 都可能又不一样。
    #
    # ## 为什么不用 `Start-Process -RedirectStandardOutput`
    #
    # 它同样字节直通、内容正确，但**会截断目标文件**（实测：跨轮次重启后
    # 日志里丢了 `[时间戳]` 那几行服务自己的记录）⇒ 不能累积。
    #
    # ## 为什么 `cmd /c` 是对的
    #
    # `cmd.exe` 的 `>>` 是**字节级**的：打开文件、把子进程原始输出字节
    # 写进去，中间**没有任何文本解码** ⇒ 与"用 Python 捕获"完全等价
    # （`_enc_by_command.py` 已证明 CLI 自身输出干净的 UTF-8）。
    # 而且 `>>` 是**追加**，不截断。
    $cmdLine = '"{0}" auto "{1}" --watch-only --interval {2} >> "{3}" 2>> "{4}"' -f `
        $exe, $Library, $IntervalSeconds, $outLog, $errLog
    & cmd.exe /c $cmdLine
    $code = $LASTEXITCODE

    Write-ServiceLog "第 $round 轮退出（code=$code），5 秒后重启"
    if ($Once) { exit $code }
    Start-Sleep -Seconds 5
}
