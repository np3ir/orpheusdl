@echo off
rem Global launcher: run OrpheusDL from any window as  orpheus <link/args>
rem Locates the install (orpheus.py) on C: first, then D: (transition/portable-disk).
rem Downloads go to the absolute download_path in config, so the calling dir doesn't matter.
setlocal
set "INST="
if exist "C:\OrpheusDL\orpheus.py" set "INST=C:\OrpheusDL"
if not defined INST if exist "D:\OrpheusDL\orpheus.py" set "INST=D:\OrpheusDL"
if not defined INST (
    echo [X] OrpheusDL no encontrado en C:\OrpheusDL ni D:\OrpheusDL.
    exit /b 1
)
pushd "%INST%"
python orpheus.py %*
set "_rc=%errorlevel%"
popd
endlocal & exit /b %_rc%
