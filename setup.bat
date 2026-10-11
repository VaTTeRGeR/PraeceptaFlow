@echo off
rem Creates the virtual environment if missing and installs the build dependencies.
rem Usage: setup.bat
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    python -m venv .venv || exit /b 1
)

"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -r requirements-build.txt || exit /b 1
echo Set up .venv
