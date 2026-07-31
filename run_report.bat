@echo off
cd /d "C:\Users\sampa\OneDrive\Desktop\wbox-cli\project-ashby-automation"

REM Use the venv wboxcli shim if available, otherwise fall back to venv Python
IF EXIST "%USERPROFILE%\.jobcli\venv\Scripts\wboxcli.exe" (
    "%USERPROFILE%\.jobcli\venv\Scripts\wboxcli.exe" db send-daily-report
) ELSE IF EXIST "%USERPROFILE%\.jobcli\venv\Scripts\python.exe" (
    "%USERPROFILE%\.jobcli\venv\Scripts\python.exe" -m jobcli db send-daily-report
) ELSE (
    echo ERROR: wboxcli venv not found at %USERPROFILE%\.jobcli\venv
    echo Run 'wboxcli setup' first to initialize the environment.
    exit /b 1
)
