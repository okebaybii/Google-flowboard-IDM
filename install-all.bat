@echo off
setlocal
echo =======================================
echo Flowboard Installation Script
echo =======================================
echo.

REM Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python (3.10+ recommended) and check "Add Python to PATH".
    pause
    exit /b 1
)

REM Check for Node.js / NPM
npm --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js/NPM is not installed or not in PATH!
    echo Please install Node.js from https://nodejs.org/
    pause
    exit /b 1
)

echo [1/2] Installing Backend (Agent) dependencies...
cd /d "%~dp0\agent"
if exist ".venv" (
    echo Virtual environment already exists.
) else (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Upgrading pip...
python -m pip install --upgrade pip
echo Installing packages from requirements.txt...
pip install -r requirements.txt
echo Backend dependencies installed.
echo.

echo [2/2] Installing Frontend dependencies...
cd /d "%~dp0\frontend"
call npm install
echo Frontend dependencies installed.
echo.

echo Installation complete! You can now run start-all.bat.
pause
