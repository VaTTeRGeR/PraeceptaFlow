@echo off
rem Removes the PyInstaller output folders (build\ and dist\).
setlocal
cd /d "%~dp0"

for %%D in (build dist) do (
    if exist "%%D\" (
        rmdir /s /q "%%D" && echo Removed %%D\
    )
)
