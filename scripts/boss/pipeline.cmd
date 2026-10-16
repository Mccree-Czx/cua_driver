@echo off
REM ===================================================================
REM  boss-pipeline 常驻包装（挂了自动重拉）
REM
REM  ASCII only on purpose: cmd 默认用 GBK 读脚本，中文注释容易踩编码。
REM  中文说明见同目录的 README.md。
REM
REM  WHY the port check first:
REM    If a stale process still holds the port, the new one exits silently
REM    (EADDRINUSE only lands in the log) -- you think you restarted but
REM    you are still looking at the old build. Bit us twice on 2026-10-16.
REM ===================================================================

setlocal
set "REPO=%~dp0..\.."
set "LOGDIR=%REPO%\.run\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\boss-pipeline.log"

REM Task Scheduler does not inherit the interactive PATH.
set "PATH=%USERPROFILE%\AppData\Local\Microsoft\WinGet\Links;%ProgramFiles%\nodejs;%PATH%"

cd /d "%REPO%\services\boss-pipeline"

:loop
netstat -ano | findstr ":8100 " | findstr "LISTENING" >nul
if errorlevel 1 goto start
echo [%date% %time%] port 8100 is taken, wait 10s >> "%LOG%"
timeout /t 10 /nobreak >nul
goto loop

:start
echo [%date% %time%] starting boss-pipeline on 0.0.0.0:8100 >> "%LOG%"
REM --host 0.0.0.0 is REQUIRED: the Mac executor reaches this over the LAN.
uv run --no-project uvicorn app.main:app --host 0.0.0.0 --port 8100 >> "%LOG%" 2>&1
echo [%date% %time%] exited with %errorlevel%, restart in 10s >> "%LOG%"
timeout /t 10 /nobreak >nul
goto loop
