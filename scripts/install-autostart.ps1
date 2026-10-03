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
$startup = [Environment]::GetFolderPath('Startup')

# ----------------------------------------------------------------------
# 要自启的服务清单
#
# ## 为什么做成"表"而不是写死一个
#
# 原来只装一个快捷方式（守望服务）。现在多了一个**忙闲监视器**
# （动态资源占用：用户打游戏/编译时就放下 `_pause` 闸门让路）。
# 两个都要跨重启活着，所以这里列成表，Install/Remove/Status 三个动作
# 都按表走 —— 加第三个服务时只需要往表里加一行。
#
# ## `Args` 为什么写在 EncodedCommand 里而不是快捷方式的参数里
#
# 快捷方式指向的是 `powershell.exe -EncodedCommand <base64>`。
# `-EncodedCommand` 之后**不能再跟普通参数**（它们会被当成
# 待执行脚本的一部分而报错）。所以每个服务要带的参数，
# 都拼进那段被 base64 编码的 PowerShell 代码里。
# ----------------------------------------------------------------------
$Services = @(
    [pscustomobject]@{
        Key     = 'watch'
        Link    = 'NovaLoc 守望服务.lnk'
        Target  = Join-Path $PSScriptRoot 'launch-watch-hidden.ps1'
        # 守望服务自己内部会去读 NOVALOC_LIB / NOVALOC_DATA_ROOT
        Code    = "Start-Process -FilePath '{0}' -WindowStyle Hidden"
        Desc    = 'NovaLoc 守望服务：自动汉化游戏库中新增的游戏'
        Process = '--watch'
    },
    [pscustomobject]@{
        Key     = 'busy'
        Link    = 'NovaLoc 忙闲监视器.lnk'
        Target  = Join-Path $PSScriptRoot 'watch-busy.ps1'
        # ★ 这个要显式带上 -- 见上面关于 EncodedCommand 的说明
        Code    = "Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File','{0}') -WindowStyle Hidden"
        Desc    = 'NovaLoc 忙闲监视器：用户在用机器/打游戏时让路，空闲时恢复翻译'
        Process = 'busy'
    }
)

function Get-LinkPath($svc) { Join-Path $startup $svc.Link }

function Write-Head([string]$text) {
    Write-Host ''
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('  ' + ('─' * 66)) -ForegroundColor DarkGray
}

function Show-Status {
    Write-Head '自启服务状态'
    Write-Host "    仓库根      $repo"
    Write-Host "    启动文件夹  $startup"
    Write-Host ''
    $sh = New-Object -ComObject WScript.Shell
    foreach ($svc in $Services) {
        $lnk = Get-LinkPath $svc
        Write-Host "  ── $($svc.Desc)" -ForegroundColor DarkGray
        if (Test-Path -LiteralPath $lnk) {
            Write-Host "    快捷方式    ✅ 已安装" -ForegroundColor Green
            $s = $sh.CreateShortcut($lnk)
            Write-Host "    路径        $lnk"
            Write-Host "    指向        $($s.TargetPath)"
            Write-Host "    工作目录    $($s.WorkingDirectory)"
            if (Test-Path -LiteralPath $svc.Target) {
                Write-Host "    目标脚本    ✅ $($svc.Target)" -ForegroundColor Green
            } else {
                Write-Host "    目标脚本    ❌ 丢失：$($svc.Target)" -ForegroundColor Red
            }
        } else {
            Write-Host '    快捷方式    ❌ 未安装' -ForegroundColor Yellow
        }
        $running = @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.CommandLine -and $_.CommandLine -match $svc.Process -and
                    $_.Name -match 'novaloc|powershell|python'
                }
        )
        if ($running.Count -gt 0) {
            Write-Host "    当前进程    ✅ $($running.Count) 个在跑" -ForegroundColor Green
        } else {
            Write-Host '    当前进程    ⚠️ 没有在跑' -ForegroundColor Yellow
        }
        Write-Host ''
    }
}

function Install-Autostart {
    $sh = New-Object -ComObject WScript.Shell
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $installed = @()
    foreach ($svc in $Services) {
        if (-not (Test-Path -LiteralPath $svc.Target)) {
            Write-Host "    ⚠️ 跳过（缺少脚本）：$($svc.Target)" -ForegroundColor Yellow
            continue
        }
        # ★ -EncodedCommand：一个参数、无引号、无代码页问题
        $code = $svc.Code -f $svc.Target
        $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
        $lnk = Get-LinkPath $svc
        $s = $sh.CreateShortcut($lnk)
        $s.TargetPath = $psExe
        $s.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $b64"
        $s.WorkingDirectory = $repo
        $s.Description = $svc.Desc
        $s.WindowStyle = 7                      # 7 = 最小化
        $s.Save()
        $installed += $lnk
    }

    Write-Head '安装完成'
    foreach ($l in $installed) {
        Write-Host "    快捷方式    $l" -ForegroundColor Green
    }
    Write-Host '    下次登录时会自动启动这些服务。'
}

function Remove-Autostart {
    Write-Head '移除'
    $n = 0
    foreach ($svc in $Services) {
        $lnk = Get-LinkPath $svc
        if (Test-Path -LiteralPath $lnk) {
            Remove-Item -LiteralPath $lnk -Force
            Write-Host "    删除了 $lnk" -ForegroundColor Green
            $n++
        }
    }
    if ($n -eq 0) { Write-Host '    本来就没有安装。' -ForegroundColor Yellow }
}

switch ($Action) {
    'install' { Install-Autostart }
    'remove' { Remove-Autostart }
    'status' { Show-Status }
}
