<#
.SYNOPSIS
    为 NovaLoc 新译创建虚拟环境并安装依赖。

.DESCRIPTION
    依次做四件事：
      1. 创建 .venv（已存在则复用）；
      2. 升级 pip；
      3. 安装本包与推理后端 extra（有 NVIDIA 显卡用 dml，否则用 cpu）；
      4. 运行 novaloc doctor 做环境自检。

    安装前会检查 onnxruntime 冲突：`onnxruntime`、`onnxruntime-directml`、
    `onnxruntime-gpu` 提供**同一个包名**，同时装两个会让 `DmlExecutionProvider`
    静默消失、OCR 退回 CPU 慢 40~140 倍。检测到冲突脚本会**停下**并打印修复命令。

.PARAMETER Extra
    强制指定推理后端 extra：dml / cpu / gpu。留空则自动判断。

.PARAMETER NoDoctor
    装完不跑 novaloc doctor。

.PARAMETER Force
    即使 .venv 已存在也重建（会先删除旧目录）。

.EXAMPLE
    .\scripts\setup.ps1
    .\scripts\setup.ps1 -Extra cpu
    .\scripts\setup.ps1 -Force -NoDoctor
#>

[CmdletBinding()]
param(
    [ValidateSet('dml', 'cpu', 'gpu', '')]
    [string]$Extra = '',

    [switch]$NoDoctor,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Windows 控制台默认不是 UTF-8，不设这个中文字体会变乱码。
$env:PYTHONIOENCODING = 'utf-8'

# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

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

function Stop-WithError {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host "[x] $Message" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 定位仓库根目录（脚本可能在 scripts/ 下，也可能被复制到别处）
# ---------------------------------------------------------------------------

$repoRoot = $PSScriptRoot
if (-not (Test-Path (Join-Path $repoRoot 'pyproject.toml'))) {
    $parent = Split-Path -Parent $PSScriptRoot
    if (Test-Path (Join-Path $parent 'pyproject.toml')) {
        $repoRoot = $parent
    } else {
        Stop-WithError "找不到 pyproject.toml。请在仓库根目录或 scripts/ 下运行本脚本。`n当前推断的根目录：$repoRoot"
    }
}

Write-Host ''
Write-Host 'NovaLoc 新译 —— 环境安装' -ForegroundColor Magenta
Write-Info "仓库根目录：$repoRoot"

$venvDir = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'

# ---------------------------------------------------------------------------
# 1. 创建虚拟环境
# ---------------------------------------------------------------------------

Write-Step '检查 Python 与虚拟环境'

if ($Force -and (Test-Path $venvDir)) {
    Write-Warn '-Force 指定，正在删除旧的 .venv …'
    Remove-Item -LiteralPath $venvDir -Recurse -Force
}

if (-not (Test-Path $venvPython)) {
    # 找 base Python：优先 py launcher，其次 PATH 上的 python
    $basePython = $null
    $pyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        try {
            $candidate = & py -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) {
                $basePython = $candidate.Trim()
            }
        } catch {
            $basePython = $null
        }
    }
    if (-not $basePython) {
        $pyCmd = Get-Command 'python' -ErrorAction SilentlyContinue
        if ($null -ne $pyCmd) {
            $basePython = $pyCmd.Source
        }
    }
    if (-not $basePython) {
        Stop-WithError "找不到 Python。请先安装 Python 3.11 或更高版本：https://www.python.org/downloads/`n提示：安装时记得勾选「Add python.exe to PATH」。"
    }

    # 版本下限检查：pyproject.toml 要求 >= 3.11
    $verText = & $basePython -c 'import sys; print("%d.%d" % sys.version_info[:2])'
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "无法执行 Python：$basePython"
    }
    $verText = $verText.Trim()
    $parts = $verText.Split('.')
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
        Stop-WithError "Python 版本过低：需要 3.11+，当前是 $verText`n（pyproject.toml 里 requires-python = \">=3.11\"）"
    }
    Write-Info "使用 Python $verText（$basePython）"

    Write-Info "创建虚拟环境：$venvDir"
    & $basePython -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Stop-WithError "创建虚拟环境失败。可以手工执行：`n    $basePython -m venv `"$venvDir`""
    }
    Write-Ok '虚拟环境已创建'
} else {
    $verText = (& $venvPython -c 'import sys; print("%d.%d" % sys.version_info[:2])').Trim()
    Write-Ok "复用已有虚拟环境（Python $verText）"
}

# ---------------------------------------------------------------------------
# 2. 检查 onnxruntime 冲突（在装之前查，避免越装越乱）
# ---------------------------------------------------------------------------

function Get-InstalledOnnxRuntimes {
    param([Parameter(Mandatory)][string]$Python)

    $names = @('onnxruntime', 'onnxruntime-directml', 'onnxruntime-gpu')
    $found = @()

    # pip list --format=json 比解析自由文本可靠得多
    $raw = & $Python -m pip list --format=json --disable-pip-version-check 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        return $found
    }
    try {
        $items = $raw | ConvertFrom-Json
    } catch {
        return $found
    }
    foreach ($item in $items) {
        if ($names -contains $item.name) {
            $found += [pscustomobject]@{ Name = $item.name; Version = $item.version }
        }
    }
    return $found
}

Write-Step '检查 onnxruntime 冲突'

$installed = @(Get-InstalledOnnxRuntimes -Python $venvPython)
if ($installed.Count -gt 1) {
    Write-Host ''
    Write-Host '[x] 检测到多个 onnxruntime 变体同时安装：' -ForegroundColor Red
    foreach ($pkg in $installed) {
        Write-Host ("      {0} == {1}" -f $pkg.Name, $pkg.Version) -ForegroundColor Red
    }
    Write-Host ''
    Write-Host '    这三个包提供**同一个 onnxruntime 包名**，会互相覆盖。' -ForegroundColor Yellow
    Write-Host '    后果是 DmlExecutionProvider 静默消失，OCR 退回 CPU，慢 40~140 倍。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '    请按下面三行修复（任选其中一个后端）：' -ForegroundColor Yellow
    Write-Host ''
    Write-Host ("    & `"{0}`" -m pip uninstall -y onnxruntime onnxruntime-directml onnxruntime-gpu" -f $venvPython) -ForegroundColor White
    Write-Host ("    & `"{0}`" -m pip install `"onnxruntime-directml>=1.18`"   # Windows GPU（推荐）" -f $venvPython) -ForegroundColor White
    Write-Host ("    # 或者没有独显时：& `"{0}`" -m pip install `"onnxruntime>=1.18`"" -f $venvPython) -ForegroundColor White
    Write-Host ''
    Write-Host '    修好后重新运行本脚本。' -ForegroundColor Yellow
    exit 1
}
if ($installed.Count -eq 1) {
    Write-Info ("已安装：{0} == {1}" -f $installed[0].Name, $installed[0].Version)
} else {
    Write-Info '尚未安装任何推理后端，将按显卡情况选择。'
}

# ---------------------------------------------------------------------------
# 3. 选 extra：有 NVIDIA 显卡用 dml，否则 cpu
# ---------------------------------------------------------------------------

function Test-NvidiaGpu {
    # 只看名字里含 NVIDIA 的显卡。用 CIM 而不是 Get-Process，
    # 因为这里问的是"有没有硬件"，不是"有没有在跑的东西"。
    try {
        $gpus = Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop
    } catch {
        Write-Info "无法查询显卡信息：$($_.Exception.Message)"
        return $false
    }
    foreach ($gpu in $gpus) {
        if ($gpu.Name -and $gpu.Name -match 'NVIDIA') {
            Write-Info "检测到显卡：$($gpu.Name)"
            return $true
        }
    }
    $names = @($gpus | ForEach-Object { $_.Name }) -join ' / '
    Write-Info "未检测到 NVIDIA 显卡（已发现：$names）"
    return $false
}

Write-Step '选择推理后端'

$chosen = $Extra
if (-not $chosen) {
    if (Test-NvidiaGpu) {
        $chosen = 'dml'
        Write-Ok '检测到 NVIDIA 显卡 -> 使用 DirectML（dml）'
    } else {
        $chosen = 'cpu'
        Write-Warn '没有 NVIDIA 显卡 -> 回退到 CPU 版（cpu），OCR 会慢 40~140 倍'
        Write-Info '如果是 AMD/Intel 独显，DirectML 也能加速：改用 .\scripts\setup.ps1 -Extra dml'
    }
} else {
    Write-Ok "按参数指定使用：$chosen"
}

$extras = "[$chosen]"

# ---------------------------------------------------------------------------
# 4. 升级 pip 并安装
# ---------------------------------------------------------------------------

Write-Step '升级 pip'

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'pip 升级失败，继续尝试安装（可能只是网络问题）。'
} else {
    Write-Ok 'pip 已是最新'
}

Write-Step "安装 nova-loc$extras"

Write-Info '这一步会下载 numpy / opencv / fonttools / onnxruntime 等较大的包，请耐心等待。'
Write-Host ''

Push-Location $repoRoot
try {
    & $venvPython -m pip install -e ".$extras"
    $installCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($installCode -ne 0) {
    Write-Host ''
    Stop-WithError @"
安装失败（pip 退出码 $installCode）。

常见原因与对策：
  1. 网络问题：加国内镜像重试
       & "$venvPython" -m pip install -e ".$extras" -i https://pypi.tuna.tsinghua.edu.cn/simple
  2. 首次装 onnxruntime-directml 时同时装了别的变体：
       先卸载三个，再只装一个（见上面的修复命令）。
  3. Python 版本不对：需要 3.11+。
"@
}
Write-Ok '安装完成'

# 装完再查一次冲突 —— 可能是依赖链把别的变体带进来了
$installedAfter = @(Get-InstalledOnnxRuntimes -Python $venvPython)
if ($installedAfter.Count -gt 1) {
    Write-Host ''
    Write-Warn '安装过程中引入了多个 onnxruntime 变体，DirectML 可能失效：'
    foreach ($pkg in $installedAfter) {
        Write-Host ("      {0} == {1}" -f $pkg.Name, $pkg.Version) -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host '    修复：' -ForegroundColor Yellow
    Write-Host ("    & `"{0}`" -m pip uninstall -y onnxruntime onnxruntime-gpu" -f $venvPython) -ForegroundColor White
    Write-Host ("    & `"{0}`" -m pip install --force-reinstall `"onnxruntime-directml>=1.18`"" -f $venvPython) -ForegroundColor White
    Write-Host ''
}

# ---------------------------------------------------------------------------
# 5. novaloc doctor
# ---------------------------------------------------------------------------

if ($NoDoctor) {
    Write-Step '跳过 novaloc doctor（-NoDoctor）'
} else {
    Write-Step '运行环境自检：novaloc doctor'

    $novalocExe = Join-Path $venvDir 'Scripts\novaloc.exe'
    if (Test-Path $novalocExe) {
        & $novalocExe doctor
    } else {
        # 控制台脚本可能因为 Scripts 不在 PATH 或生成失败而缺失
        Write-Info 'novaloc.exe 不存在，改用 python -m 方式调用。'
        Push-Location $repoRoot
        try {
            & $venvPython -m novaloc.cli doctor
        } finally {
            Pop-Location
        }
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'doctor 返回了非零退出码，说明有需要处理的环境问题（见上面的表格）。'
    }
}

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '----------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host '安装完成。接下来的步骤：' -ForegroundColor Green
Write-Host ''
Write-Host '  1. 激活虚拟环境：' -ForegroundColor White
Write-Host "       .\.venv\Scripts\Activate.ps1" -ForegroundColor Gray
Write-Host ''
Write-Host '  2. 装 Ollama 并把模型目录挪到空间充足的分区（C 盘紧张时必做）：' -ForegroundColor White
Write-Host '       winget install Ollama.Ollama' -ForegroundColor Gray
Write-Host '       setx OLLAMA_MODELS "D:\NovaLoc\ollama-models"' -ForegroundColor Gray
Write-Host '       （setx 只对新终端生效，之后要重开终端）' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  3. 拉取翻译模型：' -ForegroundColor White
Write-Host '       novaloc ollama pull' -ForegroundColor Gray
Write-Host ''
Write-Host '  4. 启动界面：' -ForegroundColor White
Write-Host '       novaloc serve        # 默认 http://127.0.0.1:8791' -ForegroundColor Gray
Write-Host ''
Write-Host '  开发时用 scripts\dev.ps1（后端 8000 + Vite 5173）。' -ForegroundColor DarkGray
Write-Host '----------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host ''
