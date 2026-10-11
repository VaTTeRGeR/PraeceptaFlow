@echo off
rem Builds dist\PraeceptaFlow\PraeceptaFlow.exe with the virtual environment from setup.bat.
rem Usage: build.bat [--onefile]
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo No virtual environment found. Run setup.bat first.
    exit /b 1
)

set "MODE=--onedir"
set "EXE=dist\PraeceptaFlow\PraeceptaFlow.exe"
if /i "%~1"=="--onefile" (
    set "MODE=--onefile"
    set "EXE=dist\PraeceptaFlow.exe"
)

"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN --windowed %MODE% --name PraeceptaFlow src\main.py || exit /b 1
echo Built %EXE%
