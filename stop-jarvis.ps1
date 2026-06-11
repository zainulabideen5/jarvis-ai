# JARVIS shutdown -- stops all services cleanly.
#
# Usage:
#   pwsh d:\jarvis\stop-jarvis.ps1                    # stop everything
#   pwsh d:\jarvis\stop-jarvis.ps1 -Service Server    # stop one
#
# Services stopped: Server (port 8000), Dashboard (port 3000), Desktop Agent.

[CmdletBinding()]
param(
    [ValidateSet("All", "Server", "Dashboard", "Agent")]
    [string]$Service = "All"
)

$ErrorActionPreference = "Continue"

function Stop-OnPort {
    param([int]$Port, [string]$Label)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "  $Label : already stopped"
        return
    }
    $procIds = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($p in $procIds) {
        if ($p -gt 0) {
            try {
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                Write-Host "  $Label : stopped (PID $p)"
            } catch {}
        }
    }
}

function Stop-Agent {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*jarvis_agent*' }
    if (-not $procs) {
        Write-Host "  Agent : already stopped"
        return
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  Agent : stopped (PID $($p.ProcessId))"
        } catch {}
    }
}

Write-Host ""
Write-Host "=== JARVIS shutdown ==="
Write-Host ""

if ($Service -eq "All" -or $Service -eq "Dashboard") { Stop-OnPort 3000 "Dashboard" }
if ($Service -eq "All" -or $Service -eq "Server")    { Stop-OnPort 8000 "Server" }
if ($Service -eq "All" -or $Service -eq "Agent")     { Stop-Agent }

Start-Sleep -Seconds 2

Write-Host ""
Write-Host "=== Final status ==="
$srv  = if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { "still running" } else { "down" }
$dash = if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) { "still running" } else { "down" }
$agt  = if (Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*jarvis_agent*' }) { "still running" } else { "down" }
Write-Host "  Server (8000):    $srv"
Write-Host "  Dashboard (3000): $dash"
Write-Host "  Desktop Agent:    $agt"
Write-Host ""
