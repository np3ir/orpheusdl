@echo off
setlocal DisableDelayedExpansion
rem Portable launcher: this file stays beside orpheus.py, wherever installed.
set "INST=%~dp0"
if not exist "%INST%orpheus.py" (
    echo [ERROR] orpheus.py is missing beside this launcher.
    exit /b 1
)
set "ORPHEUS_PYTHON=%INST%.venv\Scripts\python.exe"
if not exist "%ORPHEUS_PYTHON%" (
    rem Preserve legacy installations that already use a working global Python.
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Run install.ps1 to create the Python environment.
        exit /b 1
    )
    set "ORPHEUS_PYTHON=python.exe"
)
pushd "%INST%"
if errorlevel 1 exit /b 1
"%ORPHEUS_PYTHON%" "%INST%orpheus.py" %*
set "ORPHEUS_EXIT=%errorlevel%"
popd
endlocal & exit /b %ORPHEUS_EXIT%
