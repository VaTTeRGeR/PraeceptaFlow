@echo off
rem Builds dist\PraeceptaFlow\PraeceptaFlow.exe from a clean virtual environment.
rem Usage: build.bat [--onefile]
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    python -m venv .venv || exit /b 1
)

"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -r requirements-build.txt || exit /b 1

set "MODE=--onedir"
set "EXE=dist\PraeceptaFlow\PraeceptaFlow.exe"
if /i "%~1"=="--onefile" (
    set "MODE=--onefile"
    set "EXE=dist\PraeceptaFlow.exe"
)

"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN %MODE% --name PraeceptaFlow src\main.py || exit /b 1
echo Built %EXE%
