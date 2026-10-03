# NovaLoc 忙闲监视器
#
# 作用：轮询"机器是不是被用户占着"，并维护数据根下的 `_pause` 闸门文件。
#       翻译进程在每批之前检查这个文件，存在就睡到它消失。
#
# 为什么单独做成一个进程而不是塞进 `novaloc auto`：
#   * `auto` 可能被重启，而"机器忙不忙"这个判断应该跨重启持续有效；
#   * 用户可以手动 `New-Item _pause` 立刻让路（有意保留的口子），
#     监视器不能把这个文件删掉 —— 所以它只在**自己判定空闲**时才删。
#
# ⚠️ 这个文件必须存成 **UTF-8 with BOM**，否则 PowerShell 5.1 会按 ANSI(GBK)
#    解码，中文字符串会乱码（`scripts/fix-ps1-bom.ps1` 负责修）。

[CmdletBinding()]
param(
    [string]$DataRoot = $(if ($env:NOVALOC_DATA_ROOT) { $env:NOVALOC_DATA_ROOT } else { 'D:\NovaLoc' }),
    [string]$Library  = $(if ($env:NOVALOC_LIB) { $env:NOVALOC_LIB } else { 'E:\lush\1\newlytransport' }),
    [double]$Interval = 20,
    [double]$CalmDown = 90,
    [string]$Log      = '',
    [switch]$Once
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[ERROR] python not found: $py"
    exit 1
}
if (-not $Log) { $Log = Join-Path $DataRoot 'busy-watch.out.log' }

Write-Host "NovaLoc busy watcher"
Write-Host "  data root : $DataRoot"
Write-Host "  library   : $Library"
Write-Host "  interval  : $Interval s   calm-down: $CalmDown s"
Write-Host "  log       : $Log"
Write-Host "  gate file : $(Join-Path $DataRoot '_pause')"
Write-Host ""

# 把 `-Once` 透传；否则常驻轮询
$extra = @()
if ($Once) { $extra += '--once' }

# -u：不要缓冲，日志才能实时看到
& $py -u -m novaloc.translate.busy `
    --data-root $DataRoot `
    --library $Library `
    --interval $Interval `
    --calm-down $CalmDown `
    --log $Log `
    @extra
exit $LASTEXITCODE
