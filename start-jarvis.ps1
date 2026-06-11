# JARVIS startup -- PowerShell version.
#
# Why a .ps1 alongside the .bat:
#   The .bat uses `start "Title" /min cmd /c ...` which spawns child cmd
#   windows. When start-jarvis.bat is invoked from a non-interactive parent
#   (login script, scheduled task, automation tool), those child windows
#   don't always survive -- the parent process exits and Windows tears down
#   the child cmd handles before the long-running uvicorn/vite/python finish
#   booting.
#
#   This script uses Win32_Process.Create via Invoke-CimMethod, which spawns
#   processes detached from the caller's session. They survive even when this
#   script exits.
#
# Usage:
#   pwsh d:\jarvis\start-jarvis.ps1               # start all services
#   pwsh d:\jarvis\start-jarvis.ps1 -Force        # force restart (kills first)
#   pwsh d:\jarvis\start-jarvis.ps1 -Service Server   # only one service
#
# Services managed: Server (port 8000), Dashboard (port 3000), Desktop Agent.

[CmdletBinding()]
param(
    [switch]$Force,
    [ValidateSet("All", "Server", "Dashboard", "Agent")]
    [string]$Service = "All"
)

$ErrorActionPreference = "Continue"

# ---- helpers ----

function Test-PortListening {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-AgentRunning {
    return [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*jarvis_agent*' })
}

function Stop-OnPort {
    param([int]$Port, [string]$Label)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $procIds = $conns | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($p in $procIds) {
            if ($p -gt 0) {
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                Write-Host "  Stopped $Label (PID $p)"
            }
        }
        Start-Sleep -Seconds 2
    }
}

function Stop-Agent {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*jarvis_agent*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped Agent (PID $($_.ProcessId))"
        }
}

function Start-Detached {
    param(
        [string]$CommandLine,
        [string]$WorkingDir,
        [string]$Label
    )
    $params = @{
        CommandLine = $CommandLine
        CurrentDirectory = $WorkingDir
    }
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $params -ErrorAction SilentlyContinue
    if ($result -and $result.ReturnValue -eq 0) {
        Write-Host "  Started $Label (PID $($result.ProcessId))"
        return $true
    } else {
        Write-Host "  FAILED to start $Label (Win32_Process Create returned $($result.ReturnValue))"
        return $false
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSec = 30,
        [int]$IntervalSec = 1
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Seconds $IntervalSec
    }
    return $false
}

# ---- service definitions ----

$svcServer = @{
    Name = "Server"
    Port = 8000
    CommandLine = '"d:\jarvis\server\venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000'
    WorkingDir = 'd:\jarvis\server'
    HealthCheck = { Test-PortListening 8000 }
}

$svcDashboard = @{
    Name = "Dashboard"
    Port = 3000
    # Vite is slow to compile from cold cache. Use a longer timeout via a
    # per-service `TimeoutSec` override so slower laptops don't false-fail.
    CommandLine = 'cmd.exe /c "set PATH=C:\Program Files\nodejs;%PATH% && npm.cmd run dev"'
    WorkingDir = 'd:\jarvis\dashboard'
    HealthCheck = { Test-PortListening 3000 }
    TimeoutSec = 90
}

$svcAgent = @{
    Name = "Agent"
    Port = 0  # no port
    CommandLine = '"d:\jarvis\desktop-agent\venv\Scripts\python.exe" -m jarvis_agent'
    WorkingDir = 'd:\jarvis\desktop-agent'
    HealthCheck = { Test-AgentRunning }
}

$allServices = @($svcServer, $svcDashboard, $svcAgent)
$selected = switch ($Service) {
    "Server"    { @($svcServer) }
    "Dashboard" { @($svcDashboard) }
    "Agent"     { @($svcAgent) }
    default     { $allServices }
}

# ---- main ----

Write-Host ""
Write-Host "=== JARVIS startup ==="
if ($Force) { Write-Host "Mode: FORCE RESTART" } else { Write-Host "Mode: start-if-missing" }
Write-Host ""

# Stop first if -Force
if ($Force) {
    Write-Host "Stopping existing services..."
    if ($selected -contains $svcServer)    { Stop-OnPort 8000 "Server" }
    if ($selected -contains $svcDashboard) { Stop-OnPort 3000 "Dashboard" }
    if ($selected -contains $svcAgent)     { Stop-Agent }
    Start-Sleep -Seconds 2
}

# Start each selected service if not already healthy
foreach ($svc in $selected) {
    Write-Host ""
    Write-Host "$($svc.Name):"
    if (& $svc.HealthCheck) {
        Write-Host "  Already running -- skipping"
        continue
    }
    Start-Detached -CommandLine $svc.CommandLine -WorkingDir $svc.WorkingDir -Label $svc.Name | Out-Null

    $timeoutSec = if ($svc.TimeoutSec) { $svc.TimeoutSec } else { 30 }
    Write-Host "  Waiting up to ${timeoutSec}s for $($svc.Name) to become healthy..."
    $ready = Wait-Until -Condition $svc.HealthCheck -TimeoutSec $timeoutSec
    if ($ready) {
        Write-Host "  Ready"
    } else {
        Write-Host "  TIMEOUT -- service may need manual investigation"
    }
}

# Final status summary
Write-Host ""
Write-Host "=== Final status ==="
$srvOk  = Test-PortListening 8000
$dashOk = Test-PortListening 3000
$agtOk  = Test-AgentRunning
Write-Host ("  Server (8000):   {0}" -f $(if ($srvOk)  { "RUNNING" } else { "DOWN" }))
Write-Host ("  Dashboard (3000): {0}" -f $(if ($dashOk) { "RUNNING" } else { "DOWN" }))
Write-Host ("  Desktop Agent:    {0}" -f $(if ($agtOk)  { "RUNNING" } else { "DOWN" }))
Write-Host ""
if ($srvOk -and $dashOk -and $agtOk) {
    Write-Host "All services up. Open http://localhost:3000"
} else {
    Write-Host "Some services down -- re-run with -Force to restart cleanly."
}
