<#
.SYNOPSIS
    开发模式：启动 NovaLoc 后端（uvicorn --reload，端口 8000）。

.DESCRIPTION
    做两件事：
      1. 检查 .venv 是否存在、依赖是否装好；
      2. 用 uvicorn 在 127.0.0.1:8000 启动后端并开启 --reload。

    前端（Vite）需要**另开一个终端**手动启动，见 -ShowFrontendHelp
    或本文件末尾的说明。

    ⚠️ 关于 --reload 的一个真实限制：
    uvicorn 的 reload 依赖 import string（"模块:属性"），而本项目的
    `novaloc serve` 传的是已经构造好的 app 对象，因此 `novaloc serve --reload`
    的自动重载**不会生效**（uvicorn 会打印一句 "You must pass the application
    as an import string to enable 'reload'" 然后忽略 reload）。
    所以本脚本改为直接调用 uvicorn 并传 import string。
    注意这也意味着 `--reload` **不监听 novaloc/*.py 的改动**（uvicorn 的 reload
    只监视 import string 直接指向的包），改了业务代码请手动 Ctrl+C 重启。

.PARAMETER BindHost
    监听地址，默认 127.0.0.1（本地工具，不要暴露到局域网）。
    参数名不用 Host，因为 $Host 是 PowerShell 的自动只读变量。

.PARAMETER Port
    监听端口，默认 8000（与 web/vite.config.ts 的代理目标一致）。

.PARAMETER NoReload
    关闭自动重载。大型游戏跑流水线时建议关掉，避免重载打断任务。

.PARAMETER ShowFrontendHelp
    只打印前端启动说明，不启动后端。

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -Port 8001
    .\scripts\dev.ps1 -NoReload
    .\scripts\dev.ps1 -ShowFrontendHelp
#>

[CmdletBinding()]
param(
    [string]$BindHost = '127.0.0.1',

    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$NoReload,

    [switch]$ShowFrontendHelp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Windows 控制台默认不是 UTF-8。uvicorn 的日志里有中文（阶段名、错误信息），
# 不设这个会变成乱码方块。
$env:PYTHONIOENCODING = 'utf-8'

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  [!] $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  $Message" -ForegroundColor Gray
}

function Write-FrontendHelp {
    $lines = @(
        '',
        '前端开发服务器（Vite）说明',
        '----------------------------------------------------------------',
        '后端的静态资源是 web/dist（随仓库提交的构建产物）。',
        '开发前端时要让 Vite 起一个热更新的 dev server，它会把 /api 与 /ws',
        '代理到后端（见 web/vite.config.ts，目标就是 http://127.0.0.1:8000）。',
        '',
        '  另开一个终端：',
        '      cd web',
        '      pnpm install        # 首次',
        '      pnpm run dev        # 默认 http://127.0.0.1:5173',
        '',
        '  然后在浏览器打开 http://127.0.0.1:5173 。',
        '  直接打开 8000 端口看到的是 web/dist 里的旧构建产物，不是你改的源码。',
        '',
        '  改完前端要提交产物时：',
        '      cd web',
        '      pnpm run build      # 输出到 web/dist（这个目录是**故意入库**的）',
        '',
        '注意：web/dist 随仓库提交，是为了让工具在没有 Node 的机器上也能跑。',
        '      不要把它加进 .gitignore。',
        '----------------------------------------------------------------'
    )
    foreach ($line in $lines) {
        Write-Host $line -ForegroundColor DarkGray
    }
}

if ($ShowFrontendHelp) {
    Write-FrontendHelp
    exit 0
}

# ---------------------------------------------------------------------------
# 定位仓库根目录
# ---------------------------------------------------------------------------

$repoRoot = $PSScriptRoot
if (-not (Test-Path (Join-Path $repoRoot 'pyproject.toml'))) {
    $parent = Split-Path -Parent $PSScriptRoot
    if (Test-Path (Join-Path $parent 'pyproject.toml')) {
        $repoRoot = $parent
    } else {
        Write-Host "[x] 找不到 pyproject.toml。请在仓库根目录或 scripts/ 下运行本脚本。" -ForegroundColor Red
        exit 1
    }
}

$venvDir = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'

Write-Host ''
Write-Host 'NovaLoc 新译 —— 开发服务器' -ForegroundColor Magenta
Write-Info "仓库根目录：$repoRoot"

# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------

Write-Step '检查虚拟环境'

if (-not (Test-Path $venvPython)) {
    Write-Warn '.venv 不存在。先运行一次安装脚本：'
    Write-Host ''
    Write-Host '      .\scripts\setup.ps1' -ForegroundColor White
    Write-Host ''
    exit 1
}

$probe = & $venvPython -c "import uvicorn, novaloc.api.app; print('ok')" 2>&1
if ($LASTEXITCODE -ne 0 -or ($probe -notmatch 'ok')) {
    Write-Host ''
    Write-Host '[x] 已安装的依赖不完整，导入后端失败：' -ForegroundColor Red
    Write-Host $probe -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '    修复：' -ForegroundColor Yellow
    Write-Host ("      & `"{0}`" -m pip install -e `".[dev]`"" -f $venvPython) -ForegroundColor White
    Write-Host ''
    exit 1
}
Write-Ok '依赖完整（uvicorn + novaloc.api.app 可导入）'

# ---------------------------------------------------------------------------
# 端口占用检查（提前报，比 uvicorn 报错更容易看懂）
# ---------------------------------------------------------------------------

Write-Step "检查端口 $Port 是否被占用"

$occupied = $null
try {
    $occupied = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        Select-Object -First 1
} catch {
    # Get-NetTCPConnection 在某些环境不可用；退回到 netstat
    $netstat = & netstat -ano -p TCP 2>$null | Select-String -Pattern ":$Port\s+.*LISTENING"
    if ($netstat) {
        $occupied = $netstat | Select-Object -First 1
    }
}

if ($null -ne $occupied) {
    Write-Warn "端口 $Port 已在监听。可能是上一次没关干净的 uvicorn。"
    Write-Info "换端口：.\scripts\dev.ps1 -Port 8001（注意 Vite 的代理目标也要跟着改）"
    Write-Info "或者结束占用进程：Get-NetTCPConnection -LocalPort $Port -State Listen | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }"
} else {
    Write-Ok "端口 $Port 空闲"
}

# ---------------------------------------------------------------------------
# 启动 uvicorn
# ---------------------------------------------------------------------------

# 用 import string 而不是 app 对象 —— 这是 uvicorn --reload 的硬性要求。
$appTarget = 'novaloc.api.app:create_app'

# create_app 是工厂函数，所以要用 --factory。
$uvicornArgs = @(
    '-m', 'uvicorn',
    $appTarget,
    '--factory',
    '--host', $BindHost,
    '--port', "$Port",
    '--log-level', 'info'
)
if (-not $NoReload) {
    $uvicornArgs += '--reload'
}

$url = "http://${BindHost}:${Port}"

Write-Step '启动后端'
Write-Info "地址：$url"
Write-Info "API 文档：$url/api/docs"
if ($NoReload) {
    Write-Info '自动重载：已关闭（-NoReload）'
} else {
    Write-Info '自动重载：已开启（只对 uvicorn 能看到的模块生效；改业务代码建议手动重启）'
}
Write-Host ''
Write-Host '  Ctrl+C 停止' -ForegroundColor DarkGray

Write-FrontendHelp

Write-Host ''

Push-Location $repoRoot
try {
    & $venvPython @uvicornArgs
    $runCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($runCode -ne 0) {
    Write-Host ''
    Write-Warn "uvicorn 退出码：$runCode"
    if ($runCode -eq 1) {
        Write-Info '退出码 1 在 Windows 上常见于 Ctrl+C 强制结束，不一定是错误。'
    }
    exit $runCode
}

Write-Host ''
Write-Ok '后端已停止。'
