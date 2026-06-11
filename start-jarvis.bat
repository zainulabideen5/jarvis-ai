@echo off
REM JARVIS auto-startup launcher — delegates to PowerShell start-jarvis.ps1.
REM
REM Why this delegates to PowerShell:
REM   The old direct cmd "start" command spawned child cmd windows that did
REM   not survive when invoked from non-interactive contexts (login scripts,
REM   scheduled tasks, automation tools). The PowerShell script uses
REM   Win32_Process.Create which detaches services from the caller cleanly.
REM
REM Placed in Windows Startup folder so it fires at login.
REM Re-running it safely is a no-op if services are already up.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-jarvis.ps1"
