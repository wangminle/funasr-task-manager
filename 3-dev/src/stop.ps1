# ASR Task Manager - 一键停止脚本 (Windows PowerShell)
# 用法: .\stop.ps1

$ErrorActionPreference = "SilentlyContinue"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RuntimeDir = Join-Path $ScriptDir "..\..\.runtime"
$PidFile    = Join-Path $RuntimeDir "pids.txt"

$ExpectedNames = @("python", "uvicorn", "node", "vite", "cmd")

Write-Host "========================================="
Write-Host "  ASR Task Manager - 停止中..."
Write-Host "========================================="

function Test-OurProcess {
    param([int]$Pid_)
    $proc = Get-Process -Id $Pid_ -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    $name = $proc.ProcessName.ToLower()
    foreach ($pat in $ExpectedNames) {
        if ($name -like "*$pat*") { return $true }
    }
    return $false
}

function Stop-ProcessTree {
    param([int]$Pid_, [string]$Label)
    $proc = Get-Process -Id $Pid_ -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "  - $Label (PID $Pid_) 已不存在"
        return
    }
    if (-not (Test-OurProcess -Pid_ $Pid_)) {
        Write-Host "  ! $Label (PID $Pid_) 已被其他进程复用，跳过"
        return
    }
    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$Pid_" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $Pid_ -Force
        Write-Host "  √ 已停止 $Label (PID $Pid_)"
    }
    catch {
        Write-Host "  X 停止 $Label (PID $Pid_) 失败: $_"
    }
}

function Stop-ByPort {
    param([int]$Port, [string]$Label)
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) { return }
    foreach ($conn in $connections) {
        $pid_ = $conn.OwningProcess
        if ($pid_ -le 0) { continue }
        if (Test-OurProcess -Pid_ $pid_) {
            $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$pid_" -ErrorAction SilentlyContinue
            foreach ($child in $children) {
                Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
            }
            try {
                Stop-Process -Id $pid_ -Force
                Write-Host "  √ 已停止 $Label (端口 $Port, PID $pid_)"
            } catch {}
        } else {
            try {
                Stop-Process -Id $pid_ -Force
                Write-Host "  √ 已停止端口 $Port 上的进程 (PID $pid_)"
            } catch {}
        }
    }
}

# ------ 按 PID 文件停止 ------
if (Test-Path $PidFile) {
    Get-Content $PidFile | ForEach-Object {
        if ($_ -match "^(.+)=(\d+)$") {
            $name = $Matches[1]
            $pid_ = [int]$Matches[2]
            Stop-ProcessTree -Pid_ $pid_ -Label $name
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# ------ 端口兜底清理 ------
Stop-ByPort -Port 8000 -Label "后端"
Stop-ByPort -Port 5173 -Label "前端"

Write-Host ""
Write-Host "  所有服务已停止。"
Write-Host "========================================="
