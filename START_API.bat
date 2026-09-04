@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo ERROR: LoraAPI\venv was not found.
    pause
    exit /b 1
)

echo Starting the Lora API on every network interface at port 8000...
echo Keep this window open while POSViewer is running.
echo.
set DEBUG=True
venv\Scripts\python.exe config\manage.py runserver 0.0.0.0:8000 --noreload

pause