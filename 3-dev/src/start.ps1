# ASR Task Manager - 一键启动脚本 (Windows PowerShell)
# 用法: .\start.ps1 [-NoFrontend] [-NoReload] [-BindHost <地址>]
param(
    [switch]$NoFrontend,
    [switch]$NoReload,
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BackendDir  = Join-Path $ScriptDir "backend"
$FrontendDir = Join-Path $ScriptDir "frontend"
$RuntimeRoot = Join-Path $ScriptDir "..\..\runtime"
$LogsDir     = Join-Path $RuntimeRoot "logs"

if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }
$LogsDir = (Resolve-Path $LogsDir).Path

$BackendOut  = Join-Path $LogsDir "backend.out.log"
$BackendErr  = Join-Path $LogsDir "backend.err.log"
$FrontendOut = Join-Path $LogsDir "frontend.out.log"
$FrontendErr = Join-Path $LogsDir "frontend.err.log"
$PidFile     = Join-Path $LogsDir "pids.txt"

if ($env:ASR_NO_RELOAD -eq "1") { $NoReload = $true }
if ($env:ASR_BIND_HOST)         { $BindHost = $env:ASR_BIND_HOST }

$ExitCode = 0

Write-Host "========================================="
Write-Host "  ASR Task Manager - 启动中..."
Write-Host "========================================="

# ------ 依赖预检 ------
Write-Host "[0/3] 检查依赖..."
$missing = @()

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $missing += "python"
}
else {
    $uvCheck = python -c "import uvicorn" 2>&1
    if ($LASTEXITCODE -ne 0) { $missing += "uvicorn (pip install uvicorn)" }
}

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    $missing += "curl"
}

if (-not $NoFrontend) {
    if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
        $missing += "npx (install Node.js)"
    }
}

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "  X 缺少必要依赖:"
    foreach ($dep in $missing) { Write-Host "    - $dep" }
    Write-Host ""
    Write-Host "  请先安装上述依赖后重试。"
    Write-Host "========================================="
    exit 1
}
Write-Host "      依赖检查通过 √"

# ------ 端口检查 ------
$ErrorActionPreference = "SilentlyContinue"
$port15797 = Get-NetTCPConnection -LocalPort 15797 -State Listen 2>$null
$port15798 = Get-NetTCPConnection -LocalPort 15798 -State Listen 2>$null
$ErrorActionPreference = "Stop"

if ($port15797) {
    Write-Host ""
    Write-Host "  X 端口 15797 已被占用 (PID: $($port15797[0].OwningProcess))"
    Write-Host "    请先运行 .\stop.ps1 或手动结束占用进程"
    Write-Host "========================================="
    exit 1
}
if (-not $NoFrontend -and $port15798) {
    Write-Host ""
    Write-Host "  X 端口 15798 已被占用 (PID: $($port15798[0].OwningProcess))"
    Write-Host "    请先运行 .\stop.ps1 或手动结束占用进程"
    Write-Host "========================================="
    exit 1
}

# ------ 后端 ------
Write-Host "[1/3] 启动后端 (uvicorn)..."

$uvicornArgs = "-m uvicorn app.main:app --host $BindHost --port 15797"
if (-not $NoReload) { $uvicornArgs += " --reload" }

$backendProc = Start-Process -FilePath python -ArgumentList $uvicornArgs `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $BackendOut `
    -RedirectStandardError $BackendErr `
    -WindowStyle Hidden -PassThru

"backend=$($backendProc.Id)" | Set-Content $PidFile
Write-Host "      后端 PID: $($backendProc.Id)"

# ------ 前端 ------
if (-not $NoFrontend) {
    Write-Host "[2/3] 启动前端 (vite)..."

    $frontendProc = Start-Process -FilePath cmd.exe `
        -ArgumentList "/c npx vite --host $BindHost --port 15798" `
        -WorkingDirectory $FrontendDir `
        -RedirectStandardOutput $FrontendOut `
        -RedirectStandardError $FrontendErr `
        -WindowStyle Hidden -PassThru

    "frontend=$($frontendProc.Id)" | Add-Content $PidFile
    Write-Host "      前端 PID: $($frontendProc.Id)"
}
else {
    Write-Host "[2/3] 跳过前端 (-NoFrontend)"
}

# ------ 健康检查 ------
Write-Host "[3/3] 等待服务就绪..."
Start-Sleep -Seconds 4

$backendOk = $false
for ($i = 1; $i -le 10; $i++) {
    try {
        $resp = curl.exe -sf http://127.0.0.1:15797/health 2>$null
        if ($LASTEXITCODE -eq 0) { $backendOk = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}

if ($backendOk) {
    Write-Host ""
    $health = curl.exe -s http://127.0.0.1:15797/health 2>$null
    Write-Host "  √ 后端就绪: http://127.0.0.1:15797"
    Write-Host "    健康检查: $health"
    Write-Host "    API 文档: http://127.0.0.1:15797/docs"
}
else {
    Write-Host ""
    Write-Host "  X 后端启动超时，请查看日志: $BackendErr"
    $ExitCode = 1
}

if (-not $NoFrontend) {
    Start-Sleep -Seconds 1
    try {
        curl.exe -sf http://127.0.0.1:15798 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  √ 前端就绪: http://127.0.0.1:15798"
        }
        else { throw "not ready" }
    }
    catch {
        Write-Host "  X 前端启动超时，请查看日志: $FrontendErr"
        $ExitCode = 1
    }
}

Write-Host ""
Write-Host "  日志文件:"
Write-Host "    后端: $BackendErr"
Write-Host "    前端: $FrontendErr"
Write-Host ""
Write-Host "  停止服务: .\stop.ps1"
Write-Host "========================================="

exit $ExitCode
