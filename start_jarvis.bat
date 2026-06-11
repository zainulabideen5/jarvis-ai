@echo off
title JARVIS Server
echo Starting JARVIS server on http://127.0.0.1:8000
cd /d d:\jarvis\server
:loop
"d:\jarvis\server\venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Server crashed - restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop