@echo off
REM ===================================================================
REM  BOSS executor -- drives the PHYSICAL phone over USB from this box.
REM
REM  Usage:  agent-round.cmd [main_round|daily_round|login_round|backfill_round|resume_round]
REM
REM  ASCII only on purpose: cmd reads scripts as GBK. Chinese docs: README.md.
REM  CRLF only on purpose: an LF-only .cmd gets mis-parsed by cmd.
REM ===================================================================

setlocal
set "REPO=%~dp0..\.."
set "ROUND=%~1"
if "%ROUND%"=="" set "ROUND=main_round"

if /i not "%ROUND%"=="main_round" if /i not "%ROUND%"=="daily_round" if /i not "%ROUND%"=="login_round" if /i not "%ROUND%"=="backfill_round" if /i not "%ROUND%"=="resume_round" goto usage

set "LOGDIR=%REPO%\.run\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\boss-agent.log"

REM *** Two devices are attached to this box: the physical phone (USB,
REM     VED0218928022836) and the Liepin cloud phone (127.0.0.1:<port>).
REM     Pin the physical one -- otherwise device_problem() refuses the round.
set "ANDROID_SERIAL=VED0218928022836"

REM *** The BOSS control plane now runs on 197 (boss-pipeline :8100).
set "BOSS_SERVER_URL=http://10.99.40.197:8100"

REM *** Ingest token lives outside git (.env.boss is covered by .gitignore).
set /p BOSS_INGEST_TOKEN=<"C:\cua_driver\.env.boss"

REM agent_boss is stdlib-only; adb is found by shared/android_ui.py's own
REM candidate list (C:\platform-tools\adb.exe), not by PATH.
cd /d "%REPO%"

echo [%date% %time%] start %ROUND% >> "%LOG%"
"%REPO%\.venv\Scripts\python.exe" "%REPO%\agent_boss\run_round.py" %ROUND% >> "%LOG%" 2>&1
set "RC=%errorlevel%"
echo [%date% %time%] exit %RC% %ROUND% >> "%LOG%"
exit /b %RC%

:usage
echo Usage: agent-round.cmd [main_round^|daily_round^|login_round^|backfill_round^|resume_round]
exit /b 2
