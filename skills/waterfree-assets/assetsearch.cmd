@echo off
REM WaterFree asset search. Usage:
REM   assetsearch.cmd "sci fi gun, godot, alterable" --subjects
REM
REM The tools are stdlib-only, so any Python 3.10+ works. Set WATERFREE_PYTHON
REM to override the interpreter.
setlocal
set "SCRIPT=%~dp0bin\assetsearch.py"

if defined WATERFREE_PYTHON (
    "%WATERFREE_PYTHON%" "%SCRIPT%" %*
    exit /b %ERRORLEVEL%
)
if exist "C:\Projects\.local\Scripts\python.exe" (
    "C:\Projects\.local\Scripts\python.exe" "%SCRIPT%" %*
    exit /b %ERRORLEVEL%
)
python "%SCRIPT%" %*
exit /b %ERRORLEVEL%
