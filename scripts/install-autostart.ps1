<#
.SYNOPSIS
    安装 / 移除 / 查询「开机自动启动守望服务」。

.DESCRIPTION
    把一个快捷方式放进当前用户的**启动文件夹**，指向
    `scripts\launch-watch-hidden.ps1`。登录后守望服务会自动恢复运行 ——
    这就是「后续新加游戏自动汉化」能跨重启活下来的那一环。

.PARAMETER Action
    `install`（默认）／`remove`／`status`。

.NOTES
    ## 为什么是「启动文件夹」而不是计划任务

    `Register-ScheduledTask` 在这台机器上返回 `Access is denied`（需要管理员），
    而本会话**审批提示是关闭的**，所以没有提权路径。启动文件夹不需要提权，
    且正好匹配真实需求：**用户登录期间**守望服务在跑。
    代价是不能在登录前运行 —— 对桌面汉化工具无所谓。

    ## `-EncodedCommand` 不是为了藏命令，是为了绕开引号与代码页

    快捷方式的 `Arguments` 是一个**字符串**，而仓库路径含中文。
    `Start-Process -FilePath 'D:\…\小工具\…'` 这样拼引号，在
    「cmd → powershell → 快捷方式 → powershell」这条链上必然翻车。
    `-EncodedCommand` 收一个 base64（UTF-16LE）整块参数：
    **一个参数、零引号、零代码页参与**。

    ## 编码注意事项（这个仓库里已经踩过三次同类坑）

    * `.ps1` 必须带 UTF-8 BOM，否则 PowerShell 5.1 按 ANSI 读、中文注释乱码；
      本文件与 `launch-watch-hidden.ps1` 由 `scripts\fix-ps1-bom.ps1` 统一修。
    * `.cmd` 必须**纯 ASCII**（cmd.exe 按 OEM 代码页解析，中文会被当命令执行）。
    * 快捷方式的 `WorkingDirectory` 也要设对。
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'remove', 'status')]
    [string]$Action = 'install'
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot          # scripts\ 的上一级 = 仓库根
$target = Join-Path $PSScriptRoot 'launch-watch-hidden.ps1'
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'NovaLoc 守望服务.lnk'

function Write-Head([string]$text) {
    Write-Host ''
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('  ' + ('─' * 66)) -ForegroundColor DarkGray
}

function Show-Status {
    Write-Head '守望服务自启状态'
    Write-Host "    仓库根      $repo"
    Write-Host "    启动文件夹  $startup"
    if (Test-Path -LiteralPath $lnk) {
        Write-Host "    快捷方式    ✅ 已安装" -ForegroundColor Green
        Write-Host "    路径        $lnk"
        $sh = New-Object -ComObject WScript.Shell
        $s = $sh.CreateShortcut($lnk)
        Write-Host "    指向        $($s.TargetPath)"
        Write-Host "    参数        $($s.Arguments)"
        Write-Host "    工作目录    $($s.WorkingDirectory)"
        # 目标脚本是不是还在（仓库被移动/改名就会失效）
        if (Test-Path -LiteralPath $target) {
            Write-Host "    目标脚本    ✅ $target" -ForegroundColor Green
        } else {
            Write-Host "    目标脚本    ❌ 丢失：$target" -ForegroundColor Red
        }
    } else {
        Write-Host '    快捷方式    ❌ 未安装' -ForegroundColor Yellow
    }

    $running = @(
        Get-CimInstance Win32_Process -Filter "Name='novaloc.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '--watch' }
    )
    if ($running.Count -gt 0) {
        Write-Host "    当前进程    ✅ 有 $($running.Count) 个 --watch 在跑" -ForegroundColor Green
    } else {
        Write-Host '    当前进程    ⚠️ 没有 --watch 在跑' -ForegroundColor Yellow
    }
}

function Install-Autostart {
    if (-not (Test-Path -LiteralPath $target)) {
        throw "缺少目标脚本：$target"
    }

    # ★ -EncodedCommand：一个参数、无引号、无代码页问题
    $ps = "Start-Process -FilePath '$target' -WindowStyle Hidden"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ps))
    $args = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $b64"

    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $s.Arguments = $args
    $s.WorkingDirectory = $repo
    $s.Description = 'NovaLoc 守望服务：自动汉化游戏库中新增的游戏'
    $s.WindowStyle = 7                      # 7 = 最小化
    $s.Save()

    Write-Head '安装完成'
    Write-Host "    快捷方式    $lnk" -ForegroundColor Green
    Write-Host '    下次登录时会自动启动守望服务。'
    Write-Host ''
    Write-Host '    现在就用它启动一次：' -ForegroundColor DarkGray
    Write-Host "      $target" -ForegroundColor DarkGray
}

function Remove-Autostart {
    if (Test-Path -LiteralPath $lnk) {
        Remove-Item -LiteralPath $lnk -Force
        Write-Head '已移除'
        Write-Host "    删除了 $lnk" -ForegroundColor Green
    } else {
        Write-Head '无需移除'
        Write-Host '    本来就没有安装。' -ForegroundColor Yellow
    }
}

switch ($Action) {
    'install' { Install-Autostart }
    'remove' { Remove-Autostart }
    'status' { Show-Status }
}
