@echo off
REM Reliable dashboard launcher. Vite needs a live console/stdin — if launched
REM fully detached (hidden, stdin closed) it exits instantly with no output.
REM Run via:  start "" /min cmd /k run-dashboard.bat   (keeps console + stdin)
cd /d "%~dp0"
set "PATH=C:\Program Files\nodejs;%PATH%"
title JARVIS Dashboard (vite)
"C:\Program Files\nodejs\node.exe" "node_modules\vite\bin\vite.js" --port 3000
