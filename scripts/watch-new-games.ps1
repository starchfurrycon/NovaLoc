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
    [switch]$Once,

    # 只做启动前预检并退出（用于"启动前先量一下"）。
    [switch]$Preflight
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

# ★ 数据根必须**在这里**就建出来，不能等到下面。
#   原先 `New-Item` 在 `$Preflight` 分支**之后** ⇒ `-Preflight` 退出时
#   目录还不存在 ⇒ `Write-ServiceLog` 抛
#   `IOException`（文件路径的一部分不存在）⇒ 预检什么都写不出来。
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null

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

# ---- ★ 预扫描：大库的"第一轮"会很久，必须先跟用户说清 ------------------------
#
# ## 为什么要有这一步
#
# `--watch-only` 启动时会把**库里现有的游戏全部记为"已见"**（`bootstrap`），
# 然后才进守望循环。在大库上这一步可能要好几分钟 ——
# 期间服务**看起来像卡住了**（没有任何输出）。
#
# ⚠️ 这里**不能只看目录数**就下结论：实测 `E:\lush\1\newlytransport` 的
#    `resolve()` 只要 **0.05 秒**（NTFS 的目录项缓存很快），
#    而有 1.2 万个文件的树也只要 **0.8 秒**。
#    真正的耗时在**其它**环节（`bootstrap` 逐个建 `.novaloc.json` 等）。
#    所以这一段的产物是"**给用户一个可对照的基线**"，
#    而不是"预测要跑多久" —— 后者我量不出来，就不假装能量。
if ($Preflight) {
    if (-not (Test-Path $Library)) {
        Write-ServiceLog "FATAL: 游戏库不存在（$Library）"
        exit 1
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $top = @(Get-ChildItem -LiteralPath $Library -Directory -ErrorAction SilentlyContinue)
    $sw.Stop()
    Write-ServiceLog (
        "预检：库={0} 顶层目录={1} 列举耗时={2:N0}ms" -f `
        $Library, $top.Count, $sw.ElapsedMilliseconds
    )
    Write-ServiceLog (
        "预检结论：启动时要先把这 {0} 个目录全部记为'已见'；" -f $top.Count
    )
    Write-ServiceLog (
        "  之后只处理**此后新出现**的游戏。若这一步很久，是正常现象，不是卡住。"
    )
    if ($top.Count -gt 500) {
        Write-ServiceLog (
            "  ⚠️ 库较大（>{0} 个目录），首轮 bootstrap 可能需要几分钟。" -f 500
        )
    }
    Write-ServiceLog "预检完成（ExitCode=0）"
    exit 0
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
#: 连续"秒退"的轮次计数。见下面快速退出检测的说明。
$quickExits = 0
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
    # ## ★ 为什么加了 `--backlog`
    #
    # 原来只有 `--watch-only` ⇒ **只等新游戏，拒绝碰积压**。
    # 而自启的只有这一个服务（全库翻译进程是手工起的，不在自启清单里），
    # 于是"关机 → 开机"之后积压就**永远停在那里**，且日志一切正常。
    #
    # 加 `--backlog` 后，每轮启动都会**先把库里待处理的游戏补完**，
    # 再进守望循环。因为每个游戏靠 `only_pending=True` 续跑、
    # 已完成的会秒过，所以"重启一次"的代价很小，而"积压推进"是持续的。
    #
    # ## 为什么 `cmd /c` 是对的
    #
    # `cmd.exe` 的 `>>` 是**字节级**的：打开文件、把子进程原始输出字节
    # 写进去，中间**没有任何文本解码** ⇒ 与"用 Python 捕获"完全等价
    # （`_enc_by_command.py` 已证明 CLI 自身输出干净的 UTF-8）。
    # 而且 `>>` 是**追加**，不截断。
    $cmdLine = '"{0}" auto "{1}" --watch-only --backlog --interval {2} >> "{3}" 2>> "{4}"' -f `
        $exe, $Library, $IntervalSeconds, $outLog, $errLog
    $roundStart = Get-Date
    & cmd.exe /c $cmdLine
    $code = $LASTEXITCODE
    $ranSec = [Math]::Round(((Get-Date) - $roundStart).TotalSeconds, 1)

    Write-ServiceLog "第 $round 轮退出（code=$code，运行了 ${ranSec}s），5 秒后重启"

    # ---- ★ 快速退出检测：别把"每 5 秒空转"当成正常 ---------------------------
    #
    # ## 为什么必须检测
    #
    # 曾经有一个 bug：`if not todo: return` 在守望循环**之前**无条件返回
    # （见 `tests/test_watch_cli_blocks.py` 的 docstring）。
    # 后果是子进程 0.9 秒就退出（**退出码 0**，完全"成功"），
    # 而这个 while 循环忠实地"5 秒后重启" ⇒ 日志刷成一片
    #
    #     第 N 轮：开始扫描 … 第 N 轮退出（code=0），5 秒后重启
    #
    # **看起来服务一直在工作**，实际每 5 秒空转一轮，新游戏永远等不到处理。
    # 光看日志根本发现不了 —— 直到我实测"这个命令会不会阻塞"才暴露。
    #
    # ## 判据
    #
    # 一轮"正常运行"必然**至少跑满一个扫描间隔**（子进程会在
    # `time.sleep(interval)` 上待着）。所以：
    #
    #   运行时长 < 间隔的一半  ⇒ 子进程没进循环 ⇒ **异常**
    #
    # 连续 3 次异常就**停手并大声报错**，不再无脑重启 ——
    # 免得把一个真 bug 掩盖成"服务在跑"。
    if ($ranSec -lt ($IntervalSeconds / 2)) {
        $quickExits++
        Write-ServiceLog (
            "  ⚠️ 异常：本轮只运行了 ${ranSec}s（应 ≥{0}s）—— 子进程似乎没进守望循环。" -f `
            [int]($IntervalSeconds / 2)
        )
        Write-ServiceLog "  ⚠️ 连续异常次数：$quickExits / 3"
        if ($quickExits -ge 3) {
            Write-ServiceLog (
                "  ✗ 连续 3 轮都是秒退 ⇒ 停手，不再重启。" +
                "请检查 `novaloc auto <库> --watch-only` 是否能正常阻塞（它不该立刻退出）。"
            )
            Write-ServiceLog "  提示：本仓库有专门的守卫测试 —— tests/test_watch_cli_blocks.py"
            exit 2
        }
    } else {
        $quickExits = 0
    }

    if ($Once) { exit $code }
    Start-Sleep -Seconds 5
}
