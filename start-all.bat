@echo off
setlocal
REM Personal/local use: disable login (go straight into the UI).
REM Child processes started below inherit this environment variable.
REM Set to 0 to restore the Google/Firebase login screen.
set FLOWBOARD_NO_AUTH=1
echo =======================================
echo Flowboard Startup Script
echo =======================================
echo.
echo Dang kill cac process cu... (Killing old processes...)
echo.

REM Kill any existing python.exe processes
taskkill /F /IM python.exe >nul 2>&1

REM Kill any existing node.exe processes
taskkill /F /IM node.exe >nul 2>&1

REM Wait a moment for processes to fully terminate
timeout /t 2 /nobreak >nul

echo.
echo Chon che do chay (Select run mode):
echo [1] Chay an (Hide CMD windows)
echo [2] Mo giao dien (Show CMD windows)
set /p RUN_MODE="Chon (1/2): "

if "%RUN_MODE%"=="1" goto hidden
goto shown

:hidden
echo Dang chay Backend va Frontend an... (Starting in background...)
echo Set WshShell = CreateObject("WScript.Shell") > run_hidden.vbs
echo WshShell.Run "cmd /c cd /d """ ^& WScript.Arguments(0) ^& """ && call .venv\Scripts\activate.bat && python -m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101 --reload", 0, False >> run_hidden.vbs
echo WshShell.Run "cmd /c cd /d """ ^& WScript.Arguments(1) ^& """ && npm run dev", 0, False >> run_hidden.vbs

cscript //nologo run_hidden.vbs "%~dp0agent" "%~dp0frontend"
del run_hidden.vbs

echo Vui long doi 5 giay de he thong khoi dong...(Waiting 5 seconds for startup...)
timeout /t 5 /nobreak >nul
start http://localhost:1234
echo Flowboard dang chay ngam! (Flowboard running in background!)
echo Luu y: De tat, ban can vao Task Manager de tat 'python.exe' va 'node.exe'
pause
exit

:shown
echo Dang chay Backend...
cd /d "%~dp0agent"
start "Flowboard Backend" cmd /k "call .venv\Scripts\activate.bat && python -m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101 --reload"

echo Dang chay Frontend...
cd /d "%~dp0frontend"
start "Flowboard Frontend" cmd /k "npm run dev"

echo Vui long doi 5 giay de he thong khoi dong...(Waiting 5 seconds for startup...)
timeout /t 5 /nobreak >nul
start http://localhost:1234
echo Flowboard da duoc khoi dong! (Flowboard started!)
pause
exit
