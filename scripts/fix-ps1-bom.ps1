# scripts/fix-ps1-bom.ps1
<#
.SYNOPSIS
    给本仓库的 .ps1 补上 UTF-8 BOM，并做语法检查。

.DESCRIPTION
    ★ 为什么需要这个脚本（踩过两次的坑）

    Windows PowerShell **5.1** 读 `.ps1` 时，**没有 BOM 就按系统 ANSI
    代码页（本机 GBK）解码**，不是 UTF-8。于是含中文的脚本会被解成乱码，
    而乱码里的引号/括号会破坏语法 ⇒ 报出**看起来毫不相关**的错误：

        字符串缺少终止符: "。
        语句块或类型定义中缺少右"}"。

    实测：我用编辑器改了一次 `watch-new-games.ps1`，BOM 被去掉，
    脚本立刻变成"启动就失败、连日志都不写"。而项目里原有的
    `dev.ps1` / `setup.ps1` **都是带 BOM 的** —— 所以带 BOM 是本仓库
    的既有约定，不是权宜之计。

    ⚠️ 用任何工具编辑 `scripts\*.ps1` 之后，**必须重新跑一次本脚本**。
       否则下次有人执行就会遇到上面那种莫名其妙的语法错误。

.PARAMETER Check
    只检查，不修改（用于 CI / 提交前校验）。

.EXAMPLE
    pwsh -File scripts\fix-ps1-bom.ps1
    pwsh -File scripts\fix-ps1-bom.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
$utf8Bom = New-Object System.Text.UTF8Encoding($true)

$changed = 0
$checked = 0
$failed = 0

foreach ($f in Get-ChildItem -Path $scriptDir -Filter '*.ps1' -File | Sort-Object Name) {
    $checked++
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF

    # 是否含非 ASCII（纯 ASCII 脚本不加 BOM 也能正常工作）
    $hasNonAscii = $false
    foreach ($b in $bytes) { if ($b -ge 0x80) { $hasNonAscii = $true; break } }

    if ($hasBom) {
        Write-Host ("  [BOM 已存在] {0}" -f $f.Name)
    }
    elseif (-not $hasNonAscii) {
        Write-Host ("  [纯 ASCII，无需 BOM] {0}" -f $f.Name)
    }
    elseif ($Check) {
        Write-Host ("  [缺少 BOM] {0}  <-- 需要修" -f $f.Name) -ForegroundColor Red
        $failed++
        continue
    }
    else {
        # 按 UTF-8 读入（无 BOM 时也当作 UTF-8），再带 BOM 写回
        $text = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        [System.IO.File]::WriteAllText($f.FullName, $text, $utf8Bom)
        Write-Host ("  [已补 BOM] {0}" -f $f.Name) -ForegroundColor Green
        $changed++
    }

    # 语法检查（无论有没有改，都验一遍）
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $f.FullName, [ref]$null, [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors -and $parseErrors.Count -gt 0) {
        Write-Host ("     语法错误 {0} 个：" -f $parseErrors.Count) -ForegroundColor Red
        $parseErrors | Select-Object -First 5 | ForEach-Object {
            Write-Host ("       L{0}: {1}" -f $_.Extent.StartLineNumber, $_.Message) -ForegroundColor Red
        }
        $failed++
    }
}

Write-Host ""
Write-Host ("  检查 {0} 个 .ps1：补 BOM {1} 个，失败 {2} 个" -f $checked, $changed, $failed)

if ($failed -gt 0) { exit 1 }
exit 0
