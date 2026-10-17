@echo off
REM ===================================================================
REM  Cloud-phone round runner -- the 10.99.40.66 executor.
REM
REM  Usage:  round.cmd main_round|daily_round|backfill_round
REM
REM  ASCII only on purpose: cmd reads scripts as GBK, Chinese comments
REM  break on some consoles. Chinese docs live in README.md next to this.
REM
REM  WHY these env vars are set here and not in .env:
REM    - .env is the *pipeline* side (DATABASE_URL / MINIO_* / ...).
REM      run_round.py reads the ones below straight from the process
REM      environment; it never looks in .env for them.
REM    - Task Scheduler does not inherit the interactive environment.
REM ===================================================================

setlocal
REM Derived from the script's own location -- same as scripts\boss\pipeline.cmd.
set "REPO=%~dp0..\.."
set "ROUND=%~1"

if "%ROUND%"=="" goto usage
if /i not "%ROUND%"=="main_round" if /i not "%ROUND%"=="daily_round" if /i not "%ROUND%"=="backfill_round" goto usage

set "LOGDIR=%REPO%\.run\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\cloud-round.log"

REM node launches pi; uv is used by run_round for its DB reads. platform-tools is
REM on PATH because pi's own shell tool calls adb directly (e.g. the ADBKeyboard
REM broadcast for Chinese input). mobile-use resolves adb by itself and never
REM looks at PATH -- see ANDROID_HOME below -- but pi does.
set "PATH=C:\platform-tools;%ProgramFiles%\nodejs;%ProgramFiles%\Python312\Scripts;%PATH%"

REM *** mobile-mcp resolves adb from a FIXED candidate list and never from
REM     PATH (see mobile-use getAdbPath). On this box adb lives only at
REM     C:\platform-tools\adb.exe, and ANDROID_HOME is the single lever it
REM     honours: it appends platform-tools\adb.exe. Without this the MCP
REM     server throws "ADB not found" at module load and Pi gets no tools.
set "ANDROID_HOME=C:\"

REM *** Marks this machine as the dedicated cloud-phone executor:
REM     run_round.ensure_device() then discovers the dynamic adb port
REM     itself. The port changes between sessions -- never hardcode it,
REM     and never `adb disconnect` the cloud phone.
set "HR_CLOUD_PHONE=1"

REM *** Isolate Pi's config dir. ~/.pi/agent on this box is the user's
REM     general pi install; sharing mcp.json would make that pi spawn
REM     hr-tools and mobile-mcp too (adb grab / prod DB access).
set "HR_PI_AGENT_DIR=C:\Users\Administrator\.pi\hr-agent"

REM *** Per-round scope cap. The cloud is ~2-3x slower per UI operation than
REM     the real phone (measured 2026-10-16: real dump 2-3s vs cloud median
REM     5.9-9.2s; one job scan 60s vs 137s). rounds.md's "20 per round" is
REM     sized for the real phone and cannot finish inside the 3600s timeout.
REM     Cap the scope rather than raising the timeout: the timeout also sets
REM     LOCK_STALE_SECONDS, i.e. how long one stuck round can monopolise the phone.
REM     HR_ROUND_MAX_GREETS is NOT set here -- pass it only for a bounded test.
set "HR_ROUND_MAX_ITEMS=5"

REM httpx trust_env would otherwise route 127.0.0.1 calls through a proxy.
set "NO_PROXY=127.0.0.1,localhost,::1"

cd /d "%REPO%"

REM No separate "is the cloud phone client up?" check here on purpose:
REM ensure_device() already refuses the round with a precise reason when
REM it cannot find the endpoint. A second check would be a second source
REM of truth for the same fact -- and those drift apart.

echo [%date% %time%] start %ROUND% >> "%LOG%"
"%REPO%\.venv\Scripts\python.exe" "%REPO%\agent\run_round.py" %ROUND% >> "%LOG%" 2>&1
set "RC=%errorlevel%"
echo [%date% %time%] exit %RC% %ROUND% >> "%LOG%"
exit /b %RC%

:usage
echo Usage: round.cmd main_round^|daily_round^|backfill_round
echo.
echo   main_round      touch-enabled; drives the phone for real
echo   daily_round     end-of-day reconciliation
echo   backfill_round  read-only PDF backfill, window-exempt
echo.
echo NOT available on the cloud phone yet: login / account switching.
echo The cloud anchor map has no login / logout section (unprobed), so
echo flows.py raises KeyError by design. Do not schedule login_watch here.
exit /b 2
