# NovaLoc 忙闲监视器（带**守护循环**）
#
# 作用：轮询"机器是不是被用户占着"，并维护数据根下的 `_pause` 闸门文件。
#       翻译进程在每批之前检查这个文件，存在就睡到它消失。
#
# ## ★ 为什么要加守护循环（实测问题）
#
# 最早只是"起一次监视器"。第一次启动**一行日志都没写就死了**，
# 于是完全分不清"活着但空闲"和"压根没起来"——两者都是"日志没动静"。
# 所以这个脚本现在：监视器退出就重启；连续快速退出 N 次就放弃
# （配置错的话否则会空转刷日志）。
#
# ## ★ 为什么日志由 PowerShell 自己写，而不是靠 `>>` 重定向
#
# 实测：`cmd` 把 `powershell.exe ... >> file` 重定向时，文件是
# **UTF-16LE 带 BOM**（开头 `ff fe`）。之后任何按 UTF-8 读的工具
# 看到的都是乱码。所以这里用 `Add-Content -Encoding UTF8` 明确写，
# Python 侧也用 `--log` 自己写同一个文件。
#
# ## 为什么监视器是独立进程，而不是 `novaloc auto` 里的一个线程
#
# 闸门必须**跨 `auto` 重启**继续有效。若监视器活在 `auto` 里，
# 重启翻译进程的同时也会丢掉闸门 —— 而用户可能正在玩游戏，
# 那个窗口里文件就会被覆盖。
#
# ⚠️ 这个文件必须存成 **UTF-8 with BOM**，否则 PowerShell 5.1 会按 ANSI(GBK)
#    解码，中文字符串会乱码（`scripts/fix-ps1-bom.ps1` 负责修）。
#
# ⚠️ 另外：注释里**不能**原样写出"美元符 + 变量名 + 冒号"的形状，
#    那会被当成驱动器引用而 ParserError —— 注释并不是安全区（我踩过）。

[CmdletBinding()]
param(
    [string]$DataRoot = $(if ($env:NOVALOC_DATA_ROOT) { $env:NOVALOC_DATA_ROOT } else { 'D:\NovaLoc' }),
    [string]$Library  = $(if ($env:NOVALOC_LIB) { $env:NOVALOC_LIB } else { 'E:\lush\1\newlytransport' }),
    [double]$Interval = 20,
    [double]$CalmDown = 90,
    [int]$MaxQuickExits = 5
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[ERROR] python not found: $py"
    exit 1
}

$outLog = Join-Path $DataRoot 'busy-watch.out.log'
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null

# ★ 只有 **Python 侧**写这个日志文件。
#
# 为什么不让 PowerShell 也写：两边各自追加会交错（PowerShell 一次
# `Add-Content` 与 Python 一次缓冲 flush 之间没有同步），日志读起来会
# 前言不搭后语。而且 PowerShell 的 `-Encoding UTF8` 会**加 BOM**，
# 而 Python 追加时不会 ⇒ 文件中间凭空多出 `ef bb bf`。
#
# 故障排查信息通过 `--log` 交给 Python 打印，它已经会用 utf-8 追加。
function Say([string]$text) {
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text"
}

Say 'supervisor started'
Say "  data root : $DataRoot"
Say "  library   : $Library"
Say "  interval  : $Interval s   calm-down: $CalmDown s"
Say "  gate file : $(Join-Path $DataRoot '_pause')"

$quick = 0
$round = 0
while ($true) {
    $round++
    $t0 = Get-Date
    Say "round ${round}: starting monitor"

    # `-u` = 不缓冲，运行中就能读到日志。
    # `--log` 让 Python 侧也把状态变化追加进同一个文件（它自己控制编码）。
    & $py -u -m novaloc.translate.busy `
        --data-root $DataRoot `
        --library $Library `
        --interval $Interval `
        --calm-down $CalmDown `
        --log $outLog
    $code = $LASTEXITCODE
    $ran = ((Get-Date) - $t0).TotalSeconds
    Say "monitor exited code=$code after $([math]::Round($ran,1))s"

    if ($ran -lt 30) {
        $quick++
        Say "  quick exit $quick/$MaxQuickExits"
        if ($quick -ge $MaxQuickExits) {
            # 不要指向 busy-watch.err.log —— 现在没有那个文件（日志统一由
            # Python 写进 busy-watch.out.log）。给一条可直接粘贴的命令。
            Say '  too many quick exits - stopping; run the monitor by hand to see why:'
            Say ('    ' + $py + ' -u -m novaloc.translate.busy --data-root ' + $DataRoot)
            exit 2
        }
        Start-Sleep -Seconds 10
    } else {
        $quick = 0
        Start-Sleep -Seconds 5
    }
}
