@echo off
setlocal DisableDelayedExpansion
rem Portable launcher for artist_best_quality.py (best-FLAC-across-services by ISRC).
rem Stays beside the script; the install folder is already on PATH, so just run:
rem     abq "https://tidal.com/artist/10411"
set "INST=%~dp0"
if not exist "%INST%artist_best_quality.py" (
    echo [ERROR] artist_best_quality.py is missing beside this launcher.
    exit /b 1
)
set "ORPHEUS_PYTHON=%INST%.venv\Scripts\python.exe"
if not exist "%ORPHEUS_PYTHON%" (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Run install.ps1 to create the Python environment.
        exit /b 1
    )
    set "ORPHEUS_PYTHON=python.exe"
)
pushd "%INST%"
if errorlevel 1 exit /b 1
"%ORPHEUS_PYTHON%" "%INST%artist_best_quality.py" %*
set "ABQ_EXIT=%errorlevel%"
popd
endlocal & exit /b %ABQ_EXIT%
