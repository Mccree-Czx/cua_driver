@echo off
REM ===================================================================
REM  boss-console (Next.js) 常驻包装
REM
REM  ASCII only on purpose -- see pipeline.cmd.
REM
REM  IMPORTANT: `next start` serves the PREBUILT bundle in .next/.
REM  Restarting does NOT rebuild. After any frontend change you must
REM  `pnpm --filter boss-console build` or you keep serving the old page.
REM  (hr-console lost a whole day to exactly this on 2026-10-15.)
REM ===================================================================

setlocal
set "REPO=%~dp0..\.."
set "LOGDIR=%REPO%\.run\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\boss-console.log"

set "PATH=%USERPROFILE%\AppData\Local\Microsoft\WinGet\Links;%ProgramFiles%\nodejs;%PATH%"

cd /d "%REPO%"

:loop
netstat -ano | findstr ":3200 " | findstr "LISTENING" >nul
if errorlevel 1 goto start
echo [%date% %time%] port 3200 is taken, wait 10s >> "%LOG%"
timeout /t 10 /nobreak >nul
goto loop

:start
if not exist "%REPO%\apps\boss-console\.next\BUILD_ID" (
  echo [%date% %time%] no build yet, building first >> "%LOG%"
  call corepack pnpm --filter boss-console build >> "%LOG%" 2>&1
)
echo [%date% %time%] starting boss-console on 3200 >> "%LOG%"
call corepack pnpm --filter boss-console exec next start -p 3200 >> "%LOG%" 2>&1
echo [%date% %time%] exited with %errorlevel%, restart in 10s >> "%LOG%"
timeout /t 10 /nobreak >nul
goto loop
